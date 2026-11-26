"""Local integration: a fresh public checkout without PDFs skips these explicit checks."""
import json
import asyncio
import hashlib
import pytest
from mechanic.config import ROOT, Settings
from mechanic.corpus import Corpus
from mechanic.agent import Agent
from mechanic.evaluate import evaluate

pytestmark = pytest.mark.skipif(not Settings().corpus.exists(),reason='Нужен локально подготовленный корпус PDF')


def test_real_corpus_acceptance_thresholds():
    result=evaluate(Settings())
    assert result['positive_hits']>=15
    assert result['negative_refusals']==8
    assert result['search_acceptance_passed']
    assert not result['live_model_semantics_verified']


def test_ground_truth_case_array_is_frozen():
    data=json.loads((ROOT/'tests/data/retrieval_cases.json').read_text(encoding='utf-8'))
    digest=hashlib.sha256(json.dumps(data['cases'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert digest==data['cases_sha256']


def test_all_eight_refusals_before_model_or_ticket():
    class NoExternalCalls:
        async def json(self,*a): raise AssertionError('Unexpected LLM call')
        async def diagnose(self,*a): raise AssertionError('Unexpected ML call')
        async def ticket(self,*a): raise AssertionError('Unexpected TOiR call')
    agent=Agent(NoExternalCalls(),NoExternalCalls(),Corpus(Settings().corpus))
    cases=json.loads((ROOT/'tests/data/retrieval_cases.json').read_text(encoding='utf-8'))['cases']
    from mechanic.clients import ServiceError
    for case in cases:
        if case.get('expected')!='refusal':continue
        state={'pair':('fixture','fixture'),'history':[],'diagnosis':None}
        with pytest.raises(ServiceError):
            asyncio.run(agent.run(state,{'engine_model':case['engine']},case['question']))
        assert state['draft'] is None and state['answer'] is None
