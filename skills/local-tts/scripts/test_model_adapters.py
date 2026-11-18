import argparse
import base64
import contextlib
import importlib
import io
import json
import os
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tts_cli
from app.engines.base import TTSRequest, TTSCapability, validate_request
from app.engines.mlx import engine as mlx_engine
from app.engines.registry import REGISTRY, get_engine, register


@contextlib.contextmanager
def temporary_modules(modules):
    absent = object()
    previous = {name: sys.modules.get(name, absent) for name in modules}
    try:
        sys.modules.update(modules)
        yield
    finally:
        for name, old in previous.items():
            if old is absent:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


FAKE_WORKER = r'''
import base64, json, os, struct, sys
fd = int(sys.argv[1])
def send(value):
    data = json.dumps(value).encode()
    os.write(fd, struct.pack('!I', len(data)) + data)
send({'version': 1, 'id': 0, 'ok': True})
print('worker stdout is not protocol', flush=True)
for line in sys.stdin.buffer:
    data = json.loads(line)
    if data['operation'] == 'close':
        send({'version': 1, 'id': data['id'], 'ok': True})
        break
    with open(os.environ['FAKE_REQUESTS'], 'a') as f:
        f.write(json.dumps(data, ensure_ascii=False) + '\n')
    if data['text'] == 'exit':
        os._exit(1)
    elif data['text'] == 'broken':
        send({'version': 2, 'id': data['id'], 'ok': True})
    elif data['text'] == 'invalid-audio':
        send({'version': 1, 'id': data['id'], 'ok': True, 'audio': 'not-base64!'})
    else:
        send({'version': 1, 'id': data['id'], 'ok': True, 'audio': os.environ['FAKE_WAV']})
'''


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        Path('config.yaml').write_text('voices_dir: ./voices\n', encoding='utf-8')
        Path('voices/example').mkdir(parents=True)
        sf.write('voices/example/ref.wav', np.zeros(16000), 16000)
        Path('voices/example/meta.json').write_text(json.dumps({'prompt_text': '参考录音'}), encoding='utf-8')
        Path('fake_worker.py').write_text(FAKE_WORKER, encoding='utf-8')
        buffer = io.BytesIO()
        sf.write(buffer, np.zeros(24000), 24000, format='WAV')
        self.env = patch.dict(os.environ, {'FAKE_REQUESTS': str(Path('requests.jsonl').resolve()),
                                           'FAKE_WAV': base64.b64encode(buffer.getvalue()).decode()})
        self.env.start()
        self.worker = patch.object(mlx_engine, 'WORKER', Path('fake_worker.py').resolve())
        self.worker.start()
        self.engines = []

    def tearDown(self):
        for engine in self.engines:
            engine.unload()
        self.worker.stop()
        self.env.stop()
        os.chdir(self.previous)
        self.tmp.cleanup()

    def engine(self):
        engine = mlx_engine.Qwen3TTSEngine()
        engine.python = Path(sys.executable)
        engine._check_paths = lambda: None
        self.engines.append(engine)
        return engine

    def test_cli_generates_selected_model_with_voice_store(self):
        import tts_cli
        engine = self.engine()
        args = argparse.Namespace(command='synth', text='你好。', file=None, voice='example',
                                  model='qwen3tts', speed=1.0, output='result.wav')
        with patch.object(tts_cli, 'engine_for', return_value=engine):
            result = tts_cli.run(args)
        self.assertEqual(result['model'], 'qwen3tts')
        self.assertEqual(result['voice_id'], 'example')
        self.assertEqual(result['sample_rate'], 24000)
        self.assertTrue(Path(result['output']).is_file())

    def test_model_listing_does_not_spawn_worker(self):
        import tts_cli
        with patch.object(mlx_engine.subprocess, 'Popen', side_effect=AssertionError('unexpected worker')):
            result = tts_cli.run(argparse.Namespace(command='models'))
        ids = {item['id'] for item in result['models']}
        self.assertTrue({'qwen3tts', 'voxcpm2'}.issubset(ids))

    def test_registry_and_duplicate_ids(self):
        self.assertIn('qwen3tts', REGISTRY)
        self.assertIn('voxcpm2', REGISTRY)
        with self.assertRaisesRegex(ValueError, '重复'):
            register('qwen3tts')(mlx_engine.Qwen3TTSEngine)

    def test_automatic_discovery_for_new_adapter(self):
        import app.engines as package
        import app.engines.registry as registry
        root = Path('plugins').resolve()
        (root / 'fixture_adapter').mkdir(parents=True)
        (root / 'fixture_adapter' / 'engine.py').write_text(
            "from app.engines.registry import register\n"
            "from app.engines.mock_engine import MockEngine\n"
            "@register('fixture_adapter')\nclass Fixture(MockEngine): pass\n")
        package.__path__.append(str(root))
        try:
            with patch.object(registry, '__file__', str(root / 'registry.py')):
                registry._import_engines()
            self.assertIn('fixture_adapter', REGISTRY)
        finally:
            package.__path__.remove(str(root))
            REGISTRY.pop('fixture_adapter', None)
            sys.modules.pop('app.engines.fixture_adapter.engine', None)
            sys.modules.pop('app.engines.fixture_adapter', None)

    def test_capabilities_reject_unimplemented_controls(self):
        cap = TTSCapability()
        for request in (TTSRequest('a', phoneme_overrides={0: 'a1'}),
                        TTSRequest('a', breaks=[(1, 100)]),
                        TTSRequest('a', emphasis_spans=[(0, 1)]),
                        TTSRequest('a', speed=1.2)):
            with self.assertRaises(ValueError):
                validate_request(request, cap)
        self.assertFalse(mlx_engine.Qwen3TTSEngine.capability.supports_streaming)
        self.assertEqual(mlx_engine.VoxCPM2Engine.capability.sample_rate, 48000)

    def test_subprocess_reused_and_uses_selected_voice(self):
        engine = self.engine()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            first = engine.synthesize(TTSRequest('你好', voice_id='example'))
            pid = engine._process.pid
            second = engine.synthesize(TTSRequest('再见', voice_id='example'))
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(engine._process.pid, pid)
        self.assertEqual(first, second)
        requests = [json.loads(line) for line in Path('requests.jsonl').read_text().splitlines()]
        self.assertEqual([item['text'] for item in requests], ['你好', '再见'])
        self.assertEqual(requests[0]['ref_text'], '参考录音')
        self.assertEqual(requests[0]['ref_audio'], str(Path('voices/example/ref.wav').resolve()))
        engine.unload()
        self.assertFalse(engine.is_loaded())

    def test_missing_or_unsafe_voice_is_rejected_before_loading(self):
        engine = self.engine()
        for name in ('default', '../outside', 'missing'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                engine.synthesize(TTSRequest('你好', voice_id=name))
        self.assertFalse(engine.is_loaded())

    def test_wrong_protocol_stops_worker(self):
        engine = self.engine()
        with self.assertRaisesRegex(RuntimeError, '响应不匹配'):
            engine.synthesize(TTSRequest('broken', voice_id='example'))
        self.assertFalse(engine.is_loaded())

    def test_worker_maps_voice_inputs_per_model(self):
        worker_module = importlib.import_module('app.engines.mlx.worker')
        for kind in ('qwen', 'vox'):
            with self.subTest(kind=kind):
                calls = []
                class Result:
                    sample_rate = 24000
                    audio = np.zeros(16, dtype=np.float32)
                class Model:
                    def generate(self, **kwargs):
                        calls.append(kwargs)
                        yield Result()
                class Thread:
                    def __init__(self, **kwargs):
                        pass
                    def start(self):
                        pass
                core = types.ModuleType('mlx.core')
                core.eval = lambda value: None
                mlx = types.ModuleType('mlx')
                mlx.core = core
                utils = types.ModuleType('mlx_audio.tts.utils')
                utils.load_model = lambda path: Model()
                audio_package = types.ModuleType('mlx_audio')
                audio_package.__path__ = []
                tts_package = types.ModuleType('mlx_audio.tts')
                tts_package.__path__ = []
                modules = {'mlx': mlx, 'mlx.core': core, 'mlx_audio': audio_package,
                           'mlx_audio.tts': tts_package, 'mlx_audio.tts.utils': utils}
                request = {'version': 1, 'id': 1, 'operation': 'synthesize',
                           'text': '你好', 'ref_audio': '/ref.wav', 'ref_text': '参考音频',
                           'instruct': 'happy'}
                stream = types.SimpleNamespace(buffer=io.BytesIO((json.dumps(request) + '\n').encode()))
                read_fd, write_fd = os.pipe()
                try:
                    with temporary_modules(modules), patch.object(worker_module.threading, 'Thread', Thread), \
                            patch.object(sys, 'argv', ['worker', str(write_fd), '/weights', kind, str(os.getpid())]), \
                            patch.object(sys, 'stdin', stream):
                        self.assertEqual(worker_module.main(), 0)
                    os.close(write_fd)
                    write_fd = None
                    with os.fdopen(read_fd, 'rb') as response:
                        read_fd = None
                        for expected_id in (0, 1):
                            length = struct.unpack('!I', response.read(4))[0]
                            result = json.loads(response.read(length))
                            self.assertTrue(result['ok'])
                            self.assertEqual(result['id'], expected_id)
                finally:
                    if read_fd is not None:
                        os.close(read_fd)
                    if write_fd is not None:
                        os.close(write_fd)
                self.assertEqual(calls[0]['ref_audio'], '/ref.wav')
                if kind == 'qwen':
                    self.assertEqual(calls[0]['ref_text'], '参考音频')
                    self.assertNotIn('instruct', calls[0])
                else:
                    self.assertNotIn('ref_text', calls[0])
                    self.assertEqual(calls[0]['instruct'], 'happy')

    def vox_engine(self):
        engine = mlx_engine.VoxCPM2Engine()
        engine.python = Path(sys.executable)
        engine._check_paths = lambda: None
        self.engines.append(engine)
        buffer = io.BytesIO()
        sf.write(buffer, np.zeros(48000), 48000, format='WAV')
        os.environ['FAKE_WAV'] = base64.b64encode(buffer.getvalue()).decode()
        return engine

    def test_vox_optional_reference_prompt_is_request_scoped(self):
        engine = self.vox_engine()
        engine.synthesize_standard(TTSRequest('默认', voice_id='example'))
        fingerprint = engine.reference_identity('example')
        engine.synthesize_standard(TTSRequest('组合', voice_id='example',
                                              reference_mode='reference_plus_prompt',
                                              reference_fingerprint=fingerprint))
        engine.synthesize_standard(TTSRequest('再次默认', voice_id='example'))
        requests = [json.loads(line) for line in Path('requests.jsonl').read_text().splitlines()]
        self.assertNotIn('reference_mode', requests[0])
        self.assertNotIn('prompt_text', requests[0])
        self.assertEqual(requests[1]['reference_mode'], 'reference_plus_prompt')
        self.assertEqual(requests[1]['prompt_text'], '参考录音')
        self.assertEqual(requests[1]['ref_audio'], str(Path('voices/example/ref.wav').resolve()))
        self.assertNotIn('reference_mode', requests[2])
        self.assertNotIn('prompt_text', requests[2])

    def test_cli_combined_mode_with_voice_store(self):
        engine = self.vox_engine()
        args = argparse.Namespace(command='synth', text='你好。', file=None, voice='example',
                                  model='voxcpm2', speed=1.0, output='combined.wav',
                                  reference_mode='reference_plus_prompt')
        with patch.object(tts_cli, 'engine_for', return_value=engine):
            result = tts_cli.run(args)
        self.assertEqual(result['reference_mode'], 'reference_plus_prompt')
        self.assertEqual(result['sample_rate'], 48000)
        request = json.loads(Path('requests.jsonl').read_text().splitlines()[0])
        self.assertEqual(request['prompt_text'], '参考录音')
        self.assertEqual(request['reference_mode'], 'reference_plus_prompt')

    def test_cli_combined_mode_rejects_diagnostics_without_task_creation(self):
        engine = self.vox_engine()
        args = argparse.Namespace(command='synth', text='你好。', file=None, voice='example',
                                  model='voxcpm2', speed=1.0, output='combined.wav',
                                  reference_mode='reference_plus_prompt', task_dir='task',
                                  diagnostic_log=str(Path('trace.jsonl').resolve()))
        with patch.object(tts_cli, 'engine_for', return_value=engine):
            with self.assertRaisesRegex(ValueError, '暂不支持诊断日志'):
                tts_cli.run(args)
        self.assertFalse(Path('task').exists())
        self.assertFalse(Path('combined.wav').exists())
        self.assertFalse(engine.is_loaded())

    def test_preview_requires_model_and_exposes_selected_mode(self):
        with self.assertRaisesRegex(ValueError, '必须指定模型'):
            tts_cli.run(argparse.Namespace(command='parse', text='你好', file=None,
                                           reference_mode='reference_plus_prompt'))
        result = tts_cli.run(argparse.Namespace(command='parse', text='你好', file=None,
                                                model='voxcpm2', reference_mode='reference_plus_prompt'))
        self.assertEqual(result['provider_preview']['reference_mode'], 'reference_plus_prompt')
        with self.assertRaisesRegex(ValueError, '上下文不匹配'):
            mlx_engine.Qwen3TTSEngine().capabilities({'mode': 'reference_plus_prompt'})
        self.assertEqual(mlx_engine.VoxCPM2Engine().capabilities({'mode': 'reference_plus_prompt'})['emotion']['status'],
                         'unsupported')

    def test_reference_prompt_ignores_stale_voice_cache(self):
        engine = self.vox_engine()
        sf.write('other.wav', np.full(16000, .5), 16000)
        engine.clone_voice('other.wav', 'example', '旧逐字稿')
        engine.synthesize_standard(TTSRequest('组合', voice_id='example', reference_mode='reference_plus_prompt'))
        request = json.loads(Path('requests.jsonl').read_text().splitlines()[0])
        self.assertEqual(request['ref_audio'], str(Path('voices/example/ref.wav').resolve()))
        self.assertEqual(request['prompt_text'], '参考录音')

    def test_reference_prompt_rejects_missing_transcript_and_conflict(self):
        engine = self.vox_engine()
        for content in ('{"prompt_text": ""}', '{"prompt_text": null}', '{}'):
            Path('voices/example/meta.json').write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                engine.reference_identity('example')
            self.assertFalse(engine.is_loaded())
        Path('voices/example/meta.json').write_text('{"prompt_text": "参考录音"}')
        with self.assertRaisesRegex(ValueError, '冲突'):
            engine.compile_input(TTSRequest('你好', voice_id='example', reference_mode='reference_plus_prompt',
                                             emotion_spans=[(0, 2, 'happy')], accepted_controls=('emotion',)))
        self.assertFalse(engine.is_loaded())
        with self.assertRaisesRegex(ValueError, '不支持'):
            self.engine().compile_input(TTSRequest('你好', voice_id='example', reference_mode='reference_plus_prompt'))

    def test_reference_prompt_asset_change_fails_before_generation(self):
        engine = self.vox_engine()
        fingerprint = engine.reference_identity('example')
        Path('voices/example/meta.json').write_text('{"prompt_text": "已变更"}')
        with self.assertRaisesRegex(ValueError, '身份不一致'):
            engine.synthesize_standard(TTSRequest('你好', voice_id='example',
                                                  reference_mode='reference_plus_prompt',
                                                  reference_fingerprint=fingerprint))
        self.assertFalse(engine.is_loaded())
        self.assertFalse(Path('requests.jsonl').exists())

    def test_reference_prompt_asset_change_during_generation_fails(self):
        engine = self.vox_engine()
        fingerprint = engine.reference_identity('example')
        with patch.object(engine, 'open_session'), patch.object(engine, '_call') as call:
            def change_reference(*args, **kwargs):
                Path('voices/example/meta.json').write_text('{"prompt_text": "已变更"}')
                return {'audio': os.environ['FAKE_WAV']}
            call.side_effect = change_reference
            with self.assertRaisesRegex(ValueError, '发生变化或不可用'):
                engine.synthesize_standard(TTSRequest('你好', voice_id='example',
                                                      reference_mode='reference_plus_prompt',
                                                      reference_fingerprint=fingerprint))
        self.assertEqual(call.call_count, 1)

    def test_reference_prompt_audio_change_during_generation_fails(self):
        engine = self.vox_engine()
        fingerprint = engine.reference_identity('example')
        with patch.object(engine, 'open_session'), patch.object(engine, '_call') as call:
            def change_reference(*args, **kwargs):
                Path('voices/example/ref.wav').write_bytes(b'changed')
                return {'audio': os.environ['FAKE_WAV']}
            call.side_effect = change_reference
            with self.assertRaisesRegex(ValueError, '发生变化或不可用'):
                engine.synthesize_standard(TTSRequest('你好', voice_id='example',
                                                      reference_mode='reference_plus_prompt',
                                                      reference_fingerprint=fingerprint))
        self.assertEqual(call.call_count, 1)

    def test_worker_combined_mode_validates_and_maps_inputs(self):
        worker_module = importlib.import_module('app.engines.mlx.worker')
        cases = [
            ('vox', {'reference_mode': 'reference_plus_prompt', 'prompt_text': '逐字稿'}, True),
            ('vox', {'reference_mode': 'reference_plus_prompt', 'prompt_text': ''}, False),
            ('vox', {'reference_mode': 'reference_plus_prompt', 'prompt_text': '逐字稿', 'ref_audio': None}, False),
            ('vox', {'reference_mode': 'reference_plus_prompt', 'prompt_text': '逐字稿', 'instruct': 'happy'}, False),
            ('qwen', {'reference_mode': 'reference_plus_prompt', 'prompt_text': '逐字稿'}, False),
            ('vox', {'prompt_text': '逐字稿'}, False),
            ('vox', {'reference_mode': 'other'}, False),
        ]
        for kind, extra, success in cases:
            with self.subTest(kind=kind, extra=extra):
                calls = []
                class Result:
                    sample_rate = 24000
                    audio = np.zeros(16, dtype=np.float32)
                class Model:
                    def generate(self, **kwargs):
                        calls.append(kwargs)
                        yield Result()
                class Thread:
                    def __init__(self, **kwargs):
                        pass
                    def start(self):
                        pass
                core = types.ModuleType('mlx.core')
                core.eval = lambda value: None
                mlx = types.ModuleType('mlx')
                mlx.core = core
                utils = types.ModuleType('mlx_audio.tts.utils')
                utils.load_model = lambda path: Model()
                audio_package = types.ModuleType('mlx_audio')
                audio_package.__path__ = []
                tts_package = types.ModuleType('mlx_audio.tts')
                tts_package.__path__ = []
                modules = {'mlx': mlx, 'mlx.core': core, 'mlx_audio': audio_package,
                           'mlx_audio.tts': tts_package, 'mlx_audio.tts.utils': utils}
                request = {'version': 1, 'id': 1, 'operation': 'synthesize',
                           'text': '你好', 'ref_audio': '/ref.wav', 'ref_text': '参考录音', **extra}
                stream = types.SimpleNamespace(buffer=io.BytesIO((json.dumps(request) + '\n').encode()))
                read_fd, write_fd = os.pipe()
                try:
                    with temporary_modules(modules), patch.object(worker_module.threading, 'Thread', Thread), \
                            patch.object(sys, 'argv', ['worker', str(write_fd), '/weights', kind, str(os.getpid())]), \
                            patch.object(sys, 'stdin', stream):
                        self.assertEqual(worker_module.main(), 0)
                    os.close(write_fd)
                    write_fd = None
                    with os.fdopen(read_fd, 'rb') as response:
                        read_fd = None
                        length = struct.unpack('!I', response.read(4))[0]
                        self.assertTrue(json.loads(response.read(length))['ok'])
                        length = struct.unpack('!I', response.read(4))[0]
                        reply = json.loads(response.read(length))
                        self.assertEqual(reply['ok'], success)
                        if not success:
                            self.assertNotIn('逐字稿', json.dumps(reply, ensure_ascii=False))
                finally:
                    if read_fd is not None:
                        os.close(read_fd)
                    if write_fd is not None:
                        os.close(write_fd)
                if success:
                    self.assertEqual(calls[0]['ref_audio'], '/ref.wav')
                    self.assertEqual(calls[0]['prompt_audio'], '/ref.wav')
                    self.assertEqual(calls[0]['prompt_text'], '逐字稿')
                    self.assertNotIn('ref_text', calls[0])
                else:
                    self.assertEqual(calls, [])

    def test_worker_exit_and_invalid_audio_stop_worker(self):
        for text, message in (('exit', '退出'), ('invalid-audio', '音频无效')):
            with self.subTest(text=text):
                engine = self.engine()
                with self.assertRaisesRegex((ValueError, RuntimeError), message):
                    engine.synthesize(TTSRequest(text, voice_id='example'))
                self.assertFalse(engine.is_loaded())

    def test_api_rejects_unsupported_markup_before_loading(self):
        api = importlib.import_module('app.api.tts')
        engine = self.engine()
        with patch.object(api, 'load_config', return_value={'engine': 'qwen3tts', 'voices_dir': './voices'}), \
                patch.object(api, 'get_engine', return_value=engine):
            with self.assertRaises(api.HTTPException) as failure:
                api.tts(api.TTSBody(text='你好[break 500]世界', voice_id='example'))
        self.assertEqual(failure.exception.status_code, 400)
        self.assertIn('停顿', failure.exception.detail)
        self.assertFalse(engine.is_loaded())

    def test_api_combined_mode_and_emotion_conflict(self):
        api = importlib.import_module('app.api.tts')
        engine = self.vox_engine()
        with patch.object(api, 'load_config', return_value={'engine': 'voxcpm2', 'voices_dir': './voices'}), \
                patch.object(api, 'get_engine', return_value=engine):
            preview = api.parse_preview(api.ParseBody(text='<emotion type="happy">你好</emotion>',
                                                      model='voxcpm2', reference_mode='reference_plus_prompt'))
            self.assertIn('冲突', preview['provider_preview']['error'])
            self.assertNotIn('参考录音', json.dumps(preview, ensure_ascii=False))
            body = api.TTSBody(text='<emotion type="happy">你好</emotion>', voice_id='example',
                               reference_mode='reference_plus_prompt', accept_controls=['emotion'])
            with self.assertRaises(api.HTTPException) as failure:
                api.tts(body)
            self.assertEqual(failure.exception.status_code, 400)
            self.assertIn('冲突', failure.exception.detail)
            self.assertFalse(engine.is_loaded())
            body = api.TTSBody(text='<emotion type="happy">你好</emotion>', voice_id='example',
                               reference_mode='reference_plus_prompt', drop_controls=['emotion'])
            response = api.tts(body)
            self.assertEqual(response.headers['x-tts-reference-mode'], 'reference_plus_prompt')
            request = json.loads(Path('requests.jsonl').read_text().splitlines()[0])
            self.assertEqual(request['prompt_text'], '参考录音')
            self.assertNotIn('instruct', request)

    def test_missing_reference_transcript_for_qwen(self):
        Path('voices/example/meta.json').write_text('{"prompt_text": ""}')
        engine = self.engine()
        with self.assertRaisesRegex(ValueError, '逐字文本'):
            engine.synthesize(TTSRequest('你好', voice_id='example'))
        self.assertFalse(engine.is_loaded())


if __name__ == '__main__':
    unittest.main()
