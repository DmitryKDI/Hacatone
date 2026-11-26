import asyncio
import json
import logging
from time import time
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest

from mechanic.clients import ServiceError
from mechanic.config import Settings
from mechanic.gigachat import CHAT_URL, OAUTH_URL, giga_response_format
from mechanic.providers import Provider


@pytest.mark.parametrize('epoch_multiplier', [1, 1000])
def test_gigachat_reuses_token_and_recovers_once_after_401(epoch_multiplier, caplog):
    issued = []
    chat_tokens = []
    settings = Settings(provider='gigachat', gigachat_auth_key='ExampleAuthorizationKey')

    def handle(request):
        if str(request.url) == OAUTH_URL:
            assert request.headers['authorization'] == 'Basic ExampleAuthorizationKey'
            assert UUID(request.headers['rquid']).version == 4
            assert parse_qs(request.content.decode()) == {'scope': ['GIGACHAT_API_PERS']}
            token = f'local-test-token-{len(issued) + 1}'
            issued.append(token)
            return httpx.Response(200, json={'access_token': token,
                                             'expires_at': int((time() + 1800) * epoch_multiplier)})
        assert str(request.url) == CHAT_URL
        token = request.headers['authorization']
        chat_tokens.append(token)
        request_body = json.loads(request.content)
        assert request_body['model'] == 'GigaChat-3-Ultra'
        assert request_body['response_format']['type'] == 'json_schema'
        assert request_body['messages'][0]['role'] == 'system'
        if len(chat_tokens) == 3:
            return httpx.Response(401, text='local-test-token-1')
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ready":true}'}}],
                                         'usage': {'prompt_tokens': 12, 'completion_tokens': 3}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = Provider(settings, client)
            assert await provider.json('JSON', {}) == {'ready': True}
            assert await provider.json('JSON', {}) == {'ready': True}
            assert await provider.json('JSON', {'items': []}) == {'ready': True}

    with caplog.at_level(logging.INFO, logger='uvicorn.error.mechanic'):
        asyncio.run(scenario())
    assert len(issued) == 2 and chat_tokens == [
        'Bearer local-test-token-1', 'Bearer local-test-token-1', 'Bearer local-test-token-1',
        'Bearer local-test-token-2',
    ]
    assert 'ExampleAuthorizationKey' not in repr(settings) + caplog.text
    assert 'local-test-token-' not in caplog.text
    assert '"stage": "verify"' in caplog.text and '"input_tokens": 12' in caplog.text


def test_gigachat_oauth_failure_redacts_response_and_stops_generation(caplog):
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(401, text='ExampleAuthorizationKey echoed by upstream')

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            settings = Settings(provider='gigachat', gigachat_auth_key='ExampleAuthorizationKey')
            with pytest.raises(ServiceError, match='OAuth: HTTP 401') as raised:
                await Provider(settings, client).json('JSON', {})
            assert 'ExampleAuthorizationKey' not in str(raised.value)

    with caplog.at_level(logging.INFO, logger='uvicorn.error.mechanic'):
        asyncio.run(scenario())
    assert calls == [OAUTH_URL]
    assert 'ExampleAuthorizationKey' not in caplog.text


def test_gigachat_requires_a_key_and_recognized_scope():
    assert 'GIGACHAT_AUTH_KEY' in Settings(provider='gigachat').llm_problem()
    assert 'GIGACHAT_SCOPE' in Settings(provider='gigachat', gigachat_auth_key='abc', gigachat_scope='invalid').llm_problem()


def test_gigachat_schemas_require_search_and_a_source_selector():
    initial = giga_response_format({'remaining_steps': 5, 'fragments': []})['schema']
    assert initial['properties']['tool']['enum'] == ['search']
    assert 'query' in initial['required']
    retrieved = giga_response_format({'remaining_steps': 2, 'fragments': [{'id': 'manual-p34'}]})['schema']
    operation = retrieved['properties']['answer']['properties']['operations']['items']
    assert operation['properties']['fragment_id']['enum'] == ['manual-p34']
    assert {choice['required'][0] for choice in operation['oneOf']} == {'passage', 'numeric_ref', 'quote'}
