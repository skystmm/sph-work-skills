import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tts_cli as cli
from app.parser.chunker import chunk_text_with_metadata


class CliTests(unittest.TestCase):
    def setUp(self):
        self.old = Path.cwd()
        self.temp = tempfile.TemporaryDirectory()
        os.chdir(self.temp.name)
        Path('config.yaml').write_text('engine: mock\nvoices_dir: ./voices\n', encoding='utf-8')
        sf.write('ref.wav', np.zeros(16000), 16000)
        cli.run(argparse.Namespace(command='voices', action='add', id='one', name='声音一', audio='ref.wav', prompt_text='参考文本', authorized=True))
        cli.run(argparse.Namespace(command='voices', action='add', id='two', name='声音二', audio='ref.wav', prompt_text='参考文本', authorized=True))

    def tearDown(self):
        os.chdir(self.old)
        self.temp.cleanup()

    def synth(self, voice=None, output='result.wav', text='你好世界。'):
        return cli.run(argparse.Namespace(command='synth', text=text, file=None, voice=voice, model='mock', speed=1.0, output=output))

    def test_voice_selection(self):
        with self.assertRaisesRegex(ValueError, '没有默认音色'):
            self.synth()
        cli.run(argparse.Namespace(command='voices', action='default', voice='one'))
        self.assertEqual(self.synth('two')['voice_id'], 'two')
        self.assertEqual(self.synth(output='second.wav')['voice_id'], 'one')
        self.assertEqual(cli.settings_read(Path('voices/.tts-cli.json'))['default_voice'], 'one')

    def test_host_output_directory_and_precedence(self):
        host = Path(self.temp.name).resolve() / 'host output'
        env_dir = Path(self.temp.name).resolve() / 'env output'
        ref_hash = cli.file_hash(Path('voices/one/ref.wav'))
        base = dict(command='synth', text='你好。', file=None, voice='one', model='mock', speed=1.0)
        with patch.dict(os.environ, {'VOICE_TTS_OUTPUT_DIR': str(env_dir)}):
            result = cli.run(argparse.Namespace(**base, output=None, output_dir=str(host)))
            self.assertEqual(Path(result['output']).parent, host)
            result = cli.run(argparse.Namespace(**base, output=None, output_dir=None))
            self.assertEqual(Path(result['output']).parent, env_dir)
            explicit = Path(self.temp.name).resolve() / 'named.wav'
            result = cli.run(argparse.Namespace(**base, output=str(explicit), output_dir=None))
            self.assertEqual(Path(result['output']), explicit)
            with self.assertRaisesRegex(ValueError, '不能同时'):
                cli.run(argparse.Namespace(**base, output=str(explicit), output_dir=str(host)))
            with self.assertRaisesRegex(ValueError, '已存在'):
                cli.run(argparse.Namespace(**base, output=str(explicit), output_dir=None))
        self.assertEqual(cli.file_hash(Path('voices/one/ref.wav')), ref_hash)
        self.assertFalse((host / 'voices').exists())
        self.assertFalse((host / 'models').exists())

    def test_host_output_rejects_relative_and_auto_cache_follows_output(self):
        base = dict(command='synth', text='你好。', file=None, voice='one', model='mock', speed=1.0, output=None)
        with self.assertRaisesRegex(ValueError, '绝对路径'):
            cli.run(argparse.Namespace(**base, output_dir='relative'))
        with patch.dict(os.environ, {'VOICE_TTS_OUTPUT_DIR': 'relative'}):
            with self.assertRaisesRegex(ValueError, '绝对路径'):
                cli.run(argparse.Namespace(**base, output_dir=None))
        host = Path(self.temp.name).resolve() / 'host'
        result = cli.run(argparse.Namespace(**base, output_dir=str(host), workers='auto', task_dir=None))
        self.assertEqual(Path(result['output']).parent, host)
        self.assertEqual(Path(result['task_dir']).parent, host)
        self.assertTrue((Path(result['task_dir']) / 'task.json').is_file())

    def test_registration_guards(self):
        for name in ('../outside', 'default', 'one'):
            with self.assertRaises(ValueError):
                cli.run(argparse.Namespace(command='voices', action='add', id=name, name='x', audio='ref.wav', prompt_text='x', authorized=True))
        with self.assertRaisesRegex(ValueError, '授权'):
            cli.run(argparse.Namespace(command='voices', action='add', id='three', name='x', audio='ref.wav', prompt_text='x', authorized=False))

    def test_long_text_and_break_mapping(self):
        chunks = chunk_text_with_metadata('甲乙丙丁戊', {2: 'bing3'}, [(0, 100), (2, 200), (2, 300), (5, 400)], [(1, 4)], max_len=2, min_len=0)
        self.assertEqual(''.join(c['text'] for c in chunks), '甲乙丙丁戊')
        self.assertEqual([b for c in chunks for b in c['breaks']], [(0, 100), (2, 500), (1, 400)])
        self.assertEqual(chunks[1]['overrides'], {0: 'bing3'})
        self.assertEqual(chunks[0]['emphasis_spans'], [(1, 2)])
        self.assertEqual(chunks[1]['emphasis_spans'], [(0, 2)])
        result = self.synth('one', text='你好世界。' * 45)
        self.assertGreater(result['chunks'], 1)
        self.assertTrue(Path(result['output']).is_file())

    def test_default_chunk_lengths_and_explicit_pause(self):
        engine = cli.engine_for('mock')
        text = ('甲' * 20 + '。') * 3
        with patch.object(engine, 'synthesize', wraps=engine.synthesize) as synthesize:
            result = self.synth('one', text=text)
        self.assertEqual(result['chunks'], 2)
        self.assertEqual([len(call.args[0].text) for call in synthesize.call_args_list], [42, 21])
        with patch.object(engine, 'synthesize', wraps=engine.synthesize) as synthesize:
            self.synth('one', output='pause.wav', text='甲乙[break 500]丙丁')
        self.assertEqual(len(synthesize.call_args_list), 2)
        self.assertEqual(synthesize.call_args_list[0].args[0].breaks, [(2, 500)])

    def test_whitespace_only_chunk_is_not_synthesized(self):
        engine = cli.engine_for('mock')
        with patch.object(engine, 'synthesize', wraps=engine.synthesize) as synthesize:
            self.synth('one', text='这是一段足够长的正文内容。\n')
        self.assertTrue(all(call.args[0].text.strip() for call in synthesize.call_args_list))

    def task_synth(self, text='你好世界。' * 45, output='result.wav', **kwargs):
        options = dict(command='synth', text=text, file=None, voice='one', model='mock',
                       speed=1.0, output=output, task_dir='task')
        options.update(kwargs)
        return cli.run(argparse.Namespace(**options))

    def test_interrupted_task_resumes_in_order(self):
        engine = cli.engine_for('mock')
        original = engine.synthesize
        calls = []
        def interrupted(req):
            calls.append(req.text)
            if len(calls) == 2:
                raise RuntimeError('中断')
            return original(req)
        text = '你好世界。' * 45 + '\n'
        with patch.object(engine, 'synthesize', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, '中断'):
                self.task_synth(text=text)
        self.assertFalse(Path('result.wav').exists())
        self.assertTrue(Path('task/000000.json').is_file())
        with patch.object(engine, 'synthesize', wraps=original) as synth:
            resumed = self.task_synth(text=text)
        self.assertEqual(synth.call_count, resumed['chunks'] - 1)
        self.assertTrue(all(call.args[0].text.strip() for call in synth.call_args_list))
        self.synth('one', output='direct.wav', text=text)
        a, sr = sf.read('result.wav', always_2d=True)
        b, sr2 = sf.read('direct.wav', always_2d=True)
        self.assertEqual(sr, sr2)
        np.testing.assert_array_equal(a, b)

    def test_task_identity_rejects_changes(self):
        self.task_synth(text='你好。')
        for changes in (dict(text='你好。 '), dict(text='<emphasis>你好</emphasis>。'),
                        dict(text='你好。[break 10]'), dict(voice='two'), dict(speed=1.1)):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, '身份不兼容'):
                self.task_synth(output='another.wav', **changes)
        meta = Path('voices/one/meta.json')
        saved = meta.read_text()
        info = json.loads(saved)
        info['prompt_text'] += '变化'
        meta.write_text(json.dumps(info))
        with self.assertRaisesRegex(ValueError, '身份不兼容'):
            self.task_synth(text='你好。', output='another.wav')
        meta.write_text(saved)
        sf.write('voices/one/ref.wav', np.ones(16000) * .1, 16000)
        with self.assertRaisesRegex(ValueError, '身份不兼容'):
            self.task_synth(text='你好。', output='another.wav')

    def test_model_file_and_configuration_identity(self):
        Path('weights').mkdir()
        Path('weights/model.bin').write_bytes(b'original')
        Path('config.yaml').write_text('engine: mock\nvoices_dir: ./voices\nengines:\n  mock:\n    model_path: ./weights\n')
        self.task_synth(text='你好。')
        Path('weights/model.bin').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '身份不兼容'):
            self.task_synth(text='你好。', output='another.wav')
        Path('weights/model.bin').write_bytes(b'original')
        with Path('config.yaml').open('a') as f:
            f.write('other: changed\n')
        with self.assertRaisesRegex(ValueError, '身份不兼容'):
            self.task_synth(text='你好。', output='another.wav')

    def test_corrupt_records_and_audio_fail_closed(self):
        self.task_synth(text='你好。')
        record = Path('task/000000.json')
        contents = record.read_bytes()
        record.write_text('{')
        with self.assertRaisesRegex(ValueError, '损坏'):
            self.task_synth(text='你好。', output='another.wav')
        record.write_bytes(contents)
        info = json.loads(contents)
        wav = Path('task') / (info['sha256'] + '.wav')
        wav.write_bytes(b'damaged')
        engine = cli.engine_for('mock')
        with patch.object(engine, 'synthesize', wraps=engine.synthesize) as synth:
            self.task_synth(text='你好。', output='another.wav')
        self.assertEqual(synth.call_count, 1)
        quarantined = list(Path('task').glob('*.wav.corrupt-*'))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), b'damaged')
        repaired = json.loads(record.read_text())
        repaired_wav = Path('task') / (repaired['sha256'] + '.wav')
        self.assertEqual(cli.file_hash(repaired_wav), repaired['sha256'])

    def test_lock_and_missing_chunk(self):
        self.task_synth(text='你好。')
        with cli.task_lock('task'):
            with self.assertRaisesRegex(ValueError, '其他进程'):
                self.task_synth(text='你好。', output='another.wav')
        record = json.loads(Path('task/000000.json').read_text())
        (Path('task') / (record['sha256'] + '.wav')).unlink()
        engine = cli.engine_for('mock')
        with patch.object(engine, 'synthesize', wraps=engine.synthesize) as synth:
            self.task_synth(text='你好。', output='another.wav')
        self.assertEqual(synth.call_count, 1)
        repaired = json.loads(Path('task/000000.json').read_text())
        self.assertTrue((Path('task') / (repaired['sha256'] + '.wav')).is_file())

    def test_repair_interruption_preserves_damaged_data(self):
        self.task_synth(text='你好。')
        record_path = Path('task/000000.json')
        original_record = record_path.read_bytes()
        record = json.loads(original_record)
        wav = Path('task') / (record['sha256'] + '.wav')
        wav.write_bytes(b'truncated')
        engine = cli.engine_for('mock')
        with patch.object(engine, 'synthesize', side_effect=RuntimeError('中断')):
            with self.assertRaisesRegex(RuntimeError, '中断'):
                self.task_synth(text='你好。', output='another.wav')
        self.assertEqual(record_path.read_bytes(), original_record)
        self.assertEqual(next(Path('task').glob('*.wav.corrupt-*')).read_bytes(), b'truncated')
        self.task_synth(text='你好。', output='another.wav')
        repaired = json.loads(record_path.read_text())
        self.assertTrue((Path('task') / (repaired['sha256'] + '.wav')).is_file())

    def test_task_audio_symlink_rejected(self):
        self.task_synth(text='你好。')
        record = json.loads(Path('task/000000.json').read_text())
        wav = Path('task') / (record['sha256'] + '.wav')
        wav.unlink()
        wav.symlink_to(Path('ref.wav').resolve())
        with self.assertRaisesRegex(ValueError, '不安全'):
            self.task_synth(text='你好。', output='another.wav')

    def test_existing_directory_and_output_are_preserved(self):
        Path('task').mkdir()
        Path('task/user-file').write_text('keep')
        with self.assertRaisesRegex(ValueError, '空目录'):
            self.task_synth(text='你好。')
        self.assertEqual(Path('task/user-file').read_text(), 'keep')
        Path('result.wav').write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, '已存在'):
            self.task_synth(text='你好。')
        self.assertEqual(Path('result.wav').read_bytes(), b'keep')

    def test_task_processor_saves_out_of_order(self):
        chunks = [{'text': str(i), 'overrides': {}, 'breaks': [], 'emphasis_spans': []} for i in range(3)]
        def processor(pending, save):
            for index, _ in reversed(pending):
                buf = io.BytesIO()
                sf.write(buf, np.full(20, index / 10), 24000, format='WAV', subtype='FLOAT')
                save(index, buf.getvalue())
        with cli.task_lock('task') as directory:
            pieces, rate = cli.task_pieces(directory, {'version': 1}, chunks, None, 'one', 1.0, processor)
        self.assertEqual(rate, 24000)
        self.assertEqual([round(float(data[0, 0]), 1) for data in pieces], [0.0, 0.1, 0.2])

    def test_task_processor_failure_keeps_completed(self):
        chunks = [{'text': str(i), 'overrides': {}, 'breaks': [], 'emphasis_spans': []} for i in range(2)]
        def processor(pending, save):
            buf = io.BytesIO()
            sf.write(buf, np.zeros(20), 24000, format='WAV')
            save(pending[0][0], buf.getvalue())
            raise RuntimeError('worker failed')
        with cli.task_lock('task') as directory, self.assertRaisesRegex(RuntimeError, 'worker failed'):
            cli.task_pieces(directory, {'version': 1}, chunks, None, 'one', 1.0, processor)
        self.assertTrue(Path('task/000000.json').is_file())
        self.assertFalse(Path('task/000001.json').exists())

    def test_auto_workers_fails_closed_and_records_task(self):
        result = self.task_synth(text='你好。', workers='auto')
        self.assertEqual(result['requested_workers'], 'auto')
        self.assertEqual(result['actual_workers'], 1)
        self.assertIn('未执行校准', result['worker_reason'])
        self.assertEqual(Path(result['task_dir']), Path('task').resolve())
        self.assertTrue(Path('task/000000.json').is_file())
        for workers in ('2', '4'):
            with self.subTest(workers=workers), self.assertRaisesRegex(ValueError, '尚未通过'):
                self.task_synth(text='你好。', output='unsupported.wav', workers=workers)
        self.assertFalse(Path('unsupported.wav').exists())

    def test_failed_generation_does_not_publish(self):
        with patch('tts_cli.engine_for', side_effect=ValueError('模型不可用')):
            with self.assertRaises(ValueError):
                self.synth('one')
        self.assertFalse(Path('result.wav').exists())

    def test_markup_and_output_guards(self):
        with self.assertRaises(ValueError):
            cli.parsed_input('<prosody rate="slow">你好</prosody>')
        self.synth('one')
        original = Path('result.wav').read_bytes()
        with self.assertRaises(ValueError):
            self.synth('one')
        self.assertEqual(Path('result.wav').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
