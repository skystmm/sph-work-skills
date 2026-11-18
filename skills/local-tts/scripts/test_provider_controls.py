import argparse
import base64
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT))
import tts_cli as cli
from app.engines.base import ProviderPlan, ProviderStep, TTSRequest
from app.engines.mlx.engine import VoxCPM2Engine
from app.parser.chunker import chunk_text_with_metadata, validate_chunk_plan
from app.parser.ssml import apply_confirmed_drops, parse_ssml


class StandardInputTests(unittest.TestCase):
    def test_standard_ir_preserves_unicode_and_source_positions(self):
        text = '﻿🙂请[重=chong2]新。[break 500]<emotion type="平静、温柔">你好世界。</emotion>'
        doc = parse_ssml(text)
        self.assertEqual(doc.plain_text, '🙂请重新。你好世界。')
        self.assertEqual(doc.phoneme_overrides, {2: 'chong2'})
        self.assertEqual(doc.breaks, [(5, 500)])
        self.assertEqual(doc.emotion_spans, [(5, 10, '平静、温柔')])
        self.assertEqual([span.kind for span in doc.control_spans], ['phoneme', 'break', 'emotion'])
        self.assertEqual(text[doc.control_spans[0].source_start:doc.control_spans[0].source_end], '[重=chong2]')
        self.assertEqual(doc.control_spans[-1].start, 5)
        self.assertEqual(doc.control_spans[-1].end, 10)

    def test_emotion_nesting_and_invalid_input_fail_closed(self):
        cases = [
            '<emotion type="happy">请[重=chong2]新</emotion>',
            '<emotion type="happy">你好[break 500]</emotion>',
            '<emotion type="happy">你好<break time="500ms"/></emotion>',
            '<emotion type="happy"><emotion type="sad">你好</emotion></emotion>',
            '<emphasis><emotion type="happy">你好</emotion></emphasis>',
            '<emotion type="">你好</emotion>',
            '<emotion type="happy)malicious(">你好</emotion>',
            '<emotion type="happy">你好',
            '[重=chong]',
            '[break 60001]',
        ]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_ssml(text)

    def test_emotion_remaps_across_multiple_chunks(self):
        doc = parse_ssml('<emotion type="happy">' + '你好。' * 36 + '</emotion>')
        chunks = chunk_text_with_metadata(doc.plain_text, max_len=40, min_len=10,
                                          emotion_spans=doc.emotion_spans)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(''.join(c['text'] for c in chunks), doc.plain_text)
        for chunk in chunks:
            self.assertEqual(chunk['emotion_spans'], [(0, len(chunk['text']), 'happy')])

    def test_chunk_plan_rejects_lost_text_and_control(self):
        doc = parse_ssml('请[重=chong2]新。[break 500]<emotion type="happy">太好了。</emotion>')
        chunks = chunk_text_with_metadata(doc.plain_text, doc.phoneme_overrides, doc.breaks,
                                          doc.emphasis_spans, max_len=40, min_len=10,
                                          emotion_spans=doc.emotion_spans)
        validate_chunk_plan(doc, chunks)
        corrupted = [dict(chunk) for chunk in chunks]
        corrupted[0]['text'] = corrupted[0]['text'][1:]
        with self.assertRaisesRegex(ValueError, '原文'):
            validate_chunk_plan(doc, corrupted)
        corrupted = [dict(chunk) for chunk in chunks]
        corrupted[0]['overrides'] = {}
        with self.assertRaisesRegex(ValueError, '控制项'):
            validate_chunk_plan(doc, corrupted)
        corrupted = [dict(chunk) for chunk in chunks]
        corrupted[-1]['emotion_spans'] = []
        with self.assertRaisesRegex(ValueError, '控制项'):
            validate_chunk_plan(doc, corrupted)

    def test_preview_uses_provider_conversion_without_loading(self):
        args = argparse.Namespace(command='parse', text='请[重=chong2]新。', file=None, model='voxcpm2')
        with patch.object(VoxCPM2Engine, 'load', side_effect=AssertionError('unexpected loading')):
            result = cli.run(args)
        preview = result['provider_preview']
        self.assertEqual(preview['steps'][0]['text'], '请{chong2}新。')
        self.assertEqual(preview['requires_confirmation'], ['phoneme'])

    def test_drops_require_explicit_type_and_retain_source_positions(self):
        doc = parse_ssml('请[重=chong2]新[break 500]开始。')
        modified, report = apply_confirmed_drops(doc, ['phoneme'])
        self.assertEqual(modified.plain_text, '请重新开始。')
        self.assertFalse(modified.phoneme_overrides)
        self.assertEqual(modified.breaks, [(3, 500)])
        self.assertEqual(report[0]['kind'], 'phoneme')
        self.assertEqual(doc.raw[report[0]['source_start']:report[0]['source_end']], '[重=chong2]')
        self.assertEqual(doc.phoneme_overrides, {1: 'chong2'})
        with self.assertRaises(ValueError):
            apply_confirmed_drops(doc, ['emotion'])


