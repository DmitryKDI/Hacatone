from typing import Literal
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ServiceError(Exception):
    pass


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    vehicle_id: str
    session_id: str
    status: Literal['normal', 'anomaly', 'check_failed']
    anomaly_type: str | None = Field(pattern=r'^F(0[1-9]|1[0-4])$')
    anomaly_title: str | None
    anomaly_score: float | None = Field(ge=0, le=1)
    confidence: float | None = Field(ge=0, le=1)
    signal_quality: Literal['good', 'warning', 'bad']
    description: str
    model_version: str

    @model_validator(mode='after')
    def consistent(self):
        if self.status == 'check_failed':
            if self.signal_quality != 'bad' or any(x is not None for x in
                    (self.anomaly_type, self.anomaly_title, self.anomaly_score, self.confidence)):
                raise ValueError('Некорректный ответ check_failed')
        elif self.status == 'normal':
            if self.anomaly_type is not None or self.anomaly_title is not None:
                raise ValueError('У нормы не должно быть типа неисправности')
        elif not self.anomaly_type or not self.anomaly_title or self.confidence is None or self.anomaly_score is None:
            raise ValueError('Неполные данные отклонения')
        return self

    @property
    def usable(self):
        return self.status != 'check_failed' and self.signal_quality != 'bad'

    @property
    def warning(self):
        return self.usable and (self.signal_quality == 'warning' or
                                (self.confidence is not None and self.confidence < .70))


class Services:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=15, trust_env=False)

    async def close(self):
        await self.client.aclose()

    async def diagnose(self, vehicle_id, session_id):
        try:
            r = await self.client.post(self.settings.ml_url + '/diagnose', json={
                'vehicle_id': vehicle_id, 'session_id': session_id})
            if r.status_code != 200:
                raise ServiceError(f'ML: ошибка HTTP {r.status_code}.')
            result = Diagnosis.model_validate(r.json())
            if (result.vehicle_id, result.session_id) != (vehicle_id, session_id):
                raise ValueError('Несовпадающая пара')
            return result
        except httpx.RequestError:
            raise ServiceError('ML недоступен или превысил время ожидания.') from None
        except (ValueError, TypeError):
            raise ServiceError('ML вернул ответ, не соответствующий выбранной проверке или контракту.') from None

    async def ticket(self, payload):
        # A POST can have committed even when its response is lost. Never retry.
        try:
            r = await self.client.post(self.settings.toir_url + '/tickets', json=payload)
        except httpx.RequestError:
            raise ServiceError('Результат отправки неизвестен. Автоматический повтор отключён: возможен дубль.') from None
        if r.status_code == 418:
            raise ServiceError('ТОиР отклонила прежнюю схему заявки (418).')
        if r.status_code != 201:
            raise ServiceError(f'ТОиР: ошибка HTTP {r.status_code}; создание не подтверждено.')
        try:
            data = r.json()
            if data.get('status') != 'created' or not isinstance(data.get('ticket_id'), str) or not data['ticket_id'].strip():
                raise ValueError()
            return data['ticket_id']
        except (ValueError, TypeError, AttributeError):
            raise ServiceError('Результат отправки неизвестен: ТОиР вернула некорректное подтверждение.') from None
