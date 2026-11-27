"""Synthetic conflicting instructions. These texts are not maintenance recommendations."""
import copy
import json
import sqlite3
import httpx
import pytest
from fastapi.testclient import TestClient
from mechanic.app import create_app
from mechanic.clients import Diagnosis
from mechanic.config import ROOT, Settings
from mechanic.corpus import Corpus, terms
from mechanic.providers import Provider

FIRST = 'Для выбранного двигателя проверить условный узел без нагрузки.'
SECOND = 'Для выбранного двигателя проверять условный узел только под нагрузкой.'
DATA = json.loads((ROOT/'neurodvs_hackathon_mock_2026-11-16/data/vehicles.json').read_text(encoding='utf-8'))
VEHICLE, SESSION = next((v,s) for v in DATA['vehicles'] for s in v['sessions']
                        if v['engine_model']=='ЯМЗ-236М2' and s['status']=='anomaly' and s['session_id']!='S-0005')


@pytest.fixture
def sources(tmp_path):
    path=tmp_path/'sources.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('''PRAGMA user_version=2;
            CREATE TABLE chunks(id TEXT,doc TEXT,page INTEGER,text TEXT,quality TEXT,meta TEXT);
            CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED,doc,tokens);''')
        for cid,doc,page,text in [('a','236-re',19,FIRST),('b','236-rk',20,SECOND)]:
            db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?)',(cid,doc,page,text,'text','{}'))
            db.execute('INSERT INTO search VALUES(?,?,?)',(cid,doc,' '.join(terms(text))))
    return Corpus(path)


def answer(quote=FIRST):
    evidence={'text':quote,'fragment_id':'a','quote':quote}
    return {'sufficient':True,'claims':[evidence],'operations':[evidence]}


def conflict_response(context,kind='contradiction'):
    fragments={f['id']:f for f in context['fragments']}
    return {'verdicts':[{'id':i['id'],'status':'supported'} for i in context['items']],
            'comparison':'conflict','conflicts':[{'kind':kind,'sources':[
                {'fragment_id':cid,'quote':fragments[cid]['text']} for cid in ('a','b')]}]}


class Services:
    def __init__(self):
        self.sent=[]

    async def diagnose(self,*pair):
        return Diagnosis.model_validate({k:SESSION[k] for k in Diagnosis.model_fields})

    async def ticket(self,payload):
        self.sent.append(payload)
        return 'TKT-SYNTHETIC'


class Verifier:
    def __init__(self,result=conflict_response,decisions=None):
        self.result=result
        self.decisions=decisions or [{'tool':'search','query':'условный узел'},
                                    {'tool':'create_ticket','answer':answer()}]
        self.calls=[]
        self.last_decision=None

    async def json(self,system,context):
        self.calls.append(context)
        if 'items' in context:
            return self.result(context)
        if self.decisions:
            self.last_decision = self.decisions.pop(0)
        return self.last_decision


def connect(sources,provider):
    services=Services()
    client=TestClient(create_app(services=services,provider=provider,corpus=sources))
    info=client.get('/api/session').json()
    client.headers['X-CSRF-Token']=info['csrf']
    pair={'vehicle_id':VEHICLE['vehicle_id'],'session_id':SESSION['session_id']}
    revision=client.post('/api/select',json=pair).json()['revision']
    return client,services,pair,revision


@pytest.mark.parametrize('kind',['contradiction','uncertain_applicability'])
@pytest.mark.parametrize('same_document',[False,True])
def test_both_sources_shown_and_confirmation_cannot_send(sources,kind,same_document):
    if same_document:
        with sqlite3.connect(sources.path) as db:
            db.execute("UPDATE chunks SET doc='236-re' WHERE id='b'")
    provider=Verifier(lambda c:conflict_response(c,kind))
    client,services,pair,revision=connect(sources,provider)
    response=client.post('/api/action',json={'revision':revision,'intent':'diagnose'})
    assert response.status_code==200
    result=response.json()
    assert result['status']=='needs_clarification' and not result['can_draft']
    assert result['claims']==[] and result['draft'] is None and 'preview_id' not in result
    assert 'производителя' in result['message'] and 'модификации' in result['message']
    quotations=result['conflicts'][0]['sources']
    assert [q['quote'] for q in quotations]==[FIRST,SECOND]
    assert [q['pdf_page'] for q in quotations]==[19,20]
    assert all(q['manual'] for q in quotations)
    assert {f['id'] for f in provider.calls[-1]['fragments']}=={'a','b'}
    assert client.post('/api/send',json={'revision':revision,'preview_id':'confirmed'}).status_code==409
    call_count=len(provider.calls)
    repeated=client.post('/api/action',json={'revision':revision,'intent':'draft','question':'Я подтверждаю первую инструкцию.'})
    assert repeated.json()['status']=='needs_clarification' and len(provider.calls)==call_count
    # Selecting the same pair again does not clear the unresolved source issue.
    revision=client.post('/api/select',json=pair).json()['revision']
    assert client.post('/api/action',json={'revision':revision,'intent':'draft'}).json()['status']=='needs_clarification'
    assert client.post('/api/send',json={'revision':revision,'preview_id':'confirmed'}).status_code==409
    other=next((v,s) for v in DATA['vehicles'] for s in v['sessions'] if v['vehicle_id']!=pair['vehicle_id'])
    client.post('/api/select',json={'vehicle_id':other[0]['vehicle_id'],'session_id':other[1]['session_id']})
    revision=client.post('/api/select',json=pair).json()['revision']
    assert client.post('/api/action',json={'revision':revision,'intent':'draft'}).json()['status']=='needs_clarification'
    assert len(provider.calls)==call_count
    assert not services.sent


def test_later_search_does_not_erase_an_earlier_source(sources):
    provider=Verifier(decisions=[{'tool':'search','query':'без'}, {'tool':'search','query':'только'},
                                 {'tool':'finish','answer':{'sufficient':False}}])
    client,services,_,revision=connect(sources,provider)
    result=client.post('/api/action',json={'revision':revision,'intent':'diagnose'}).json()
    assert result['status']=='needs_clarification'
    assert {f['id'] for f in provider.calls[-1]['fragments']}=={'a','b'}
    assert provider.calls[-1]['items']==[] and not services.sent


@pytest.mark.parametrize('mutation',['missing_comparison','unknown_status','duplicate_id','empty_conflict',
    'conflict_marked_clear','foreign_fragment','fabricated_quote','identical_quotes','comparison_failed'])
def test_invalid_verification_never_shows_invented_conflicts_or_recommendations(sources,mutation):
    def invalid(context):
        value=conflict_response(context)
        if mutation=='missing_comparison': value.pop('comparison')
        if mutation=='unknown_status': value['verdicts'][0]['status']='probably'
        if mutation=='duplicate_id': value['verdicts'][1]['id']=value['verdicts'][0]['id']
        if mutation=='empty_conflict': value['conflicts']=[]
        if mutation=='conflict_marked_clear': value['comparison']='clear'
        if mutation=='foreign_fragment': value['conflicts'][0]['sources'][1]['fragment_id']='not-retrieved'
        if mutation=='fabricated_quote': value['conflicts'][0]['sources'][1]['quote']='Вымышленная инструкция для теста.'
        if mutation=='identical_quotes': value['conflicts'][0]['sources'][1]=copy.deepcopy(value['conflicts'][0]['sources'][0])
        if mutation=='comparison_failed': value.update(comparison='insufficient',conflicts=[])
        return value
    client,services,_,revision=connect(sources,Verifier(invalid))
    response=client.post('/api/action',json={'revision':revision,'intent':'diagnose'})
    assert response.status_code==503 and 'error' in response.json()
    assert 'conflicts' not in response.json() and 'claims' not in response.json()
    assert client.post('/api/send',json={'revision':revision,'preview_id':'invented'}).status_code==409
    assert not services.sent


def test_disputed_numbers_are_quoted_without_authorizing_a_norm(sources):
    first='В тестовом режиме параметр условного узла составляет 10 кПа.'
    second='В тестовом режиме параметр условного узла составляет 20 кПа.'
    with sqlite3.connect(sources.path) as db:
        db.execute('UPDATE chunks SET text=? WHERE id=?',(first,'a'))
        db.execute('UPDATE chunks SET text=? WHERE id=?',(second,'b'))
    provider=Verifier(decisions=[{'tool':'search','query':'условный узел'},
                                 {'tool':'create_ticket','answer':answer(first)}])
    client,services,_,revision=connect(sources,provider)
    result=client.post('/api/action',json={'revision':revision,'intent':'diagnose'}).json()
    assert result['status']=='needs_clarification' and not result['claims'] and not result['draft']
    assert [s['quote'] for s in result['conflicts'][0]['sources']]==[first,second]
    assert not services.sent


def test_clear_general_procedure_can_still_be_previewed(sources):
    def clear(context):
        return {'verdicts':[{'id':i['id'],'status':'supported'} for i in context['items']],
                'comparison':'clear','conflicts':[]}
    client,services,_,revision=connect(sources,Verifier(clear))
    result=client.post('/api/action',json={'revision':revision,'intent':'diagnose'}).json()
    assert result['draft']['operations']==[FIRST] and result['can_draft']
    assert client.post('/api/send',json={'revision':revision,'preview_id':result['preview_id']}).status_code==200
    assert len(services.sent)==1


def test_stored_conflict_quotes_are_rechecked(sources):
    client,services,_,revision=connect(sources,Verifier())
    assert client.post('/api/action',json={'revision':revision,'intent':'diagnose'}).json()['conflicts']
    with sqlite3.connect(sources.path) as db:
        db.execute("UPDATE chunks SET text='Changed synthetic source' WHERE id='b'")
    response=client.post('/api/action',json={'revision':revision,'intent':'draft'})
    assert response.status_code==503 and 'conflicts' not in response.json()
    assert client.post('/api/send',json={'revision':revision,'preview_id':'invented'}).status_code==409
    assert not services.sent


def test_new_conflict_invalidates_a_previously_valid_preview(sources):
    checks=[]
    def result(context):
        checks.append(context)
        if len(checks)==1:
            return {'verdicts':[{'id':i['id'],'status':'supported'} for i in context['items']],
                    'comparison':'clear','conflicts':[]}
        return conflict_response(context)
    provider=Verifier(result,decisions=[{'tool':'search','query':'без'}, {'tool':'create_ticket','answer':answer()},
        {'tool':'search','query':'условный узел'}, {'tool':'finish','answer':{'sufficient':False}}])
    client,services,_,revision=connect(sources,provider)
    first=client.post('/api/action',json={'revision':revision,'intent':'diagnose'}).json()
    assert first['preview_id'] and first['draft']
    second=client.post('/api/action',json={'revision':revision,'intent':'chat','question':'Как проверить условный узел?'}).json()
    assert second['status']=='needs_clarification'
    assert client.post('/api/send',json={'revision':revision,'preview_id':first['preview_id']}).status_code==409
    assert not services.sent


def test_different_explicit_conditions_can_remain_clear(sources):
    cold='На холодном двигателе проверить условный узел без нагрузки.'
    warm='На прогретом двигателе проверить условный узел под нагрузкой.'
    with sqlite3.connect(sources.path) as db:
        db.execute('UPDATE chunks SET text=? WHERE id=?',(cold,'a'))
        db.execute('UPDATE chunks SET text=? WHERE id=?',(warm,'b'))
    def clear(context):
        assert {f['text'] for f in context['fragments']}=={cold,warm}
        return {'verdicts':[{'id':i['id'],'status':'supported'} for i in context['items']],
                'comparison':'clear','conflicts':[]}
    provider=Verifier(clear,decisions=[{'tool':'search','query':'условный узел'},
                                       {'tool':'finish','answer':answer(cold)}])
    client,services,_,revision=connect(sources,provider)
    result=client.post('/api/action',json={'revision':revision,'intent':'chat',
                       'question':'Как проверить условный узел на холодном двигателе?'}).json()
    assert 'conflicts' not in result and result['claims'][0]['quote']==cold
    assert not services.sent


@pytest.mark.parametrize('rejected',['claim','operation','priority_high'])
def test_any_unsupported_required_item_blocks_the_whole_recommendation(sources,rejected):
    structured=answer()
    structured.update(urgency='high',priority_basis=structured['claims'][0])
    def result(context):
        assert rejected in {i['kind'] for i in context['items']}
        return {'comparison':'clear','conflicts':[], 'verdicts':[
            {'id':i['id'],'status':'insufficient' if i['kind']==rejected else 'supported'} for i in context['items']]}
    provider=Verifier(result,decisions=[{'tool':'search','query':'условный узел'},
                                        {'tool':'create_ticket','answer':structured}])
    client,services,_,revision=connect(sources,provider)
    response=client.post('/api/action',json={'revision':revision,'intent':'diagnose'})
    assert response.status_code==503 and 'claims' not in response.json()
    assert client.post('/api/send',json={'revision':revision,'preview_id':'confirmed'}).status_code==409
    assert not services.sent


def test_semantically_unsupported_part_is_excluded(sources):
    structured=answer()
    structured['spare_parts']=[{'text':'Условная тестовая деталь','fragment_id':'a','quote':FIRST}]
    def result(context):
        return {'comparison':'clear','conflicts':[], 'verdicts':[
            {'id':i['id'],'status':'unsupported' if i['kind']=='part' else 'supported'} for i in context['items']]}
    provider=Verifier(result,decisions=[{'tool':'search','query':'условный узел'},
                                        {'tool':'create_ticket','answer':structured}])
    client,services,_,revision=connect(sources,provider)
    response=client.post('/api/action',json={'revision':revision,'intent':'diagnose'})
    assert response.status_code==200 and response.json()['draft']['spare_parts']==[]
    assert not services.sent


@pytest.mark.parametrize('vendor',['openai','yandex'])
def test_conflict_comparison_through_both_http_adapters(sources,vendor):
    scripted=Verifier()
    calls=[]
    def handler(request):
        body=json.loads(request.content)
        context=json.loads(body['messages'][-1]['content' if vendor=='openai' else 'text'])
        calls.append(context)
        value=conflict_response(context) if 'items' in context else scripted.decisions.pop(0)
        value=json.dumps(value,ensure_ascii=False)
        return httpx.Response(200,json=({'choices':[{'message':{'content':value}}]} if vendor=='openai'
            else {'result':{'alternatives':[{'message':{'text':value}}]}}))
    settings=Settings(provider=vendor,openai_key='test',openai_model='test',yandex_key='test',
                      yandex_folder='test',yandex_model='gpt://test/test/latest')
    http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client,services,_,revision=connect(sources,Provider(settings,http))
    with client:
        result=client.post('/api/action',json={'revision':revision,'intent':'diagnose'}).json()
        assert result['status']=='needs_clarification'
        assert len(calls)==3 and len(calls[-1]['fragments'])==2
        assert not services.sent
