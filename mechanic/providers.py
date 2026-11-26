import asyncio
import json
import httpx
from .clients import ServiceError


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
            raise ServiceError(problem)
        text = json.dumps(context, ensure_ascii=False)
        if len(text) > 48000:
            raise ServiceError('Контекст слишком велик. Уточните вопрос.')
        if self.settings.provider == 'openai':
            url = 'https://api.openai.com/v1/chat/completions'
            headers = {'Authorization': 'Bearer ' + self.settings.openai_key}
            body = {'model': self.settings.openai_model, 'messages': [
                {'role': 'system', 'content': system}, {'role': 'user', 'content': text}],
                'response_format': {'type': 'json_object'}, 'max_completion_tokens': 2500}
        else:
            url = 'https://ai.api.cloud.yandex.net/foundationModels/v1/completion'
            headers = {'Authorization': 'Api-Key ' + self.settings.yandex_key,
                       'x-folder-id': self.settings.yandex_folder}
            body = {'modelUri': self.settings.yandex_model,
                    'completionOptions': {'stream': False, 'temperature': 0, 'maxTokens': '2500'},
                    'messages': [{'role': 'system', 'text': system}, {'role': 'user', 'text': text}],
                    'jsonObject': True}
        for attempt in range(2):
            try:
                response = await self.client.post(url, headers=headers, json=body)
            except httpx.RequestError:
                if attempt == 0:
                    await asyncio.sleep(.4)
                    continue
                raise ServiceError('LLM: сеть недоступна или превышено время ожидания.') from None
            if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                await asyncio.sleep(.4)
                continue
            if response.status_code != 200:
                # Never expose response bodies: they can echo credentials or requests.
                raise ServiceError(f'LLM {self.settings.provider}: HTTP {response.status_code}. Проверьте доступ, модель и квоты.')
            try:
                data = response.json()
                if self.settings.provider == 'openai':
                    value = data['choices'][0]['message']['content']
                else:
                    value = data['result']['alternatives'][0]['message']['text']
                result = json.loads(value)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
            except (KeyError, IndexError, TypeError, ValueError):
                raise ServiceError('LLM вернула непригодный JSON. Ответ не используется.') from None
