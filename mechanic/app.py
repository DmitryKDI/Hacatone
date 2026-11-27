import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, Request, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic
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
from . import inspection

HERE = Path(__file__).parent
admin_login = HTTPBasic(realm='Digital Mechanic')
MANUAL_IDS = {manual.title: doc for doc, manual in BY_ID.items()}


def extract_manual_page(path, page):
    import pymupdf

    with pymupdf.open(path) as source, pymupdf.open() as output:
        output.insert_pdf(source, from_page=page - 1, to_page=page - 1,
                          links=False, annots=False, widgets=False)
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


class Send(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int
    preview_id: str


class InspectionResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int
    result: Literal['match', 'mismatch']


def create_app(settings=None, services=None, provider=None, corpus=None, *, require_admin=False):
    settings = settings or Settings.load()
    services = services or Services(settings)
    provider = provider or Provider(settings)
    corpus = corpus or Corpus(settings.corpus, settings.manuals)
    catalog = Catalog(settings.vehicles)
    agent = Agent(provider, services, corpus)
    inspections = inspection.InspectionStore(settings.corpus.with_name('inspections.sqlite'))
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
        try:
            if require_admin or settings.admin_password:
                if not settings.admin_password:
                    raise HTTPException(503, 'Установите ADMIN_PASSWORD на сервере.')
                credentials = await admin_login(request)
                valid_name = secrets.compare_digest(credentials.username.encode(), b'admin')
                valid_password = secrets.compare_digest(credentials.password.encode(), settings.admin_password.encode())
                if not (valid_name and valid_password):
                    raise HTTPException(401, 'Требуется вход администратора.',
                                        headers={'WWW-Authenticate': 'Basic realm="Digital Mechanic"'})
        except HTTPException as exc:
            response = JSONResponse({'detail': exc.detail}, status_code=exc.status_code, headers=exc.headers)
        else:
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

    def inspection_status(state):
        if not inspection.applies(state.get('diagnosis')):
            return None
        record = state.get('inspection')
        return {'result': record['result'] if record else None,
                'recorded_at': record['recorded_at'] if record else None}

    def can_draft(state):
        diagnosis = state.get('diagnosis')
        status = inspection_status(state)
        if status is not None:
            return status['result'] == 'mismatch' and not state['conflicts']
        return bool(diagnosis and diagnosis.usable and diagnosis.status == 'anomaly' and state['answer'])

    def allow_sources(state, sources):
        state['source_pages'].update((source['doc'], source['pdf_page']) for source in sources)

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
        response.set_cookie('mechanic_session', sid, httponly=True, samesite='strict', max_age=7200,
                            secure=request.url.scheme == 'https')
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
                         history=[], conflicts=conflicts, diagnosis_fragments=[], inspection=None,
                         preview_inspection=None, source_pages=set())
            return {'revision': state['revision']}

    @app.post('/api/inspection')
    async def record_inspection(request: Request, body: InspectionResult):
        state = state_for(request)
        async with state['lock']:
            selected(state, body.revision)
            if not inspection.applies(state['diagnosis']):
                raise HTTPException(409, 'Сначала получите результат диагностики для сверки меток.')
            state['inspection'] = inspections.save(state['pair'], body.result)
            state['preview_id'] = state['draft'] = state['preview_inspection'] = None
            message = ('Метки не совпадают. Результат сохранён. Можно подготовить заявку на регулировку по п. 5.12.1 РК.'
                       if body.result == 'mismatch' else
                       'Метки совпали. Результат сохранён. Заявка на регулировку не нужна. Отклонение по топливоподаче остаётся.')
            return {'inspection': inspection_status(state), 'can_draft': can_draft(state), 'message': message}

    @app.post('/api/action')
    async def action(request: Request, body: Action):
        state = state_for(request)
        async with state['lock']:
            vehicle = selected(state, body.revision)
            if body.intent == 'chat' and not body.question.strip():
                raise HTTPException(422, 'Введите вопрос.')
            state['preview_id'] = None
            state['inspection'] = inspections.get(state['pair']) if state['pair'] == inspection.PAIR else None
            if (state['pair'] == inspection.PAIR and body.intent == 'chat'
                    and inspection.asks_about_rule(body.question)):
                return {'message': inspection.reply(body.question),
                        'revision': state['revision'], 'claims': [], 'draft': None,
                        'inspection': inspection_status(state), 'can_draft': can_draft(state)}
            try:
                result = await agent.run(state, vehicle, body.question, body.intent)
                if state['draft']:
                    state['preview_id'] = secrets.token_urlsafe(24)
                    record = state.get('inspection')
                    state['preview_inspection'] = record['version'] if record else None
                    result['preview_id'] = state['preview_id']
                    result['operations_evidence'] = [e.model_dump() for e in state['answer'].operations]
                    result['draft_sources'] = [{**ref, 'doc': MANUAL_IDS[ref['manual']]}
                                               for ref in state['draft']['references']]
                    allow_sources(state, result['draft_sources'])
                    result['priority_basis'] = (state['answer'].priority_basis.model_dump()
                                                if state['answer'].priority_basis else None)
                d = state.get('diagnosis')
                result['summary'] = verdict_summary(d, state['answer'])
                result['observations'] = d.description if d else ''
                result['warning'] = bool(d and d.warning)
                result['can_draft'] = can_draft(state)
                result['inspection'] = inspection_status(state)
                result['revision'] = state['revision']
                allow_sources(state, result['claims'])
                state['history'].extend([{'role': 'user', 'text': body.question or body.intent},
                                         {'role': 'assistant', 'text': ' '.join(c['text'] for c in result['claims'])[:1500]}])
                state['history'] = state['history'][-6:]
                return result
            except InstructionConflict as exc:
                state['draft'] = state['answer'] = state['preview_id'] = None
                state['conflicts'] = exc.conflicts
                state['conflicts_by_pair'][state['pair']] = exc.conflicts
                for conflict in exc.conflicts:
                    allow_sources(state, conflict['sources'])
                return {'status': 'needs_clarification', 'message':
                    'Обнаружено противоречие технических инструкций. Обе формулировки приведены ниже. '
                    f'Уточните применимость у специалиста или производителя для конкретной модификации ДВС '
                    f'(выбранный двигатель: {vehicle["engine_model"]}). '
                    'Агент не выбирает одну инструкцию как бесспорную; рекомендация и зависимая заявка заблокированы.',
                    'conflicts': exc.conflicts, 'claims': [], 'draft': None, 'can_draft': False,
                    'inspection': inspection_status(state),
                    'summary': verdict_summary(state.get('diagnosis'), conflict=True),
                    'observations': state['diagnosis'].description if state.get('diagnosis') else '',
                    'warning': bool(state.get('diagnosis') and state['diagnosis'].warning), 'revision': state['revision']}
            except (ServiceError, ValueError) as exc:
                state['draft'] = state['answer'] = state['preview_id'] = None
                d = state.get('diagnosis')
                return JSONResponse(status_code=503, content={'error': str(exc),
                    'message': basic_result(d) if d else '', 'revision': state['revision'],
                    'inspection': inspection_status(state), 'can_draft': can_draft(state),
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
                record = None
                if inspection.applies(state['diagnosis']):
                    record = inspections.get(state['pair'])
                    if not record or record['version'] != state['preview_inspection']:
                        raise ServiceError('Результат осмотра изменился. Нужен новый предпросмотр.')
                fresh = draft_payload(state['answer'], state['diagnosis'], vehicle['engine_model'], corpus,
                                      state.get('answer_question', ''), inspection_record=record)
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
        if (vehicle['engine_model'] not in m.engines or not 1 <= page <= m.pages
                or (doc, page) not in state['source_pages']):
            raise HTTPException(404)
        # Local display only. No public static mount of the manual directory.
        path = settings.manuals / m.filename
        if not path.is_file():
            raise HTTPException(404, 'PDF отсутствует в локальной папке.')
        try:
            await run_in_threadpool(corpus.assert_source, doc)
            content = await run_in_threadpool(extract_manual_page, path, page)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from None
        return Response(content, media_type='application/pdf',
                        headers={'Content-Disposition': f'inline; filename="{doc}-{page}.pdf"'})

    return app


app = create_app(require_admin=True)
