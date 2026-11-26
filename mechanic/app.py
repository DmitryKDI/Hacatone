import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from .config import Settings
from .catalog import Catalog
from .clients import Services, ServiceError
from .corpus import Corpus
from .manuals import BY_ID
from .providers import Provider
from .agent import Agent, basic_result, verdict_summary
from .grounding import draft_payload, InstructionConflict

HERE = Path(__file__).parent


class Select(BaseModel):
    model_config = ConfigDict(extra='forbid')
    vehicle_id: str
    session_id: str


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int
    intent: Literal['diagnose', 'chat', 'draft']
    question: str = Field(default='', max_length=1500)


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

    @asynccontextmanager
    async def lifespan(app):
        yield
        for obj in (services, provider):
            if hasattr(obj, 'close'):
                await obj.close()

    app = FastAPI(title='Цифровой механик', lifespan=lifespan)
    app.mount('/static', StaticFiles(directory=HERE / 'static'), name='static')
    templates = Jinja2Templates(directory=HERE / 'templates')

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        return response

    def state_for(request, write=True):
        sid = request.cookies.get('mechanic_session')
        state = sessions.get(sid)
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

    @app.get('/', response_class=HTMLResponse)
    async def home(request: Request):
        return templates.TemplateResponse(request=request, name='index.html', context={})

    @app.get('/favicon.ico', include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    @app.get('/api/catalog')
    async def vehicles():
        return catalog.public()

    @app.get('/api/session')
    async def session(request: Request):
        sid = request.cookies.get('mechanic_session')
        state = sessions.get(sid)
        if state is None or time.monotonic() - state['touched'] > 7200:
            for old in list(sessions):
                if time.monotonic() - sessions[old]['touched'] > 7200:
                    sessions.pop(old)
            if len(sessions) >= 200:
                raise HTTPException(503, 'Достигнут лимит сессий. Повторите позже.')
            sid = secrets.token_urlsafe(32)
            state = {'csrf': secrets.token_urlsafe(32), 'revision': 0, 'pair': None,
                     'diagnosis': None, 'answer': None, 'draft': None, 'preview_id': None,
                     'history': [], 'conflicts': [], 'conflicts_by_pair': {},
                     'lock': asyncio.Lock(), 'touched': time.monotonic()}
            sessions[sid] = state
        data = {'csrf': state['csrf'], 'revision': state['revision'], 'pair': state['pair'],
                'provider': settings.provider, 'llm_problem': settings.llm_problem(),
                'corpus_ready': settings.corpus.is_file()}
        response = JSONResponse(data)
        response.set_cookie('mechanic_session', sid, httponly=True, samesite='strict', max_age=7200)
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
                         history=[], conflicts=conflicts)
            return {'revision': state['revision']}

    @app.post('/api/action')
    async def action(request: Request, body: Action):
        state = state_for(request)
        async with state['lock']:
            vehicle = selected(state, body.revision)
            if body.intent == 'chat' and not body.question.strip():
                raise HTTPException(422, 'Введите вопрос.')
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
                state['history'].extend([{'role': 'user', 'text': body.question or body.intent},
                                         {'role': 'assistant', 'text': ' '.join(c['text'] for c in result['claims'])[:1500]}])
                state['history'] = state['history'][-6:]
                return result
            except InstructionConflict as exc:
                state['draft'] = state['answer'] = state['preview_id'] = None
                state['conflicts'] = exc.conflicts
                state['conflicts_by_pair'][state['pair']] = exc.conflicts
                return {'status': 'needs_clarification', 'message':
                    'Обнаружено противоречие технических инструкций. Обе формулировки приведены ниже. '
                    f'Уточните применимость у специалиста или производителя для конкретной модификации ДВС '
                    f'(выбранный двигатель: {vehicle["engine_model"]}). '
                    'Агент не выбирает одну инструкцию как бесспорную; рекомендация и зависимая заявка заблокированы.',
                    'conflicts': exc.conflicts, 'claims': [], 'draft': None, 'can_draft': False,
                    'summary': verdict_summary(state.get('diagnosis'), conflict=True),
                    'observations': state['diagnosis'].description if state.get('diagnosis') else '',
                    'warning': bool(state.get('diagnosis') and state['diagnosis'].warning), 'revision': state['revision']}
            except (ServiceError, ValueError) as exc:
                state['draft'] = state['answer'] = state['preview_id'] = None
                d = state.get('diagnosis')
                return JSONResponse(status_code=503, content={'error': str(exc),
                    'message': basic_result(d) if d else '', 'revision': state['revision'],
                    'summary': verdict_summary(d), 'observations': d.description if d else '',
                    'warning': bool(d and d.warning)})

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
            try:
                ticket_id = await services.ticket(fresh)
                return {'ticket_id': ticket_id, 'status': 'created', 'revision': state['revision']}
            except ServiceError as exc:
                return JSONResponse(status_code=502, content={'error': str(exc), 'revision': state['revision']})

    @app.get('/manual/{doc}/{page}')
    async def manual(request: Request, doc: str, page: int):
        state = state_for(request, write=False)
        if not state['pair'] or doc not in BY_ID:
            raise HTTPException(404)
        vehicle = catalog.pair(*state['pair'])[0]
        m = BY_ID[doc]
        if vehicle['engine_model'] not in m.engines or not 1 <= page <= m.pages:
            raise HTTPException(404)
        # Local display only. No public static mount of the manual directory.
        path = settings.manuals / m.filename
        if not path.is_file():
            raise HTTPException(404, 'PDF отсутствует в локальной папке.')
        try:
            corpus.assert_source(doc)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return FileResponse(path, media_type='application/pdf')

    return app


app = create_app()
