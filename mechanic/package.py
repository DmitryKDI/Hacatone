"""Create a reviewable source archive; never publish manuals, corpora, or Git history."""
import argparse
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from .config import ROOT

ROOT_FILES = {'README.md', 'HANDOFF.md', 'THIRD_PARTY.md', 'requirements.txt', '.env.example', '.gitignore'}
SOURCE_ROOTS = {'mechanic', 'tests', 'docs', 'openspec', 'neurodvs_hackathon_mock_2026-11-16', 'scripts'}
EXTENSIONS = {'.py', '.ps1', '.js', '.cjs', '.css', '.html', '.md', '.json', '.yaml', '.yml', '.txt'}
EXCLUDED = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache', 'corpus', 'документы', 'dist', 'node_modules'}


def source_files(root):
    root = root.resolve()
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if any(part in EXCLUDED for part in rel.parts) or not path.is_file():
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError('Ссылка выходит за границы проекта: ' + str(rel))
        if len(rel.parts) == 1:
            if rel.name in ROOT_FILES:
                yield path, rel
        elif rel.parts[0] in SOURCE_ROOTS and (path.suffix.lower() in EXTENSIONS or path.name == '.env.example'):
            if any(part.startswith('.') for part in rel.parts):
                if path.name not in {'.env.example', '.openspec.yaml', '.gitkeep'}:
                    continue
            yield path, rel


def package(root=ROOT, output=None):
    output = output or root/'dist/digital-mechanic-source.zip'
    output.parent.mkdir(parents=True, exist_ok=True)
    files = list(source_files(root))
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for path, rel in files:
            archive.write(path, rel.as_posix())
    return output, len(files)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Локальный архив исходников без PDF, индексов, ключей и истории Git.')
    parser.parse_args()
    path, count = package()
    print(f'{path}: {count} файлов; архив не опубликован.')
