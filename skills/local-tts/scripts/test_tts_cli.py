import argparse
import io
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
