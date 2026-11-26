import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4


logger = logging.getLogger('uvicorn.error.mechanic')
action_id = ContextVar('mechanic_action_id', default=None)


def event(name, **fields):
    record = {'at': datetime.now(UTC).isoformat(), 'event': name,
              'action_id': action_id.get(), **fields}
    logger.info('%s', json.dumps(record, ensure_ascii=True))


@contextmanager
def action_log(kind):
    identifier = uuid4().hex
    token = action_id.set(identifier)
    started = monotonic()
    try:
        event('action.start', kind=kind)
        yield identifier
    finally:
        event('action.end', elapsed_ms=round((monotonic() - started) * 1000))
        action_id.reset(token)
