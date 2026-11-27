"""Local-only PDF corpus. No extracted text belongs in the public repository."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from functools import lru_cache
from contextlib import closing
import snowballstemmer
from .manuals import MANUALS, BY_ID
from .config import Settings
from .review import load_reviews
from .scope import ENGINE, query_scope_problem, passage_scope_problem, operating_mode

STEMMER = snowballstemmer.stemmer('russian')
STOP = set('и в во на по для с со из как какой какая какие это при ли можно нужно мне мы он она или о об к до не чем что почему'.split())


@lru_cache(maxsize=40000)
def stem(word):
    if word in {'ремень', 'ремня', 'ремню', 'ремнем', 'ремне', 'ремни', 'ремней', 'ремням', 'ремнями', 'ремнях'}:
        return 'ремн'
    return STEMMER.stemWord(word)


def terms(text):
    return list(dict.fromkeys(stem(w) for w in re.findall(r'[а-яa-z0-9]+', text.lower().replace('ё', 'е'))
                             if len(w) > 1 and w not in STOP))


def normalize(text):
    return ' '.join(text.split())


def canonical_number(value):
    value = value.replace(',', '.').replace('−', '-')
    sign = '-' if value.startswith('-') else ''
    whole, _, decimal = value.lstrip('+-').partition('.')
    whole = whole.lstrip('0') or '0'
    decimal = decimal.rstrip('0')
    return (sign if whole != '0' or decimal else '') + whole + ('.' + decimal if decimal else '')


def number_tokens(text):
    text = ENGINE.sub('', text)
    return {canonical_number(n) for n in re.findall(r'\d+(?:[.,]\d+)?', text)}


REFERENCE = re.compile(r'№\s*\d+(?:[.,]\d+)?\b')


def norm_number_tokens(text):
    text = REFERENCE.sub('', text)
    text = re.sub(r'^\s*\d{1,2}[.)]\s+', '', text)
    return number_tokens(text)


_NUMBER = r'[+\-−]?\d+(?:[.,]\d+)?'
_INTERVAL = rf'(?P<first>{_NUMBER})(?:\s*(?:[–—-]|…|\.\.\.|до)\s*(?P<last>{_NUMBER}))?'
_UNIT_SYMBOL = r'(?:МПа|кПа|Па|бар|атм|кгс|кг|мг|г|мкм|мм|см|км|м|мл|л|мин|об|час|ч|сек|с|кВт|Вт|мВ|В|мА|А|Ом|Гц|Н|lb|psi|rpm|in|ft|MPa|kPa|Pa|bar|mm|cm|kg|N)'
_UNIT_PART = rf'{_UNIT_SYMBOL}(?:[¹²³2-3]|[⁻–—−-][¹1])?(?!\w)'
_UNIT = rf'(?:[°º]\s*[CС]|%|{_UNIT_PART}(?:\s*[/·⋅*]\s*{_UNIT_PART})*)'
_QUANTITY = re.compile(rf'(?<![\w.,+−-]){_INTERVAL}\s*(?P<unit>{_UNIT})')
_RANGE = re.compile(rf'(?<![\w.,+−-])({_NUMBER})\s*(?:[–—-]|…|\.\.\.|до)\s*({_NUMBER})')
_WRENCH_SIZE = re.compile(rf'\b[Ss]\s*=\s*({_NUMBER})')


def numeric_evidence_problem(statement, quote):
    """Keep numbers attached to units and preserve ordered range endpoints.

    No unit conversion is inferred. Meaning and omitted conditions still require
    the independent verifier; a literal checked quotation always passes here.
    """
    def quantities(text):
        values = {(canonical_number(n), 'S=') for n in _WRENCH_SIZE.findall(text)}
        for match in _QUANTITY.finditer(REFERENCE.sub('', ENGINE.sub('', text))):
            unit = re.sub(r'\s+', '', match['unit']).translate(str.maketrans({'¹': '1', '²': '2', '³': '3', '⁻': '-', '⁺': '+', '−': '-', '–': '-', '—': '-', 'º': '°'}))
            if unit in {'°С', '°C'}:
                unit = '°C'
            values.add((canonical_number(match['first']), unit))
            if match['last'] is not None:
                values.add((canonical_number(match['last']), unit))
        return values

    if not quantities(statement) <= quantities(quote):
        return 'Связь числа и единицы измерения не подтверждена выдержкой.'
    ranges = lambda text: {tuple(canonical_number(n) for n in pair) for pair in _RANGE.findall(text)}
    if not ranges(statement) <= ranges(quote):
        return 'Порядок или границы числового диапазона не подтверждены выдержкой.'
    return None


def expand_faults(query):
    from .config import ROOT
    path = ROOT / 'neurodvs_hackathon_mock_2026-11-16/data/fault_types.json'
    names = {f['code']: f['title'] for f in json.loads(path.read_text(encoding='utf-8'))}
    return re.sub(r'\bF\d{2}\b', lambda m: names.get(m[0].upper(), m[0]), query, flags=re.I)


def build(settings, ocr=False):
    import pymupdf
    missing = [m.filename for m in MANUALS if not (settings.manuals / m.filename).is_file()]
    if missing:
        raise ValueError('Получите шесть PDF от организатора в MANUALS_DIR. Отсутствуют: ' + ', '.join(missing))
    tessdata = None
    if ocr:
        try:
            tessdata = pymupdf.get_tessdata(settings.tessdata or None)
        except RuntimeError:
            raise ValueError('Для OCR задайте TESSDATA_PREFIX с rus.traineddata и eng.traineddata.') from None
        if not all((Path(tessdata)/(lang+'.traineddata')).is_file() for lang in ('rus','eng')):
            raise ValueError('В TESSDATA_PREFIX отсутствуют rus.traineddata или eng.traineddata.')
    settings.corpus.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement preserves the last usable corpus when extraction fails.
    fd, name = tempfile.mkstemp(dir=settings.corpus.parent, suffix='.sqlite')
    os.close(fd)
    temporary = Path(name)
    report = {'documents': [], 'pages': 0, 'chunks': 0, 'review_pages': []}
    reviews = load_reviews(settings.corpus.parent / 'review/decisions.json')
    try:
        with sqlite3.connect(temporary) as db:
            db.executescript('''
                PRAGMA user_version=2;
                CREATE TABLE documents(id TEXT PRIMARY KEY,title TEXT,sha256 TEXT,pages INTEGER);
                CREATE TABLE pages(doc TEXT,page INTEGER,text TEXT,quality TEXT,PRIMARY KEY(doc,page));
                CREATE TABLE chunks(id TEXT PRIMARY KEY,doc TEXT,page INTEGER,text TEXT,quality TEXT,meta TEXT NOT NULL);
                CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED,doc,tokens);
            ''')
            for m in MANUALS:
                path = settings.manuals / m.filename
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                with pymupdf.open(path) as pdf:
                    if len(pdf) != m.pages:
                        raise ValueError(f'{m.id}: число страниц изменилось; проверьте редакцию PDF.')
                    db.execute('INSERT INTO documents VALUES(?,?,?,?)', (m.id, m.title, digest, len(pdf)))
                    for i, page in enumerate(pdf, 1):
                        text = page.get_text(sort=True)
                        quality = 'text'
                        reviewed = reviews.get(f'{m.id}:{i}')
                        if reviewed and reviewed.get('pdf_sha256') != digest:
                            raise ValueError(f'{m.id}:{i}: PDF изменился после визуальной проверки.')
                        candidate = len(text.strip()) < 80 or len(page.get_text().strip()) < 80
                        if candidate:
                            quality = 'needs_review'
                            if ocr and (not reviewed or reviewed.get('kind') in {'scanned_table', 'scanned_text'}):
                                try:
                                    rotation = page.rotation
                                    page.set_rotation((rotation + (reviewed or {}).get('rotation', 0)) % 360)
                                    tp = page.get_textpage_ocr(language='rus+eng', full=True, dpi=200,
                                                             tessdata=tessdata)
                                    text = page.get_text(textpage=tp, sort=True)
                                    quality = 'ocr_needs_review'
                                except Exception:
                                    quality = 'ocr_unavailable'
                                finally:
                                    page.set_rotation(rotation)
                        if reviewed:
                            kind = reviewed['kind']
                            if kind in {'illustration', 'cover', 'notes'} or (not ocr and kind in {'scanned_table','scanned_text'}):
                                quality = kind
                            elif kind == 'verified_ocr':
                                text, quality = reviewed['verified_text'], 'verified_ocr'
                            elif kind == 'verified_text':
                                quality = 'verified_text'
                        if candidate:
                            report['review_pages'].append({'manual': m.id, 'pdf_page': i, 'quality': quality,
                                                         'reviewed': bool(reviewed)})
                        numeric_passages = reviewed.get('numeric_passages', []) if reviewed else []
                        for passage in numeric_passages:
                            if (not isinstance(passage, dict) or not passage.get('text', '').strip() or
                                    normalize(passage['text']) not in normalize(text) or
                                    not set(passage.get('engines', [])) <= set(m.engines) or
                                    not passage.get('engines') or passage.get('mode') not in {'bench', 'operation'}):
                                raise ValueError(f'{m.id}:{i}: норматив не соответствует тексту или применимости.')
                        # Unreviewed OCR/sparse pages remain auditable, but never ground an answer.
                        db.execute('INSERT INTO pages VALUES(?,?,?,?)', (m.id, i, text, quality))
                        if quality in {'text', 'verified_text', 'verified_ocr'}:
                            parts = [text[max(0, start-250):start+1850] for start in range(0, len(text), 1600)]
                            for passage in numeric_passages:
                                if not any(normalize(passage['text']) in normalize(part) for part in parts):
                                    parts.append(passage['text'])
                            for j, chunk in enumerate(parts):
                                cid = f'{m.id}:{i}:{j}:{digest[:12]}'
                                meta = json.dumps({'numeric_passages': numeric_passages, 'reviewed': bool(reviewed)}, ensure_ascii=False)
                                db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?)', (cid, m.id, i, chunk, quality, meta))
                                db.execute('INSERT INTO search VALUES(?,?,?)', (cid, m.id, ' '.join(terms(chunk))))
                                report['chunks'] += 1
                        report['pages'] += 1
                    report['documents'].append({'id': m.id, 'pages': len(pdf), 'sha256': digest})
        db.close()
        os.replace(temporary, settings.corpus)
        settings.corpus.with_suffix('.report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return report
    finally:
        if 'db' in locals():
            db.close()
        temporary.unlink(missing_ok=True)


class Corpus:
    def __init__(self, path, manuals_dir=None):
        self.path = path
        self.manuals_dir = manuals_dir

    def assert_source(self, doc):
        if self.manuals_dir is None:
            return  # Isolated synthetic test corpora need no copyrighted source files.
        if doc not in BY_ID:
            raise ValueError('Неизвестный документ.')
        with closing(self.connection()) as db:
            row = db.execute('SELECT sha256 FROM documents WHERE id=?', (doc,)).fetchone()
        path = self.manuals_dir / BY_ID[doc].filename
        if not row or not path.is_file():
            raise ValueError('Исходный PDF отсутствует. Получите его от организатора.')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != row['sha256']:
            raise ValueError('PDF изменился после индексации. Перестройте корпус и повторите проверку страниц.')

    def connection(self):
        if not self.path.is_file():
            raise ValueError('Корпус не подготовлен. Получите PDF и выполните python -m mechanic.corpus.')
        db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)
        if db.execute('PRAGMA user_version').fetchone()[0] != 2:
            db.close()
            raise ValueError('Формат корпуса устарел. Перестройте: python -m mechanic.corpus.')
        db.row_factory = sqlite3.Row
        return db

    def query_problem(self, engine, query):
        problem = query_scope_problem(engine, query)
        if problem:
            return problem
        tokens = terms(ENGINE.sub('', expand_faults(query)))[:45]
        if not tokens:
            return 'Нет поисковых признаков. Уточните узел или процедуру.'
        # Multiple unseen technical words are not supported merely by generic matches such as "блок".
        docs = [m.id for m in MANUALS if engine in m.engines]
        if not docs:
            return 'Для выбранного двигателя нет зарегистрированных руководств.'
        unknown = []
        known_specific = []
        generic = set(terms('блок управление автомобиль двигатель система инструкция руководство обновление проверить проверка работа'))
        with closing(self.connection()) as db:
            for token in tokens:
                expression = 'doc : (' + ' OR '.join('"'+d+'"' for d in docs) + ') AND tokens : "' + token + '"'
                exists = db.execute('SELECT 1 FROM search WHERE search MATCH ? LIMIT 1', (expression,)).fetchone()
                if len(token) >= 5 and not exists:
                    unknown.append(token)
                elif exists and token not in generic and len(token) >= 4:
                    known_specific.append(token)
        if len(unknown) >= 2 and len(known_specific) < 2:
            return 'В доступном корпусе недостаточно подтверждения темы вопроса. Уточните запрос; неполнота поиска не доказывает отсутствия инструкции в PDF.'
        return None

    def search(self, engine, query, limit=5):
        if self.query_problem(engine, query):
            return []
        docs = [m.id for m in MANUALS if engine in m.engines]
        tokens = terms(ENGINE.sub('', expand_faults(query)))[:45]
        if not docs or not tokens:
            return []
        # Document constraints are part of the MATCH expression, before BM25 ranking.
        expression = 'doc : (' + ' OR '.join('"'+d+'"' for d in docs) + ') AND tokens : (' + ' OR '.join('"'+t+'"' for t in tokens) + ')'
        with closing(self.connection()) as db:
            rows = db.execute('''SELECT c.*,bm25(search) AS rank FROM search JOIN chunks c ON c.id=search.id
                WHERE search MATCH ? ORDER BY rank LIMIT ?''', (expression, max(30, limit * 4))).fetchall()
            candidates = [self._public(dict(r)) for r in rows]
            # Re-rank by lexical coverage and phrase proximity. No case IDs or expected pages are used.
            def score(row):
                body_terms = set(terms(row['text']))
                coverage = sum(t in body_terms for t in tokens) / len(tokens)
                return (-coverage, row['rank'])
            candidates.sort(key=score)
            return candidates[:limit]

    @staticmethod
    def _public(row):
        row['meta'] = json.loads(row.get('meta') or '{}')
        row['manual'] = BY_ID[row['doc']].title
        row['pdf_page'] = row['page']
        return row

    def fragment(self, fragment_id, engine):
        with closing(self.connection()) as db:
            row = db.execute('SELECT * FROM chunks WHERE id=?', (fragment_id,)).fetchone()
            if (not row or row['doc'] not in BY_ID or engine not in BY_ID[row['doc']].engines or
                    row['quality'] not in {'text', 'verified_text', 'verified_ocr'} or
                    not 1 <= row['page'] <= BY_ID[row['doc']].pages):
                raise ValueError('Источник не существует или неприменим к двигателю.')
            result = self._public(dict(row))
        self.assert_source(result['doc'])
        return result

    def validate_evidence(self, fragment_id, engine, quote, statement='', question=''):
        fragment = self.fragment(fragment_id, engine)
        statement_scope = query_scope_problem(engine, statement)
        if statement_scope:
            raise ValueError(statement_scope)
        if normalize(quote) not in normalize(fragment['text']):
            raise ValueError('Выдержка не найдена в источнике.')
        if not number_tokens(statement) <= number_tokens(quote):
            raise ValueError('Число в утверждении не подтверждено выдержкой.')
        numeric_problem = numeric_evidence_problem(statement, quote)
        if numeric_problem:
            raise ValueError(numeric_problem)
        problem = passage_scope_problem(engine, normalize(quote), normalize(fragment['text']), question)
        problem = problem or passage_scope_problem(engine, normalize(quote), normalize(fragment['text']), question + ' ' + statement)
        if problem:
            raise ValueError(problem)
        # Numeric technical assertions require a visually verified full passage with its conditions.
        if norm_number_tokens(statement):
            approved = fragment['meta'].get('numeric_passages', [])
            if not any(engine in p.get('engines', []) and p.get('mode') == operating_mode(question) and
                       normalize(p['text']) in normalize(quote) and
                       norm_number_tokens(statement) <= norm_number_tokens(p['text']) and
                       numeric_evidence_problem(statement, p['text']) is None for p in approved):
                raise ValueError('Числовой норматив не прошёл визуальную проверку с единицами и условиями.')
        return fragment


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Локальная подготовка шести руководств; индекс не публиковать.')
    parser.add_argument('--ocr', action='store_true', help='OCR кандидатов; нужен Tesseract с rus+eng. Результат требует проверки.')
    args = parser.parse_args()
    try:
        result = build(Settings.load(), args.ocr)
        print(json.dumps({k: len(v) if isinstance(v, list) else v for k, v in result.items()}, ensure_ascii=False))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')
