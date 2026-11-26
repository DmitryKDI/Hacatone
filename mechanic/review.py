"""Local review decisions are bound to source PDF hashes; never publish this store."""
import argparse
import hashlib
import json
from pathlib import Path
from .config import Settings
from .manuals import BY_ID

KINDS = {'illustration', 'cover', 'notes', 'scanned_table', 'scanned_text', 'verified_text', 'verified_ocr'}


def load_reviews(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('Некорректный локальный реестр проверки PDF.')
    return data


def record(settings, manual, page, kind, note='', text_file=None, numeric_file=None, rotation=0):
    if manual not in BY_ID or not 1 <= page <= BY_ID[manual].pages or kind not in KINDS:
        raise ValueError('Неизвестный документ, страница или вид проверки.')
    pdf = settings.manuals / BY_ID[manual].filename
    item = {'pdf_sha256': hashlib.sha256(pdf.read_bytes()).hexdigest(), 'kind': kind, 'note': note}
    if rotation not in {0,90,180,270}:
        raise ValueError('Поворот должен быть 0, 90, 180 или 270.')
    item['rotation'] = rotation
    if text_file:
        if kind != 'verified_ocr':
            raise ValueError('Проверенный текст OCR допускается только с kind=verified_ocr.')
        item['verified_text'] = Path(text_file).read_text(encoding='utf-8')
    if kind == 'verified_ocr' and not item.get('verified_text', '').strip():
        raise ValueError('Для verified_ocr требуется текст, сверенный с изображением.')
    if numeric_file:
        item['numeric_passages'] = json.loads(Path(numeric_file).read_text(encoding='utf-8'))
    store = settings.corpus.parent / 'review/decisions.json'
    store.parent.mkdir(parents=True, exist_ok=True)
    data = load_reviews(store)
    data[f'{manual}:{page}'] = item
    store.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Локальный допуск PDF после визуальной сверки; не автоматическая верификация.')
    parser.add_argument('--manual', choices=BY_ID, required=True)
    parser.add_argument('--page', type=int, required=True)
    parser.add_argument('--kind', choices=sorted(KINDS), required=True)
    parser.add_argument('--note', default='')
    parser.add_argument('--text-file', help='Сверенный с PNG текст OCR, локальный UTF-8 файл')
    parser.add_argument('--numeric-file', help='Локальный JSON со сверенными полными выдержками и engines/mode')
    parser.add_argument('--rotation', type=int, choices=[0,90,180,270], default=0)
    args = parser.parse_args()
    try:
        record(Settings.load(), args.manual, args.page, args.kind, args.note, args.text_file, args.numeric_file, args.rotation)
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')
