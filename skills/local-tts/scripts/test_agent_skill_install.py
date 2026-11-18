import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = 'SKILL.md'
COMMAND_SOURCE = 'commands'
ENTRY_SOURCE = 'entry-skills'


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "语音 project's files"
        (self.root / 'scripts').mkdir(parents=True)
        shutil.copy(ROOT / 'scripts/install_agent_skill.py', self.root / 'scripts')
        for relative in (MAIN_SOURCE, f'{COMMAND_SOURCE}/voice_clone.md',
                         f'{COMMAND_SOURCE}/voice_gen.md', f'{ENTRY_SOURCE}/voice-clone/SKILL.md',
                         f'{ENTRY_SOURCE}/voice-gen/SKILL.md'):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / relative, destination)
        self.home = self.base / 'test home'
        self.home.mkdir()
        self.paths = [self.home / path for path in (
            '.agents/skills/local-tts/SKILL.md', '.codebuddy/skills/local-tts/SKILL.md',
            '.claude/commands/voice_clone.md', '.claude/commands/voice_gen.md',
            '.agents/skills/voice-clone/SKILL.md', '.agents/skills/voice-gen/SKILL.md')]
        self.old_paths = [self.home / '.claude/commands/gen.md', self.home / '.agents/skills/gen/SKILL.md']

    def invoke(self, action):
        # Both HOME and --home are isolated; never run this test against the real home.
        env = dict(os.environ, HOME=str(self.home), PYTHONDONTWRITEBYTECODE='1')
        return subprocess.run([sys.executable, '-I', str(self.root / 'scripts/install_agent_skill.py'), action,
                               '--home', str(self.home)], env=env, text=True, capture_output=True, cwd=self.base)

    def module(self):
        spec = importlib.util.spec_from_file_location('installer_under_test', self.root / 'scripts/install_agent_skill.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def call(self, module, action='install'):
        output, error = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', ['installer', action, '--home', str(self.home)]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            result = module.main()
        return result, output.getvalue(), error.getvalue()

    def legacy_content(self, module, key):
        # Reproduce the PRE-RENAME renderer independently, including its root binding.
        body = module.LEGACY_SOURCES[key].rstrip() + '\n\n'
        body += '## 本机安装位置\n\n'
        if key == 'main':
            body += (f'本次安装的 TTS 项目根目录为 `{self.root}`。'
                     '如设置了 VOICE_TTS_ROOT，优先使用该变量；否则使用此目录。'
                     '使用根目录下的 .venv/bin/python 执行 scripts/tts_cli.py。'
                     '运行时同时检查 Python、CLI 和模型配置是否存在，不执行依赖安装。\n\n')
        else:
            body += (f'本次安装的 local-tts 根目录为 `{self.root}`。'
                     f'请先读取并遵循该安装的主技能文件 `{self.root / MAIN_SOURCE}`；设置 VOICE_TTS_ROOT 时使用该目录下的 `{MAIN_SOURCE}`。不要依赖技能列表中出现主技能。'
                     '如设置了 VOICE_TTS_ROOT，优先使用该变量；否则使用此目录。\n\n')
        body += '```bash\n' + f'VOICE_TTS_ROOT=${{VOICE_TTS_ROOT:-{shlex.quote(str(self.root))}}}\n'
        if key == 'main':
            body += '"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" voices list\n'
        body += '```\n'
        return self.sign(body)

    @staticmethod
    def sign(body):
        return body + '\n<!-- local-tts-install sha256=' + hashlib.sha256(body.encode()).hexdigest() + ' -->\n'

    def seed_legacy(self, module=None):
        module = module or self.module()
        files = dict(zip(self.paths[:3] + [self.paths[4]] + self.old_paths,
                         ['main', 'main', 'command-voice_clone', 'skill-voice-clone', 'command-gen', 'skill-gen']))
        expected = {}
        for path, key in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            expected[path] = self.legacy_content(module, key).encode()
            path.write_bytes(expected[path])
        return expected

    def home_snapshot(self):
        return {str(path.relative_to(self.home)): ('link', os.readlink(path)) if path.is_symlink()
                else ('dir',) if path.is_dir() else ('file', path.read_bytes(), path.stat().st_ino)
                for path in self.home.rglob('*')
                if not path.relative_to(self.home).parts[0].startswith('.local-tts-backup-')}

    def backups(self):
        return sorted(self.home.glob('.local-tts-backup-*'))

    def assert_originals_backed_up(self, expected):
        found = {}
        for backup in self.backups():
            manifest = json.loads((backup / 'manifest.json').read_text())
            self.assertEqual(manifest['root'], str(self.root))
            for item in manifest['originals']:
                path = backup / item['file']
                # A failed transaction's planned manifest may include unmoved targets.
                if path.exists():
                    self.assertTrue(path.is_file(), path)
                    found[Path(item['path'])] = path.read_bytes()
        for path, data in expected.items():
            self.assertEqual(found[path], data, path)

    def test_install_repeat_and_uninstall(self):
        self.assertEqual(self.invoke('check').returncode, 1)
        first = self.invoke('install')
        self.assertEqual(first.returncode, 0, first.stderr)
        inodes = [path.stat().st_ino for path in self.paths]
        self.assertEqual(self.invoke('install').returncode, 0)
        self.assertEqual([path.stat().st_ino for path in self.paths], inodes)
        self.assertEqual(self.backups(), [])
        result = self.invoke('check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([x['state'] for x in json.loads(result.stdout)['targets']], ['installed'] * 6)
        self.assertEqual([x['state'] for x in json.loads(result.stdout)['legacy_targets']], ['absent'] * 2)
        for path in self.paths:
            self.assertIn(str(self.root), path.read_text(encoding='utf-8'))
        for path in self.paths[2:]:
            text = path.read_text(encoding='utf-8')
            self.assertIn(str(self.root / MAIN_SOURCE), text)
            self.assertIn('请先读取并遵循', text)
        for path in (self.paths[0], self.paths[1], self.paths[4], self.paths[5]):
            self.assertTrue(path.read_text(encoding='utf-8').startswith('---\n'))
        self.assertIn('name: voice-gen', self.paths[5].read_text())
        self.assertIn('$voice-gen', self.paths[5].read_text())
        for path in self.paths[:2]:
            text = path.read_text(encoding='utf-8')
            self.assertIn('用户未指定模型时，每次 `synth` 显式传入 `--model voxcpm2`', text)
            self.assertIn('用户指定模型时仅传入用户指定的模型', text)
            self.assertIn('不静默切换', text)
            self.assertIn('reference_plus_prompt', text)
            self.assertIn('CLI 或 API', text)
        assignment = next(line for line in self.paths[0].read_text().splitlines() if line.startswith('VOICE_TTS_ROOT='))
        env = os.environ.copy()
        env.pop('VOICE_TTS_ROOT', None)
        resolved = subprocess.run(['sh', '-c', assignment + '\nprintf "%s" "$VOICE_TTS_ROOT"'],
                                  env=env, text=True, capture_output=True, check=True)
        self.assertEqual(Path(resolved.stdout), self.root.resolve())
        expected = {path: path.read_bytes() for path in self.paths}
        self.assertEqual(self.invoke('uninstall').returncode, 0)
        self.assertTrue(all(not path.exists() for path in self.paths))
        self.assert_originals_backed_up(expected)
        self.assertEqual(self.invoke('uninstall').returncode, 0)

    def test_current_main_is_preserved_when_adding_entries(self):
        module = self.module()
        expected = module.content()
        for path in self.paths[:2]:
            path.parent.mkdir(parents=True)
            path.write_text(expected, encoding='utf-8')
        before = [path.stat().st_ino for path in self.paths[:2]]
        self.assertEqual(self.invoke('install').returncode, 0)
        self.assertEqual([path.stat().st_ino for path in self.paths[:2]], before)
        self.assertEqual([path.read_text(encoding='utf-8') for path in self.paths[:2]], [expected] * 2)

    def test_conflict_and_user_modification_protected(self):
        for target in self.paths:
            with self.subTest(target=target):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('existing user data')
                before = self.home_snapshot()
                self.assertEqual(self.invoke('install').returncode, 1)
                self.assertEqual(before, self.home_snapshot())
                target.unlink()
                if target.parent.name in ('local-tts', 'voice-clone', 'voice-gen'):
                    target.parent.rmdir()
        self.assertEqual(self.invoke('install').returncode, 0)
        self.paths[3].write_text('user changes')
        before = self.home_snapshot()
        self.assertEqual(self.invoke('install').returncode, 1)
        self.assertEqual(self.invoke('uninstall').returncode, 1)
        self.assertEqual(before, self.home_snapshot())

    def test_symlinks_are_conflicts(self):
        for target in (self.paths[2], self.paths[4].parent, self.home / '.codebuddy'):
            with self.subTest(target=target):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(self.base / 'missing')
                before = self.home_snapshot()
                self.assertEqual(self.invoke('install').returncode, 1)
                self.assertEqual(before, self.home_snapshot())
                target.unlink()
        self.assertEqual(self.invoke('install').returncode, 0)
        self.paths[5].unlink()
        self.paths[5].symlink_to(self.paths[4])
        before = self.home_snapshot()
        self.assertEqual(self.invoke('uninstall').returncode, 1)
        self.assertEqual(before, self.home_snapshot())

    def test_unknown_outdated_and_extra_files_are_preserved(self):
        self.assertEqual(self.invoke('install').returncode, 0)
        for target in (self.paths[0], self.paths[4], self.paths[5]):
            extra = target.parent / '.hidden-user-file'
            extra.write_text('keep')
            before = self.home_snapshot()
            self.assertEqual(self.invoke('install').returncode, 1)
            self.assertEqual(self.invoke('uninstall').returncode, 1)
            self.assertEqual(before, self.home_snapshot())
            extra.unlink()
        with (self.root / MAIN_SOURCE).open('a') as stream:
            stream.write('\nnew source')
        with (self.root / COMMAND_SOURCE / 'voice_gen.md').open('a') as stream:
            stream.write('\nnew source')
        before = self.home_snapshot()
        self.assertEqual(self.invoke('install').returncode, 1)
        # A self-hashed old release is unknown, not authorization to delete it.
        self.assertEqual(self.invoke('uninstall').returncode, 1)
        self.assertEqual(before, self.home_snapshot())

    def test_install_failure_rolls_back_only_new_files(self):
        module = self.module()
        for path in self.paths[:2]:
            path.parent.mkdir(parents=True)
            path.write_text(module.content(), encoding='utf-8')
        before = self.home_snapshot()
        real_link = module.os.link
        count = 0
        def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('injected failure')
            return real_link(*args, **kwargs)
        with mock.patch.object(module.os, 'link', side_effect=fail_second):
            result, _, error = self.call(module)
        self.assertEqual(result, 1)
        self.assertIn('injected failure', error)
        self.assertEqual(before, self.home_snapshot())
        self.assertEqual(len(self.backups()), 1)
        self.assertIn('回滚完成', error)

    def test_cleanup_failure_is_reported(self):
        module = self.module()
        real_publish = module.Transaction.publish
        real_rename = module.os.rename
        def publish(transaction, item, index):
            if index == 1:
                raise OSError('injected install failure')
            real_publish(transaction, item, index)
        def rename(source, destination, **kwargs):
            if str(destination).startswith('rollback-'):
                raise OSError('injected cleanup failure')
            return real_rename(source, destination, **kwargs)
        with mock.patch.object(module.Transaction, 'publish', publish), \
                mock.patch.object(module.os, 'rename', side_effect=rename):
            result, _, error = self.call(module)
        self.assertEqual(result, 1)
        self.assertIn('清理失败', error)
        self.assertIn('injected cleanup failure', error)
        self.assertEqual(len(self.backups()), 1)
        self.assertIn(str(self.backups()[0]), error)

    def test_exact_legacy_migration_and_repeat(self):
        module = self.module()
        expected = self.seed_legacy(module)
        # These digests pin the actual captured pre-rename entry sources.
        self.assertEqual(hashlib.sha256(module.LEGACY_SOURCES['command-gen'].encode()).hexdigest(),
                         '37680e72f5a557e3c6fca438a930acf812209db83ce6d4493a76fc6f8d52a1c7')
        self.assertEqual(hashlib.sha256(module.LEGACY_SOURCES['skill-gen'].encode()).hexdigest(),
                         '9e6672d0f43b6eb858a01ca3cfbb3e80264518c64605b9927c77c14e81b696d9')
        for key in module.LEGACY_SOURCES:
            self.assertEqual(self.legacy_content(module, key), module.render(module.LEGACY_SOURCES[key], main=key == 'main'))
        check = self.invoke('check')
        self.assertEqual(check.returncode, 1)
        self.assertEqual([item['state'] for item in json.loads(check.stdout)['legacy_targets']], ['legacy', 'legacy'])
        result = self.invoke('install')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(path.exists() for path in self.paths))
        self.assertTrue(all(not path.exists() for path in self.old_paths))
        self.assertFalse(self.old_paths[1].parent.exists())
        self.assertEqual(self.invoke('check').returncode, 0)
        self.assert_originals_backed_up({path: data for path, data in expected.items()
                                        if path in self.old_paths or path.read_bytes() != data})
        before = self.home_snapshot()
        self.assertEqual(self.invoke('install').returncode, 0)
        self.assertEqual(before, self.home_snapshot())

    def test_legacy_main_only_is_upgraded_without_force_uninstall(self):
        module = self.module()
        expected = self.seed_legacy(module)
        for path in (self.paths[2], self.paths[4], *self.old_paths):
            path.unlink()
            if path.name == 'SKILL.md':
                path.parent.rmdir()
        result = self.invoke('install')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invoke('check').returncode, 0)
        self.assert_originals_backed_up({path: expected[path] for path in self.paths[:2]})

    def test_migration_conflicts_abort_before_writes(self):
        module = self.module()
        self.seed_legacy(module)
        # New-name conflicts and BOTH old-name conflicts must be preflighted.
        for path in (self.paths[3], self.paths[5], *self.old_paths, self.paths[0]):
            with self.subTest(path=path):
                existed = path.exists()
                original = path.read_bytes() if existed else None
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('user data')
                before = self.home_snapshot()
                self.assertEqual(self.invoke('install').returncode, 1)
                self.assertEqual(self.invoke('uninstall').returncode, 1)
                self.assertEqual(before, self.home_snapshot())
                if existed:
                    path.write_bytes(original)
                else:
                    path.unlink()
                    if path.name == 'SKILL.md':
                        path.parent.rmdir()

    def test_self_hash_wrong_root_and_old_versions_are_not_provenance(self):
        module = self.module()
        expected = self.seed_legacy(module)
        for path in (self.paths[0], self.paths[2], self.old_paths[0], self.old_paths[1]):
            original = expected[path].decode()
            body = original.rpartition('\n' + module.MARK)[0]
            for bad in (self.sign('arbitrary user content'), self.sign(body + '\nolder release'),
                        self.sign(body.replace(str(self.root), '/another/project')),
                        original.replace('sha256=', 'sha256=0')):
                with self.subTest(path=path, bad=bad[:30]):
                    path.write_text(bad)
                    before = self.home_snapshot()
                    self.assertEqual(self.invoke('install').returncode, 1)
                    self.assertEqual(self.invoke('uninstall').returncode, 1)
                    self.assertEqual(before, self.home_snapshot())
            path.write_bytes(expected[path])

    def test_legacy_symlink_extra_empty_directory_and_nonregular_conflicts(self):
        module = self.module()
        expected = self.seed_legacy(module)
        extra = self.old_paths[1].parent / 'user.txt'
        extra.write_text('preserve')
        before = self.home_snapshot()
        self.assertEqual(self.invoke('install').returncode, 1)
        self.assertEqual(before, self.home_snapshot())
        extra.unlink()
        for path in self.old_paths:
            path.unlink()
            path.symlink_to(self.base / 'missing')
            self.assertEqual(self.invoke('install').returncode, 1)
            self.assertTrue(path.is_symlink())
            path.unlink()
            path.write_bytes(expected[path])
        self.old_paths[1].unlink()
        self.assertEqual(self.invoke('install').returncode, 1)  # Foreign empty skill directory.
        self.old_paths[1].parent.rmdir()
        self.old_paths[0].unlink()
        self.old_paths[0].mkdir()
        self.assertEqual(self.invoke('install').returncode, 1)

    def test_failures_at_each_migration_step_restore_exact_originals(self):
        module = self.module()
        self.seed_legacy(module)
        before = self.home_snapshot()
        for method in ('write_stage', 'detach', 'publish'):
            real = getattr(module.Transaction, method)
            count = 0
            def fail(transaction, *args, **kwargs):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError('injected ' + method)
                return real(transaction, *args, **kwargs)
            with self.subTest(method=method), mock.patch.object(module.Transaction, method, fail):
                result, _, error = self.call(module)
                self.assertEqual(result, 1)
                self.assertIn('injected ' + method, error)
                self.assertEqual(before, self.home_snapshot())
        real_remove = module.remove_directory
        def fail_old(path, *args, **kwargs):
            if path == self.old_paths[1].parent:
                raise OSError('injected rmdir')
            return real_remove(path, *args, **kwargs)
        with mock.patch.object(module, 'remove_directory', fail_old):
            result, _, error = self.call(module)
        self.assertEqual(result, 1)
        self.assertIn('injected rmdir', error)
        self.assertEqual(before, self.home_snapshot())

    def test_preflight_change_after_staging_preserves_user_edit(self):
        module = self.module()
        self.seed_legacy(module)
        real_stage = module.Transaction.stage
        def stage(transaction, changes):
            real_stage(transaction, changes)
            self.paths[0].write_text('concurrent edit before apply')
        with mock.patch.object(module.Transaction, 'stage', stage):
            result, _, error = self.call(module)
        self.assertEqual(result, 1)
        self.assertIn('预检后', error)
        self.assertEqual(self.paths[0].read_text(), 'concurrent edit before apply')
        self.assertTrue(all(path.exists() for path in self.old_paths))
        self.assertFalse(self.paths[3].exists())
        self.assertEqual(self.backups(), [])

    def test_concurrent_replacement_at_publication_is_not_overwritten(self):
        module = self.module()
        originals = self.seed_legacy(module)
        real_publish = module.Transaction.publish
        def publish(transaction, item, index):
            if item['path'] == self.paths[0]:
                self.paths[0].write_text('concurrent replacement')
            return real_publish(transaction, item, index)
        with mock.patch.object(module.Transaction, 'publish', publish):
            result, _, error = self.call(module)
        self.assertEqual(result, 1)
        self.assertEqual(self.paths[0].read_text(), 'concurrent replacement')
        self.assertIn('回滚存在冲突', error)
        self.assert_originals_backed_up({self.paths[0]: originals[self.paths[0]]})
        self.assertTrue(all(path.exists() for path in self.old_paths))

    def test_rollback_preserves_in_place_and_replaced_and_symlink_edits(self):
        module = self.module()
        originals = self.seed_legacy(module)
        original_first = originals[self.paths[0]]
        real_publish = module.Transaction.publish
        for mutation in ('in-place', 'replaced', 'symlink'):
            with self.subTest(mutation=mutation):
                def publish(transaction, item, index):
                    if index == 1:
                        if mutation != 'in-place':
                            self.paths[0].unlink()
                        if mutation == 'symlink':
                            self.paths[0].symlink_to(self.base / 'outside-file')
                        else:
                            self.paths[0].write_text('concurrent edit after publication')
                        raise OSError('injected later failure')
                    real_publish(transaction, item, index)
                with mock.patch.object(module.Transaction, 'publish', publish):
                    result, _, error = self.call(module)
                self.assertEqual(result, 1)
                self.assertIn('未覆盖冲突文件', error)
                if mutation == 'symlink':
                    self.assertTrue(self.paths[0].is_symlink())
                    self.assertFalse((self.base / 'outside-file').exists())
                else:
                    self.assertEqual(self.paths[0].read_text(), 'concurrent edit after publication')
                self.assert_originals_backed_up({self.paths[0]: original_first})
                self.paths[0].unlink()
                self.paths[0].write_bytes(original_first)
                for backup in self.backups():
                    shutil.rmtree(backup)

    def test_rollback_link_conflict_keeps_both_versions_recoverable(self):
        module = self.module()
        originals = self.seed_legacy(module)
        real_restore = module.Transaction.restore
        real_publish = module.Transaction.publish
        def publish(transaction, item, index):
            if index == 1:
                raise OSError('injected later failure')
            real_publish(transaction, item, index)
        def restore(transaction, name, path):
            if path == self.paths[0]:
                path.write_text('created during rollback')
            return real_restore(transaction, name, path)
        with mock.patch.object(module.Transaction, 'publish', publish), \
                mock.patch.object(module.Transaction, 'restore', restore):
            result, _, error = self.call(module)
        self.assertEqual(result, 1)
        self.assertEqual(self.paths[0].read_text(), 'created during rollback')
        self.assertIn('原文件保存在', error)
        self.assert_originals_backed_up({self.paths[0]: originals[self.paths[0]]})

    def test_ancestor_swapped_to_symlink_is_never_followed(self):
        module = self.module()
        self.seed_legacy(module)
        external = self.base / 'external commands'
        external.mkdir()
        sentinel = external / 'voice_gen.md'
        sentinel.write_text('external data')
        real_stage = module.Transaction.stage
        commands = self.home / '.claude/commands'
        saved = commands.with_name('saved-commands')
        def stage(transaction, changes):
            real_stage(transaction, changes)
            commands.rename(saved)
            commands.symlink_to(external, target_is_directory=True)
        with mock.patch.object(module.Transaction, 'stage', stage):
            result, _, _ = self.call(module)
        self.assertEqual(result, 1)
        self.assertTrue(commands.is_symlink())
        self.assertEqual(sentinel.read_text(), 'external data')
        self.assertEqual(sorted(path.name for path in external.iterdir()), ['voice_gen.md'])
        self.assertEqual(self.backups(), [])

    def test_default_home_argument_uses_only_temporary_home(self):
        env = dict(os.environ, HOME=str(self.home), PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-I', str(self.root / 'scripts/install_agent_skill.py'), 'install'],
                                env=env, cwd=self.base, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(path.exists() for path in self.paths))
        self.assertEqual(self.invoke('check').returncode, 0)

    def test_selected_home_symlink_is_rejected(self):
        external = self.base / 'foreign home'
        external.mkdir()
        sentinel = external / 'user.txt'
        sentinel.write_text('preserve')
        self.home.rmdir()
        self.home.symlink_to(external, target_is_directory=True)
        for action in ('check', 'install', 'uninstall'):
            self.assertEqual(self.invoke(action).returncode, 1)
        self.assertEqual(sentinel.read_text(), 'preserve')
        self.assertEqual(sorted(path.name for path in external.iterdir()), ['user.txt'])

    def test_extra_file_appearing_during_migration_is_preserved(self):
        module = self.module()
        original = self.seed_legacy(module)
        real_apply = module.Transaction.apply
        extra = self.paths[0].parent / 'concurrent-user-file'
        def apply(transaction, changes):
            real_apply(transaction, changes)
            extra.write_text('preserve')
        with mock.patch.object(module.Transaction, 'apply', apply):
            result, _, error = self.call(module)
        self.assertEqual(result, 1)
        self.assertIn('发布后', error)
        self.assertEqual(extra.read_text(), 'preserve')
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)
        self.assert_originals_backed_up({path: data for path, data in original.items()
                                        if path in self.old_paths or path in self.paths[:2]})
        self.assertFalse(self.paths[3].exists())
        self.assertFalse(self.paths[5].exists())

    def test_known_legacy_uninstall_and_failure_are_recoverable(self):
        module = self.module()
        expected = self.seed_legacy(module)
        before = self.home_snapshot()
        real_detach = module.Transaction.detach
        count = 0
        def detach(transaction, item, index):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError('injected uninstall failure')
            real_detach(transaction, item, index)
        with mock.patch.object(module.Transaction, 'detach', detach):
            result, _, error = self.call(module, 'uninstall')
        self.assertEqual(result, 1)
        self.assertIn('injected uninstall failure', error)
        self.assertEqual(before, self.home_snapshot())
        result = self.invoke('uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(not path.exists() for path in expected))
        self.assert_originals_backed_up(expected)


if __name__ == '__main__':
    unittest.main()
