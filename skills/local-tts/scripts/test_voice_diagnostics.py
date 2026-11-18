import argparse
import base64
import contextlib
import hashlib
import importlib
import io
import json
import os
import stat
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import tts_cli as cli
from app.engines.base import TTSRequest
from app.engines.mlx import engine as mlx_engine
from app.engines.mlx.diagnostics import DiagnosticLog

FAKE_WORKER = r'''
import hashlib, json, os, struct, sys
fd = int(sys.argv[1])
def send(value):
    payload = json.dumps(value).encode()
    frame = struct.pack('!I', len(payload)) + payload
    while frame:
        frame = frame[os.write(fd, frame):]
def digest(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()
send({'version': 1, 'id': 0, 'ok': True})
for line in sys.stdin.buffer:
    request = json.loads(line)
    if request['operation'] == 'close':
        send({'version': 1, 'id': request['id'], 'ok': True})
        break
    with open(os.environ['DIAGNOSTIC_MARKER'], 'a') as f:
        f.write('on\n' if 'diagnostic' in request else 'off\n')
    mode = os.environ.get('DIAGNOSTIC_FAKE_MODE', '')
    if mode == 'crash':
        os._exit(1)
    diagnostic = request.get('diagnostic')
    response = {'version': 1, 'id': request['id'], 'ok': True, 'audio': os.environ['DIAGNOSTIC_FAKE_WAV']}
    if diagnostic is not None:
        before = digest(request['ref_audio'])
        if mode == 'mutate':
            with open(request['ref_audio'], 'ab') as f:
                f.write(b'changed')
        after = digest(request['ref_audio'])
        context = dict(diagnostic)
        if mode == 'bad_context':
            context['step_index'] += 1
        response['diagnostic'] = {'context': context, 'before_sha256': before,
                                  'after_sha256': after, 'elapsed_ms': 2}
        if mode == 'error':
            response['ok'] = False
            response['error'] = 'RuntimeError'
            response.pop('audio')
    send(response)
'''


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        os.chdir(self.base)
        Path('config.yaml').write_text('voices_dir: ./voices\n', encoding='utf-8')
        Path('voices/example').mkdir(parents=True)
        sf.write('voices/example/ref.wav', np.full(2400, .1), 24000)
        Path('voices/example/meta.json').write_text(json.dumps({'prompt_text': '秘密参考转写'}), encoding='utf-8')
        self.ref = Path('voices/example/ref.wav').resolve()
        self.ref_hash = hashlib.sha256(self.ref.read_bytes()).hexdigest()
        Path('fake_worker.py').write_text(FAKE_WORKER, encoding='utf-8')
        buffer = io.BytesIO()
        sf.write(buffer, np.full(80, .2), 48000, format='WAV')
        self.wav = buffer.getvalue()
        self.marker = self.base / 'marker'
        self.env = patch.dict(os.environ, {'DIAGNOSTIC_MARKER': str(self.marker),
                                           'DIAGNOSTIC_FAKE_WAV': base64.b64encode(self.wav).decode()}, clear=False)
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
        engine = mlx_engine.VoxCPM2Engine()
        engine.python = Path(sys.executable)
        engine._check_paths = lambda: None
        self.engines.append(engine)
        return engine

    def log(self, name='trace.jsonl'):
        return DiagnosticLog(self.base / name, 'example', self.ref_hash)

    def records(self, name='trace.jsonl'):
        return [json.loads(line) for line in (self.base / name).read_text().splitlines()]

    def request(self):
        return TTSRequest(text='秘密文本', voice_id='example', breaks=[(2, 500)])

    def test_opt_in_records_correlated_steps_without_private_content(self):
        engine = self.engine()
        with self.log() as trace, engine.diagnostic_chunk(trace, 4):
            data = engine.synthesize_standard(self.request())
        audio, rate = sf.read(io.BytesIO(data), dtype='float32')
        self.assertEqual(rate, 48000)
        self.assertEqual(len(audio), 80 * 2 + 24000)
        self.assertTrue(np.all(audio[80:24080] == 0))
        rows = self.records()
        self.assertEqual([r['event'] for r in rows], ['run_start', 'step_start', 'step_finish',
                                                        'step_silence', 'step_start', 'step_finish', 'diagnostic_finish'])
        starts = [r for r in rows if r['event'] == 'step_start']
        finishes = [r for r in rows if r['event'] == 'step_finish']
        self.assertEqual([r['step_index'] for r in starts], [0, 2])
        self.assertEqual([r['chunk_index'] for r in starts], [4, 4])
        self.assertEqual([r['chunk_number'] for r in starts], [5, 5])
        self.assertEqual(len({r['attempt_id'] for r in starts}), 1)
        self.assertEqual([r['worker_request_id'] for r in starts], [r['worker_request_id'] for r in finishes])
        self.assertTrue(all(r['before_sha256'] == r['after_sha256'] == self.ref_hash for r in finishes))
        self.assertEqual([r['outcome'] for r in finishes], ['success', 'success'])
        self.assertEqual(next(r for r in rows if r['event'] == 'step_silence')['milliseconds'], 500)
        text = (self.base / 'trace.jsonl').read_text()
        for secret in ('秘密文本', '秘密参考转写', str(self.ref), base64.b64encode(self.wav).decode()):
            self.assertNotIn(secret, text)
        self.assertEqual(stat.S_IMODE((self.base / 'trace.jsonl').stat().st_mode), 0o600)

    def test_default_call_sends_no_diagnostics(self):
        self.engine().synthesize_standard(TTSRequest(text='秘密文本', voice_id='example'))
        self.assertEqual(self.marker.read_text(), 'off\n')
        self.assertFalse((self.base / 'trace.jsonl').exists())

    def test_silence_step_is_not_reported_as_model_call(self):
        engine = self.engine()
        with self.log() as trace, engine.diagnostic_chunk(trace, 2):
            data = engine.synthesize_standard(TTSRequest(text='', breaks=[(0, 500)], voice_id='example'))
        audio, rate = sf.read(io.BytesIO(data), dtype='float32')
        self.assertEqual((len(audio), rate), (24000, 48000))
        self.assertTrue(np.all(audio == 0))
        self.assertFalse(self.marker.exists())
        self.assertFalse(engine.is_loaded())
        self.assertEqual([r['event'] for r in self.records()], ['run_start', 'step_silence', 'diagnostic_finish'])

    def test_diagnostic_voice_must_match_request_before_loading(self):
        engine = self.engine()
        with self.assertRaisesRegex(ValueError, '音色 ID'):
            with self.log() as trace, engine.diagnostic_chunk(trace, 0):
                engine.synthesize_standard(TTSRequest(text='测试', voice_id='other'))
        self.assertFalse(engine.is_loaded())
        self.assertFalse(self.marker.exists())

    def test_reference_plus_prompt_rejects_diagnostics_before_worker(self):
        engine = self.engine()
        request = TTSRequest(text='测试', voice_id='example', reference_mode='reference_plus_prompt')
        with self.assertRaisesRegex(ValueError, '暂不支持诊断日志'):
            with self.log() as trace, engine.diagnostic_chunk(trace, 0):
                engine.synthesize_standard(request)
        self.assertFalse(engine.is_loaded())
        self.assertFalse(self.marker.exists())
        text = (self.base / 'trace.jsonl').read_text()
        self.assertNotIn('秘密参考转写', text)
        self.assertNotIn('测试', text)

    def test_changed_reference_aborts_and_records_mismatch(self):
        engine = self.engine()
        with patch.dict(os.environ, {'DIAGNOSTIC_FAKE_MODE': 'mutate'}):
            with self.assertRaisesRegex(RuntimeError, '参考音频哈希'):
                with self.log() as trace, engine.diagnostic_chunk(trace, 0):
                    engine.synthesize_standard(TTSRequest(text='测试', voice_id='example'))
        row = next(r for r in self.records() if r['event'] == 'step_finish')
        self.assertEqual(row['outcome'], 'reference_mismatch')
        self.assertEqual(row['before_sha256'], self.ref_hash)
        self.assertNotEqual(row['after_sha256'], self.ref_hash)
        self.assertFalse(engine.is_loaded())

    def test_protocol_mismatch_and_worker_exit_are_unknown(self):
        for mode in ('bad_context', 'crash'):
            with self.subTest(mode=mode):
                engine = self.engine()
                name = mode + '.jsonl'
                with patch.dict(os.environ, {'DIAGNOSTIC_FAKE_MODE': mode}):
                    with self.assertRaises(RuntimeError):
                        with self.log(name) as trace, engine.diagnostic_chunk(trace, 0):
                            engine.synthesize_standard(TTSRequest(text='测试', voice_id='example'))
                row = next(r for r in self.records(name) if r['event'] == 'step_finish')
                self.assertEqual(row['outcome'], 'unknown')
                self.assertFalse(engine.is_loaded())

    def test_worker_failure_is_recorded_without_exception_text(self):
        engine = self.engine()
        with patch.dict(os.environ, {'DIAGNOSTIC_FAKE_MODE': 'error'}):
            with self.assertRaisesRegex(RuntimeError, '模型子进程'):
                with self.log() as trace, engine.diagnostic_chunk(trace, 0):
                    engine.synthesize_standard(TTSRequest(text='秘密文本', voice_id='example'))
        row = next(r for r in self.records() if r['event'] == 'step_finish')
        self.assertEqual(row['outcome'], 'error')
        self.assertNotIn('RuntimeError', (self.base / 'trace.jsonl').read_text())

    def test_start_log_failure_prevents_dispatch(self):
        engine = self.engine()
        engine.load()
        with self.log() as trace:
            original = trace.record
            def fail_start(event, **fields):
                if event == 'step_start':
                    raise RuntimeError('simulated log failure')
                return original(event, **fields)
            with patch.object(trace, 'record', side_effect=fail_start):
                with self.assertRaisesRegex(RuntimeError, 'simulated'):
                    with engine.diagnostic_chunk(trace, 0):
                        engine.synthesize_standard(TTSRequest(text='测试', voice_id='example'))
        self.assertFalse(self.marker.exists())
        self.assertFalse(engine.is_loaded())

    def test_finish_log_failure_prevents_result(self):
        engine = self.engine()
        with self.assertRaisesRegex(RuntimeError, 'simulated'):
            with self.log() as trace:
                original = trace.record
                def fail_finish(event, **fields):
                    if event == 'step_finish':
                        raise RuntimeError('simulated log failure')
                    return original(event, **fields)
                with patch.object(trace, 'record', side_effect=fail_finish):
                    with engine.diagnostic_chunk(trace, 0):
                        engine.synthesize_standard(TTSRequest(text='测试', voice_id='example'))
        self.assertTrue(self.marker.exists())
        self.assertEqual([r['event'] for r in self.records()], ['run_start', 'step_start', 'diagnostic_finish'])
        self.assertFalse(engine.is_loaded())

    def test_log_destination_is_private_exclusive_and_not_symlink(self):
        with self.log():
            pass
        with self.assertRaises(FileExistsError):
            self.log()
        Path('link.jsonl').symlink_to('trace.jsonl')
        with self.assertRaises(FileExistsError):
            self.log('link.jsonl')
        Path('public').mkdir(mode=0o755)
        os.chmod('public', 0o755)
        with self.assertRaisesRegex(ValueError, '仅用户可访问'):
            DiagnosticLog(self.base / 'public/trace.jsonl', 'example', self.ref_hash)
        with self.assertRaisesRegex(ValueError, '绝对'):
            DiagnosticLog('relative.jsonl', 'example', self.ref_hash)

    def test_logger_rejects_sensitive_unrecognized_fields(self):
        with self.log() as trace:
            with self.assertRaisesRegex(ValueError, '字段'):
                trace.record('step_start', text='秘密文本')
            with self.assertRaisesRegex(ValueError, '字段'):
                trace.record('step_start', outcome='秘密参考转写')

    def test_resumed_chunks_report_reused_without_inference(self):
        engine = self.engine()
        chunks = [{'text': '甲', 'overrides': {}, 'breaks': [], 'emphasis_spans': []},
                  {'text': '乙', 'overrides': {}, 'breaks': [], 'emphasis_spans': []}]
        with cli.task_lock('task') as directory, self.log('first.jsonl') as trace:
            cli.task_pieces(directory, {'version': 1}, chunks, engine, 'example', 1.0, diagnostic=trace)
        marker = self.marker.read_text()
        with cli.task_lock('task') as directory, self.log('second.jsonl') as trace:
            cli.task_pieces(directory, {'version': 1}, chunks, engine, 'example', 1.0, diagnostic=trace)
        self.assertEqual(marker, self.marker.read_text())
        rows = self.records('second.jsonl')
        reused = [r for r in rows if r['event'] == 'chunk_reused']
        self.assertEqual([r['chunk_number'] for r in reused], [1, 2])
        self.assertTrue(all(r['observation'] == 'reused_not_observed' for r in reused))
        self.assertFalse(any(r['event'] == 'step_start' for r in rows))

    def test_cli_end_to_end_records_new_then_reused_chunks(self):
        engine = self.engine()
        args = argparse.Namespace(command='synth', text='测试。' * 35, file=None, stdin=False,
                                  voice='example', model='voxcpm2', speed=1.0,
                                  output=str(self.base / 'out.wav'), output_dir=None,
                                  task_dir=str(self.base / 'task'), diagnostic_log=str(self.base / 'first.jsonl'))
        with patch.object(cli, 'engine_for', return_value=engine), \
                patch.object(cli, 'model_identity', return_value={'model': 'test'}), \
                patch.object(cli, 'runtime_identity', return_value={'runtime': 'test'}), \
                patch.object(engine, 'runtime_identity', return_value={'adapter': 'test'}):
            first = cli.run(args)
            self.assertGreater(first['chunks'], 1)
            self.assertTrue(Path(first['output']).is_file())
            args.output = str(self.base / 'resumed.wav')
            args.diagnostic_log = str(self.base / 'second.jsonl')
            second = cli.run(args)
        self.assertEqual(first['chunks'], second['chunks'])
        first_rows = self.records('first.jsonl')
        second_rows = self.records('second.jsonl')
        self.assertEqual(sum(r['event'] == 'chunk_saved' for r in first_rows), first['chunks'])
        self.assertEqual(sum(r['event'] == 'chunk_reused' for r in second_rows), first['chunks'])
        self.assertFalse(any(r['event'] == 'step_start' for r in second_rows))
        self.assertTrue(all(r['task_reference_sha256'] == self.ref_hash for r in first_rows + second_rows))

    def test_cli_rejects_diagnostics_for_non_vox_model(self):
        args = argparse.Namespace(command='synth', text='你好', file=None, stdin=False,
                                  voice='example', model='mock', speed=1.0,
                                  output=str(self.base / 'out.wav'), output_dir=None,
                                  task_dir=str(self.base / 'task'), diagnostic_log=str(self.base / 'trace.jsonl'))
        with self.assertRaisesRegex(ValueError, '仅支持'):
            cli.run(args)
        self.assertFalse((self.base / 'trace.jsonl').exists())

    def test_cli_rejects_log_inside_any_task_or_equal_to_output(self):
        engine = self.engine()
        task = self.base / 'new-task'
        args = argparse.Namespace(command='synth', text='你好', file=None, stdin=False,
                                  voice='example', model='voxcpm2', speed=1.0,
                                  output=str(self.base / 'out.wav'), output_dir=None,
                                  task_dir=str(task), diagnostic_log=str(task / 'trace.jsonl'))
        old_task = self.base / 'old-task'
        old_task.mkdir()
        (old_task / '.lock').touch()
        (old_task / 'task.json').write_text('{}')
        with patch.object(cli, 'engine_for', return_value=engine):
            for path in (task / 'trace.jsonl', self.base / 'out.wav', old_task / 'trace.jsonl'):
                args.diagnostic_log = str(path)
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, '独立'):
                    cli.run(args)
        self.assertFalse(task.exists())
        self.assertEqual((old_task / 'task.json').read_text(), '{}')


