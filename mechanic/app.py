import asyncio
import logging
import json
import sqlite3
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4
from fastapi import FastAPI, Request, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from .config import LUNA_MODEL, Settings
from .catalog import Catalog
from .clients import Services, ServiceError
from .corpus import Corpus
from .manuals import BY_ID
from .providers import Provider
from .agent import Agent, basic_result, verdict_summary
from .grounding import draft_payload, InstructionConflict
from .journal import action_log, event
from .conversations import ConversationMissing, ConversationStore
from .team_access import Actor, TeamAccess, is_local

HERE = Path(__file__).parent
logger = logging.getLogger(__name__)


def mount_local_voice(app, model_dir):
    if model_dir is None:
        return False
    try:
        from voicekit import recognition
        from voicekit.server import create_voice_router

        engine = recognition.SherpaEngine(model_dir)
        assets = Path(recognition.__file__).with_name('static')
        app.mount('/voice-kit', StaticFiles(directory=assets), name='voice-kit')
    except (ImportError, OSError, RuntimeError, ValueError):
        logger.warning('Локальный голосовой модуль не запустился. Проверьте установку и VOICE_MODEL_DIR.')
        return False
    app.include_router(create_voice_router(engine), prefix='/voice')
    return True


def extract_manual_page(path, page):
    import pymupdf

    with pymupdf.open(path) as source, pymupdf.open() as output:
        if not 1 <= page <= source.page_count:
            raise ValueError('Страница отсутствует в PDF.')
        output.insert_pdf(source, from_page=page - 1, to_page=page - 1, links=False, annots=False, widgets=False)
        return output.tobytes(garbage=4, deflate=True)


class Select(BaseModel):
    model_config = ConfigDict(extra='forbid')
    vehicle_id: str
    session_id: str


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int
    intent: Literal['diagnose', 'chat', 'draft']
    question: str = Field(default='', max_length=1500)


class FinishConversation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int
    conversation_id: str = Field(min_length=1, max_length=64)


