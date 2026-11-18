import atexit
import base64
import contextlib
import hashlib
import io
import json
import os
import platform
import re
import select
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from app.config import load_config
from app.engines import TTSEngine
from app.engines.base import ProviderPlan, ProviderStep, TTSRequest, TTSCapability
from app.engines.registry import register
from app.voice_store import VoiceStore

WORKER = Path(__file__).with_name('worker.py')
MAX_MESSAGE = 32 * 1024 * 1024


def _hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _read_exact(fd, count, deadline):
    data = bytearray()
    while len(data) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise TimeoutError('模型子进程响应超时')
        part = os.read(fd, count - len(data))
        if not part:
            raise RuntimeError('模型子进程意外退出')
        data.extend(part)
    return bytes(data)


def _write_all(fd, data, deadline):
    view = memoryview(data)
    while view:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
            raise TimeoutError('模型子进程请求超时')
        try:
            count = os.write(fd, view)
        except BlockingIOError:
            continue
        if count <= 0:
            raise RuntimeError('模型子进程意外退出')
        view = view[count:]


class MLXEngine(TTSEngine):
    backend = 'mlx-audio'
    kind = ''
    default_model_dir = ''
    capability = TTSCapability(
        supports_phoneme=False, supports_break_ms=False, supports_emphasis=False,
        supports_streaming=False, supports_chinese=True, clone_mode='zero_shot',
        sample_rate=24000, supports_speed=False,
    )

    def __init__(self):
        cfg = load_config().get('engines', {}).get(self.name, {})
        self.model_dir = Path(cfg.get('model_path') or self.default_model_dir).expanduser()
        self.python = Path(cfg.get('python') or './.venv-mlx/bin/python').expanduser()
        self._process = None
        self._response_fd = None
        self._next_id = 0
        self._voices = {}
        self._lock = threading.RLock()
        self._diagnostic = None
        atexit.register(self.unload)

    def readiness(self):
        model = self.model_dir.is_dir() and (self.model_dir / 'config.json').is_file() and (self.model_dir / 'model.safetensors').is_file()
        if self.kind == 'qwen':
            model = model and (self.model_dir / 'speech_tokenizer').is_dir()
        return {'runtime': 'present' if self.python.is_file() and os.access(self.python, os.X_OK) else 'missing',
                'model': 'present' if model else 'missing',
                'platform': 'supported' if sys.platform == 'darwin' and platform.machine() == 'arm64' else 'unsupported'}

    def _check_paths(self):
        state = self.readiness()
        if state['platform'] != 'supported':
            raise RuntimeError('MLX 模型目前仅支持 Apple Silicon macOS')
        if state['runtime'] != 'present':
            raise FileNotFoundError(f'MLX Python 未找到: {self.python}')
        if state['model'] != 'present':
            raise FileNotFoundError(f'本地模型目录不完整: {self.model_dir}')
        if self.kind == 'qwen' and not (self.model_dir / 'speech_tokenizer').is_dir():
            raise FileNotFoundError('Qwen 模型缺少 speech_tokenizer')
        if self.model_dir.is_symlink():
            raise ValueError('模型目录不能是符号链接')
        for directory, dirs, files in os.walk(self.model_dir, followlinks=False):
            for name in dirs + files:
                if (Path(directory) / name).is_symlink():
                    raise ValueError('模型目录不能包含符号链接，请先导入完整模型')

    def resume_identity(self):
        self._check_paths()
        script = '''
import base64,csv,hashlib,importlib.metadata as metadata,io,json,sys
packages = []
for distribution in metadata.distributions():
    name = distribution.metadata.get('Name')
    record = distribution.read_text('RECORD')
    if not name or record is None:
        raise RuntimeError('distribution identity unavailable')
    digest = hashlib.sha256()
    for path, expected, size in sorted(csv.reader(io.StringIO(record))):
        if not expected:
            if path.endswith(('.py', '.so', '.dylib', '.pth')):
                raise RuntimeError('runtime file has no digest')
            continue
        algorithm, encoded = expected.split('=', 1)
        if algorithm != 'sha256':
            raise RuntimeError('unsupported runtime digest')
        content = hashlib.sha256()
        with distribution.locate_file(path).open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                content.update(block)
        actual = base64.urlsafe_b64encode(content.digest()).rstrip(b'=').decode('ascii')
        if actual != encoded:
            raise RuntimeError('runtime file changed')
        digest.update(path.encode('utf-8') + b'\\0' + content.digest())
    packages.append((name, distribution.version, digest.hexdigest()))
print(json.dumps({'python': sys.version, 'executable': sys.executable, 'packages': sorted(packages)}))
'''
        try:
            result = subprocess.run([str(self.python.absolute()), '-I', '-c', script],
                                    capture_output=True, text=True, timeout=180, check=True)
            runtime = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise ValueError('无法确定 MLX 运行环境身份，拒绝恢复任务') from exc
        engines_dir = Path(__file__).parents[1]
        return {'version': 1, 'adapter_sha256': _hash(Path(__file__)),
                'worker_sha256': _hash(WORKER), 'python_sha256': _hash(self.python.resolve()),
                'contract_sha256': _hash(engines_dir / '__init__.py'),
                'capabilities_sha256': _hash(engines_dir / 'base.py'),
                'registry_sha256': _hash(engines_dir / 'registry.py'), 'runtime': runtime}

    @contextlib.contextmanager
    def diagnostic_chunk(self, log, index):
        if self.kind != 'vox' or type(index) is not int or index < 0:
            raise ValueError('诊断只支持 VoxCPM 的有效分块')
        with self._lock:
            if self._diagnostic is not None:
                raise RuntimeError('诊断分块上下文冲突')
            self._diagnostic = {'log': log, 'chunk_index': index, 'attempt_id': uuid.uuid4().hex}
            try:
                yield
            finally:
                self._diagnostic = None

    def _receive(self, request_id, allow_failure=False):
        deadline = time.monotonic() + 1200
        length = struct.unpack('!I', _read_exact(self._response_fd, 4, deadline))[0]
        if length <= 0 or length > MAX_MESSAGE:
            raise RuntimeError('模型子进程返回的数据长度无效')
        try:
            response = json.loads(_read_exact(self._response_fd, length, deadline))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError('模型子进程返回的数据无效') from exc
        if not isinstance(response, dict) or response.get('version') != 1 or response.get('id') != request_id:
            raise RuntimeError('模型子进程响应不匹配')
        if type(response.get('ok')) is not bool:
            raise RuntimeError('模型子进程响应无效')
        if response.get('ok') is not True and not allow_failure:
            error = response.get('error', 'UnknownError')
            if not isinstance(error, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,100}', error):
                error = 'UnknownError'
            raise RuntimeError(f'模型子进程加载或合成失败: {error}')
        return response

    def _call(self, operation, diagnostic_step=None, **values):
        with self._lock:
            if not self._process or self._process.poll() is not None:
                raise RuntimeError('模型子进程未运行')
            self._next_id += 1
            request_id = self._next_id
            request = {'version': 1, 'id': request_id, 'operation': operation, **values}
            state = self._diagnostic if operation == 'synthesize' else None
            fields = None
            context = None
            if state is not None:
                if type(diagnostic_step) is not int or diagnostic_step < 0:
                    raise ValueError('诊断步骤无效')
                index = state['chunk_index']
                fields = {'chunk_index': index, 'chunk_number': index + 1,
                          'step_index': diagnostic_step, 'step_number': diagnostic_step + 1,
                          'attempt_id': state['attempt_id'], 'worker_request_id': request_id}
                context = {'version': 1, 'run_id': state['log'].run_id,
                           'attempt_id': state['attempt_id'], 'chunk_index': index,
                           'step_index': diagnostic_step}
                request['diagnostic'] = context
            payload = json.dumps(request, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
            if len(payload) > 1024 * 1024:
                raise ValueError('模型请求过大')
            started = False
            completion_attempted = False
            try:
                if fields is not None:
                    state['log'].record('step_start', **fields)
                    started = True
                _write_all(self._process.stdin.fileno(), payload, time.monotonic() + 1200)
                response = self._receive(request_id, allow_failure=fields is not None)
                if fields is None:
                    return response
                observation = response.get('diagnostic')
                if (not isinstance(observation, dict)
                        or set(observation) != {'context', 'before_sha256', 'after_sha256', 'elapsed_ms'}
                        or observation['context'] != context
                        or any(value is not None and (not isinstance(value, str)
                               or not re.fullmatch(r'[0-9a-f]{64}', value))
                               for value in (observation['before_sha256'], observation['after_sha256']))
                        or (observation['elapsed_ms'] is not None
                            and (type(observation['elapsed_ms']) is not int or observation['elapsed_ms'] < 0))
                        or (response['ok'] and (not observation['before_sha256'] or not observation['after_sha256']
                                                or observation['elapsed_ms'] is None))):
                    raise RuntimeError('模型子进程诊断响应无效')
                before = observation['before_sha256']
                after = observation['after_sha256']
                outcome = 'error' if not response['ok'] else (
                    'success' if before == after == state['log'].reference_sha256 else 'reference_mismatch')
                observed = {'before_sha256': before, 'after_sha256': after}
                if observation['elapsed_ms'] is not None:
                    observed['elapsed_ms'] = observation['elapsed_ms']
                completion_attempted = True
                state['log'].record('step_finish', **fields, **observed, outcome=outcome)
                if outcome == 'reference_mismatch':
                    raise RuntimeError('参考音频哈希与任务身份不一致，已停止合成')
                if outcome == 'error':
                    raise RuntimeError('模型子进程加载或合成失败')
                return response
            except BaseException:
                if started and not completion_attempted:
                    try:
                        state['log'].record('step_finish', **fields, outcome='unknown')
                    except Exception:
                        pass
                self._stop()
                raise

    def load(self):
        with self._lock:
            if self.is_loaded():
                return
            self._check_paths()
            read_fd, write_fd = os.pipe()
            env = os.environ.copy()
            env.update({'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'})
            try:
                process = subprocess.Popen(
                    [str(self.python.absolute()), '-I', str(WORKER), str(write_fd),
                     str(self.model_dir.resolve()), self.kind, str(os.getpid())],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    pass_fds=(write_fd,), env=env, start_new_session=True,
                )
            except BaseException:
                os.close(read_fd)
                raise
            finally:
                os.close(write_fd)
            self._process = process
            self._response_fd = read_fd
            try:
                os.set_blocking(process.stdin.fileno(), False)
                self._receive(0)
            except BaseException:
                self._stop()
                raise

    def is_loaded(self):
        return self._process is not None and self._process.poll() is None

    def _stop(self):
        process = self._process
        self._process = None
        if self._response_fd is not None:
            os.close(self._response_fd)
            self._response_fd = None
        if process:
            if process.stdin:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired as exc:
                        raise RuntimeError('无法确认模型子进程退出') from exc
            else:
                process.wait()

    def unload(self):
        with self._lock:
            if self._process:
                try:
                    if self.is_loaded():
                        self._call('close')
                finally:
                    self._stop()
            self._voices.clear()

    def clone_voice(self, ref_wav_path: str, voice_id: str, prompt_text: str = ''):
        if not Path(ref_wav_path).is_file():
            raise ValueError('参考音频不存在')
        self._voices[voice_id] = (str(Path(ref_wav_path).resolve()), prompt_text)
        return voice_id

    def forget_voice(self, voice_id: str):
        self._voices.pop(voice_id, None)

    def _voice(self, voice_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', voice_id) or voice_id == 'default':
            raise ValueError('请明确指定本地音色')
        if voice_id in self._voices:
            return self._voices[voice_id]
        return self._stored_voice(voice_id)

    def _stored_voice(self, voice_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', voice_id) or voice_id == 'default':
            raise ValueError('请明确指定本地音色')
        store = VoiceStore(load_config().get('voices_dir', './voices'))
        folder = store.base / voice_id
        wav = folder / 'ref.wav'
        meta_path = folder / 'meta.json'
        if (folder.is_symlink() or folder.resolve().parent != store.base.resolve() or wav.is_symlink()
                or meta_path.is_symlink() or not wav.is_file() or not meta_path.is_file()):
            raise ValueError('音色不存在或参考音频不可用')
        meta = store.get(voice_id)
        if not isinstance(meta, dict) or not isinstance(meta.get('prompt_text', ''), str):
            raise ValueError('音色信息无效')
        return str(wav.resolve()), meta.get('prompt_text', '')

    def reference_identity(self, voice_id: str) -> dict:
        if self.kind != 'vox':
            raise ValueError('该模型不支持参考音频加逐字稿模式')
        path, text = self._stored_voice(voice_id)
        if not text.strip() or len(text.encode('utf-8')) > 65536:
            raise ValueError('参考音频加逐字稿模式需要非空且不超过 64 KiB 的逐字稿')
        return {'audio_sha256': _hash(Path(path)),
                'prompt_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}

    def _check_reference_identity(self, voice_id, expected):
        try:
            if self.reference_identity(voice_id) != expected:
                raise ValueError('参考音频或逐字稿已变化')
        except Exception as exc:
            if self._process is not None:
                self._stop()
            raise ValueError('参考音频或逐字稿在合成期间发生变化或不可用') from exc

    def validate_voice(self, voice_id: str) -> None:
        _, ref_text = self._voice(voice_id)
        if self.kind == 'qwen' and not ref_text.strip():
            raise ValueError('Qwen 克隆需要参考录音对应的逐字文本')

    def compile_input(self, req: TTSRequest) -> ProviderPlan:
        self.validate_request(req)
        mapping = tuple((i, i + 1, i, i + 1) for i in range(len(req.text)))
        return ProviderPlan(1, (ProviderStep(kind='speak', text=req.text,
                                               source_end=len(req.text), source_map=mapping),), req)

    def synthesize(self, req: TTSRequest) -> bytes:
        return self.execute_plan(self.compile_input(req))

    def execute_plan(self, plan: ProviderPlan) -> bytes:
        req = plan.request
        self.validate_request(req)
        if self._diagnostic is not None and req.voice_id != self._diagnostic['log'].voice_id:
            raise ValueError('诊断音色 ID 与合成请求不一致')
        if self._diagnostic is not None and req.reference_mode == 'reference_plus_prompt':
            raise ValueError('参考音频加逐字稿模式暂不支持诊断日志')
        self.validate_voice(req.voice_id)
        if req.reference_mode == 'reference_plus_prompt':
            ref_audio, ref_text = self._stored_voice(req.voice_id)
            expected = self.reference_identity(req.voice_id)
            current = {'audio_sha256': _hash(Path(ref_audio)),
                       'prompt_sha256': hashlib.sha256(ref_text.encode('utf-8')).hexdigest()}
            if current != expected or (req.reference_fingerprint is not None and req.reference_fingerprint != expected):
                raise ValueError('参考音频或逐字稿与任务身份不一致')
        else:
            ref_audio, ref_text = self._voice(req.voice_id)
            expected = None
        pieces = []
        for step_index, step in enumerate(plan.steps):
            if step.kind == 'silence':
                if self._diagnostic is not None:
                    state = self._diagnostic
                    index = state['chunk_index']
                    state['log'].record('step_silence', chunk_index=index, chunk_number=index + 1,
                                        step_index=step_index, step_number=step_index + 1,
                                        attempt_id=state['attempt_id'], milliseconds=step.milliseconds)
                pieces.append(np.zeros(round(self.capability.sample_rate * step.milliseconds / 1000), dtype=np.float32))
                continue
            self.open_session()
            inputs = {'text': step.text, 'ref_audio': ref_audio}
            if self.kind == 'qwen':
                inputs['ref_text'] = ref_text
            elif req.reference_mode == 'reference_plus_prompt':
                self._check_reference_identity(req.voice_id, expected)
                inputs['reference_mode'] = req.reference_mode
                inputs['prompt_text'] = ref_text
            if step.instruction:
                inputs['instruct'] = step.instruction
            if self._diagnostic is None:
                response = self._call('synthesize', **inputs)
            else:
                response = self._call('synthesize', diagnostic_step=step_index, **inputs)
            if expected is not None:
                self._check_reference_identity(req.voice_id, expected)
            try:
                wav = base64.b64decode(response['audio'], validate=True)
                samples, rate = sf.read(io.BytesIO(wav), dtype='float32', always_2d=True)
                if (not len(samples) or rate != self.capability.sample_rate or samples.shape[1] != 1
                        or not np.isfinite(samples).all()):
                    raise ValueError('invalid waveform')
                pieces.append(samples[:, 0])
            except (KeyError, ValueError, RuntimeError) as exc:
                self._stop()
                raise ValueError('模型返回的音频无效') from exc
        if not pieces or not sum(map(len, pieces)):
            raise ValueError('没有可合成的音频')
        output = io.BytesIO()
        sf.write(output, np.concatenate(pieces), self.capability.sample_rate, format='WAV')
        return output.getvalue()


@register('qwen3tts')
class Qwen3TTSEngine(MLXEngine):
    kind = 'qwen'
    default_model_dir = './models/Qwen3-TTS'


@register('voxcpm2')
class VoxCPM2Engine(MLXEngine):
    kind = 'vox'
    default_model_dir = './models/VoxCPM2'
    capability = TTSCapability(
        supports_phoneme=True, supports_break_ms=True, supports_emphasis=False,
        supports_streaming=False, supports_chinese=True, clone_mode='zero_shot',
        sample_rate=48000, supports_speed=False, supports_emotion=True,
        experimental_controls=('phoneme', 'emotion'),
        reference_modes=('reference', 'reference_plus_prompt'),
    )

    def compile_input(self, req: TTSRequest) -> ProviderPlan:
        self.validate_request(req)
        length = len(req.text)
        if re.search(r'\{[a-züv]+[0-4]\}', req.text, re.I):
            raise ValueError('请使用 [字=拼音] 标准注音，不能直接输入模型原生音素')
        for pos, value in req.phoneme_overrides.items():
            if not isinstance(pos, int) or not 0 <= pos < length or not isinstance(value, str) or not re.fullmatch(r'[a-züv]+[0-4]', value):
                raise ValueError(f'注音无效: {pos}')
        break_at = {}
        for pos, ms in req.breaks:
            if not isinstance(pos, int) or not 0 <= pos <= length or not isinstance(ms, int) or not 0 <= ms <= 60000:
                raise ValueError('停顿位置或时长无效')
            break_at[pos] = break_at.get(pos, 0) + ms
        emotions = sorted(req.emotion_spans)
        previous = 0
        for start, end, instruction in emotions:
            if not (isinstance(start, int) and isinstance(end, int) and previous <= start < end <= length
                    and isinstance(instruction, str) and instruction.strip()
                    and len(instruction) <= 160 and not any(ch in instruction for ch in '(){}[]<>\r\n')
                    and all(ord(ch) >= 32 for ch in instruction)):
                raise ValueError('情绪范围或描述无效')
            previous = end
        boundaries = {0, length, *break_at}
        for start, end, _ in emotions:
            boundaries.update((start, end))
        steps = []
        cuts = sorted(boundaries)
        if 0 in break_at:
            steps.append(ProviderStep(kind='silence', milliseconds=break_at[0]))
        for start, end in zip(cuts, cuts[1:]):
            if req.text[start:end].strip():
                parts = ['{' + req.phoneme_overrides[i] + '}' if i in req.phoneme_overrides else req.text[i]
                         for i in range(start, end)]
                positions = [0]
                for part in parts:
                    positions.append(positions[-1] + len(part))
                source_map = tuple((i, i + 1, positions[i - start], positions[i - start + 1])
                                   for i in range(start, end))
                instruction = next((value for a, b, value in emotions if a <= start and end <= b), '')
                steps.append(ProviderStep(kind='speak', text=''.join(parts), instruction=instruction,
                                          source_start=start, source_end=end, source_map=source_map))
            if end in break_at:
                steps.append(ProviderStep(kind='silence', milliseconds=break_at[end],
                                          source_start=end, source_end=end))
        return ProviderPlan(1, tuple(steps), req)
