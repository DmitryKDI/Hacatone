"""Opt-in integration with running organizer services. Never starts or restarts them."""
import json
import os
from collections import Counter
from dataclasses import replace
import httpx
import pytest
from fastapi.testclient import TestClient
from mechanic.app import create_app
from mechanic.clients import Services
from mechanic.config import ROOT, Settings

pytestmark = pytest.mark.skipif(os.environ.get('MECHANIC_LIVE_CHECKS') != '1',
                                reason='Set MECHANIC_LIVE_CHECKS=1 for running organizer services')


def test_all_195_pairs_through_application_without_llm_or_ticket():
    # Deliberately verify unavailable-generation behavior, even if a developer has credentials.
    settings = replace(Settings.load(), openai_key='')
    calls = []

    async def record(request):
        calls.append({'method': request.method, 'path': request.url.path})
        assert request.method == 'POST' and request.url.path == '/diagnose'

    http = httpx.AsyncClient(timeout=10, trust_env=False, event_hooks={'request': [record]})
    data = json.loads(settings.vehicles.read_text(encoding='utf-8'))
    statuses, groups, results = Counter(), set(), []
    with TestClient(create_app(settings, services=Services(settings, http))) as client:
        session = client.get('/api/session').json()
        client.headers['X-CSRF-Token'] = session['csrf']
        for vehicle in data['vehicles']:
            groups.add(vehicle['engine_model'])
            for check in vehicle['sessions']:
                selected = client.post('/api/select', json={'vehicle_id': vehicle['vehicle_id'], 'session_id': check['session_id']})
                assert selected.status_code == 200
                revision = selected.json()['revision']
                response = client.post('/api/action', json={'revision': revision, 'intent': 'diagnose'})
                body = response.json()
                status = check['status']
                statuses[status] += 1
                expected_warning = check['signal_quality'] != 'bad' and status != 'check_failed' and (
                    check['signal_quality'] == 'warning' or
                    (check['confidence'] is not None and check['confidence'] < .70))
                assert body['warning'] == expected_warning
                if status == 'normal':
                    assert response.status_code == 200 and 'не выявила отклонений' in body['message']
                    assert body['claims'] == [] and body['draft'] is None
                elif status == 'check_failed':
                    assert response.status_code == 200 and 'Повторите запись' in body['message']
                    assert body['claims'] == [] and body['draft'] is None
                else:
                    assert response.status_code == 503 and 'OPENAI_API_KEY' in body['error']
                    assert check['anomaly_title'] in body['message']
                assert client.post('/api/send', json={'revision': revision, 'preview_id': 'not-a-preview'}).status_code == 409
                results.append({'vehicle_id': vehicle['vehicle_id'], 'session_id': check['session_id'],
                                'status': status, 'app_http': response.status_code, 'warning': body['warning']})
    assert statuses == {'normal': 111, 'anomaly': 69, 'check_failed': 15}
    assert len(groups) == 5 and len(calls) == 195
    assert all(c['path'] == '/diagnose' for c in calls)
    # Contains organizer metadata and outcomes, no extracted manual text or credentials.
    report = {'scope': 'live ML HTTP through in-process application, LLM deliberately disabled',
              'pairs': len(results), 'statuses': dict(statuses), 'engine_models': sorted(groups),
              'ml_requests': len(calls), 'toir_requests': 0, 'live_llm_verified': False, 'results': results}
    path = ROOT/'corpus/stage-8-live.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
