import os
import re
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
    gigachat_auth_key: str = field(default='', repr=False)
    gigachat_scope: str = 'GIGACHAT_API_PERS'
    gigachat_model: str = 'GigaChat-3-Ultra'
    gigachat_ca_file: str = field(default='', repr=False)
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
            provider=values.get('LLM_PROVIDER', 'openai'),
            openai_key=values.get('OPENAI_API_KEY', ''), openai_model=values.get('OPENAI_MODEL', ''),
            yandex_key=values.get('YANDEX_API_KEY', ''), yandex_folder=values.get('YANDEX_FOLDER_ID', ''),
            yandex_model=values.get('YANDEX_MODEL', ''),
            gigachat_auth_key=values.get('GIGACHAT_AUTH_KEY', ''),
            gigachat_scope=values.get('GIGACHAT_SCOPE', cls.gigachat_scope),
            gigachat_model=values.get('GIGACHAT_MODEL', cls.gigachat_model),
            gigachat_ca_file=str(path('GIGACHAT_CA_FILE', '')) if values.get('GIGACHAT_CA_FILE') else '',
            manuals=path('MANUALS_DIR', 'документы'), corpus=path('CORPUS_PATH', 'corpus/manuals.sqlite'),
            tessdata=values.get('TESSDATA_PREFIX', ''), manual_pages=values.get('MANUAL_PAGES', '1') == '1')

    def llm_problem(self):
        if self.provider not in ('openai', 'yandex', 'gigachat'):
            return 'LLM_PROVIDER должен быть openai, yandex или gigachat.'
        if self.provider == 'openai' and not (self.openai_key and self.openai_model):
            return 'Настройте OPENAI_API_KEY и OPENAI_MODEL в локальном .env.'
        if self.provider == 'yandex' and not (self.yandex_key and self.yandex_folder and self.yandex_model):
            return 'Настройте YANDEX_API_KEY, YANDEX_FOLDER_ID и YANDEX_MODEL в локальном .env.'
        if self.provider == 'gigachat':
            if not (self.gigachat_auth_key and self.gigachat_model):
                return 'Настройте GIGACHAT_AUTH_KEY и GIGACHAT_MODEL в локальном .env.'
            if not re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', self.gigachat_auth_key):
                return 'Укажите ключ GIGACHAT_AUTH_KEY в формате Base64 без префикса Basic.'
            if self.gigachat_scope not in ('GIGACHAT_API_PERS', 'GIGACHAT_API_B2B', 'GIGACHAT_API_CORP'):
                return 'Укажите допустимый GIGACHAT_SCOPE в локальном .env.'
        return None
