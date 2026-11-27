import asyncio
import json
from time import monotonic

import httpx

from .clients import ServiceError
from .config import LUNA_MODEL
from .journal import event

CHAT_URL = 'https://api.openai.com/v1/chat/completions'


class IncompleteResponse(ServiceError):
    pass


def token_counts(data):
    usage = data.get('usage')
    if not isinstance(usage, dict):
        return {}
    counts = {}
    for field, key in (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens')):
        count = usage.get(key)
        if type(count) is int and count >= 0:
            counts[field] = count
        elif isinstance(count, str) and count.isascii() and count.isdecimal() and len(count) <= 12:
            counts[field] = int(count)
    return counts


class Provider:
    """Use Luna for tool decisions, answers and separate source verification."""

    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client

    async def close(self):
        if self.client is not None:
            await self.client.aclose()

    async def json(self, system, context):
        problem = self.settings.llm_problem()
        if problem:
            raise ServiceError(problem)
        text = json.dumps(context, ensure_ascii=False)
        if len(text) > 48000:
            raise ServiceError('Контекст слишком велик. Уточните вопрос.')
        key = self.settings.openai_key
        if key in system or key in text:
            raise ServiceError('Запрос содержит ключ API. Удалите его из текста.')
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=30, trust_env=False)
        headers = {'Authorization': 'Bearer ' + key}
        body = {
            'model': LUNA_MODEL,
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': text}],
            'response_format': {'type': 'json_object'},
            'max_completion_tokens': 3500,
            'store': False,
        }
        for attempt in range(2):
            started = monotonic()
            log_fields = {'provider': 'openai', 'model': LUNA_MODEL,
                          'stage': 'verify' if 'items' in context else 'decide', 'attempt': attempt + 1}
            try:
                response = await self.client.post(CHAT_URL, headers=headers, json=body, follow_redirects=False)
            except httpx.RequestError:
                event('llm.request', **log_fields, outcome='network_error',
                      elapsed_ms=round((monotonic() - started) * 1000))
                if attempt == 0:
                    await asyncio.sleep(.4)
                    continue
                raise ServiceError('Luna: сеть недоступна или превышено время ожидания.') from None
            log_fields.update(http=response.status_code, elapsed_ms=round((monotonic() - started) * 1000))
            if response.status_code != 200:
                event('llm.request', **log_fields, outcome='http_error')
                if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                    await asyncio.sleep(.4)
                    continue
                # The response body may contain private request data.
                raise ServiceError(f'Luna: HTTP {response.status_code}. Проверьте ключ, доступ к модели и квоты.')
            try:
                data = response.json()
                log_fields.update(token_counts(data))
                choice = data['choices'][0]
                if choice.get('finish_reason') == 'length':
                    event('llm.request', **log_fields, outcome='truncated')
                    raise IncompleteResponse('Ответ не поместился в лимит. Верни не более трёх коротких утверждений.')
                result = json.loads(choice['message']['content'])
                if not isinstance(result, dict):
                    raise ValueError()
            except (AttributeError, KeyError, IndexError, TypeError, ValueError):
                event('llm.request', **log_fields, outcome='invalid_response')
                raise ServiceError('Luna вернула непригодный JSON. Ответ не используется.') from None
            if key in json.dumps(result, ensure_ascii=False):
                event('llm.request', **log_fields, outcome='invalid_response')
                raise ServiceError('Ответ Luna содержит закрытые данные. Ответ не используется.')
            event('llm.request', **log_fields, outcome='ok')
            return result
