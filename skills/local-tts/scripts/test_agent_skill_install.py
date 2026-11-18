import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / '语音 project'
        (self.root / 'scripts').mkdir(parents=True)
        shutil.copy(ROOT / 'scripts/install_agent_skill.py', self.root / 'scripts')
        shutil.copy(ROOT / 'SKILL.md', self.root / 'SKILL.md')
        self.home = self.base / 'test home'
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, action):
        return subprocess.run([sys.executable, str(self.root / 'scripts/install_agent_skill.py'), action,
                               '--home', str(self.home)], text=True, capture_output=True, cwd=self.base)

    def test_install_repeat_and_uninstall(self):
        self.assertEqual(self.invoke('check').returncode, 1)
        self.assertEqual(self.invoke('install').returncode, 0)
        self.assertEqual(self.invoke('install').returncode, 0)
        result = self.invoke('check')
        self.assertEqual(result.returncode, 0)
        self.assertEqual([x['state'] for x in json.loads(result.stdout)['targets']], ['installed', 'installed'])
        skill = self.home / '.agents/skills/local-tts/SKILL.md'
        installed = skill.read_text(encoding='utf-8')
        self.assertIn(str(self.root), installed)
        for target in ('.agents', '.codebuddy'):
            text = (self.home / target / 'skills/local-tts/SKILL.md').read_text(encoding='utf-8')
            self.assertIn('用户未指定模型时，每次 `synth` 显式传入 `--model voxcpm2`', text)
            self.assertIn('用户指定模型时仅传入用户指定的模型', text)
            self.assertIn('不静默切换', text)
            self.assertIn('仅当用户明确选择时，VoxCPM 可在 `parse` 和 `synth` 使用 `--reference-mode reference_plus_prompt`', text)
            self.assertIn('默认模式仍为 `reference`', text)
        assignment = next(line for line in installed.splitlines() if line.startswith('VOICE_TTS_ROOT='))
        import os
        env = os.environ.copy()
        env.pop('VOICE_TTS_ROOT', None)
        resolved = subprocess.run(['sh', '-c', assignment + '\nprintf "%s" "$VOICE_TTS_ROOT"'],
                                  env=env, text=True, capture_output=True, check=True)
        self.assertEqual(Path(resolved.stdout), self.root.resolve())
        self.assertEqual(self.invoke('uninstall').returncode, 0)
        self.assertFalse(skill.exists())

    def test_conflict_and_user_modification_protected(self):
        target = self.home / '.codebuddy/skills/local-tts'
        target.mkdir(parents=True)
        (target / 'SKILL.md').write_text('existing user skill')
        self.assertEqual(self.invoke('install').returncode, 1)
        self.assertFalse((self.home / '.agents/skills/local-tts').exists())
        self.assertEqual((target / 'SKILL.md').read_text(), 'existing user skill')
        shutil.rmtree(target)
        self.assertEqual(self.invoke('install').returncode, 0)
        (target / 'SKILL.md').write_text('user changes')
        self.assertEqual(self.invoke('uninstall').returncode, 1)
        self.assertEqual((target / 'SKILL.md').read_text(), 'user changes')

    def test_outdated_removal(self):
        self.assertEqual(self.invoke('install').returncode, 0)
        with (self.root / 'SKILL.md').open('a') as f:
            f.write('\nnew source')
        self.assertEqual(self.invoke('install').returncode, 1)
        self.assertEqual(self.invoke('uninstall').returncode, 0)


if __name__ == '__main__':
    unittest.main()
