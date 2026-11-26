import json
import hashlib
import sqlite3
from pathlib import Path
import pytest
from mechanic.corpus import Corpus, terms, expand_faults
from mechanic.scope import query_scope_problem, passage_scope_problem
from mechanic.review import load_reviews
from mechanic.config import Settings
from mechanic.manuals import BY_ID
from dataclasses import replace


@pytest.fixture
def reviewed_corpus(tmp_path):
    path = tmp_path/'reviewed.sqlite'
    quote = 'Давление масла на прогретом двигателе должно быть не менее 100 кПа.'
    approved = {'numeric_passages': [{'text': quote, 'engines': ['ЯМЗ-236М2'], 'mode': 'operation'}]}
    with sqlite3.connect(path) as db:
        db.executescript('''PRAGMA user_version=2;
            CREATE TABLE chunks(id TEXT PRIMARY KEY,doc TEXT,page INTEGER,text TEXT,quality TEXT,meta TEXT);
            CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED,doc,tokens);''')
        rows = [
            ('good','236-re',19,quote,'verified_text',json.dumps(approved)),
            ('unreviewed','236-re',20,quote,'text','{}'),
            ('scan','236-rk',144,'Распознано 9999 кПа.','ocr_needs_review','{}'),
            ('imaginary_page','236-re',9999,quote,'text','{}'),
            ('foreign','236-re',21,'ЯМЗ-238\n'+quote,'text','{}'),
            ('bench','236-rk',267,'При стендовой обкатке проверить подачу масла.','text','{}'),
        ]
        db.executemany('INSERT INTO chunks VALUES(?,?,?,?,?,?)',rows)
        for cid,doc,_,text,_,_ in rows:
            db.execute('INSERT INTO search VALUES(?,?,?)',(cid,doc,' '.join(terms(text))))
    db.close()
    return Corpus(path),quote


def test_exact_numeric_passage_with_conditions(reviewed_corpus):
    corpus,quote=reviewed_corpus
    assert corpus.validate_evidence('good','ЯМЗ-236М2',quote,'Не менее 100 кПа','при эксплуатации')['page']==19
    with pytest.raises(ValueError,match='визуальную'):
        corpus.validate_evidence('good','ЯМЗ-236М2','не менее 100 кПа.','100 кПа','при эксплуатации')
    with pytest.raises(ValueError,match='визуальную'):
        corpus.validate_evidence('unreviewed','ЯМЗ-236М2',quote,'100 кПа','при эксплуатации')
    with pytest.raises(ValueError,match='Число'):
        corpus.validate_evidence('good','ЯМЗ-236М2',quote,'10 кПа','при эксплуатации')


def test_unreadable_table_and_imaginary_page_never_ground(reviewed_corpus):
    corpus,_=reviewed_corpus
    for cid in ['scan','imaginary_page']:
        with pytest.raises(ValueError): corpus.fragment(cid,'ЯМЗ-236М2')


def test_foreign_context_and_bench_not_used_for_operation(reviewed_corpus):
    corpus,quote=reviewed_corpus
    with pytest.raises(ValueError,match='модификации'):
        corpus.validate_evidence('foreign','ЯМЗ-236М2',quote,'Проверить давление масла.')
    with pytest.raises(ValueError,match='обкатки'):
        corpus.validate_evidence('bench','ЯМЗ-236М2','При стендовой обкатке проверить подачу масла.','Проверить подачу масла.','при эксплуатации')
    assert corpus.validate_evidence('bench','ЯМЗ-236М2','При стендовой обкатке проверить подачу масла.','Проверить подачу масла.','Как выполнить обкатку?')


@pytest.mark.parametrize('selected,requested',[
    ('ЯМЗ-236М2','ЯМЗ-238'),('ЯМЗ-536','ЯМЗ-5361'),
    ('ЯМЗ-236М2','ЯМЗ-236НЕ'),('ЯМЗ-536','ЯМЗ-536-10'),
    ('ЗМЗ-40904.10','ЗМЗ-40905.10'),('ЗМЗ-40905.10','ЗМЗ-40904.10')])
def test_modification_isolation(selected,requested):
    assert query_scope_problem(selected,'Норматив для '+requested)
    assert passage_scope_problem(selected,'Для '+requested+' выполнить проверку.','')
    assert query_scope_problem(selected,'Норматив для '+selected) is None