class VoxConversionTests(unittest.TestCase):
    def test_provider_contract_lifecycle_and_plan(self):
        from app.engines.mock_engine import MockEngine
        engine = MockEngine()
        document = parse_ssml('你好世界。')
        self.assertEqual(engine.id, 'mock')
        self.assertEqual(engine.contract_version, 1)
        self.assertEqual(engine.capability_context(), {'provider': 'mock', 'backend': 'mock', 'mode': 'reference',
                                                        'model_revision': None, 'model_sha256': None,
                                                        'runtime_version': None})
        with self.assertRaisesRegex(ValueError, '上下文不匹配'):
            engine.capabilities({'mode': 'hifi'})
        with patch.object(engine, 'load', side_effect=AssertionError('unexpected load')):
            self.assertIn('phoneme', engine.capabilities())
            self.assertEqual(engine.plan_chunks(document)[0]['text'], '你好世界。')
        self.assertIs(engine.open_session(), engine)
        self.assertTrue(engine.is_loaded())
        engine.close()
        engine.close()
        self.assertFalse(engine.is_loaded())

    def test_new_provider_owns_conversion_and_execution(self):
        from app.engines.mock_engine import MockEngine
        class NewProvider(MockEngine):
            def compile_input(self, request):
                return ProviderPlan(1, (ProviderStep(kind='speak', text='converted'),), request)
            def execute_plan(self, plan):
                return plan.steps[0].text.encode()
        self.assertEqual(NewProvider().synthesize_standard(TTSRequest('原文')), b'converted')

    def test_compile_is_model_owned_and_requires_confirmation(self):
        engine = VoxCPM2Engine()
        request = TTSRequest(text='重要。开心。', phoneme_overrides={0: 'zhong4'}, breaks=[(3, 500)],
                             emotion_spans=[(3, 6, 'happy')])
        with self.assertRaisesRegex(ValueError, '实验性'):
            engine.compile_input(request)
        request.accepted_controls = ('phoneme', 'emotion')
        plan = engine.compile_input(request)
        self.assertEqual([s.kind for s in plan.steps], ['speak', 'silence', 'speak'])
        self.assertEqual(plan.steps[0].text, '{zhong4}要。')
        self.assertEqual(plan.steps[0].source_map[0], (0, 1, 0, len('{zhong4}')))
        self.assertEqual(plan.steps[1].milliseconds, 500)
        self.assertEqual(plan.steps[2].instruction, 'happy')
        self.assertEqual(request.text, '重要。开心。')

    def test_silence_is_exact_and_instruct_is_scoped(self):
        engine = VoxCPM2Engine()
        request = TTSRequest(text='甲乙', breaks=[(1, 500)], emotion_spans=[(1, 2, 'happy')],
                             voice_id='test', accepted_controls=('emotion',))
        buf = io.BytesIO()
        sf.write(buf, np.full(8, .25, dtype=np.float32), 48000, format='WAV')
        response = {'audio': base64.b64encode(buf.getvalue()).decode('ascii')}
        with patch.object(engine, 'validate_voice'), patch.object(engine, '_voice', return_value=('/ref.wav', '')), \
                patch.object(engine, 'is_loaded', return_value=True), patch.object(engine, '_call', return_value=response) as call:
            result = engine.synthesize(request)
        samples, rate = sf.read(io.BytesIO(result), dtype='float32')
        self.assertEqual(rate, 48000)
        self.assertEqual(len(samples), 8 + 24000 + 8)
        self.assertTrue(np.all(samples[8:24008] == 0))
        self.assertNotIn('instruct', call.call_args_list[0].kwargs)
        self.assertEqual(call.call_args_list[1].kwargs['instruct'], 'happy')

    def test_cosyvoice_conversion_exposes_source_mapping(self):
        from app.engines.cosyvoice.engine import CosyVoice3Engine, to_tokens
        engine = CosyVoice3Engine()
        request = TTSRequest(text='甲重乙', phoneme_overrides={1: 'zhong4'}, emphasis_spans=[(0, 2)])
        with patch.object(engine, 'validate_request'):
            plan = engine.compile_input(request)
        step = plan.steps[0]
        mapped = [step.text[a:b] for _, _, a, b in step.source_map]
        self.assertEqual(mapped, ['甲', to_tokens('zhong4'), '乙'])
        self.assertEqual(request.text, '甲重乙')

    def test_macsay_uses_explicit_digital_silence(self):
        from app.engines.macsay import MacSayEngine
        engine = MacSayEngine()
        engine._loaded = True
        request = TTSRequest(text='甲乙', breaks=[(1, 500)], voice_id='default')
        def execute(command, **kwargs):
            if command[0] == 'afconvert':
                sf.write(command[-1], np.full(8, .25), 22050, format='WAV')
        with patch('app.engines.macsay.subprocess.run', side_effect=execute):
            data = engine.synthesize_standard(request)
        samples, rate = sf.read(io.BytesIO(data), dtype='float32')
        self.assertEqual(rate, 22050)
        self.assertEqual(len(samples), 8 + 11025 + 8)
        self.assertTrue(np.all(samples[8:11033] == 0))

    def test_mock_uses_explicit_digital_silence(self):
        from app.engines.mock_engine import MockEngine
        request = TTSRequest(text='甲乙', breaks=[(1, 500)], voice_id='test')
        audio, rate = sf.read(io.BytesIO(MockEngine().synthesize_standard(request)), dtype='float32')
        start = 11025
        self.assertEqual(rate, 22050)
        self.assertTrue(np.all(audio[start:start + 11025] == 0))

    def test_bad_pinyin_and_emotion_ranges_fail_before_inference(self):
        engine = VoxCPM2Engine()
        for req in (TTSRequest('你好', phoneme_overrides={2: 'hao3'}, accepted_controls=('phoneme',)),
                    TTSRequest('你好', breaks=[(1, 60000), (1, 1)]),
                    TTSRequest('你好', phoneme_overrides={0: 'hao'}, accepted_controls=('phoneme',)),
                    TTSRequest('请{chong2}新检查。'),
                    TTSRequest('你好', emotion_spans=[(0, 2, 'happy'), (1, 2, 'sad')],
                               accepted_controls=('emotion',))):
            with self.subTest(req=req), self.assertRaises(ValueError):
                engine.compile_input(req)


