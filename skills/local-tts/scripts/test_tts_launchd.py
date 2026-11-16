import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tts_launchd as launchd

SCRIPT = str(Path(launchd.__file__).resolve())


class FakeLaunchctl:
    def __init__(self, mode='normal'):
        self.mode = mode
        self.process = None
        self.labels = []
        self.config_path = None
        self.modes = None

    def bootstrap(self, label, config_path):
        self.labels.append(('bootstrap', label))
        self.config_path = config_path
        self.modes = (stat.S_IMODE(os.stat(os.path.dirname(config_path)).st_mode),
                      stat.S_IMODE(os.stat(config_path).st_mode))
        if self.mode == 'bootstrap_error':
            raise OSError('bootstrap failed')
        if self.mode in ('bad_token', 'bad_group', 'crash', 'timeout', 'crash_after_result', 'hang_after_start'):
            code = '''import json,os,socket,sys,time
c=json.load(open(sys.argv[1])); mode=sys.argv[2]
if mode == 'crash': sys.exit(12)
if mode == 'timeout': time.sleep(10); sys.exit(0)
s=socket.socket(socket.AF_UNIX); s.connect(c['socket'])
p=os.getpid(); g=os.getpgrp()
h={'type':'hello','token':('wrong' if mode == 'bad_token' else c['token']),'pid':p,'pgid':(g+1 if mode == 'bad_group' else g)}
s.sendall((json.dumps(h)+'\\n').encode())
if mode in ('crash_after_result', 'hang_after_start'):
 s.recv(1024)
 if mode == 'crash_after_result':
  s.sendall(b'{"type":"result","index":1,"data":"ok"}\\n'); s.recv(1024); os._exit(9)
 time.sleep(10)
else: time.sleep(.3)
'''
            args = [sys.executable, '-c', code, config_path, self.mode]
        else:
            args = [sys.executable, SCRIPT, '--job', config_path]
        self.process = subprocess.Popen(args, start_new_session=True,
                                        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def authenticate(self, label, pid, pgid):
        if (self.mode == 'identity_mismatch' or self.process is None or
                self.process.pid != pid or pgid != pid):
            raise RuntimeError('independent job identity mismatch')

    def bootout(self, label):
        self.labels.append(('bootout', label))
        if self.process is not None:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()  # Popen owns this exact test child.
                self.process.wait(timeout=5)
        if self.mode == 'bootout_error':
            raise OSError('bootout failed')


class LaunchdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tts-tests-')
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def run_case(self, fake, chunks=None, callback=None, timeout=5):
        if chunks is None:
            chunks = [(1, {'text': '一'}), (2, {'text': 'two'})]
        if callback is None:
            callback = lambda index, data: None
        return launchd.run(chunks, callback, fake, timeout=timeout, temp_root=self.root)

    def test_success_permissions_labels_and_confirmation(self):
        fake = FakeLaunchctl()
        results = []
        self.assertEqual(self.run_case(fake, callback=lambda i, d: results.append((i, d))), (1, 2))
        self.assertEqual(results, [(1, '一'.encode()), (2, b'two')])
        self.assertEqual(fake.modes, (0o700, 0o600))
        self.assertEqual(fake.labels[0][1], fake.labels[1][1])
        self.assertTrue(fake.labels[0][1].startswith('local.tts.mock.'))
        self.assertEqual(os.listdir(self.root), [])
        fake2 = FakeLaunchctl()
        self.run_case(fake2, chunks=[])
        self.assertNotEqual(fake.labels[0][1], fake2.labels[0][1])

    def test_token_group_crash_and_timeout_fail_closed(self):
        for mode in ('bad_token', 'bad_group', 'crash', 'timeout'):
            with self.subTest(mode=mode):
                fake = FakeLaunchctl(mode)
                with self.assertRaises(launchd.LaunchdError) as error:
                    self.run_case(fake, timeout=.5)
                self.assertFalse(error.exception.cleanup_confirmed)
                self.assertEqual(error.exception.completed_indices, ())
                self.assertEqual(fake.labels[0][1], fake.labels[1][1])

    def test_bootstrap_and_bootout_errors(self):
        for mode in ('bootstrap_error', 'bootout_error'):
            fake = FakeLaunchctl(mode)
            with self.assertRaises(launchd.LaunchdError) as error:
                self.run_case(fake)
            self.assertFalse(error.exception.cleanup_confirmed)
            if mode == 'bootstrap_error':
                self.assertEqual(len(fake.labels), 1)
            else:
                self.assertEqual(fake.labels[0][1], fake.labels[1][1])
        self.assertEqual(error.exception.completed_indices, (1, 2))

    def test_callback_failure_preserves_only_confirmed(self):
        fake = FakeLaunchctl()
        def callback(i, data):
            if i == 2:
                raise ValueError('callback failed')
        with self.assertRaises(launchd.LaunchdError) as error:
            self.run_case(fake, callback=callback)
        self.assertEqual(error.exception.completed_indices, (1,))
        self.assertFalse(error.exception.cleanup_confirmed)

    def test_existing_config_not_overwritten_or_removed(self):
        real_open = os.open
        existing = []
        def collide(path, flags, *args, **kwargs):
            if str(path).endswith('config.json'):
                fd = real_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(fd, b'user data')
                os.close(fd)
                existing.append(path)
            return real_open(path, flags, *args, **kwargs)
        fake = FakeLaunchctl()
        with mock.patch.object(launchd.os, 'open', side_effect=collide):
            with self.assertRaises(launchd.LaunchdError):
                self.run_case(fake)
        self.assertEqual(Path(existing[0]).read_bytes(), b'user data')
        self.assertEqual(fake.labels, [])

    def test_disconnect_after_ack_and_timeout_after_handshake(self):
        for mode in ('crash_after_result', 'hang_after_start'):
            with self.subTest(mode=mode):
                received = []
                with self.assertRaises(launchd.LaunchdError) as error:
                    self.run_case(FakeLaunchctl(mode), callback=lambda i, d: received.append(i), timeout=.8)
                self.assertFalse(error.exception.cleanup_confirmed)
                self.assertEqual(error.exception.completed_indices, (1,) if mode == 'crash_after_result' else ())
                self.assertEqual(received, [1] if mode == 'crash_after_result' else [])

    def test_kernel_peer_pid_mismatch_rejected(self):
        fake = FakeLaunchctl()
        received = []
        with mock.patch.object(launchd, '_peer_pid', return_value=-1):
            with self.assertRaises(launchd.LaunchdError) as error:
                self.run_case(fake, callback=lambda *args: received.append(args))
        self.assertFalse(error.exception.cleanup_confirmed)
        self.assertEqual(received, [])

    def test_independent_identity_mismatch(self):
        fake = FakeLaunchctl('identity_mismatch')
        received = []
        with self.assertRaises(launchd.LaunchdError) as error:
            self.run_case(fake, callback=lambda *args: received.append(args))
        self.assertFalse(error.exception.cleanup_confirmed)
        self.assertEqual(received, [])

    def test_real_adapter_refuses_without_running_commands(self):
        runner = mock.Mock()
        adapter = launchd.LaunchctlAdapter(runner=runner)
        with self.assertRaises(launchd.LaunchdError) as error:
            self.run_case(adapter)
        self.assertFalse(error.exception.cleanup_confirmed)
        runner.assert_not_called()
        self.assertEqual(adapter.domain, f'gui/{os.getuid()}')
        self.assertEqual(os.listdir(self.root), [])

    def test_real_models_never_bootstrap(self):
        fake = FakeLaunchctl()
        with self.assertRaises(launchd.LaunchdError):
            launchd.run([], lambda *_: None, fake, model='cosyvoice', temp_root=self.root)
        self.assertEqual(fake.labels, [])


if __name__ == '__main__':
    unittest.main()
