"""Conservative document applicability checks; semantic verification still follows."""
import re

ENGINE = re.compile(r'(ЯМЗ|ЯМ3|ЗМЗ|ЗМ3|УМЗ|КАМАЗ)[\s–—-]*(\d{3,5}(?:[.\-]\d+)?(?:[А-ЯA-Z]{1,4}\d*)?)', re.I)
COMMON_HEADER = re.compile(r'Руководство\s+по\s+ремонту\s+двигателей\s+' + ENGINE.pattern +
                           r'(?:\s*,\s*' + ENGINE.pattern + r')+', re.I)


def engines_in(text):
    return [brand.upper().replace('3', 'З') + '-' + number.upper().replace('M', 'М')
            for brand, number in ENGINE.findall(text)]


def matches_engine(mentioned, selected):
    aliases = {selected, selected.split('.')[0]}
    if selected == 'ЯМЗ-236М2':
        aliases.add('ЯМЗ-236')
    return mentioned in aliases


def query_scope_problem(engine, query):
    if any(not matches_engine(m, engine) for m in engines_in(query)):
        return 'Запрошена другая модель или модификация двигателя. Выберите соответствующую машину.'
    return None


def operating_mode(text):
    text = text.lower()
    bench = bool(re.search(r'обкат|стендов|испытан', text))
    operation = bool(re.search(r'эксплуатац|в рейс|в движении', text))
    if bench and operation:
        return 'mixed'
    return 'bench' if bench else 'operation'


def unknown_modification(text):
    return bool(re.search(r'(?:модификаци[яию]|комплектаци[яию]|исполнени[ея])[^.!?]{0,60}'
                          r'(?:неизвестн|не\s+указан|не\s+установлен|не\s+определен)', text.lower()))


def restricted_variant_instruction(text):
    return bool(re.search(r'\bдля\s+(?:первой|второй|отдельной|данной|этой|конкретной|одной)\s+модификаци', text.lower()))


def passage_scope_problem(engine, quote, source, question=''):
    # A running title lists the whole manual's coverage, not the active variant.
    # Remove only that title for applicability checks, preserving the real quote.
    def without_common_header(text):
        header = COMMON_HEADER.search(text)
        if header and header.start() < 100 and any(matches_engine(m, engine) for m in engines_in(header[0])):
            return text[:header.start()] + text[header.end():]
        return text
    quote, source = without_common_header(quote), without_common_header(source)
    if unknown_modification(question) and restricted_variant_instruction(quote):
        return 'Применимость процедуры к неизвестной модификации не подтверждена.'
    if any(not matches_engine(m, engine) for m in engines_in(quote)):
        return 'Выдержка относится к другой модели/модификации.'
    # Use the closest explicit engine before the quotation, not a later paragraph.
    prefix = source.split(quote, 1)[0] if quote in source else ''
    previous = engines_in(prefix[-400:])
    if previous and not matches_engine(previous[-1], engine):
        return 'Предшествующее условие относится к другой модификации.'
    source_mode = operating_mode(quote)
    requested = operating_mode(question)
    if source_mode == 'bench' and requested != 'bench':
        return 'Норматив или процедура обкатки/испытаний не подтверждает режим эксплуатации.'
    cold_source = bool(re.search(r'холодн', quote.lower()))
    warm_source = bool(re.search(r'прогрет|горяч', quote.lower()))
    if re.search(r'холодн', question.lower()) and warm_source and not cold_source:
        return 'Условия для прогретого двигателя не подтверждают холодный режим.'
    if re.search(r'прогрет|горяч', question.lower()) and cold_source and not warm_source:
        return 'Условия для холодного двигателя не подтверждают прогретый режим.'
    return None