class RegenerationTests(unittest.TestCase):
    def setUp(self):
        self.old = Path.cwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        Path('config.yaml').write_text('engine: mock\nvoices_dir: ./voices\n', encoding='utf-8')
        sf.write('ref.wav', np.zeros(16000), 16000)
        cli.run(argparse.Namespace(command='voices', action='add', id='voice', name='声音',
                                   audio='ref.wav', prompt_text='参考文本', authorized=True))
        self.text = '测试长文内容。' * 20

    def tearDown(self):
        os.chdir(self.old)
        self.tmp.cleanup()

    def run_task(self, **changes):
        args = dict(command='synth', text=self.text, file=None, voice='voice', model='mock',
                    speed=1.0, output='original.wav', task_dir='task')
        args.update(changes)
        return cli.run(argparse.Namespace(**args))

    def test_regenerates_exactly_one_chunk_and_preserves_source(self):
        initial = self.run_task()
        self.assertGreater(initial['chunks'], 1)
        before = {p.name: p.read_bytes() for p in Path('task').iterdir() if p.is_file()}
        old_output = Path('original.wav').read_bytes()
        engine = cli.engine_for('mock')
        with patch.object(engine, 'synthesize', wraps=engine.synthesize) as synth:
            result = cli.run(argparse.Namespace(command='task', action='regenerate', source_task='task',
                                                chunk=2, task_dir='revision', output='revised.wav',
                                                accept_source_runtime_change=False, accept_control=None,
                                                drop_control=None))
        self.assertEqual(synth.call_count, 1)
        self.assertEqual(result['regenerated_chunk'], 2)
        self.assertEqual(Path('original.wav').read_bytes(), old_output)
        self.assertEqual({p.name: p.read_bytes() for p in Path('task').iterdir() if p.is_file()}, before)
        old = json.loads(Path('task/000000.json').read_text())
        new = json.loads(Path('revision/000000.json').read_text())
        self.assertEqual(old['sha256'], new['sha256'])
        manifest = json.loads(Path('revision/task.json').read_text())
        self.assertEqual(manifest['parent']['regenerate_chunk'], 2)
        self.assertTrue(Path('revised.wav').is_file())

    def test_regeneration_restores_missing_unchanged_chunk_without_resynthesizing(self):
        initial = self.run_task()
        self.run_task(output='revised.wav', task_dir='revision', source_task='task', regenerate_chunk=1)
        last = json.loads(Path('revision', f"{initial['chunks'] - 1:06d}.json").read_text())
        missing = Path('revision', last['sha256'] + '.wav')
        missing.unlink()
        engine = cli.engine_for('mock')
        with patch.object(engine, 'synthesize', wraps=engine.synthesize) as synth:
            self.run_task(output='resumed.wav', task_dir='revision', source_task='task', regenerate_chunk=1)
        self.assertEqual(synth.call_count, 0)
        self.assertEqual(cli.file_hash(missing), last['sha256'])

    def test_regeneration_rejects_corrupted_unchanged_chunk(self):
        initial = self.run_task()
        self.run_task(output='revised.wav', task_dir='revision', source_task='task', regenerate_chunk=1)
        last = json.loads(Path('revision', f"{initial['chunks'] - 1:06d}.json").read_text())
        damaged = Path('revision', last['sha256'] + '.wav')
        damaged.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, '音频路径冲突'):
            self.run_task(output='resumed.wav', task_dir='revision', source_task='task', regenerate_chunk=1)
        self.assertEqual(damaged.read_bytes(), b'corrupt')
        self.assertFalse(Path('resumed.wav').exists())

    def test_runtime_mismatch_requires_explicit_confirmation(self):
        with patch.object(cli, 'runtime_identity', return_value={'version': 'old'}):
            self.run_task()
        with patch.object(cli, 'runtime_identity', return_value={'version': 'new'}):
            with self.assertRaisesRegex(ValueError, 'accept-source-runtime-change'):
                self.run_task(output='revised.wav', task_dir='revision', source_task='task', regenerate_chunk=1)
            result = self.run_task(output='revised.wav', task_dir='revision', source_task='task',
                                   regenerate_chunk=1, accept_source_runtime_change=True)
        self.assertEqual(result['regenerated_chunk'], 1)
        manifest = json.loads(Path('revision/task.json').read_text())
        self.assertTrue(manifest['parent']['runtime_change_confirmed'])

    def test_task_inspection_and_legacy_migration(self):
        self.run_task()
        inspected = cli.run(argparse.Namespace(command='task', action='inspect', task_dir='task'))
        self.assertEqual(inspected['chunks'][0]['index'], 1)
        self.assertEqual(inspected['chunks'][0]['start_seconds'], 0.0)
        self.assertEqual(inspected['chunks'][0]['status'], 'verified')
        prior = json.loads(Path('task/task.json').read_text())
        prior['version'] = 1
        for key in ('accepted_controls', 'dropped_controls', 'emotion_spans', 'control_spans'):
            prior.pop(key, None)
        Path('task/task.json').write_text(json.dumps(prior, ensure_ascii=False))
        before = {p.name: p.read_bytes() for p in Path('task').iterdir() if p.is_file()}
        with self.assertRaisesRegex(ValueError, 'accept-source-runtime-change'):
            self.run_task(output='revised.wav', task_dir='revision', source_task='task', regenerate_chunk=2)
        self.run_task(output='revised.wav', task_dir='revision', source_task='task', regenerate_chunk=2,
                      accept_source_runtime_change=True)
        self.assertEqual({p.name: p.read_bytes() for p in Path('task').iterdir() if p.is_file()}, before)
        self.assertTrue(json.loads(Path('revision/task.json').read_text())['parent']['runtime_change_confirmed'])

    def test_legacy_mode_validation_is_fail_closed(self):
        self.run_task()
        prior = json.loads(Path('task/task.json').read_text())
        self.assertEqual(cli.task_reference_mode(prior), 'reference')
        legacy = dict(prior, version=1)
        legacy.pop('reference_mode', None)
        self.assertEqual(cli.task_reference_mode(legacy), 'reference')
        for invalid in (dict(prior, version=999), dict(prior, reference_mode='unknown'),
                        dict(prior, reference_mode='reference_plus_prompt'),
                        dict(prior, version=1, reference_mode='reference_plus_prompt')):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                cli.task_reference_mode(invalid)

    def test_reference_mode_cannot_be_changed_by_runtime_override(self):
        self.run_task()
        prior = json.loads(Path('task/task.json').read_text())
        meta = prior['voice_metadata']
        import hashlib
        fingerprint = {'audio_sha256': prior['voice_wav_sha256'],
                       'prompt_sha256': hashlib.sha256(meta['prompt_text'].encode()).hexdigest()}
        changed = dict(prior, model='voxcpm2', reference_mode='reference_plus_prompt',
                       reference_fingerprint=fingerprint)
        Path('cross').mkdir()
        before = {p.name: p.read_bytes() for p in Path('task').iterdir() if p.is_file()}
        with self.assertRaisesRegex(ValueError, '模式或身份不匹配'):
            cli.seed_regenerated_task(Path('cross'), 'task', 0, changed, True)
        self.assertEqual({p.name: p.read_bytes() for p in Path('task').iterdir() if p.is_file()}, before)
        self.assertEqual(list(Path('cross').iterdir()), [])

    def test_combined_task_resume_and_regenerate_inherit_mode(self):
        engine = cli.engine_for('mock')
        import hashlib
        fingerprint = {'audio_sha256': cli.file_hash(Path('voices/voice/ref.wav')),
                       'prompt_sha256': hashlib.sha256('参考文本'.encode()).hexdigest()}
        with patch.object(cli, 'engine_for', return_value=engine), \
                patch.object(engine, 'reference_identity', return_value=fingerprint), \
                patch.object(engine.capability, 'reference_modes', ('reference', 'reference_plus_prompt')):
            result = self.run_task(model='voxcpm2', reference_mode='reference_plus_prompt')
            self.assertEqual(result['reference_mode'], 'reference_plus_prompt')
            manifest = json.loads(Path('task/task.json').read_text())
            self.assertEqual(manifest['reference_mode'], 'reference_plus_prompt')
            self.assertEqual(manifest['reference_fingerprint'], fingerprint)
            inspected = cli.run(argparse.Namespace(command='task', action='inspect', task_dir='task'))
            self.assertEqual(inspected['reference_mode'], 'reference_plus_prompt')
            with patch.object(engine, 'synthesize', wraps=engine.synthesize) as synth:
                self.run_task(model='voxcpm2', reference_mode='reference_plus_prompt', output='resumed.wav')
                self.assertEqual(synth.call_count, 0)
            revised = cli.run(argparse.Namespace(command='task', action='regenerate', source_task='task',
                                               chunk=2, task_dir='revision', output='revised.wav',
                                               accept_source_runtime_change=False, accept_control=None,
                                               drop_control=None))
            self.assertEqual(revised['reference_mode'], 'reference_plus_prompt')
            self.assertEqual(json.loads(Path('revision/task.json').read_text())['reference_fingerprint'], fingerprint)
            with self.assertRaisesRegex(ValueError, '模式或身份不匹配'):
                self.run_task(model='voxcpm2', reference_mode='reference', output='wrong.wav',
                              task_dir='wrong', source_task='task', regenerate_chunk=1,
                              accept_source_runtime_change=True)
            self.assertFalse(Path('wrong.wav').exists())

    def test_cli_degradation_requires_specific_user_choice(self):
        with self.assertRaisesRegex(ValueError, '不支持情绪'):
            self.run_task(text='<emotion type="happy">你好</emotion>。')
        with self.assertRaisesRegex(ValueError, '不在当前输入'):
            self.run_task(text='你好。', drop_control=['emotion'])
        result = self.run_task(text='<emotion type="happy">你好</emotion>。', drop_control=['emotion'])
        self.assertEqual(result['dropped_controls'][0]['kind'], 'emotion')
        manifest = json.loads(Path('task/task.json').read_text())
        self.assertEqual(manifest['dropped_controls'], result['dropped_controls'])
        with self.assertRaisesRegex(ValueError, '不能同时'):
            self.run_task(text='<emotion type="happy">你好</emotion>。', output='other.wav',
                          drop_control=['emotion'], accept_control=['emotion'])

    def test_api_preview_uses_the_same_provider_conversion(self):
        from app.api import tts as api
        with patch.object(VoxCPM2Engine, 'load', side_effect=AssertionError('unexpected loading')):
            result = api.parse_preview(api.ParseBody(text='请[重=chong2]新。', model='voxcpm2'))
        self.assertEqual(result['provider_preview']['steps'][0]['text'], '请{chong2}新。')
        self.assertEqual(result['provider_preview']['requires_confirmation'], ['phoneme'])

    def test_api_shares_validation_and_explicit_degradation(self):
        from app.api import tts as api
        with patch.object(api, 'load_config', return_value={'engine': 'mock', 'voices_dir': './voices'}):
            with self.assertRaises(api.HTTPException) as failure:
                api.tts(api.TTSBody(text='<emotion type="happy">你好</emotion>', voice_id='voice'))
            self.assertEqual(failure.exception.status_code, 400)
            response = api.tts(api.TTSBody(text='<emotion type="happy">你好</emotion>', voice_id='voice',
                                          drop_controls=['emotion']))
            self.assertEqual(response.status_code, 200)
            self.assertIn('drop', response.headers['x-tts-dropped-controls'])
            with self.assertRaises(api.HTTPException) as nested:
                api.tts(api.TTSBody(text='<emotion type="happy">[重=chong2]</emotion>', voice_id='voice'))
            self.assertEqual(nested.exception.status_code, 400)

    def test_source_mismatch_and_corrupt_source_fail_closed(self):
        self.run_task()
        with self.assertRaisesRegex(ValueError, '不匹配'):
            self.run_task(text=self.text + '修改', output='revised.wav', task_dir='revision',
                          source_task='task', regenerate_chunk=1)
        record = json.loads(Path('task/000000.json').read_text())
        Path('task', record['sha256'] + '.wav').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            self.run_task(output='other.wav', task_dir='other-revision', source_task='task', regenerate_chunk=1)
        self.assertEqual(Path('task', record['sha256'] + '.wav').read_bytes(), b'corrupt')
        self.assertFalse(Path('other.wav').exists())


if __name__ == '__main__':
    unittest.main()