def test_warm_norm_not_transferred_to_cold(reviewed_corpus):
    corpus,quote=reviewed_corpus
    with pytest.raises(ValueError,match='холодный'):
        corpus.validate_evidence('good','ЯМЗ-236М2',quote,'100 кПа','Какое давление на холодном двигателе?')


def test_fault_codes_resolve_to_words():
    assert 'Клапанный механизм' in expand_faults('F01')
    assert 'Цепь ГРМ' in expand_faults('f14')
    assert 'F15' in expand_faults('F15')


def test_stale_schema_is_explicit(tmp_path):
    path=tmp_path/'old.sqlite'
    db=sqlite3.connect(path);db.close()
    with pytest.raises(ValueError,match='устарел'): Corpus(path).connection()


def test_missing_and_malformed_review_store(tmp_path):
    path=tmp_path/'review.json'
    assert load_reviews(path)=={}
    path.write_text('[]',encoding='utf-8')
    with pytest.raises(ValueError): load_reviews(path)


def test_unknown_topic_has_insufficient_basis(reviewed_corpus):
    corpus,_=reviewed_corpus
    question='Как обновить прошивку блока управления автопилотом автомобиля?'
    assert corpus.query_problem('ЯМЗ-236М2',question)
    assert corpus.search('ЯМЗ-236М2',question)==[]


def test_runtime_source_hash_change_blocks_reference(reviewed_corpus,tmp_path):
    corpus,_=reviewed_corpus
    folder=tmp_path/'manuals';folder.mkdir()
    path=folder/BY_ID['236-re'].filename;path.write_bytes(b'original source test fixture')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(corpus.path) as db:
        db.execute('CREATE TABLE documents(id TEXT PRIMARY KEY,sha256 TEXT)')
        db.execute('INSERT INTO documents VALUES(?,?)',('236-re',digest))
    db.close()
    guarded=Corpus(corpus.path,folder)
    assert guarded.fragment('good','ЯМЗ-236М2')['page']==19
    path.write_bytes(b'changed source test fixture')
    with pytest.raises(ValueError,match='изменился'): guarded.fragment('good','ЯМЗ-236М2')


def test_missing_ocr_languages_preserves_previous_index(tmp_path,monkeypatch):
    import mechanic.corpus as module
    import pymupdf
    path=tmp_path/'old.sqlite';path.write_bytes(b'previous corpus')
    monkeypatch.setattr(module,'MANUALS',())
    monkeypatch.setattr(pymupdf,'get_tessdata',lambda *_:str(tmp_path/'missing_languages'))
    with pytest.raises(ValueError,match='traineddata'):
        module.build(replace(Settings(),corpus=path),ocr=True)
    assert path.read_bytes()==b'previous corpus'


def test_reviewed_external_ocr_rebuilds_without_tesseract(tmp_path,monkeypatch):
    import mechanic.corpus as module
    import pymupdf
    from mechanic.review import record
    manual=replace(BY_ID['236-rk'],pages=1)
    folder=tmp_path/'manuals';folder.mkdir()
    document=pymupdf.open();document.new_page()
    document.save(folder/manual.filename);document.close()
    settings=replace(Settings(),manuals=folder,corpus=tmp_path/'corpus/manuals.sqlite')
    text='Синтетический текст после внешнего OCR: проверить условный узел.'
    text_file=tmp_path/'verified.txt';text_file.write_text(text,encoding='utf-8')
    record(settings,'236-rk',1,'verified_ocr','Synthetic external OCR fixture',text_file=text_file)
    monkeypatch.setattr(module,'MANUALS',(manual,))
    def no_tesseract(*args):
        raise AssertionError('A verified external OCR import must not need Tesseract')
    monkeypatch.setattr(pymupdf,'get_tessdata',no_tesseract)
    report=module.build(settings)
    assert report['pages']==1 and report['chunks']==1
    with sqlite3.connect(settings.corpus) as db:
        row=db.execute('SELECT text,quality FROM pages').fetchone()
        assert row==(text,'verified_ocr')
        assert json.loads(db.execute('SELECT meta FROM chunks').fetchone()[0])['numeric_passages']==[]
