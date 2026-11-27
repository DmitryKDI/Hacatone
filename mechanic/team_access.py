"""Optional HTTP Basic access for a single repair team."""
from dataclasses import dataclass
from getpass import getpass
from hashlib import scrypt
from hmac import compare_digest
from pathlib import Path
from secrets import token_bytes
from tempfile import NamedTemporaryFile
import argparse
import json
import re

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPBasic

LOCAL_HOSTS = {'127.0.0.1', '::1', 'localhost'}
security = HTTPBasic(auto_error=False)


@dataclass(frozen=True)
class Actor:
    id: str
    name: str


def is_local(request):
    return (request.client is not None and request.client.host in LOCAL_HOSTS
            and request.url.hostname in LOCAL_HOSTS)


def derive(password, salt):
    return scrypt(password.encode('utf-8'), salt=salt, n=2**14, r=8, p=1, dklen=32)


class TeamAccess:
    def __init__(self, path):
        self.users = None
        if path is not None:
            entries = json.loads(Path(path).read_text(encoding='utf-8'))
            self.users = {name: tuple(bytes.fromhex(part) for part in value.split(':')) for name, value in entries.items()}
            if not self.users or any(len(record) != 2 or len(record[0]) != 16 or len(record[1]) != 32
                                     for record in self.users.values()):
                raise ValueError('Некорректный файл учётных записей механиков.')

    async def authenticate(self, request):
        if self.users is None:
            return None
        if request.url.scheme != 'https' and not is_local(request):
            raise HTTPException(403, 'Для доступа механиков требуется HTTPS.')
        credentials = await security(request)
        if credentials and len(credentials.password) <= 512:
            record = self.users.get(credentials.username)
            salt, expected = record or (bytes(16), bytes(32))
            actual = await run_in_threadpool(derive, credentials.password, salt)
            if compare_digest(actual, expected) and record is not None:
                return Actor(f'user:{credentials.username}', credentials.username)
        raise HTTPException(401, 'Войдите под учётной записью механика.',
                            headers={'WWW-Authenticate': 'Basic realm="Hacatone", charset="UTF-8"'})


def main():
    parser = argparse.ArgumentParser(description='Добавить учётную запись ремонтной команды.')
    parser.add_argument('username')
    parser.add_argument('--file', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,60}', args.username):
        parser.error('Логин должен содержать до 60 латинских букв, цифр, точек, дефисов или подчёркиваний.')
    users = json.loads(args.file.read_text(encoding='utf-8')) if args.file.exists() else {}
    if args.username in users:
        parser.error('Такая учётная запись уже существует.')
    password = getpass('Пароль: ')
    if not 12 <= len(password) <= 512 or not password.isascii() or getpass('Повторите пароль: ') != password:
        parser.error('Пароль должен содержать от 12 до 512 символов ASCII. Повтор должен совпасть.')
    salt = token_bytes(16)
    users[args.username] = f'{salt.hex()}:{derive(password, salt).hex()}'
    args.file.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=args.file.parent, encoding='utf-8', mode='w', delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(users, output, ensure_ascii=False, indent=2)
            output.close()
            temporary.replace(args.file)
        finally:
            temporary.unlink(missing_ok=True)



if __name__ == '__main__':
    main()
