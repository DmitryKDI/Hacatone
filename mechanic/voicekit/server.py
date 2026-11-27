"""FastAPI router for a single local microphone session."""
import asyncio
import json
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket
from starlette.concurrency import run_in_threadpool

from .recognition import VoiceSession

LOOPBACK_NAMES = {'localhost', '127.0.0.1', '::1'}
FRAME_MAX = 6400
CONTROL_MAX = 512
IDLE_TIMEOUT = 180


def local_origin(websocket):
    origin = websocket.headers.get('origin', '')
    parsed = urlsplit(origin)
    scheme = 'https' if websocket.url.scheme == 'wss' else 'http'
    return parsed.scheme == scheme and parsed.hostname in LOOPBACK_NAMES and parsed.netloc == websocket.url.netloc


def create_voice_router(engine):
    router = APIRouter()
    active = asyncio.Lock()

    @router.websocket('/stream')
    async def voice(websocket: WebSocket):
        try:
            allowed = local_origin(websocket)
        except ValueError:
            allowed = False
        if not allowed:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        if active.locked():
            await websocket.send_json({'type': 'error', 'message': 'Микрофон уже используется другой вкладкой.'})
            await websocket.close(code=1013)
            return
        async with active:
            session = VoiceSession(engine)
            while True:
                try:
                    message = await asyncio.wait_for(websocket.receive(), IDLE_TIMEOUT)
                except TimeoutError:
                    await websocket.close(code=1000)
                    return
                if message['type'] == 'websocket.disconnect':
                    return
                pcm = message.get('bytes')
                if pcm is not None:
                    if not 0 < len(pcm) <= FRAME_MAX or len(pcm) % 2:
                        await websocket.close(code=1009)
                        return
                    replies = await run_in_threadpool(session.feed, pcm)
                else:
                    raw = message.get('text') or ''
                    try:
                        control = json.loads(raw) if len(raw) <= CONTROL_MAX else None
                        if not isinstance(control, dict):
                            raise ValueError('Некорректная команда микрофона.')
                        replies = await run_in_threadpool(session.control, control)
                    except json.JSONDecodeError:
                        replies = [{'type': 'error', 'message': 'Некорректная команда микрофона.'}]
                    except ValueError as error:
                        replies = [{'type': 'error', 'message': str(error)}]
                for reply in replies:
                    await websocket.send_json(reply)

    return router
