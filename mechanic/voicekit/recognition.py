"""Local streaming Russian ASR and the voice interaction state."""
from array import array
from dataclasses import dataclass
from pathlib import Path
import re
import sys

SAMPLE_RATE = 16000
END_COMMAND = 'готово'
DEFAULT_NAME = 'Механик'
ENDPOINT_SECONDS = 0.65
SILENCE_SECONDS = 2.5
START_TIMEOUT_SECONDS = 8.0
MAX_DICTATION_SECONDS = 60.0
MAX_TEXT = 1500

NAME_PATTERN = re.compile(r'[А-ЯЁа-яё]{2,20}(?:[ -][А-ЯЁа-яё]{2,20}){0,2}')


def normalize(text):
    return ' '.join(re.findall(r'[а-я0-9]+', text.casefold().replace('ё', 'е')))


def validate_name(name):
    if not isinstance(name, str) or len(name) > 36 or not NAME_PATTERN.fullmatch(name.strip()):
        raise ValueError('Введите короткое имя кириллицей, например «Саша» или «Механик».')
    return name.strip()


@dataclass(frozen=True)
class Reading:
    text: str
    endpoint: bool


class SherpaEngine:
    def __init__(self, model_dir):
        import sherpa_onnx

        files = {
            'tokens': 'tokens.txt',
            'encoder': 'encoder.int8.onnx',
            'decoder': 'decoder.onnx',
            'joiner': 'joiner.int8.onnx',
        }
        paths = {role: Path(model_dir) / filename for role, filename in files.items()}
        if not all(path.is_file() for path in paths.values()):
            raise FileNotFoundError('Локальная модель отсутствует. Выполните prepare.py.')
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            **{role: str(path) for role, path in paths.items()},
            num_threads=2, sample_rate=SAMPLE_RATE, provider='cpu',
            enable_endpoint_detection=True, rule1_min_trailing_silence=4,
            rule2_min_trailing_silence=ENDPOINT_SECONDS, rule3_min_utterance_length=25,
        )

    def stream(self):
        return self.recognizer.create_stream()

    def read(self, stream, pcm):
        samples = array('h', pcm)
        if sys.byteorder != 'little':
            samples.byteswap()
        stream.accept_waveform(SAMPLE_RATE, [sample / 32768 for sample in samples])
        self.decode(stream)
        return Reading(self.recognizer.get_result(stream).strip(), self.recognizer.is_endpoint(stream))

    def finish(self, stream):
        stream.accept_waveform(SAMPLE_RATE, [0.0] * (SAMPLE_RATE // 2))
        stream.input_finished()
        self.decode(stream)
        return self.recognizer.get_result(stream).strip()

    def decode(self, stream):
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)


class VoiceSession:
    def __init__(self, engine):
        self.engine = engine
        self.name = DEFAULT_NAME
        self.testing = False
        self.reset('waiting')

    def reset(self, state):
        self.state = state
        self.stream = self.engine.stream() if state != 'paused' else None
        self.segments = []
        self.elapsed = 0.0
        self.quiet = 0.0
        self.partial = ''

    def configure(self, name):
        self.name = validate_name(name)
        self.testing = False
        self.reset('waiting')
        return [{'type': 'state', 'state': 'waiting', 'phrase': f'{self.name}, слушай'}]

    def start(self, voice=False):
        self.testing = False
        self.reset('recording')
        return [{'type': 'state', 'state': 'recording', 'activated': voice}]

    def control(self, message):
        action = message.get('action')
        if action == 'configure':
            return self.configure(message.get('name'))
        if action == 'start' and self.state == 'waiting':
            return self.start()
        if action == 'test' and self.state == 'waiting':
            self.reset('waiting')
            self.testing = True
            return [{'type': 'testing'}]
        if action == 'finish' and self.state == 'recording':
            return self.finish()
        if action == 'pause':
            self.testing = False
            self.reset('paused')
            return [{'type': 'state', 'state': 'paused'}]
        if action == 'resume' and self.state == 'paused':
            return self.configure(self.name)
        return [{'type': 'error', 'message': 'Команда недоступна в текущем режиме.'}]

    def feed(self, pcm):
        if self.state == 'paused':
            return []
        reading = self.engine.read(self.stream, pcm)
        if self.state == 'waiting':
            return self.wait_for_name(reading)
        return self.dictate(pcm, reading)

    def wait_for_name(self, reading):
        if not reading.endpoint:
            return []
        if normalize(reading.text) == normalize(f'{self.name} слушай'):
            if self.testing:
                self.testing = False
                self.reset('waiting')
                return [{'type': 'tested'}]
            return self.start(voice=True)
        self.stream = self.engine.stream()
        return []

    def dictate(self, pcm, reading):
        duration = len(pcm) / (2 * SAMPLE_RATE)
        self.elapsed += duration
        self.quiet += duration
        changed = reading.text != self.partial
        if changed and reading.text:
            self.quiet = 0.0
        self.partial = reading.text
        if reading.endpoint:
            if normalize(reading.text) == END_COMMAND:
                return self.finish(keep_current=False)
            if reading.text:
                self.segments.append(reading.text)
            self.partial = ''
            self.stream = self.engine.stream()
            changed = True
        text = ' '.join([*self.segments, self.partial]).strip()
        if len(text) >= MAX_TEXT or self.elapsed >= MAX_DICTATION_SECONDS:
            return self.finish()
        if text and self.quiet >= SILENCE_SECONDS:
            return self.finish()
        if not text and self.elapsed >= START_TIMEOUT_SECONDS:
            return self.finish()
        return [{'type': 'partial', 'text': text}] if changed else []

    def finish(self, keep_current=True):
        tail = self.engine.finish(self.stream) if keep_current else ''
        if normalize(tail) == END_COMMAND:
            tail = ''
        text = ' '.join([*self.segments, tail]).strip()[:MAX_TEXT]
        self.reset('waiting')
        return [{'type': 'final', 'text': text}, {'type': 'state', 'state': 'waiting'}]
