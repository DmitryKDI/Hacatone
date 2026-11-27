"""The organizer's F7 decision for the documented early-injection scenario."""
from contextlib import closing
from datetime import UTC, datetime
import re
import sqlite3
from uuid import uuid4
from .clients import ServiceError


PAIR = ('M333YK47', 'S-0005')
MANUAL = '236-rk'
LABELS = {'mismatch': 'Метки не совпадают', 'match': 'Метки совпали'}
RULE = (
    'Отметки «метки не совпадают» достаточно. Измеренный угол вводить не нужно. '
    'Подготовьте заявку на регулировку по п. 5.12.1 РК и подтвердите её отдельно. '
    'Если метки совпали, сохраните результат без заявки на регулировку. '
    'Отклонение по топливоподаче остаётся: ранний впрыск был одной из возможных причин.'
)


def applies(diagnosis):
    return (diagnosis is not None and diagnosis.usable and diagnosis.status == 'anomaly'
            and (diagnosis.vehicle_id, diagnosis.session_id) == PAIR and diagnosis.anomaly_type == 'F08')


def asks_about_rule(question):
    return (re.search(r'метк|уг(?:ол|ла)|результат осмотра', question, re.I)
            and re.search(r'достат|нуж|треб|обяз|сохран|фикс|отметк|заяв|если|совпал', question, re.I))


def reply(question):
    first = re.split(r'[?!.]', question, maxsplit=1)[0].lower()
    if 'достаточно' in first and ('метк' in first or 'отметк' in first):
        return 'Да. ' + RULE
    if re.search(r'уг(?:ол|ла)', first) and re.search(r'нуж|треб|обяз', first):
        return 'Нет. ' + RULE
    if re.search(r'метки совпал', first):
        answer = ('Сохраните результат «метки совпали» без заявки на регулировку. '
                  'Отклонение по топливоподаче остаётся: ранний впрыск был одной из возможных причин.')
        if 'правильно ли' in first:
            return 'Да. ' + answer
        if re.search(r'(нуж|треб|надо).*?(заяв|регулиров)', first):
            return 'Нет. ' + answer
        return answer
    return RULE


def require_mismatch(record):
    if record is None:
        raise ServiceError('Сначала отметьте результат сверки меток. Измеренный угол вводить не нужно.')
    if record['result'] == 'match':
        raise ServiceError('Метки совпали. Заявка на регулировку не нужна. Отклонение по топливоподаче остаётся.')


def operation_applies(fragment, quote):
    return (fragment['doc'] == MANUAL and fragment['page'] == 257
            and re.search(r'^совместить метку Е\b.*меткой Д\b.*подачи топлива.*;$',
                          ' '.join(quote.split()), re.I) is not None)


class InspectionStore:
    def __init__(self, path):
        self.path = path

    def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS inspections (
                vehicle_id TEXT, session_id TEXT, result TEXT NOT NULL, recorded_at TEXT NOT NULL,
                version TEXT NOT NULL, PRIMARY KEY(vehicle_id, session_id))""")
        return db

    def get(self, pair):
        if not self.path.exists():
            return None
        with closing(self.open()) as db:
            row = db.execute(
                'SELECT result, recorded_at, version FROM inspections WHERE vehicle_id=? AND session_id=?',
                pair,
            ).fetchone()
        return {'result': row[0], 'recorded_at': row[1], 'version': row[2]} if row else None

    def save(self, pair, result):
        recorded_at = datetime.now(UTC).isoformat(timespec='seconds')
        version = uuid4().hex
        with closing(self.open()) as db, db:
            db.execute("""INSERT INTO inspections VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(vehicle_id, session_id) DO UPDATE SET result=excluded.result,
                recorded_at=excluded.recorded_at, version=excluded.version""", (*pair, result, recorded_at, version))
        return {'result': result, 'recorded_at': recorded_at, 'version': version}
