import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    ml_url: str = 'http://127.0.0.1:8001'
    toir_url: str = 'http://127.0.0.1:8002'
    provider: str = 'openai'
    openai_key: str = field(default='', repr=False)
    openai_model: str = ''
    yandex_key: str = field(default='', repr=False)
    yandex_folder: str = ''
    yandex_model: str = ''
    manuals: Path = ROOT / 'документы'
    corpus: Path = ROOT / 'corpus/manuals.sqlite'
    tessdata: str = ''
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
            provider=values.get('LLM_PROVIDER', 'openai'),
            openai_key=values.get('OPENAI_API_KEY', ''), openai_model=values.get('OPENAI_MODEL', ''),
            yandex_key=values.get('YANDEX_API_KEY', ''), yandex_folder=values.get('YANDEX_FOLDER_ID', ''),
            yandex_model=values.get('YANDEX_MODEL', ''),
            manuals=path('MANUALS_DIR', 'документы'), corpus=path('CORPUS_PATH', 'corpus/manuals.sqlite'),
            tessdata=values.get('TESSDATA_PREFIX', ''))

    def llm_problem(self):
        if self.provider not in ('openai', 'yandex'):
            return 'LLM_PROVIDER должен быть openai или yandex.'
        if self.provider == 'openai' and not (self.openai_key and self.openai_model):
            return 'Настройте OPENAI_API_KEY и OPENAI_MODEL в локальном .env.'
        if self.provider == 'yandex' and not (self.yandex_key and self.yandex_folder and self.yandex_model):
            return 'Настройте YANDEX_API_KEY, YANDEX_FOLDER_ID и YANDEX_MODEL в локальном .env.'
        return None
