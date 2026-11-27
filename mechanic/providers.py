import asyncio
import json
import httpx
from .clients import ServiceError


class ModelUnavailable(ServiceError):
    pass


class QuoteSelectionError(ServiceError):
    def __init__(self, selector, fragment_id):
        super().__init__('Выбранная выдержка не принадлежит указанному фрагменту. Ответ заблокирован.')
        self.selector = selector
        self.fragment_id = fragment_id


class Provider:
    """One JSON operation used for tool decisions, answers, and independent verification."""
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=30, trust_env=False)

    async def close(self):
        await self.client.aclose()

    async def json(self, system, context):
        problem = self.settings.llm_problem()
        if problem:
            raise ModelUnavailable(problem)
        schema = context.get('_response_schema')
        catalog = context.get('_quote_catalog', {})
        payload = {k: v for k, v in context.items() if k not in {'_response_schema', '_quote_catalog'}}
        if catalog:
            payload['source_quotes'] = [{'id': key, **value} for key, value in catalog.items()]
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) > 48000:
            raise ServiceError('Контекст слишком велик. Уточните вопрос.')
        if self.settings.provider == 'openai':
            url = 'https://api.openai.com/v1/chat/completions'
            headers = {'Authorization': 'Bearer ' + self.settings.openai_key}
            luna = self.settings.openai_model == 'gpt-6-luna'
            developer_role = 'developer' if luna else 'system'
            body = {'model': self.settings.openai_model, 'messages': [
                {'role': developer_role, 'content': system}, {'role': 'user', 'content': text}],
                'response_format': {'type': 'json_object'}, 'max_completion_tokens': 2500}
            if schema:
                body['response_format'] = {'type': 'json_schema', 'json_schema': {
                    'name': 'mechanic_response', 'strict': True, 'schema': schema}}
            if self.settings.openai_model.startswith('gpt-6') and 'items' not in context:
                # Tool selection needs compact decisions; keep the verifier's default effort.
                body['reasoning_effort'] = 'low'
            if luna and schema and schema.get('properties', {}).get('tool', {}).get('enum') == ['search']:
                body['max_completion_tokens'] = 700
        else:
            url = 'https://ai.api.cloud.yandex.net/foundationModels/v1/completion'
            headers = {'Authorization': 'Api-Key ' + self.settings.yandex_key,
                       'x-folder-id': self.settings.yandex_folder}
            body = {'modelUri': self.settings.yandex_model,
                    'completionOptions': {'stream': False, 'temperature': 0, 'maxTokens': '2500'},
                    'messages': [{'role': 'system', 'text': system}, {'role': 'user', 'text': text}],
                    'jsonObject': True}
            if schema:
                body.pop('jsonObject')
                body['jsonSchema'] = {'schema': schema}
        for attempt in range(2):
            try:
                response = await self.client.post(url, headers=headers, json=body)
            except httpx.RequestError:
                if attempt == 0:
                    await asyncio.sleep(.4)
                    continue
                raise ModelUnavailable('LLM: сеть недоступна или превышено время ожидания.') from None
            if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                await asyncio.sleep(.4)
                continue
            if response.status_code != 200:
                # Never expose response bodies: they can echo credentials or requests.
                raise ModelUnavailable(f'LLM {self.settings.provider}: HTTP {response.status_code}. Проверьте доступ, модель и квоты.')
            try:
                data = response.json()
                if self.settings.provider == 'openai':
                    if data['choices'][0].get('finish_reason') == 'length':
                        if luna and 'items' in context and attempt == 0:
                            body['max_completion_tokens'] = 4000
                            continue
                        raise ServiceError('LLM достигла лимита выходных токенов. Незавершённый ответ не используется.')
                    value = data['choices'][0]['message']['content']
                else:
                    value = data['result']['alternatives'][0]['message']['text']
                result = json.loads(value)
                if not isinstance(result, dict):
                    raise ValueError()
                def restore(node, operation=False):
                    if isinstance(node, dict):
                        selector = node.get('quote')
                        if isinstance(selector, str) and selector in catalog:
                            selected = catalog[selector]
                            if 'fragment_id' in node and node['fragment_id'] != selected['fragment_id']:
                                raise QuoteSelectionError(selector, selected['fragment_id'])
                            node['fragment_id'] = selected['fragment_id']
                            node['quote'] = selected['quote']
                            if node.get('text') == selector or (operation and 'text' not in node):
                                node['text'] = selected['quote']
                        for key, child in node.items():
                            restore(child, operation=key == 'operations')
                    elif isinstance(node, list):
                        for child in node:
                            restore(child, operation=operation)
                restore(result)
                return result
            except (KeyError, IndexError, TypeError, ValueError):
                raise ServiceError('LLM вернула непригодный JSON. Ответ не используется.') from None
