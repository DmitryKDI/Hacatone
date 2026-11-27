import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LUNA_MODEL = 'gpt-6-luna'


@dataclass(frozen=True)
class Settings:
    ml_url: str = 'http://127.0.0.1:8001'
    toir_url: str = 'http://127.0.0.1:8002'
    openai_key: str = field(default='', repr=False)
    manuals: Path = ROOT / 'документы'
    corpus: Path = ROOT / 'corpus/manuals.sqlite'
    tessdata: str = ''
    manual_pages: bool = True
    vehicles: Path = ROOT / 'neurodvs_hackathon_mock_2026-11-16/data/vehicles.json'

    @classmethod
    def load(cls, env_file=ROOT / '.env'):
        values = {}
        if env_file.exists():
            for line in env_file.read_text(encoding='utf-8-sig').splitlines():
                key, sep, value = line.partition('=')
                if sep and not key.lstrip().startswith('#'):
                    values[key.strip()] = value.strip().strip('"\'')
        values.update(os.environ)
        def path(key, default):
            p = Path(values.get(key) or default)
            return p if p.is_absolute() else ROOT / p
        return cls(
            ml_url=values.get('MOCK_ML_URL', cls.ml_url).rstrip('/'),
            toir_url=values.get('MOCK_TOIR_URL', cls.toir_url).rstrip('/'),
            openai_key=values.get('OPENAI_API_KEY', '').strip(),
            manuals=path('MANUALS_DIR', 'документы'), corpus=path('CORPUS_PATH', 'corpus/manuals.sqlite'),
            tessdata=values.get('TESSDATA_PREFIX', ''), manual_pages=values.get('MANUAL_PAGES', '1') == '1')

    def llm_problem(self):
        if not self.openai_key:
            return 'Настройте OPENAI_API_KEY в окружении сервера или локальном .env.'
        return None
