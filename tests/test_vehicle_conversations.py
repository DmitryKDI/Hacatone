from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sqlite3

from fastapi.testclient import TestClient

from mechanic.agent import Agent
from mechanic.app import create_app
from mechanic.config import Settings
from mechanic.conversations import ConversationStore
from mechanic.team_access import derive

PAIR_A = ('P456AT178', 'S-0001')
PAIR_B = ('M333YK47', 'S-0005')


def connect(client):
    response = client.get('/api/session')
    assert response.status_code == 200
    client.headers['X-CSRF-Token'] = response.json()['csrf']


def select(client, pair):
    response = client.post('/api/select', json={'vehicle_id': pair[0], 'session_id': pair[1]})
    assert response.status_code == 200, response.text
    return response.json()


def chat(client, selected, question):
    response = client.post('/api/action', json={'revision': selected['revision'], 'intent': 'chat', 'question': question})
    assert response.status_code == 200, response.text
    return response.json()


def finish(client, selected, identifier):
    return client.post('/api/conversations/finish', json={'revision': selected['revision'],
                                                         'conversation_id': identifier})


def demo(app, cookies=None):
    return TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 40000), cookies=cookies)


def test_handoff_after_restart_and_vehicle_isolation(tmp_path, monkeypatch):
    async def response(self, state, vehicle, question, intent):
        previous = state['previous_context']
        reply = 'Проверить источник' if not previous else 'Продолжили: ' + previous['exchanges'][-1]['question']
        return {'message': reply, 'claims': [], 'draft': None}

    monkeypatch.setattr(Agent, 'run', response)
    path = tmp_path / 'context.sqlite'
    repairs = tmp_path / 'repair.json'
    repairs.write_text(json.dumps({PAIR_A[0]: [{'recorded_at': '2026-11-27', 'text': 'Запись мастера'}]}))
    settings = replace(Settings(), conversations_db=path, repair_log_file=repairs)
    with demo(create_app(settings)) as alice, demo(create_app(settings)) as bob:
        connect(alice)
        connect(bob)
        selected = select(alice, PAIR_A)
        other_selected = select(bob, PAIR_B)
        first = chat(alice, selected, 'Что проверить по ремню?')
        identifier = first['conversation_id']
        hidden = bob.get(f"/api/conversations/{identifier}?revision={other_selected['revision']}")
        assert hidden.status_code == 404
        matching = select(bob, PAIR_A)
        assert matching['conversations']['saved'] == []
        assert bob.get(f"/api/conversations/{identifier}?revision={matching['revision']}").status_code == 404
        alice_switch = select(alice, PAIR_B)
        assert alice_switch['conversations']['active'] is None
        selected = select(alice, PAIR_A)
        assert selected['conversations']['active']['turns'][0]['question'] == 'Что проверить по ремню?'
        saved = finish(alice, selected, identifier)
        assert saved.status_code == 200
        assert saved.json()['latest']['exchanges'][0]['question'] == 'Что проверить по ремню?'
        assert finish(alice, selected, identifier).json()['saved'] == saved.json()['saved']

    with sqlite3.connect(path) as db:
        db.execute('UPDATE conversations SET summary=NULL WHERE id=?', (identifier,))

    with demo(create_app(settings)) as next_mechanic:
        connect(next_mechanic)
        selected = select(next_mechanic, PAIR_A)
        assert selected['conversations']['repair_log'][0]['text'] == 'Запись мастера'
        assert selected['conversations']['saved'][0]['id'] == identifier
        assert selected['conversations']['latest']['exchanges'][0]['question'] == 'Что проверить по ремню?'
        actual = chat(next_mechanic, selected, 'Что дальше?')
        assert actual['message'] == 'Продолжили: Что проверить по ремню?'
        assert actual['conversation_id'] != identifier
        other = select(next_mechanic, PAIR_B)
        assert other['conversations']['latest'] is None
        assert other['conversations']['repair_log'] == []


def test_two_concurrent_conversations_survive(tmp_path):
    path = tmp_path / 'context.sqlite'
    store = ConversationStore(path)

    def exchange(owner):
        identifier = store.append(None, PAIR_A, owner, owner, owner, 'Ответ', False, [])
        store.finish(identifier, PAIR_A[0], owner)
        return identifier

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(exchange, ['ivan', 'petr']))
    history = ConversationStore(path).overview(PAIR_A[0], 'next')
    assert {item['id'] for item in history['saved']} == set(results)
    assert all(len(store.read(identifier, PAIR_A[0], 'next')['turns']) == 1 for identifier in results)


def test_unconfigured_network_client_has_no_history(tmp_path):
    settings = replace(Settings(), conversations_db=tmp_path/'context.sqlite')
    with TestClient(create_app(settings)) as client:
        connect(client)
        assert 'conversations' not in select(client, PAIR_A)
        assert not settings.conversations_db.exists()


def test_team_access_requires_independent_accounts_and_https(tmp_path):
    path = tmp_path / 'members.json'
    salt = b'0123456789abcdef'
    credential = f'{salt.hex()}:{derive("test-password-only", salt).hex()}'
    path.write_text(json.dumps({'ivan': credential, 'petr': credential}))
    settings = replace(Settings(), team_users_file=path, conversations_db=tmp_path/'context.sqlite')
    app = create_app(settings)
    with TestClient(app, base_url='http://fleet.example') as insecure:
        assert insecure.get('/api/session', auth=('ivan', 'test-password-only')).status_code == 403
    with TestClient(app, base_url='https://fleet.example') as secure:
        assert secure.get('/api/session').status_code == 401
        secure.auth = ('ivan', 'test-password-only')
        connect(secure)
        selected = select(secure, PAIR_A)
        assert 'conversations' in selected
        secure.auth = ('petr', 'test-password-only')
        assert secure.post('/api/select', json={'vehicle_id': PAIR_A[0], 'session_id': PAIR_A[1]}).status_code == 409



def test_incomplete_conversation_is_readable_by_owner_after_restart(tmp_path, monkeypatch):
    async def response(self, state, vehicle, question, intent):
        return {'message': 'Ответ без утверждения о ремонте', 'claims': [], 'draft': None}

    monkeypatch.setattr(Agent, 'run', response)
    settings = replace(Settings(), conversations_db=tmp_path/'context.sqlite')
    with demo(create_app(settings)) as author:
        connect(author)
        first = chat(author, select(author, PAIR_A), 'Неоконченный разговор')
        cookies = dict(author.cookies)
    with demo(create_app(settings), cookies) as restored:
        connect(restored)
        context = select(restored, PAIR_A)
        assert context['conversations']['latest'] is None
        record = context['conversations']['saved'][0]
        assert record['id'] == first['conversation_id']
        assert record['ended_at'] is None
        response = restored.get(f"/api/conversations/{record['id']}?revision={context['revision']}")
        assert response.json()['turns'][0]['question'] == 'Неоконченный разговор'


def test_api_key_typed_into_chat_is_never_saved(tmp_path):
    settings = replace(Settings(), conversations_db=tmp_path/'context.sqlite', openai_key='test-secret-only')
    with demo(create_app(settings)) as client:
        connect(client)
        context = select(client, PAIR_A)
        body = {'revision': context['revision'], 'intent': 'chat', 'question': 'ключ test-secret-only'}
        result = client.post('/api/action', json=body)
        assert result.status_code == 422
        assert 'test-secret-only' not in result.text
        assert select(client, PAIR_A)['conversations']['saved'] == []