class WorkerObservationTests(unittest.TestCase):
    def test_worker_hashes_reference_around_actual_model_call(self):
        worker = importlib.import_module('app.engines.mlx.worker')
        for mode in ('success', 'mutate', 'error'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                reference = Path(temp) / 'reference.wav'
                reference.write_bytes(b'original reference bytes')
                before = hashlib.sha256(reference.read_bytes()).hexdigest()
                seen = []

                class Result:
                    sample_rate = 48000
                    audio = np.zeros(16, dtype=np.float32)

                class Model:
                    def generate(self, **kwargs):
                        seen.append(kwargs)
                        if mode == 'mutate':
                            reference.write_bytes(b'changed reference bytes')
                        if mode == 'error':
                            raise ValueError('private exception')
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
                audio = types.ModuleType('mlx_audio')
                audio.__path__ = []
                tts = types.ModuleType('mlx_audio.tts')
                tts.__path__ = []
                utils = types.ModuleType('mlx_audio.tts.utils')
                utils.load_model = lambda path: Model()
                modules = {'mlx': mlx, 'mlx.core': core, 'mlx_audio': audio,
                           'mlx_audio.tts': tts, 'mlx_audio.tts.utils': utils}
                context = {'version': 1, 'run_id': 'a' * 32, 'attempt_id': 'b' * 32,
                           'chunk_index': 1, 'step_index': 2}
                request = {'version': 1, 'id': 4, 'operation': 'synthesize', 'text': '秘密文本',
                           'ref_audio': str(reference), 'diagnostic': context}
                stream = types.SimpleNamespace(buffer=io.BytesIO((json.dumps(request) + '\n').encode()))
                read_fd, write_fd = os.pipe()
                try:
                    with patch.dict(sys.modules, modules), patch.object(worker.threading, 'Thread', Thread), \
                            patch.object(sys, 'argv', ['worker', str(write_fd), '/weights', 'vox', str(os.getpid())]), \
                            patch.object(sys, 'stdin', stream):
                        self.assertEqual(worker.main(), 0)
                    os.close(write_fd)
                    write_fd = None
                    with os.fdopen(read_fd, 'rb') as response:
                        read_fd = None
                        for expected_id in (0, 4):
                            size = struct.unpack('!I', response.read(4))[0]
                            result = json.loads(response.read(size))
                            self.assertEqual(result['id'], expected_id)
                    self.assertEqual(result['ok'], mode != 'error')
                    self.assertEqual(result['diagnostic']['context'], context)
                    self.assertEqual(result['diagnostic']['before_sha256'], before)
                    self.assertEqual(result['diagnostic']['after_sha256'], hashlib.sha256(reference.read_bytes()).hexdigest())
                    self.assertNotIn('diagnostic', seen[0])
                    self.assertNotIn('private exception', json.dumps(result))
                finally:
                    if read_fd is not None:
                        os.close(read_fd)
                    if write_fd is not None:
                        os.close(write_fd)


if __name__ == '__main__':
    unittest.main()
