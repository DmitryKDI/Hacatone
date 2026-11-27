"""Optional loopback launcher using the existing Hacatone application."""
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from .voicekit.prepare import DATA, MODEL
from .voicekit.recognition import SherpaEngine
from .voicekit.server import LOOPBACK_NAMES, create_voice_router


def with_voice(core, engine):
    voice = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    voice.include_router(create_voice_router(engine), prefix='/voice')

    @voice.get('/voice/status')
    async def voice_status(request: Request):
        if request.url.hostname not in LOOPBACK_NAMES:
            raise HTTPException(404)
        return {'ready': True}

    async def app(scope, receive, send):
        # Forward lifespan and every existing route to the original app.
        if scope['type'] != 'lifespan' and scope.get('path') in {'/voice/stream', '/voice/status'}:
            await voice(scope, receive, send)
        else:
            await core(scope, receive, send)

    return app


def create_voice_app():
    from .app import app as core

    model_dir = Path(os.environ.get('VOICE_MODEL_DIR') or DATA / 'models' / MODEL)
    return with_voice(core, SherpaEngine(model_dir))