class Send(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int
    preview_id: str


def create_app(settings=None, services=None, provider=None, corpus=None):
    settings = settings or Settings.load()
    services = services or Services(settings)
    provider = provider or Provider(settings)
    corpus = corpus or Corpus(settings.corpus, settings.manuals)
    catalog = Catalog(settings.vehicles)
    agent = Agent(provider, services, corpus)
    sessions = {}
    conversations = ConversationStore(settings.conversations_db)
    access = TeamAccess(settings.team_users_file)

    @asynccontextmanager
    async def lifespan(app):
        yield
        for obj in (services, provider):
            if hasattr(obj, 'close'):
                await obj.close()

    app = FastAPI(title='Цифровой механик', lifespan=lifespan)
    app.mount('/static', StaticFiles(directory=HERE / 'static'), name='static')
    voice_ready = mount_local_voice(app, settings.voice_model_dir)
    templates = Jinja2Templates(directory=HERE / 'templates')

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        return JSONResponse({'detail': 'Некорректные параметры запроса.'}, status_code=422)

    @app.middleware('http')
    async def headers(request, call_next):
        try:
            request.state.actor = await access.authenticate(request)
        except HTTPException as exc:
            return JSONResponse({'detail': exc.detail}, status_code=exc.status_code,
                                headers={**(exc.headers or {}), 'Cache-Control': 'no-store'})
        kind = {'/api/action': 'agent', '/api/send': 'toir'}.get(request.url.path)
        if request.method == 'POST' and kind:
            with action_log(kind) as identifier:
                response = await call_next(request)
                event('action.response', http=response.status_code)
                response.headers['X-Action-Id'] = identifier
        else:
            response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        return response

    def get_session(request):
        state = sessions.get(request.cookies.get('mechanic_session'))
        actor = request.state.actor
        if state and state['authenticated_as'] == (actor.id if actor else None):
            return state
        return None

    def state_for(request, write=True):
        state = get_session(request)
        if not state or time.monotonic() - state['touched'] > 7200:
            raise HTTPException(409, 'Сессия истекла. Обновите страницу.')
        state['touched'] = time.monotonic()
        if write and not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), state['csrf']):
            raise HTTPException(403, 'Недопустимая команда сессии.')
        return state

    def selected(state, revision):
        if state['revision'] != revision or not state['pair']:
            raise HTTPException(409, 'Контекст изменился. Выберите проверку и повторите действие.')
        return catalog.pair(*state['pair'])[0]

    def repair_entries(vehicle_id):
        if settings.repair_log_file is None:
            return None
        try:
            rows = json.loads(settings.repair_log_file.read_text(encoding='utf-8')).get(vehicle_id, [])
            return [{'recorded_at': row['recorded_at'], 'text': row['text']}
                    for row in rows[-50:] if isinstance(row.get('recorded_at'), str) and isinstance(row.get('text'), str)]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            logger.warning('Не удалось прочитать журнал ремонтов.')
            return None

    def saved_context(state):
        actor = state['actor']
        if actor is None:
            return None
        vehicle_id = state['pair'][0]
        data = conversations.overview(vehicle_id, actor.id)
        identifier = state['vehicle_conversations'].get(vehicle_id)
        active = conversations.read(identifier, vehicle_id, actor.id) if identifier else None
        return {**data, 'active': active if active and not active['ended_at'] else None,
                'repair_log': repair_entries(vehicle_id)}

    async def archive_turn(state, question, result, blocked=False):
        actor = state['actor']
        if actor is None:
            return
        claims = result.get('claims', [])
        references = [*claims, *(item for conflict in result.get('conflicts', []) for item in conflict['sources'])]
        sources = [{'manual': manual, 'pdf_page': page}
                   for manual, page in dict.fromkeys((item['manual'], item['pdf_page']) for item in references)]
        reply = '\n'.join(filter(None, [result.get('message'), *(item['text'] for item in claims), result.get('error')]))
        if not reply:
            return
        vehicle_id = state['pair'][0]
        try:
            identifier = await run_in_threadpool(
                conversations.append, state['vehicle_conversations'].get(vehicle_id), state['pair'],
                actor.id, actor.name, question, reply, blocked, sources,
            )
        except (OSError, sqlite3.Error):
            logger.warning('Не удалось сохранить реплику разговора.')
            result['conversation_notice'] = 'Не удалось сохранить реплику в карточке машины.'
            return
        state['vehicle_conversations'][vehicle_id] = identifier
        result['conversation_id'] = identifier

    @app.get('/', response_class=HTMLResponse)
    async def home(request: Request):
        return templates.TemplateResponse(request=request, name='index.html', context={'voice_ready': voice_ready})

    @app.get('/favicon.ico', include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    @app.get('/api/catalog')
    async def vehicles():
        return catalog.public()

    @app.get('/api/session')
    async def session(request: Request):
        sid = request.cookies.get('mechanic_session')
        state = get_session(request)
        if state is None or time.monotonic() - state['touched'] > 7200:
            for old in list(sessions):
                if time.monotonic() - sessions[old]['touched'] > 7200:
                    sessions.pop(old)
            if len(sessions) >= 200:
                raise HTTPException(503, 'Достигнут лимит сессий. Повторите позже.')
            sid = secrets.token_urlsafe(32)
            actor = request.state.actor
            if actor is None and is_local(request):
                try:
                    local_id = UUID(request.cookies.get('mechanic_local_actor', '')).hex
                except ValueError:
                    local_id = uuid4().hex
                actor_for_history = Actor(f'local:{local_id}', 'Локальный механик')
            else:
                local_id = None
                actor_for_history = actor
            state = {'actor': actor_for_history, 'authenticated_as': actor.id if actor else None,
                     'local_id': local_id,
                     'vehicle_conversations': {}, 'previous_context': None, 'csrf': secrets.token_urlsafe(32), 'revision': 0, 'pair': None,
                     'diagnosis': None, 'answer': None, 'draft': None, 'preview_id': None,
                     'history': [], 'conflicts': [], 'conflicts_by_pair': {}, 'source_pages': set(),
                     'lock': asyncio.Lock(), 'touched': time.monotonic()}
            sessions[sid] = state
        data = {'csrf': state['csrf'], 'revision': state['revision'], 'pair': state['pair'],
                'provider': 'openai', 'model': LUNA_MODEL, 'llm_problem': settings.llm_problem(),
                'corpus_ready': settings.corpus.is_file(), 'manual_pages': settings.manual_pages,
                'voice_ready': voice_ready}
        response = JSONResponse(data)
        response.set_cookie('mechanic_session', sid, httponly=True, samesite='strict',
                            secure=request.url.scheme == 'https', max_age=7200)
        if state['local_id']:
            response.set_cookie('mechanic_local_actor', state['local_id'], httponly=True,
                                samesite='strict', secure=request.url.scheme == 'https', max_age=60 * 60 * 24 * 30)
        return response

    @app.post('/api/select')
    async def select(request: Request, body: Select):
        state = state_for(request)
        try:
            catalog.pair(body.vehicle_id, body.session_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        async with state['lock']:
            conflicts = state['conflicts_by_pair'].get((body.vehicle_id, body.session_id), [])
            state.update(pair=(body.vehicle_id, body.session_id), revision=state['revision'] + 1,
                         diagnosis=None, answer=None, answer_question='', draft=None, preview_id=None,
                         history=[], conflicts=conflicts, source_pages=set(), previous_context=None)
            result = {'revision': state['revision']}
            try:
                history = await run_in_threadpool(saved_context, state)
            except (OSError, sqlite3.Error, ConversationMissing):
                history = None
                result['conversation_notice'] = 'История разговоров временно недоступна.'
            if history is not None:
                state['previous_context'] = history['latest']
                active = history['active']
                if active:
                    same_check = [item for item in active['turns'] if item['check_id'] == body.session_id]
                    state['history'] = [
                        entry for item in same_check[-3:]
                        for entry in ({'role': 'user', 'text': item['question']},
                                      {'role': 'assistant', 'text': item['reply'][:1500]})
                    ]
                result['conversations'] = history
            return result

    @app.post('/api/action')
    async def action(request: Request, body: Action):
        state = state_for(request)
        async with state['lock']:
            vehicle = selected(state, body.revision)
            if settings.openai_key and settings.openai_key in body.question:
                raise HTTPException(422, 'Вопрос содержит ключ API. Удалите его из текста.')
            if body.intent == 'chat' and not body.question.strip():
                raise HTTPException(422, 'Введите вопрос.')
            if state['actor'] is not None and body.intent == 'chat':
                try:
                    overview = await run_in_threadpool(conversations.overview, state['pair'][0], state['actor'].id)
                    state['previous_context'] = overview['latest']
                except (OSError, sqlite3.Error):
                    logger.warning('Не удалось обновить контекст машины.')
            archive_question = body.question or (
                'Подготовить черновик заявки' if body.intent == 'draft' else 'Показать результат диагностики')
            state['preview_id'] = None
            try:
                result = await agent.run(state, vehicle, body.question, body.intent)
                if state['draft']:
                    state['preview_id'] = secrets.token_urlsafe(24)
                    result['preview_id'] = state['preview_id']
                    result['operations_evidence'] = [e.model_dump() for e in state['answer'].operations]
                    result['priority_basis'] = (state['answer'].priority_basis.model_dump()
                                                if state['answer'].priority_basis else None)
                d = state.get('diagnosis')
                result['summary'] = verdict_summary(d, state['answer'])
                result['observations'] = d.description if d else ''
                result['warning'] = bool(d and d.warning)
                result['can_draft'] = bool(d and d.usable and d.status == 'anomaly' and state['answer'])
                result['revision'] = state['revision']
                state['source_pages'].update((claim['doc'], claim['pdf_page']) for claim in result['claims'])
                state['history'].extend([{'role': 'user', 'text': body.question or body.intent},
                                         {'role': 'assistant', 'text': ' '.join(c['text'] for c in result['claims'])[:1500]}])
                state['history'] = state['history'][-6:]
                await archive_turn(state, archive_question, result)
                event('agent.response', intent=body.intent, claims=len(result['claims']), preview=bool(state['draft']))
                return result
            except InstructionConflict as exc:
                event('agent.conflict', conflicts=len(exc.conflicts))
                state['draft'] = state['answer'] = state['preview_id'] = None
                state['conflicts'] = exc.conflicts
                state['conflicts_by_pair'][state['pair']] = exc.conflicts
                state['source_pages'].update((source['doc'], source['pdf_page'])
                                             for conflict in exc.conflicts for source in conflict['sources'])
                result = {'status': 'needs_clarification', 'message':
                    'Обнаружено противоречие технических инструкций. Обе формулировки приведены ниже. '
                    f'Уточните применимость у специалиста или производителя для конкретной модификации ДВС '
                    f'(выбранный двигатель: {vehicle["engine_model"]}). '
                    'Агент не выбирает одну инструкцию как бесспорную; рекомендация и зависимая заявка заблокированы.',
                    'conflicts': exc.conflicts, 'claims': [], 'draft': None, 'can_draft': False,
                    'summary': verdict_summary(state.get('diagnosis'), conflict=True),
                    'observations': state['diagnosis'].description if state.get('diagnosis') else '',
                    'warning': bool(state.get('diagnosis') and state['diagnosis'].warning), 'revision': state['revision']}
                await archive_turn(state, archive_question, result, blocked=True)
                return result
            except (ServiceError, ValueError) as exc:
                event('agent.rejected', error_type=type(exc).__name__)
                state['draft'] = state['answer'] = state['preview_id'] = None
                d = state.get('diagnosis')
                result = {'error': str(exc),
                    'message': basic_result(d) if d else '', 'revision': state['revision'],
                    'summary': verdict_summary(d), 'observations': d.description if d else '',
                    'warning': bool(d and d.warning)}
                await archive_turn(state, archive_question, result, blocked=True)
                return JSONResponse(status_code=503, content=result)

    @app.post('/api/conversations/finish')
    async def finish_conversation(request: Request, body: FinishConversation):
        state = state_for(request)
        async with state['lock']:
            selected(state, body.revision)
            actor = state['actor']
            vehicle_id = state['pair'][0]
            if actor is None:
                raise HTTPException(403, 'История разговоров доступна только ремонтной команде или на локальном стенде.')
            if state['vehicle_conversations'].get(vehicle_id) != body.conversation_id:
                raise HTTPException(409, 'Открыт другой разговор. Проверьте выбранную машину.')
            try:
                state['previous_context'] = await run_in_threadpool(
                    conversations.finish, body.conversation_id, vehicle_id, actor.id)
                return await run_in_threadpool(saved_context, state)
            except ConversationMissing:
                raise HTTPException(404, 'Разговор не найден.') from None
            except (OSError, sqlite3.Error):
                raise HTTPException(503, 'Не удалось завершить разговор. Повторите попытку.') from None

    @app.get('/api/conversations')
    async def conversation_overview(request: Request, revision: int):
        state = state_for(request, write=False)
        async with state['lock']:
            selected(state, revision)
            if state['actor'] is None:
                raise HTTPException(403, 'История разговоров доступна только ремонтной команде или на локальном стенде.')
            try:
                return await run_in_threadpool(saved_context, state)
            except (ConversationMissing, OSError, sqlite3.Error):
                raise HTTPException(503, 'История разговоров временно недоступна.') from None

    @app.get('/api/conversations/{identifier}')
    async def conversation(request: Request, identifier: str, revision: int):
        state = state_for(request, write=False)
        async with state['lock']:
            selected(state, revision)
            actor = state['actor']
            if actor is None:
                raise HTTPException(403, 'История разговоров доступна только ремонтной команде или на локальном стенде.')
            try:
                return await run_in_threadpool(conversations.read, identifier, state['pair'][0], actor.id)
            except ConversationMissing:
                raise HTTPException(404, 'Разговор не найден для выбранной машины.') from None
            except (OSError, sqlite3.Error):
                raise HTTPException(503, 'История разговоров временно недоступна.') from None

    @app.post('/api/send')
    async def send(request: Request, body: Send):
        state = state_for(request)
        async with state['lock']:
            vehicle = selected(state, body.revision)
            if state.get('conflicts'):
                state['draft'] = state['preview_id'] = None
                raise HTTPException(409, 'Противоречие инструкций не разрешено. Зависимая заявка заблокирована.')
            if not state['preview_id'] or not secrets.compare_digest(body.preview_id, state['preview_id']):
                raise HTTPException(409, 'Нет действующего предпросмотра. Подготовьте заявку заново.')
            try:
                fresh = draft_payload(state['answer'], state['diagnosis'], vehicle['engine_model'], corpus,
                                      state.get('answer_question', ''))
                if fresh != state['draft']:
                    raise ServiceError('Состав заявки изменился. Нужен новый предпросмотр.')
            except (ServiceError, ValueError, AttributeError) as exc:
                state['draft'] = state['preview_id'] = None
                raise HTTPException(409, str(exc)) from None
            # Consume confirmation before networking, including uncertain results.
            state['draft'] = state['preview_id'] = None
            event('toir.submit', operations=len(fresh['operations']), references=len(fresh['references']))
            try:
                ticket_id = await services.ticket(fresh)
                event('toir.result', outcome='created')
                return {'ticket_id': ticket_id, 'status': 'created', 'revision': state['revision']}
            except ServiceError as exc:
                event('toir.result', outcome='unconfirmed')
                return JSONResponse(status_code=502, content={'error': str(exc), 'revision': state['revision']})

    @app.get('/manual/{doc}/{page}')
    async def manual(request: Request, doc: str, page: int):
        if not settings.manual_pages:
            raise HTTPException(404)
        state = state_for(request, write=False)
        if not state['pair'] or (doc, page) not in state['source_pages'] or doc not in BY_ID:
            raise HTTPException(404)
        revision = state['revision']
        vehicle = catalog.pair(*state['pair'])[0]
        m = BY_ID[doc]
        if vehicle['engine_model'] not in m.engines or not 1 <= page <= m.pages:
            raise HTTPException(404)
        path = settings.manuals / m.filename
        if not path.is_file():
            raise HTTPException(404, 'PDF отсутствует в локальной папке.')
        try:
            await run_in_threadpool(corpus.assert_source, doc)
            pdf = await run_in_threadpool(extract_manual_page, path, page)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if revision != state['revision']:
            raise HTTPException(409, 'Выбор проверки изменился.')
        return Response(pdf, media_type='application/pdf')

    return app


app = create_app()
