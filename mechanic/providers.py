import asyncio
import json
from time import monotonic
import httpx
from .clients import ServiceError
from .gigachat import CHAT_URL, GigaChatAuth, giga_response_format, gigachat_tls
from .journal import event


class IncompleteResponse(ServiceError):
    pass


def token_counts(data, provider):
    if provider in ('openai', 'gigachat'):
        usage = data.get('usage')
        keys = ('prompt_tokens', 'completion_tokens')
    else:
        usage = data['result'].get('usage')
        keys = ('inputTextTokens', 'completionTokens')
    counts = {}
    if isinstance(usage, dict):
        for field, key in zip(('input_tokens', 'output_tokens'), keys):
            count = usage.get(key)
            if type(count) is int and count >= 0:
                counts[field] = count
            elif isinstance(count, str) and count.isascii() and count.isdecimal() and len(count) <= 12:
                counts[field] = int(count)
    return counts


class Provider:
    """One JSON operation used for tool decisions, answers, and independent verification."""
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client
        self.gigachat_auth = None
        self.gigachat_slot = asyncio.Lock()

    async def close(self):
        if self.client is not None:
            await self.client.aclose()

    async def json(self, system, context):
        if self.settings.provider == 'gigachat':
            async with self.gigachat_slot:  # GigaChat Ultra Freemium allows one active stream.
                return await self._json(system, context)
        return await self._json(system, context)

    async def _json(self, system, context):
        problem = self.settings.llm_problem()
        if problem:
            raise ServiceError(problem)
        text = json.dumps(context, ensure_ascii=False)
        if len(text) > 48000:
            raise ServiceError('Контекст слишком велик. Уточните вопрос.')
        if self.client is None:
            trust = gigachat_tls(self.settings.gigachat_ca_file) if self.settings.provider == 'gigachat' else True
            self.client = httpx.AsyncClient(timeout=30, trust_env=False, verify=trust)
        rejected_token = None
        if self.settings.provider == 'openai':
            url = 'https://api.openai.com/v1/chat/completions'
            headers = {'Authorization': 'Bearer ' + self.settings.openai_key}
            body = {'model': self.settings.openai_model, 'messages': [
                {'role': 'system', 'content': system}, {'role': 'user', 'content': text}],
                'response_format': {'type': 'json_object'}, 'max_completion_tokens': 3500}
        elif self.settings.provider == 'gigachat':
            if self.gigachat_auth is None:
                self.gigachat_auth = GigaChatAuth(self.client, self.settings)
            url = CHAT_URL
            headers = {'Accept': 'application/json'}
            body = {'model': self.settings.gigachat_model, 'messages': [
                {'role': 'system', 'content': system}, {'role': 'user', 'content': text}],
                'response_format': giga_response_format(context),
                'max_tokens': 3500}
        else:
            url = 'https://ai.api.cloud.yandex.net/foundationModels/v1/completion'
            headers = {'Authorization': 'Api-Key ' + self.settings.yandex_key,
                       'x-folder-id': self.settings.yandex_folder}
            body = {'modelUri': self.settings.yandex_model,
                    'completionOptions': {'stream': False, 'temperature': 0, 'maxTokens': '2500'},
                    'messages': [{'role': 'system', 'text': system}, {'role': 'user', 'text': text}],
                    'jsonObject': True}
        for attempt in range(2):
            if self.gigachat_auth is not None:
                token = await self.gigachat_auth.token(rejected=rejected_token)
                headers['Authorization'] = 'Bearer ' + token
            started = monotonic()
            log_fields = {'provider': self.settings.provider, 'stage': 'verify' if 'items' in context else 'decide',
                          'attempt': attempt + 1}
            try:
                response = await self.client.post(url, headers=headers, json=body)
            except httpx.RequestError:
                event('llm.request', **log_fields, outcome='network_error',
                      elapsed_ms=round((monotonic() - started) * 1000))
                if attempt == 0:
                    await asyncio.sleep(.4)
                    continue
                raise ServiceError('LLM: сеть недоступна или превышено время ожидания.') from None
            log_fields.update(http=response.status_code, elapsed_ms=round((monotonic() - started) * 1000))
            if response.status_code != 200:
                event('llm.request', **log_fields, outcome='http_error')
            if response.status_code == 401 and self.gigachat_auth is not None and attempt == 0:
                rejected_token = token
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                await asyncio.sleep(.4)
                continue
            if response.status_code != 200:
                # Never expose response bodies: they can echo credentials or requests.
                raise ServiceError(f'LLM {self.settings.provider}: HTTP {response.status_code}. Проверьте доступ, модель и квоты.')
            try:
                data = response.json()
                log_fields.update(token_counts(data, self.settings.provider))
                if self.settings.provider in ('openai', 'gigachat'):
                    choice = data['choices'][0]
                    if choice.get('finish_reason') == 'length':
                        event('llm.request', **log_fields, outcome='truncated')
                        raise IncompleteResponse('Ответ не поместился в лимит. Верни не более трёх коротких утверждений.')
                    value = choice['message']['content']
                else:
                    value = data['result']['alternatives'][0]['message']['text']
                result = json.loads(value)
                if not isinstance(result, dict):
                    raise ValueError()
                event('llm.request', **log_fields, outcome='ok')
                return result
            except (AttributeError, KeyError, IndexError, TypeError, ValueError):
                event('llm.request', **log_fields, outcome='invalid_response')
                raise ServiceError('LLM вернула непригодный JSON. Ответ не используется.') from None
