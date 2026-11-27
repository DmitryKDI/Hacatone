from dataclasses import replace
import hashlib
import json
import sqlite3

import pymupdf
from fastapi.testclient import TestClient

from mechanic.app import create_app
from mechanic.clients import Diagnosis
from mechanic.config import Settings
from mechanic.corpus import Corpus, terms
from mechanic.inspection import PAIR
from mechanic.manuals import BY_ID


def test_admin_login_protects_web_static_api_and_document_routes():
    locked = TestClient(create_app(Settings(), require_admin=True))
    assert locked.get('/').status_code == 503
    client = TestClient(create_app(replace(Settings(), admin_password='test-password'), require_admin=True))
    for url in ('/', '/static/app.js', '/api/catalog', '/manual/236-rk/257', '/openapi.json'):
        response = client.get(url)
        assert response.status_code == 401
        assert response.headers['WWW-Authenticate'].startswith('Basic')
    for credentials in (('other', 'test-password'), ('admin', 'wrong')):
        assert client.get('/', auth=credentials).status_code == 401
    assert client.get('/', auth=('admin', 'test-password')).status_code == 200


def test_inspection_f7_and_single_page_access(tmp_path):
    settings = replace(Settings(), corpus=tmp_path/'manuals.sqlite', manuals=tmp_path,
                       admin_password='test-password')
    # Invented text and PDF for the contract test.
    work = 'Совместить метку Е с меткой Д начала подачи топлива на учебной схеме стенда;'
    text = 'Учебный стенд. ' + work
    pdf_path = tmp_path/BY_ID['236-rk'].filename
    with pymupdf.open() as pdf:
        for _ in range(258):
            pdf.new_page()
        pdf.save(pdf_path)
    with sqlite3.connect(settings.corpus) as db:
        db.executescript("""PRAGMA user_version=2;
            CREATE TABLE documents(id TEXT,sha256 TEXT);
            CREATE TABLE chunks(id TEXT,doc TEXT,page INTEGER,text TEXT,quality TEXT);
            CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED,doc,tokens);""")
        db.execute('INSERT INTO documents VALUES (?,?)', ('236-rk', hashlib.sha256(pdf_path.read_bytes()).hexdigest()))
        db.execute('INSERT INTO chunks VALUES (?,?,?,?,?)', ('p','236-rk',257,text,'text'))
        db.execute('INSERT INTO search VALUES (?,?,?)', ('p','236-rk',' '.join(terms(text))))

    class Services:
        sent = []

        async def diagnose(self, *pair):
            assert pair == PAIR
            vehicle = next(v for v in json.loads(settings.vehicles.read_text())['vehicles'] if v['vehicle_id'] == pair[0])
            row = next(s for s in vehicle['sessions'] if s['session_id'] == pair[1])
            return Diagnosis.model_validate({key: row[key] for key in Diagnosis.model_fields})

        async def ticket(self, payload):
            assert payload['operations'] == [work]
            self.sent.append(payload)
            return 'TKT-F7-TEST'

    class Provider:
        async def json(self, policy, context):
            if 'items' in context:
                return {'verdicts': [{'id': item['id'], 'status': 'supported'} for item in context['items']],
                        'comparison': 'clear', 'conflicts': []}
            if not context['fragments']:
                return {'tool': 'search', 'query': 'начало подачи топлива'}
            evidence = {'fragment_id': 'p', 'text': work, 'quote': work, 'section': 'checks'}
            draft = context['intent'] == 'draft'
            if draft:
                assert context['inspection_result'] == 'Метки не совпадают'
                assert any(context['_quote_catalog'][key]['quote'] == work for key in context['operation_quote_ids'])
            return {'tool': 'finish', 'answer': {'sufficient': True, 'claims': [evidence],
                                                  'operations': [evidence] if draft else []}}

    services = Services()

    def connect():
        app = create_app(settings, services=services, provider=Provider(), corpus=Corpus(settings.corpus, settings.manuals))
        client = TestClient(app)
        client.auth = ('admin', 'test-password')
        client.headers['X-CSRF-Token'] = client.get('/api/session').json()['csrf']
        revision = client.post('/api/select', json={'vehicle_id': PAIR[0], 'session_id': PAIR[1]}).json()['revision']
        return client, revision

    first, revision = connect()
    assert first.get('/manual/236-rk/257').status_code == 404
    rule = first.post('/api/action', json={'intent': 'chat', 'revision': revision,
                                         'question': 'Нужно ли вводить измеренное значение угла?'}).json()
    assert rule['message'].startswith('Нет.')
    rule = first.post('/api/action', json={'intent': 'chat', 'revision': revision,
                                         'question': 'Нужно ли подавать заявку, если метки совпали?'}).json()
    assert rule['message'].startswith('Нет.') and 'Отклонение по топливоподаче остаётся' in rule['message']
    diagnosed = first.post('/api/action', json={'intent': 'diagnose', 'revision': revision}).json()
    assert diagnosed['inspection']['result'] is None and not diagnosed['can_draft']
    assert first.get('/manual/236-rk/256').status_code == 404
    response = first.get('/manual/236-rk/257')
    assert response.status_code == 200
    with pymupdf.open(stream=response.content) as pdf:
        assert len(pdf) == 1
    assert first.post('/api/action', json={'intent': 'draft', 'revision': revision}).status_code == 503
    assert first.post('/api/inspection', json={'result': 'mismatch', 'revision': revision}).json()['can_draft']
    preview = first.post('/api/action', json={'intent': 'draft', 'revision': revision}).json()
    assert 'осмотра механика' in preview['draft']['conclusion']
    assert not services.sent
    assert first.post('/api/send', json={'preview_id': preview['preview_id'], 'revision': revision}).status_code == 200
    assert len(services.sent) == 1
    preview = first.post('/api/action', json={'intent': 'draft', 'revision': revision}).json()

    second, second_revision = connect()
    assert second.post('/api/action', json={'intent': 'diagnose', 'revision': second_revision}).json()['inspection']['result'] == 'mismatch'
    matched = second.post('/api/inspection', json={'result': 'match', 'revision': second_revision}).json()
    assert not matched['can_draft'] and 'Отклонение по топливоподаче остаётся' in matched['message']
    assert first.post('/api/send', json={'preview_id': preview['preview_id'], 'revision': revision}).status_code == 409
    assert len(services.sent) == 1
