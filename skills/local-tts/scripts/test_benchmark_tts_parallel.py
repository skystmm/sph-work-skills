import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'backend'
        self.root.mkdir()
        shutil.copytree(ROOT / 'app', self.root / 'app', ignore=shutil.ignore_patterns('__pycache__'))
        (self.root / 'scripts').mkdir()
        shutil.copy(ROOT / 'scripts/benchmark_tts_parallel.py', self.root / 'scripts')
        (self.root / 'config.yaml').write_text('voices_dir: ./voices\n', encoding='utf-8')
        voice = self.root / 'voices/test-voice'
        voice.mkdir(parents=True)
        (voice / 'ref.wav').write_bytes(b'mock does not decode reference')
        (voice / 'meta.json').write_text(json.dumps({'prompt_text': '测试'}), encoding='utf-8')
        self.source = self.root / 'input.txt'
        self.source.write_text('这是一段用于验证合成基准的示例文字。', encoding='utf-8')

    def invoke(self, voice):
        return subprocess.run([sys.executable, str(self.root / 'scripts/benchmark_tts_parallel.py'),
                               '--file', str(self.source), '--output-dir', str(self.root / 'result'),
                               '--voice', voice, '--workers', '1', '--timeout', '30', '--mock'],
                              capture_output=True, text=True, timeout=45)

    def test_configurable_voice_with_mock(self):
        result = self.invoke('test-voice')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.root / 'result/report.json').read_text())
        self.assertTrue(report['success'])
        self.assertEqual(report['voice'], 'test-voice')
        self.assertTrue(all(item['status'] == 'success' for item in report['results']))

    def test_voice_traversal_rejected(self):
        self.assertNotEqual(self.invoke('../other').returncode, 0)
        self.assertFalse((self.root / 'result').exists())


if __name__ == '__main__':
    unittest.main()
