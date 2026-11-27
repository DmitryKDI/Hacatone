"""Run the frozen RAG cases against GPT-6 Luna and persist only result metadata."""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from mechanic.app import create_app
from mechanic.catalog import Catalog
from mechanic.clients import ServiceError
from mechanic.config import LUNA_MODEL, ROOT, Settings
from mechanic.providers import Provider


class CorpusOnly:
    async def diagnose(self, *_):
        raise ServiceError('В контрольном наборе вызов диагностики не ожидается.')


def result_kind(response, body, matches):
    if body.get('status') == 'needs_clarification':
        return 'source_conflict'
    if matches:
        return 'expected_source'
    if body.get('claims'):
        return 'other_source'
    if response.status_code >= 400:
        error = body.get('error', '')
        if isinstance(error, str):
            if 'HTTP 429' in error:
                return 'provider_rate_limit'
            if 'Некорректное решение агента' in error:
                return 'invalid_decision'
            if 'Проверка источников вернула некорректный результат' in error:
                return 'invalid_verification'
        return 'declined'
    return 'no_sources'


async def check(args):
    settings = Settings.load()
    if problem := settings.llm_problem():
        raise SystemExit(problem)
    if not settings.corpus.is_file():
        raise SystemExit('Подготовьте локальный корпус и настройте CORPUS_PATH.')
    cases = json.loads((ROOT / 'tests/data/retrieval_cases.json').read_text(encoding='utf-8'))['cases']
    known = {case['id'] for case in cases}
    if args.case:
        if not set(args.case) <= known:
            raise SystemExit('Неизвестный идентификатор случая.')
        cases = [case for case in cases if case['id'] in args.case]

    provider = Provider(settings)
    app = create_app(settings=settings, provider=provider, services=CorpusOnly())
    fleet = Catalog(settings.vehicles).public()
    limit = asyncio.Semaphore(args.workers)

    async def run(case):
        async with limit, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                            base_url='http://mechanic') as client:
            car = next(item for item in fleet if item['engine_model'] == case['engine'])
            session = await client.get('/api/session')
            session.raise_for_status()
            client.headers['X-CSRF-Token'] = session.json()['csrf']
            selected = await client.post('/api/select', json={
                'vehicle_id': car['vehicle_id'], 'session_id': car['sessions'][0]['session_id'],
            })
            selected.raise_for_status()
            question = ' '.join(case.get('history', []) + [case['question']])
            start = monotonic()
            response = await client.post('/api/action', json={
                'revision': selected.json()['revision'], 'intent': 'chat', 'question': question,
            })
            body = response.json()
            matched = any(claim['doc'] == case.get('manual') and claim['pdf_page'] in case.get('pages', [])
                          for claim in body.get('claims', []))
            record = {
                'id': case['id'], 'http': response.status_code, 'action_id': response.headers.get('X-Action-Id'),
                'elapsed_seconds': round(monotonic() - start, 2), 'outcome': result_kind(response, body, matched),
                'expected_refusal': case.get('expected') == 'refusal',
            }
            print(record['id'], record['http'], record['outcome'], record['elapsed_seconds'], flush=True)
            return record

    try:
        records = await asyncio.gather(*(run(case) for case in cases))
    finally:
        await provider.close()
    positives = [item for item in records if not item['expected_refusal']]
    negatives = [item for item in records if item['expected_refusal']]
    summary = {
        'provider': 'openai',
        'model': LUNA_MODEL,
        'positive_with_expected_source': sum(item['outcome'] == 'expected_source' for item in positives),
        'positive_total': len(positives),
        'expected_refusals': sum(item['outcome'] == 'declined' and item['http'] == 503 for item in negatives),
        'negative_total': len(negatives),
        'cases': records,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Send questions and selected excerpts to the configured LLM.')
    parser.add_argument('--case', action='append', help='Run this case ID; may be repeated.')
    parser.add_argument('--workers', type=int, choices=(1, 2, 3), default=1)
    parser.add_argument('--report', type=Path, default=ROOT / 'dist/llm-dataset.json')
    arguments = parser.parse_args()
    if not arguments.live:
        parser.error('Pass --live to run the dataset through the OpenAI API.')
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger('uvicorn.error.mechanic').setLevel(logging.INFO)
    asyncio.run(check(arguments))
