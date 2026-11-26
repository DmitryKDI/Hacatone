"""Application checks. Synthetic evidence here is never a repair recommendation."""
import asyncio
import copy
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from mechanic.config import Settings, ROOT
from mechanic.catalog import Catalog
from mechanic.clients import Diagnosis, Services, ServiceError
from mechanic.corpus import Corpus, terms
from mechanic.grounding import Answer, ground
from mechanic.providers import Provider
from mechanic.app import create_app
from mechanic.agent import verdict_summary

DATA = json.loads((ROOT/'neurodvs_hackathon_mock_2026-11-16/data/vehicles.json').read_text(encoding='utf-8'))
VEHICLE = DATA['vehicles'][0]
ANOMALY = next((v, s) for v in DATA['vehicles'] for s in v['sessions'] if s['status'] == 'anomaly')
QUOTE = 'Проверить натяжение ремня.'


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def corpus(tmp_path):
    path = tmp_path/'test.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE chunks(id TEXT,doc TEXT,page INTEGER,text TEXT,quality TEXT);
            PRAGMA user_version=2;
            CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED,doc,tokens);''')
        for cid, doc, text in [('a', '236-re', QUOTE), ('b', '536-re', 'Проверить натяжение ремня другого двигателя.')]:
            db.execute('INSERT INTO chunks VALUES(?,?,?,?,?)', (cid,doc,19,text,'text'))
            db.execute('INSERT INTO search VALUES(?,?,?)', (cid,doc,' '.join(terms(text))))
    db.close()
    return Corpus(path)


def answer():
    e={'text':QUOTE,'fragment_id':'a','quote':QUOTE}
    return {'sufficient':True,'claims':[e], 'operations':[e], 'spare_parts':[], 'urgency':'medium'}


class ScriptedProvider:
    def __init__(self, decisions=()):
        self.decisions=list(decisions)
        self.calls=[]

    async def json(self, system, context):
        self.calls.append(context)
        if 'items' in context:
            return {'verdicts':[{'id':item['id'],'status':'supported'} for item in context['items']], 'comparison':'clear','conflicts':[]}
        return self.decisions.pop(0)


class FakeServices:
    def __init__(self, diagnosis=None, failure=False):
        self.result=diagnosis or Diagnosis.model_validate({k:v for k,v in ANOMALY[1].items() if k!='checked_at'})
        self.sent=[]
        self.failure=failure

    async def diagnose(self, vehicle_id, session_id):
        assert (vehicle_id,session_id)==(self.result.vehicle_id,self.result.session_id)
        return self.result

    async def ticket(self, payload):
        self.sent.append(copy.deepcopy(payload))
        if self.failure: raise ServiceError('Результат отправки неизвестен.')
        return 'TKT-TEST'


def test_all_195_results_and_catalog():
    catalog=Catalog(Settings().vehicles)
    assert len(catalog.public())==50
    for v in DATA['vehicles']:
        for s in v['sessions']:
            d=Diagnosis.model_validate({k:val for k,val in s.items() if k!='checked_at'})
            assert d.usable==(s['status']!='check_failed' and s['signal_quality']!='bad')
            assert catalog.pair(v['vehicle_id'],s['session_id'])[0]['engine_model']==v['engine_model']
    with pytest.raises(ValueError): catalog.pair(VEHICLE['vehicle_id'],DATA['vehicles'][1]['sessions'][0]['session_id'])


def test_warning_and_bad_precedence():
    d=Diagnosis.model_validate({k:v for k,v in ANOMALY[1].items() if k!='checked_at'})
    assert d.model_copy(update={'confidence':.6999}).warning
    assert not d.model_copy(update={'confidence':.70,'signal_quality':'good'}).warning
    assert d.model_copy(update={'confidence':.99,'signal_quality':'warning'}).warning
    bad=d.model_copy(update={'confidence':.99,'signal_quality':'bad'})
    assert not bad.usable and not bad.warning


def test_compact_verdict_preserves_uncertainty_and_actual_pair():
    d=Diagnosis.model_validate({k:v for k,v in ANOMALY[1].items() if k!='checked_at'})
    summary=verdict_summary(d.model_copy(update={'confidence':.6999}))
    assert summary['confidence']=='69.99%'
    assert summary['vehicle_id']==d.vehicle_id and summary['session_id']==d.session_id
    assert 'Не определён' in summary['risk']
    bad=verdict_summary(d.model_copy(update={'signal_quality':'bad','confidence':None}))
    assert bad['status']=='Непригодная запись' and bad['next_action']=='Повторить запись'
    assert bad['confidence']=='Нет данных'
    conflict=verdict_summary(d,conflict=True)
    assert 'противоречие' in conflict['risk'] and 'производителя' in conflict['next_action']


def test_risk_and_sections_only_come_from_verified_answer(corpus):
    data=answer();data['claims'][0]['section']='risks'
    provider=ScriptedProvider([{'tool':'search','query':'ремень'},{'tool':'finish','answer':data}])
    client,rev=connect(create_app(services=FakeServices(),provider=provider,corpus=corpus))
    response=client.post('/api/action',json={'intent':'diagnose','revision':rev})
    assert response.status_code==200
    assert response.json()['summary']['risk']==QUOTE
    assert response.json()['claims'][0]['section']=='risks'
    assert client.get('/favicon.ico').status_code==204


def test_search_filter_normalization_and_unknown_sources(corpus):
    found=corpus.search('ЯМЗ-236М2','Как проверяют натяжение ремней?')
    assert [f['id'] for f in found]==['a']
    assert corpus.search('UNKNOWN','ремень')==[]
    assert corpus.search('ЯМЗ-236М2','???')==[]
    for cid,engine in [('b','ЯМЗ-236М2'),('missing','ЯМЗ-236М2')]:
        with pytest.raises(ValueError): corpus.fragment(cid,engine)


@pytest.mark.parametrize('mutation', ['foreign','invented_quote','extra_operation','invented_number','irrelevant'])
def test_grounding_rejects_bad_links(corpus, mutation):
    a=answer()
    if mutation=='foreign': a['claims'][0]['fragment_id']='b'
    if mutation=='invented_quote': a['claims'][0]['quote']='Несуществующая выдержка.'
    if mutation=='extra_operation': a['operations'][0]['text']+=' Заменить двигатель.'
    if mutation=='invented_number': a['claims'][0]['text']='Натянуть с усилием 98765 Н.'
    class Verifier(ScriptedProvider):
        async def json(self,system,context):
            return {'verdicts':[{'id':i['id'],'status':'unsupported'} for i in context['items']], 'comparison':'clear','conflicts':[]}
    with pytest.raises(ServiceError):
        run(ground(Answer.model_validate(a),'ЯМЗ-236М2',corpus.search('ЯМЗ-236М2','ремень'),corpus,Verifier(),'вопрос',None))


@pytest.mark.parametrize('vendor',['openai','yandex'])
def test_both_provider_wire_contracts(vendor):
    calls=[]
    def handler(request):
        calls.append(request)
        if vendor=='openai': return httpx.Response(200,json={'choices':[{'message':{'content':'{"tool":"search"}'}}]})
        return httpx.Response(200,json={'result':{'alternatives':[{'message':{'text':'{"tool":"search"}'}}]}})
    async def scenario():
        s=Settings(provider=vendor,openai_key='SECRET',openai_model='chosen',yandex_key='SECRET',yandex_folder='folder',yandex_model='gpt://folder/chosen/latest')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            p=Provider(s,client)
            assert await p.json('Return JSON',{})=={'tool':'search'}
        assert 'SECRET' not in repr(s)
    run(scenario())
    assert len(calls)==1
    body=json.loads(calls[0].content)
    assert ('model' in body) if vendor=='openai' else ('modelUri' in body)


@pytest.mark.parametrize('vendor',['openai','yandex'])
@pytest.mark.parametrize('status',[401,403,429,503])
def test_provider_errors_redact_response(vendor,status):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status,text='SECRET TOKEN echo')
    async def scenario():
        s=Settings(provider=vendor,openai_key='SECRET',openai_model='model',yandex_key='SECRET',yandex_folder='folder',yandex_model='model')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ServiceError) as err: await Provider(s,client).json('JSON',{})
            assert 'SECRET' not in str(err.value)
    run(scenario())
    assert len(calls)==(2 if status in (429,503) else 1)


def test_missing_key_never_calls_network():
    async def scenario():
        def handler(request): raise AssertionError('unexpected network')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ServiceError,match='OPENAI_API_KEY'): await Provider(Settings(),client).json('JSON',{})
    run(scenario())


def test_ml_rejects_foreign_response_and_ticket_timeout_not_retried():
    calls=[]
    def handler(request):
        calls.append(request)
        if request.url.path=='/diagnose': return httpx.Response(200,json={k:v for k,v in ANOMALY[1].items() if k!='checked_at'})
        raise httpx.ReadTimeout('lost',request=request)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            services=Services(Settings(),client)
            with pytest.raises(ServiceError): await services.diagnose('OTHER','OTHER')
            with pytest.raises(ServiceError,match='неизвестен'): await services.ticket({})
    run(scenario())
    assert len(calls)==2


def connect(app):
    client=TestClient(app)
    info=client.get('/api/session').json()
    client.headers['X-CSRF-Token']=info['csrf']
    revision=client.post('/api/select',json={'vehicle_id':ANOMALY[0]['vehicle_id'],'session_id':ANOMALY[1]['session_id']}).json()['revision']
    return client,revision


def test_preview_confirmation_and_context_reset(corpus):
    decisions=[{'tool':'search','query':'ремень'}, {'tool':'create_ticket','answer':answer()}]
    services=FakeServices()
    client,rev=connect(create_app(services=services,provider=ScriptedProvider(decisions),corpus=corpus))
    result=client.post('/api/action',json={'intent':'diagnose','revision':rev}).json()
    assert result['draft']['vehicle_id']==ANOMALY[0]['vehicle_id']
    assert not services.sent
    token=result['preview_id']
    assert client.post('/api/send',json={'revision':rev,'preview_id':'bad'}).status_code==409
    assert client.post('/api/send',json={'revision':rev,'preview_id':token}).json()['ticket_id']=='TKT-TEST'
    assert services.sent[0]==result['draft']
    assert client.post('/api/send',json={'revision':rev,'preview_id':token}).status_code==409
    assert len(services.sent)==1
    client.post('/api/select',json={'vehicle_id':VEHICLE['vehicle_id'],'session_id':VEHICLE['sessions'][0]['session_id']})
    assert client.post('/api/send',json={'revision':rev,'preview_id':token}).status_code==409


def test_unknown_ticket_result_consumes_confirmation(corpus):
    services=FakeServices(failure=True)
    client,rev=connect(create_app(services=services,provider=ScriptedProvider([
        {'tool':'search','query':'ремень'},{'tool':'create_ticket','answer':answer()}]),corpus=corpus))
    result=client.post('/api/action',json={'intent':'diagnose','revision':rev}).json()
    body={'revision':rev,'preview_id':result['preview_id']}
    assert client.post('/api/send',json=body).status_code==502
    assert client.post('/api/send',json=body).status_code==409
    assert len(services.sent)==1


def test_client_cannot_override_anomaly_code_at_send(corpus):
    services=FakeServices()
    client,rev=connect(create_app(services=services,provider=ScriptedProvider([
        {'tool':'search','query':'ремень'},{'tool':'create_ticket','answer':answer()}]),corpus=corpus))
    preview=client.post('/api/action',json={'intent':'diagnose','revision':rev}).json()
    body={'revision':rev,'preview_id':preview['preview_id']}
    foreign='F14' if services.result.anomaly_type!='F14' else 'F01'
    assert client.post('/api/send',json={**body,'anomaly_type':foreign}).status_code==422
    assert not services.sent
    assert client.post('/api/send',json=body).status_code==200
    assert services.sent[0]['anomaly_type']==services.result.anomaly_type


@pytest.mark.parametrize('verdict',['unsupported','insufficient','malformed','timeout'])
def test_failed_semantic_verification_never_leaves_a_sendable_draft(corpus,verdict):
    class RejectingVerifier(ScriptedProvider):
        async def json(self,system,context):
            if 'items' not in context:
                return await super().json(system,context)
            if verdict=='timeout':
                raise ServiceError('LLM: превышено время ожидания проверки.')
            if verdict=='malformed':
                return {'verdicts':[]}
            return {'verdicts':[{'id':i['id'],'status':verdict} for i in context['items']], 'comparison':'clear','conflicts':[]}
    services=FakeServices()
    provider=RejectingVerifier([{'tool':'search','query':'ремень'}, {'tool':'create_ticket','answer':answer()}])
    client,rev=connect(create_app(services=services,provider=provider,corpus=corpus))
    result=client.post('/api/action',json={'intent':'diagnose','revision':rev})
    assert result.status_code==503 and 'error' in result.json()
    assert client.post('/api/send',json={'revision':rev,'preview_id':'invented'}).status_code==409
    assert not services.sent


def test_failed_recording_needs_no_llm_and_cannot_send(corpus):
    v,s=next((v,s) for v in DATA['vehicles'] for s in v['sessions'] if s['status']=='check_failed')
    provider=ScriptedProvider()
    client,rev=connect(create_app(services=FakeServices(Diagnosis.model_validate({k:x for k,x in s.items() if k!='checked_at'})),provider=provider,corpus=corpus))
    rev=client.post('/api/select',json={'vehicle_id':v['vehicle_id'],'session_id':s['session_id']}).json()['revision']
    result=client.post('/api/action',json={'intent':'diagnose','revision':rev})
    assert result.status_code==200 and 'Повторите запись' in result.json()['message']
    assert result.json()['draft'] is None and not provider.calls
    assert client.post('/api/send',json={'revision':rev,'preview_id':'invented'}).status_code==409


def test_document_question_uses_search_without_ml(corpus):
    class NoML(FakeServices):
        async def diagnose(self,*args): raise AssertionError('ML not needed')
    p=ScriptedProvider([{'tool':'search','query':'ремень'},{'tool':'finish','answer':answer()}])
    client,rev=connect(create_app(services=NoML(),provider=p,corpus=corpus))
    response=client.post('/api/action',json={'intent':'chat','question':'Как проверить ремень?','revision':rev})
    assert response.status_code==200
    assert [t['tool'] for t in response.json()['trace']]==['search','finish']


def test_session_separation_and_invalid_tool(corpus):
    p=ScriptedProvider([{'tool':'shell','query':'execute'}])
    app=create_app(services=FakeServices(),provider=p,corpus=corpus)
    client,rev=connect(app)
    assert client.get('/').status_code==200
    assert 'SECRET' not in client.get('/').text
    other=TestClient(app)
    assert other.post('/api/send',json={'revision':rev,'preview_id':'x'}).status_code==409
    assert client.post('/api/action',json={'intent':'chat','question':'test','revision':rev}).status_code==503
    client.headers['X-CSRF-Token']='bad'
    assert client.post('/api/action',json={'intent':'diagnose','revision':rev}).status_code==403


def test_switch_before_confirmation_never_sends(corpus):
    services=FakeServices()
    client,rev=connect(create_app(services=services,provider=ScriptedProvider([
        {'tool':'search','query':'ремень'},{'tool':'create_ticket','answer':answer()}]),corpus=corpus))
    preview=client.post('/api/action',json={'intent':'diagnose','revision':rev}).json()
    new=client.post('/api/select',json={'vehicle_id':VEHICLE['vehicle_id'],'session_id':VEHICLE['sessions'][0]['session_id']}).json()
    assert new['revision']!=rev
    assert client.post('/api/send',json={'revision':new['revision'],'preview_id':preview['preview_id']}).status_code==409
    assert client.post('/api/send',json={'revision':rev,'preview_id':preview['preview_id']}).status_code==409
    assert not services.sent


def test_bad_quality_overrides_anomaly_without_llm(corpus):
    services=FakeServices()
    services.result=services.result.model_copy(update={'signal_quality':'bad','confidence':.99})
    provider=ScriptedProvider()
    client,rev=connect(create_app(services=services,provider=provider,corpus=corpus))
    result=client.post('/api/action',json={'intent':'diagnose','revision':rev})
    assert result.status_code==200 and 'Повторите запись' in result.json()['message']
    assert not provider.calls and not services.sent


def test_agent_limit_prevents_unbounded_loop(corpus):
    p=ScriptedProvider([{'tool':'search','query':'ремень'} for _ in range(5)])
    client,rev=connect(create_app(services=FakeServices(),provider=p,corpus=corpus))
    result=client.post('/api/action',json={'intent':'chat','question':'Проверка','revision':rev})
    assert result.status_code==503 and 'лимита пяти шагов' in result.json()['error']
    assert len(p.calls)==5


def test_whitespace_evidence_rejected():
    a=answer();a['claims'][0]['quote']=' '*20
    with pytest.raises(ValueError): Answer.model_validate(a)


def test_unconfirmed_parts_removed(corpus):
    a=answer();a['spare_parts']=[{'text':'Деталь без источника','fragment_id':'missing','quote':'Выдуманная деталь'}]
    parsed=run(ground(Answer.model_validate(a),'ЯМЗ-236М2',corpus.search('ЯМЗ-236М2','ремень'),corpus,ScriptedProvider(),'проверка',None))
    assert parsed.spare_parts==[]


@pytest.mark.parametrize('vendor',['openai','yandex'])
def test_invalid_provider_json_is_not_a_result(vendor):
    def handler(request):
        if vendor=='openai': return httpx.Response(200,json={'choices':[{'message':{'content':'not JSON'}}]})
        return httpx.Response(200,json={'result':{'alternatives':[{'message':{'text':'not JSON'}}]}})
    async def scenario():
        settings=Settings(provider=vendor,openai_key='secret',openai_model='x',yandex_key='secret',yandex_folder='f',yandex_model='x')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ServiceError,match='JSON'): await Provider(settings,client).json('JSON',{})
    run(scenario())


@pytest.mark.parametrize('vendor',['openai','yandex'])
def test_agent_and_verifier_through_both_wire_adapters(corpus,vendor):
    decisions=[{'tool':'search','query':'ремень'},{'tool':'create_ticket','answer':answer()}]
    calls=[]
    def handler(request):
        body=json.loads(request.content)
        message=body['messages'][-1]
        context=json.loads(message['content' if vendor=='openai' else 'text'])
        calls.append(context)
        if 'items' in context:
            result={'verdicts':[{'id':x['id'],'status':'supported'} for x in context['items']], 'comparison':'clear','conflicts':[]}
        else:
            result=decisions.pop(0)
        value=json.dumps(result,ensure_ascii=False)
        data=({'choices':[{'message':{'content':value}}]} if vendor=='openai' else
              {'result':{'alternatives':[{'message':{'text':value}}]}})
        return httpx.Response(200,json=data)
    settings=Settings(provider=vendor,openai_key='secret',openai_model='x',yandex_key='secret',yandex_folder='f',yandex_model='gpt://f/x/latest')
    http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    services=FakeServices()
    client,rev=connect(create_app(settings,services=services,provider=Provider(settings,http),corpus=corpus))
    response=client.post('/api/action',json={'intent':'diagnose','revision':rev})
    assert response.status_code==200,response.json()
    assert len(calls)==3 and 'items' in calls[-1]
    assert not services.sent
    preview=response.json()
    assert client.post('/api/send',json={'revision':rev,'preview_id':preview['preview_id']}).status_code==200
    assert len(services.sent)==1
    run(http.aclose())
