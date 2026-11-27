"""Prepare the verified speech model and Python dependencies."""
import argparse
import hashlib
from pathlib import Path
from shutil import copyfileobj
import subprocess
import sys
import tarfile
from tempfile import NamedTemporaryFile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
DATA = ROOT.parent.parent / 'corpus' / 'voice'
MODEL = 'sherpa-onnx-streaming-zipformer-small-ru-vosk-int8-2025-08-16'
ARCHIVE = f'{MODEL}.tar.bz2'
URL = f'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{ARCHIVE}'
SHA256 = '6ba68a01ff3c5445aaf2d61e9b97b026f1149dcc9049d11af3f44f55176341d8'
MODEL_FILES = ('tokens.txt', 'encoder.int8.onnx', 'decoder.onnx', 'joiner.int8.onnx', 'README.md')


def verify(archive):
    with archive.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if digest != SHA256:
        raise ValueError('Контрольная сумма модели не совпала. Используйте официальный архив из README.')


def download(archive):
    archive.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=archive.parent, delete=False) as spool:
        temp_path = Path(spool.name)
        try:
            with urlopen(URL, timeout=60) as response:
                copyfileobj(response, spool)
        except BaseException:
            spool.close()
            temp_path.unlink(missing_ok=True)
            raise
    try:
        verify(temp_path)
        temp_path.replace(archive)
    finally:
        temp_path.unlink(missing_ok=True)


def extract(archive, data_dir):
    verify(archive)
    target = data_dir / 'models' / MODEL
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'r:bz2') as source:
        for filename in MODEL_FILES:
            member = source.getmember(f'{MODEL}/{filename}')
            if not member.isfile():
                raise ValueError(f'В архиве повреждён файл модели: {filename}')
            with source.extractfile(member) as content, (target / filename).open('wb') as output:
                copyfileobj(content, output)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--download-only', action='store_true', help='Fetch model and platform wheels for transfer.')
    mode.add_argument('--offline', action='store_true', help='Install from local wheels and model archive.')
    parser.add_argument('--data-dir', type=Path, default=DATA)
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    archive = args.archive or args.data_dir / 'assets' / ARCHIVE
    if sys.version_info[:2] != (3, 12):
        parser.error('Используйте Python 3.12 x64.')
    try:
        if not archive.is_file():
            if args.offline:
                parser.error(f'Не найден локальный архив модели: {archive}')
            download(archive)
        verify(archive)
        install = [sys.executable, '-m', 'pip', '--disable-pip-version-check']
        wheels = args.data_dir / 'wheelhouse'
        if args.download_only:
            install += ['download', '--only-binary=:all:', '--dest', str(wheels)]
        elif args.offline:
            install += ['install', '--no-index', '--find-links', str(wheels)]
        else:
            install += ['install']
        install += ['-r', str(ROOT / 'requirements.txt')]
        subprocess.run(install, cwd=ROOT, check=True)
        if not args.download_only:
            print(f'Проверенная локальная модель: {extract(archive, args.data_dir)}')
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Не удалось подготовить модуль: {error}\n')


if __name__ == '__main__':
    main()
