"""Measure a source-only checkout with local PDFs and the existing Python environment.

Does not install dependencies, run OCR, call LLM, or start organizer services.
All generated files stay under ignored dist/. Only the owned app process is stopped.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mechanic.config import ROOT, Settings
from mechanic.package import package


def main():
    settings = Settings.load()
    archive, count = package()
    clean = Path(tempfile.mkdtemp(prefix='stage-9-clean-', dir=ROOT/'dist'))
    with ZipFile(archive) as zipped:
        for name in zipped.namelist():
            assert not {'.git', '.venv', 'venv', 'corpus', 'документы', 'node_modules'} & set(Path(name).parts)
            assert Path(name).suffix.lower() not in {'.pdf', '.sqlite', '.sqlite3', '.db', '.faiss'}
            assert Path(name).name != '.env'
            target = (clean/name).resolve()
            if not target.is_relative_to(clean.resolve()):
                raise ValueError('Archive path leaves the clean checkout')
        zipped.extractall(clean)
    assert not (clean/'.git').exists() and not (clean/'corpus').exists()
    assert not list(clean.rglob('*.pdf'))
    env = dict(os.environ, MANUALS_DIR=str(settings.manuals.resolve()),
               CORPUS_PATH=str(clean/'corpus/manuals.sqlite'), PYTHONIOENCODING='utf-8')
    started = time.perf_counter()
    result = subprocess.run([sys.executable, '-m', 'mechanic.corpus'], cwd=clean, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=180)
    cold_seconds = time.perf_counter()-started
    if result.returncode:
        raise RuntimeError('Fresh corpus build failed; inspect the local environment and manuals')
    corpus_report = json.loads((clean/'corpus/manuals.report.json').read_text(encoding='utf-8'))
    with socket.socket() as available:
        available.bind(('127.0.0.1', 0))
        port = available.getsockname()[1]
    import httpx
    started = time.perf_counter()
    log_path = clean/'app-start.log'
    with log_path.open('w', encoding='utf-8') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'mechanic.app:app',
                                    '--host', '127.0.0.1', '--port', str(port)],
                                   cwd=clean, env=env, stdout=log, stderr=log,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            with httpx.Client(timeout=2, trust_env=False) as client:
                while time.perf_counter()-started < 30:
                    if process.poll() is not None:
                        raise RuntimeError('Owned application exited during startup; see ' + str(log_path))
                    try:
                        response = client.get(f'http://127.0.0.1:{port}/api/session')
                        if response.status_code == 200:
                            break
                    except httpx.RequestError:
                        pass
                    time.sleep(.1)
                else:
                    raise RuntimeError('Application did not start within 30 seconds')
                startup_seconds = time.perf_counter()-started
                statuses = {path: client.get(f'http://127.0.0.1:{port}'+path).status_code
                            for path in ('/', '/static/app.js', '/api/catalog')}
                assert set(statuses.values()) == {200}
                assert response.json()['corpus_ready']
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    report = {'source_files': count, 'clean_checkout': str(clean),
              'python': sys.version.split()[0], 'dependencies_reused': True,
              'pdfs_supplied_separately': True, 'cold_index_without_ocr_seconds': round(cold_seconds, 3),
              'documents': len(corpus_report['documents']), 'pages': corpus_report['pages'],
              'prepared_app_start_seconds': round(startup_seconds, 3), 'http_statuses': statuses,
              'owned_application_stopped': process.poll() is not None,
              'organizer_services_started_or_restarted': False,
              'full_cold_ocr_setup_verified': False, 'live_llm_verified': False,
              'complete_five_minute_acceptance': False,
              'source_archive_sha256_at_check': hashlib.sha256(archive.read_bytes()).hexdigest()}
    (ROOT/'dist/stage-9-clean-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
