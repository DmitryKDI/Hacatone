from zipfile import ZipFile
from mechanic.package import package


def test_package_excludes_manuals_indexes_and_secrets(tmp_path):
    contents = {
        'README.md':'instructions', 'HANDOFF.md':'current state', 'requirements.txt':'dependencies',
        'tests/frontend.test.cjs':'// frontend regression checks',
        'scripts/ocr_windows.ps1':'# local OCR helper without extracted content',
        '.env.example':'OPENAI_API_KEY=', '.env':'secret',
        'mechanic/app.py':'# source', 'mechanic/cache.sqlite':'extracted text',
        'corpus/review/decisions.json':'extracted passages', 'corpus/raw.txt':'extracted text',
        'документы/manual.pdf':'manual', '.git/objects/blob':'manual in history',
        'tests/data/retrieval_cases.json':'questions only', 'docs/copied.pdf':'manual',
        'neurodvs_hackathon_mock_2026-11-16/.env.example':'MOCK_ML_URL=',
        'neurodvs_hackathon_mock_2026-11-16/.env':'secret',
        'neurodvs_hackathon_mock_2026-11-16/venv/Lib/site-packages/dependency.py':'# installed dependency',
    }
    for name,text in contents.items():
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf-8')
    output,count=package(tmp_path)
    with ZipFile(output) as archive:
        names=archive.namelist()
    assert count==9
    assert all(not name.endswith(('.pdf','.sqlite','.db','.faiss')) for name in names)
    assert '.env' not in names and not any(name.startswith(('corpus/','документы/','.git/')) for name in names)
    assert 'tests/data/retrieval_cases.json' in names
    assert 'HANDOFF.md' in names and 'tests/frontend.test.cjs' in names
    assert 'scripts/ocr_windows.ps1' in names
    assert not any('venv' in name.split('/') for name in names)
    assert 'neurodvs_hackathon_mock_2026-11-16/.env.example' in names
