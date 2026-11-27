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
    voice_model_dir: Path | None = None
    conversations_db: Path = ROOT / 'corpus/vehicle_conversations.sqlite'
    team_users_file: Path | None = None
    repair_log_file: Path | None = None
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
            tessdata=values.get('TESSDATA_PREFIX', ''), manual_pages=values.get('MANUAL_PAGES', '1') == '1',
            voice_model_dir=path('VOICE_MODEL_DIR', '') if values.get('VOICE_MODEL_DIR') else None,
            conversations_db=path('CONVERSATIONS_DB', 'corpus/vehicle_conversations.sqlite'),
            team_users_file=path('MECHANIC_USERS_FILE', '') if values.get('MECHANIC_USERS_FILE') else None,
            repair_log_file=path('REPAIR_LOG_FILE', '') if values.get('REPAIR_LOG_FILE') else None)


    def llm_problem(self):
        if not self.openai_key:
            return 'Настройте OPENAI_API_KEY в окружении сервера или локальном .env.'
        return None
