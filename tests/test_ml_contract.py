"""README invariants at the HTTP boundary; no live ML or LLM is simulated as acceptance."""
import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from mechanic.app import create_app
from mechanic.clients import Services, ServiceError
from mechanic.config import ROOT, Settings

DATA = json.loads((ROOT/'neurodvs_hackathon_mock_2026-11-16/data/vehicles.json').read_text(encoding='utf-8'))
ANOMALY = next((v, s) for v in DATA['vehicles'] for s in v['sessions'] if s['status'] == 'anomaly')
NORMAL = next((v, s) for v in DATA['vehicles'] for s in v['sessions'] if s['status'] == 'normal')


@pytest.mark.parametrize('status,score,accepted', [
    ('anomaly', .49, False), ('anomaly', .499999, False),
    ('anomaly', .5, True), ('anomaly', 1., True),
    ('anomaly', 1.01, False), ('anomaly', None, False),
    ('normal', .05, True), ('normal', 0., True),
])
def test_score_consistency_through_http(status, score, accepted):
    vehicle, session = ANOMALY if status == 'anomaly' else NORMAL
    payload = {k: v for k, v in session.items() if k != 'checked_at'}
    payload['anomaly_score'] = score
    calls = []

    def handler(request):
        calls.append(request)
        assert json.loads(request.content) == {'vehicle_id': vehicle['vehicle_id'], 'session_id': session['session_id']}
        return httpx.Response(200, json=payload)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            services = Services(Settings(), client)
            if accepted:
                result = await services.diagnose(vehicle['vehicle_id'], session['session_id'])
                assert result.status == status and result.anomaly_score == score
            else:
                with pytest.raises(ServiceError, match='контракту'):
                    await services.diagnose(vehicle['vehicle_id'], session['session_id'])
    asyncio.run(scenario())
    assert len(calls) == 1


def test_inconsistent_anomaly_never_reaches_llm_or_ticket():
    vehicle, session = ANOMALY
    payload = {k: v for k, v in session.items() if k != 'checked_at'}
    payload['anomaly_score'] = .49
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.url.path == '/diagnose'
        return httpx.Response(200, json=payload)

    class NoModel:
        async def json(self, *args):
            raise AssertionError('An invalid diagnosis must stop before LLM')

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(create_app(services=Services(Settings(), http), provider=NoModel())) as client:
        info = client.get('/api/session').json()
        client.headers['X-CSRF-Token'] = info['csrf']
        revision = client.post('/api/select', json={'vehicle_id': vehicle['vehicle_id'], 'session_id': session['session_id']}).json()['revision']
        response = client.post('/api/action', json={'revision': revision, 'intent': 'diagnose'})
        assert response.status_code == 503
        assert response.json()['message'] == ''
        assert client.post('/api/send', json={'revision': revision, 'preview_id': 'invented'}).status_code == 409
    assert calls == ['/diagnose']


@pytest.mark.parametrize('endpoint,status', [
    ('diagnose', 400), ('diagnose', 404), ('diagnose', 422),
    ('tickets', 400), ('tickets', 404), ('tickets', 422), ('tickets', 418),
])
def test_service_errors_never_become_success_or_retry(endpoint, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='Private service detail must not reach the UI')

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            services = Services(Settings(), client)
            with pytest.raises(ServiceError) as error:
                if endpoint == 'diagnose':
                    await services.diagnose('vehicle', 'session')
                else:
                    await services.ticket({})
            assert str(status) in str(error.value)
            assert 'Private' not in str(error.value)
    asyncio.run(scenario())
    assert len(calls) == 1


def test_ticket_transmits_russian_json_without_losing_fields():
    payload = {'vehicle_id': ANOMALY[0]['vehicle_id'], 'session_id': ANOMALY[1]['session_id'],
               'anomaly_type': ANOMALY[1]['anomaly_type'], 'conclusion': 'Синтетическая проверка транспорта JSON',
               'urgency': 'medium', 'operations': ['Тестовая строка без ремонтной рекомендации'],
               'spare_parts': [], 'references': [{'manual': 'Тестовый документ', 'pdf_page': 1}]}
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers['Content-Type'] == 'application/json'
        assert json.loads(request.content.decode('utf-8')) == payload
        return httpx.Response(201, json={'ticket_id': 'TKT-TRANSPORT-TEST', 'status': 'created'})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await Services(Settings(), client).ticket(payload) == 'TKT-TRANSPORT-TEST'
    asyncio.run(scenario())
    assert len(calls) == 1
