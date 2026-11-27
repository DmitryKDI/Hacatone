import asyncio
import json
import httpx
import pytest
from mechanic.agent import Decision
from mechanic.config import Settings
from mechanic.grounding import Verification
from mechanic.providers import Provider
from mechanic.schemas import response_schema, quote_catalog
from mechanic.clients import ServiceError


@pytest.mark.parametrize('model,definition', [(Decision, 'Evidence'), (Verification, 'SourceQuote')])
def test_quote_choices_are_literal_and_do_not_repair_table_text(model, definition):
    source = 'Условный источник.\nПроверить клапан-\nный механизм без нагрузки.'
    fragments = [{'id': 'original', 'text': source}]
    schema = response_schema(model, fragments)
    catalog = quote_catalog(fragments)
    props = schema['$defs'][definition]['properties']
    assert props['fragment_id']['enum'] == ['original']
    normalized = ' '.join(source.split())
    quotes = [entry['quote'] for entry in catalog.values()]
    assert set(props['quote']['enum']) == set(catalog)
    assert normalized in quotes
    assert all(q in normalized for q in quotes)
    assert not any('клапанный' in q for q in quotes)
    noisy = quote_catalog([{'id': 'original', 'text': source + '\n269                 5'}])
    assert not any(q['quote'] == '269 5' for q in noisy.values())
    assert set(schema['required']) == set(schema['properties'])
    assert schema['additionalProperties'] is False


@pytest.mark.parametrize('vendor', ['openai', 'yandex'])
def test_both_adapters_request_schema_without_echoing_it_in_user_data(vendor):
    schema = response_schema(Decision)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if vendor == 'openai':
            assert body['response_format']['type'] == 'json_schema'
            assert body['response_format']['json_schema']['strict'] is True
            data = {'choices': [{'message': {'content': '{"ok":true}'}}]}
        else:
            assert 'jsonObject' not in body and body['jsonSchema']['schema'] == schema
            data = {'result': {'alternatives': [{'message': {'text': '{"ok":true}'}}]}}
        context = json.loads(body['messages'][-1]['content' if vendor == 'openai' else 'text'])
        assert context == {'question': 'check'}
        return httpx.Response(200, json=data)

    async def scenario():
        settings = Settings(provider=vendor, openai_key='test', openai_model='test',
                            yandex_key='test', yandex_folder='test', yandex_model='gpt://test/test')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await Provider(settings, client).json('JSON', {'question': 'check', '_response_schema': schema}) == {'ok': True}

    asyncio.run(scenario())
    assert len(requests) == 1


@pytest.mark.parametrize('wrong_fragment', [False, True])
def test_quote_selector_restores_only_its_original_fragment(wrong_fragment):
    original = 'Проверить условный клапан- ный механизм.'
    catalog = {'source_quote_000': {'fragment_id': 'real', 'quote': original}}
    value = {'operations': [{'fragment_id': 'foreign' if wrong_fragment else 'real',
                             'quote': 'source_quote_000'}]}
    def handler(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(value)}}]})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = Provider(Settings(openai_key='test', openai_model='test'), client)
            if wrong_fragment:
                with pytest.raises(ServiceError, match='не принадлежит'):
                    await provider.json('JSON', {'_quote_catalog': catalog})
            else:
                result = await provider.json('JSON', {'_quote_catalog': catalog})
                assert result['operations'][0]['quote'] == original
                assert result['operations'][0]['text'] == original
    asyncio.run(scenario())


def test_generation_effort_does_not_reduce_independent_verifier_effort():
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}}]})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = Provider(Settings(openai_key='test', openai_model='gpt-6-luna'), client)
            await provider.json('JSON', {'question': 'check'})
            await provider.json('JSON', {'items': []})
    asyncio.run(scenario())
    assert bodies[0]['reasoning_effort'] == 'low'
    assert 'reasoning_effort' not in bodies[1]


def test_truncated_json_is_not_a_model_format_or_success_result():
    def handler(request):
        return httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': None}}]})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ServiceError, match='лимита выходных токенов'):
                await Provider(Settings(openai_key='test', openai_model='gpt-6-luna'), client).json('JSON', {})
    asyncio.run(scenario())
