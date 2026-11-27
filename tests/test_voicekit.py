from collections import deque

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from mechanic.app import create_app
from mechanic.voice_app import with_voice
from mechanic.voicekit.recognition import Reading, VoiceSession, validate_name


class ScriptedEngine:
    def __init__(self, *readings):
        self.readings = deque(readings)
        self.opened = 0
        self.final = ''

    def stream(self):
        self.opened += 1
        return self.opened

    def read(self, stream, pcm):
        return self.readings.popleft() if self.readings else Reading('', False)

    def finish(self, stream):
        return self.final


PCM = bytes(1600)
ORIGIN = 'http://127.0.0.1:8765'
STREAM = 'ws://127.0.0.1:8765/voice/stream'


def test_custom_name_and_exact_phrase_trigger():
    session = VoiceSession(ScriptedEngine(
        Reading('механик слушай', True),
        Reading('скажите саша слушай', True),
        Reading('саша слушай', False),
        Reading('Саша, слушай!', True),
    ))
    assert session.configure('Саша')[0]['phrase'] == 'Саша, слушай'
    assert [session.feed(PCM) for _ in range(3)] == [[], [], []]
    assert session.feed(PCM) == [{'type': 'state', 'state': 'recording', 'activated': True}]


def test_test_name_does_not_open_dictation():
    session = VoiceSession(ScriptedEngine(Reading('механик слушай', True)))
    assert session.control({'action': 'test'}) == [{'type': 'testing'}]
    assert session.feed(PCM) == [{'type': 'tested'}]
    assert session.state == 'waiting'


def test_command_is_not_appended_to_question():
    session = VoiceSession(ScriptedEngine(
        Reading('как проверить', False),
        Reading('как проверить уровень масла', True),
        Reading('готово', True),
    ))
    session.control({'action': 'start'})
    assert session.feed(PCM)[0]['text'] == 'как проверить'
    assert session.feed(PCM)[0]['text'] == 'как проверить уровень масла'
    assert session.feed(PCM)[0] == {'type': 'final', 'text': 'как проверить уровень масла'}


def test_ordinary_use_of_word_gotovo_remains_in_text():
    session = VoiceSession(ScriptedEngine(Reading('что означает готово', True)))
    session.control({'action': 'start'})
    assert session.feed(PCM)[0]['text'] == 'что означает готово'
    assert session.control({'action': 'finish'})[0]['text'] == 'что означает готово'


def test_pause_ends_question_and_manual_stop_finishes_tail():
    engine = ScriptedEngine(Reading('проверка', True))
    session = VoiceSession(engine)
    session.control({'action': 'start'})
    session.feed(PCM)
    replies = []
    for _ in range(52):
        replies.extend(session.feed(PCM))
    assert {'type': 'final', 'text': 'проверка'} in replies
    session.control({'action': 'start'})
    engine.final = 'проверить уровень масла'
    assert session.control({'action': 'finish'})[0]['text'] == engine.final


def test_empty_recording_times_out_and_pause_releases_stream():
    session = VoiceSession(ScriptedEngine())
    session.control({'action': 'start'})
    replies = []
    for _ in range(162):
        replies.extend(session.feed(PCM))
    assert {'type': 'final', 'text': ''} in replies
    session.control({'action': 'pause'})
    assert session.stream is None
    assert session.feed(PCM) == []
    assert session.control({'action': 'resume'})[0]['state'] == 'waiting'


def test_invalid_name_rejected():
    for name in (None, ' ', 'Sasha', 'Имя123', 'А' * 37):
        with pytest.raises(ValueError):
            validate_name(name)


def test_websocket_names_and_transcription():
    app = with_voice(create_app(), ScriptedEngine(
        Reading('саша слушай', True), Reading('проверить уровень масла', True)))
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get('/').status_code == 200
        assert client.get('/api/catalog').status_code == 200
        assert client.get('/voice/status').json() == {'ready': True}
        assert 'ready' not in client.get('/api/session').json()
        assert client.get('/static/voice/voice-kit.js').status_code == 200
        with client.websocket_connect(STREAM, headers={'Origin': ORIGIN}) as stream:
            stream.send_json({'action': 'configure', 'name': 'Саша'})
            assert stream.receive_json()['phrase'] == 'Саша, слушай'
            stream.send_bytes(PCM)
            assert stream.receive_json()['state'] == 'recording'
            stream.send_bytes(PCM)
            assert stream.receive_json()['text'] == 'проверить уровень масла'
            stream.send_json({'action': 'finish'})
            assert stream.receive_json() == {'type': 'final', 'text': 'проверить уровень масла'}


def test_websocket_refuses_foreign_origin_oversized_frames_and_second_client():
    app = with_voice(create_app(), ScriptedEngine())
    with TestClient(app, base_url=ORIGIN) as client:
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(STREAM, headers={'Origin': 'https://example.test'}):
                pass
        assert denied.value.code == 1008
        with client.websocket_connect(STREAM, headers={'Origin': ORIGIN}) as stream:
            with client.websocket_connect(STREAM, headers={'Origin': ORIGIN}) as another:
                assert another.receive_json()['type'] == 'error'
            stream.send_bytes(bytes(6402))
            with pytest.raises(WebSocketDisconnect) as oversized:
                stream.receive_json()
            assert oversized.value.code == 1009
