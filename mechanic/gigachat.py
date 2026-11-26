import asyncio
import ssl
from time import monotonic, time
from uuid import uuid4

import httpx

from .clients import ServiceError
from .journal import event

OAUTH_URL = 'https://ngw.devices.sberbank.ru:9443/api/v2/oauth'
CHAT_URL = 'https://api.giga.chat/v1/chat/completions'
SOURCE_FIELDS = {
    'fragment_id': {'type': 'string', 'description': 'ID одного из переданных исходных фрагментов.'},
    'passage': {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 2, 'maxItems': 2,
                'description': 'Два существующих номера: начало и конец непрерывного диапазона.'},
    'numeric_ref': {'type': 'integer', 'description': 'Индекс сверенной числовой выдержки, начиная с 1.'},
    'quote': {'type': 'string', 'description': 'Точная выдержка вместо passage или numeric_ref.'},
}
ONE_SOURCE = [{'required': [name]} for name in ('passage', 'numeric_ref', 'quote')]


def giga_response_format(context):
    schema = {'type': 'object', 'additionalProperties': True}
    source_fields = dict(SOURCE_FIELDS)
    fragment_ids = [fragment['id'] for fragment in context.get('fragments', [])]
    if fragment_ids:
        source_fields['fragment_id'] = {**SOURCE_FIELDS['fragment_id'], 'enum': fragment_ids}
    if 'items' in context:
        schema.update(
            properties={
                'verdicts': {'type': 'array', 'items': {
                    'type': 'object', 'properties': {
                        'id': {'type': 'integer'},
                        'status': {'type': 'string', 'enum': ['supported', 'unsupported', 'insufficient']},
                    }, 'required': ['id', 'status'],
                }},
                'comparison': {'type': 'string', 'enum': ['clear', 'conflict', 'insufficient']},
                'conflicts': {'type': 'array', 'items': {'type': 'object', 'properties': {
                    'kind': {'type': 'string', 'enum': ['contradiction', 'uncertain_applicability']},
                    'sources': {'type': 'array', 'minItems': 2, 'maxItems': 2, 'items': {
                        'type': 'object', 'properties': source_fields, 'required': ['fragment_id'],
                        'oneOf': ONE_SOURCE,
                    }},
                }, 'required': ['kind', 'sources']}},
            },
            required=['verdicts', 'comparison', 'conflicts'],
        )
    elif 'remaining_steps' in context:
        if not context.get('fragments'):
            schema.update(properties={
                'tool': {'type': 'string', 'enum': ['search']},
                'query': {'type': 'string'},
            }, required=['tool', 'query'], additionalProperties=False)
        else:
            tools = ['finish', 'create_ticket']
            if context['remaining_steps'] > 1:
                tools.append('search')
            claim_fields = {
                **source_fields, 'text': {'type': 'string'},
                'section': {'type': 'string',
                            'enum': ['observations', 'hypotheses', 'checks', 'repair', 'risks', 'limitations']},
            }
            claim = {'type': 'object', 'properties': claim_fields, 'required': ['fragment_id', 'text'],
                     'oneOf': ONE_SOURCE,
                     'additionalProperties': False}
            answer_fields = {
                'claims': {'type': 'array', 'items': claim},
                'spare_parts': {'type': 'array', 'items': claim},
                'operations': {'type': 'array', 'items': {
                    'type': 'object', 'properties': source_fields, 'required': ['fragment_id'],
                    'oneOf': ONE_SOURCE, 'additionalProperties': False,
                }},
            }
            answer_fields.update(
                sufficient={'type': 'boolean'},
                urgency={'type': 'string', 'enum': ['low', 'medium', 'high']},
                priority_basis={'type': ['object', 'null']},
            )
            schema.update(properties={
                'tool': {'type': 'string', 'enum': tools},
                'query': {'type': ['string', 'null']},
                'answer': {'type': ['object', 'null'], 'properties': answer_fields,
                           'required': ['sufficient', 'claims'], 'additionalProperties': False},
            }, required=['tool', 'answer'], additionalProperties=False)
    return {'type': 'json_schema', 'schema': schema}


def gigachat_tls(ca_file):
    try:
        context = ssl.create_default_context()
        if ca_file:
            context.load_verify_locations(cafile=ca_file)
        return context
    except (OSError, ValueError):
        raise ServiceError('GigaChat: не удалось загрузить доверенные сертификаты. Проверьте GIGACHAT_CA_FILE.') from None


class GigaChatAuth:
    def __init__(self, client, settings):
        self.client = client
        self.settings = settings
        self.lock = asyncio.Lock()
        self._token = ''
        self._refresh_at = 0

    async def token(self, rejected=None):
        async with self.lock:
            if self._token and self._token != rejected and monotonic() < self._refresh_at:
                return self._token
            if self._token == rejected:
                self._token = ''
            started = monotonic()
            try:
                response = await self.client.post(OAUTH_URL, headers={
                    'Authorization': 'Basic ' + self.settings.gigachat_auth_key,
                    'Accept': 'application/json',
                    'RqUID': str(uuid4()),
                }, data={'scope': self.settings.gigachat_scope})
            except httpx.RequestError:
                event('llm.auth', provider='gigachat', outcome='network_error',
                      elapsed_ms=round((monotonic() - started) * 1000))
                raise ServiceError('GigaChat OAuth недоступен. Проверьте сеть и доверенный сертификат.') from None
            elapsed_ms = round((monotonic() - started) * 1000)
            if response.status_code != 200:
                event('llm.auth', provider='gigachat', outcome='http_error', http=response.status_code,
                      elapsed_ms=elapsed_ms)
                raise ServiceError(f'GigaChat OAuth: HTTP {response.status_code}. Проверьте ключ и GIGACHAT_SCOPE.')
            try:
                payload = response.json()
                token, expires_at = payload['access_token'], payload['expires_at']
                if not isinstance(token, str) or not token or not token.isascii() or not token.isprintable():
                    raise ValueError
                if type(expires_at) is not int:
                    raise ValueError
                if expires_at >= 100_000_000_000:  # Some OAuth responses use Unix milliseconds.
                    expires_at /= 1000
                ttl = min(expires_at - time(), 1800)
                if ttl <= 0:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                event('llm.auth', provider='gigachat', outcome='invalid_response', http=200, elapsed_ms=elapsed_ms)
                raise ServiceError('GigaChat OAuth вернул непригодный токен.') from None
            self._token = token
            self._refresh_at = monotonic() + max(0, ttl - 30)
            event('llm.auth', provider='gigachat', outcome='ok', http=200, elapsed_ms=elapsed_ms)
            return token
