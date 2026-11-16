import contextlib
import fcntl
import hashlib
import http.server
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import model_install as mi

DATA = b'0123456789' * 100


def manifest(data=DATA, path='weights/model.bin'):
    return {'schema': 1, 'model': 'sample', 'repository': 'official/sample',
            'revision': 'a' * 40, 'files': [{'path': path, 'size': len(data),
            'sha256': hashlib.sha256(data).hexdigest()}]}


class Handler(http.server.BaseHTTPRequestHandler):
    mode = 'normal'
    requests_seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        range_header = self.headers.get('Range')
        self.requests_seen.append(range_header)
        start = int(range_header[6:-1]) if range_header else 0
        mode = type(self).mode
        if range_header and mode != 'ignore':
            self.send_response(206)
            first = start + 1 if mode == 'bad-range' else start
            self.send_header('Content-Range', f'bytes {first}-{len(DATA)-1}/{len(DATA)}')
            body = DATA[start:]
        else:
            self.send_response(200)
            body = DATA if mode != 'corrupt' else b'x' * len(DATA)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if mode == 'interrupt':
            self.wfile.write(body[:123])
            self.wfile.flush()
            self.close_connection = True
        else:
            self.wfile.write(body)


class InstallerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.dest = self.root / 'model'
        self.stage = self.root / '.model.install-staging'
        Handler.mode = 'normal'
        Handler.requests_seen = []

    def tearDown(self):
        self.tmp.cleanup()

    def install(self):
        return mi.run_operation('install', manifest(), self.dest, base_url=self.url)

    def test_interruption_and_range_resume(self):
        Handler.mode = 'interrupt'
        with self.assertRaises((mi.InstallError, OSError)):
            self.install()
        self.assertEqual((self.stage / 'weights/model.bin.part').stat().st_size, 123)
        Handler.mode = 'normal'
        self.assertEqual(self.install(), 'installed')
        self.assertEqual(Handler.requests_seen, [None, 'bytes=123-'])
        mi.run_operation('verify', manifest(), self.dest)

    def test_ignored_range_restarts(self):
        Handler.mode = 'interrupt'
        with self.assertRaises((mi.InstallError, OSError)):
            self.install()
        Handler.mode = 'ignore'
        self.install()
        self.assertEqual((self.dest / 'weights/model.bin').read_bytes(), DATA)

    def test_bad_range_rejected_without_append(self):
        Handler.mode = 'interrupt'
        with self.assertRaises((mi.InstallError, OSError)):
            self.install()
        Handler.mode = 'bad-range'
        with self.assertRaises(mi.InstallError):
            self.install()
        self.assertEqual((self.stage / 'weights/model.bin.part').read_bytes(), DATA[:123])
        self.assertFalse(self.dest.exists())

    def test_corruption_and_recovery(self):
        Handler.mode = 'corrupt'
        with self.assertRaises(mi.InstallError):
            self.install()
        self.assertFalse(self.dest.exists())
        Handler.mode = 'normal'
        self.install()
        self.assertEqual((self.dest / 'weights/model.bin').read_bytes(), DATA)

    def test_import_and_existing_destination_unchanged(self):
        source = self.root / 'input'
        (source / 'weights').mkdir(parents=True)
        (source / 'weights/model.bin').write_bytes(DATA)
        self.assertEqual(mi.run_operation('import', manifest(), self.dest, source), 'installed')
        before = (self.dest / 'weights/model.bin').stat()
        self.assertEqual(mi.run_operation('install', manifest(), self.dest, base_url=self.url), 'verified existing destination')
        after = (self.dest / 'weights/model.bin').stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual(Handler.requests_seen, [])
        (self.dest / 'extra').write_text('keep')
        with self.assertRaises(mi.InstallError):
            mi.run_operation('import', manifest(), self.dest, source)
        self.assertEqual((self.dest / 'extra').read_text(), 'keep')

    def test_import_rejects_symlinks_and_corruption(self):
        source = self.root / 'input'
        (source / 'weights').mkdir(parents=True)
        (source / 'real').write_bytes(DATA)
        (source / 'weights/model.bin').symlink_to(source / 'real')
        with self.assertRaises((mi.InstallError, OSError)):
            mi.run_operation('import', manifest(), self.dest, source)
        (source / 'weights/model.bin').unlink()
        (source / 'weights/model.bin').write_bytes(b'wrong')
        with self.assertRaises(mi.InstallError):
            mi.run_operation('import', manifest(), self.dest, source)
        self.assertFalse(self.dest.exists())

    def test_reject_paths_and_conflicts(self):
        for path in ('../escape', '/absolute', 'a//b', 'a/./b', 'a\\b', 'a.part', '.manifest-id'):
            with self.subTest(path=path), self.assertRaises(mi.InstallError):
                mi.validate_manifest(manifest(path=path))
        m = manifest(path='x')
        m['files'].append(dict(m['files'][0], path='x/y'))
        with self.assertRaises(mi.InstallError):
            mi.validate_manifest(m)
        self.assertFalse((self.root / 'escape').exists())

    def test_destination_and_staging_symlinks_rejected(self):
        other = self.root / 'other'
        other.mkdir()
        self.dest.symlink_to(other, target_is_directory=True)
        with self.assertRaises(mi.InstallError):
            self.install()
        self.dest.unlink()
        self.stage.symlink_to(other, target_is_directory=True)
        with self.assertRaises(mi.InstallError):
            self.install()
        self.assertEqual(list(other.iterdir()), [])

    def test_staging_file_hardlink_not_truncated(self):
        source = self.root / 'input'
        (source / 'weights').mkdir(parents=True)
        (source / 'weights/model.bin').write_bytes(DATA)
        (self.stage / 'weights').mkdir(parents=True)
        self.stage.chmod(0o700)
        marker = hashlib.sha256(__import__('json').dumps(manifest(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        (self.stage / '.manifest-id').write_text(marker)
        victim = self.root / 'victim'
        victim.write_bytes(b'untouched')
        os.link(victim, self.stage / 'weights/model.bin.part')
        with self.assertRaises(mi.InstallError):
            mi.run_operation('import', manifest(), self.dest, source)
        self.assertEqual(victim.read_bytes(), b'untouched')

    def test_lock_prevents_concurrent_writer(self):
        lock = self.root / '.model.install.lock'
        with open(lock, 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(mi.InstallError):
                self.install()
        self.assertFalse(self.dest.exists())
        self.assertEqual(Handler.requests_seen, [])

    def test_atomic_publish_refuses_replacement(self):
        src = self.root / 'src'
        dst = self.root / 'dst'
        src.mkdir()
        dst.mkdir()
        (dst / 'keep').write_text('safe')
        with self.assertRaises(OSError):
            mi.publish_noreplace(src, dst)
        self.assertEqual((dst / 'keep').read_text(), 'safe')
        self.assertTrue(src.exists())

    def test_verify_read_only_and_missing(self):
        with self.assertRaises(mi.InstallError):
            mi.run_operation('verify', manifest(), self.dest)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_racing_destination_is_not_replaced(self):
        original = mi.publish_noreplace
        def race(stage, destination):
            destination.mkdir()
            (destination / 'keep').write_text('safe')
            return original(stage, destination)
        with mock.patch.object(mi, 'publish_noreplace', side_effect=race):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual((self.dest / 'keep').read_text(), 'safe')
        self.assertTrue((self.stage / '.manifest-id').exists())

    def test_cli_injected_manifest_reports_http_failure(self):
        Handler.mode = 'corrupt'
        with mock.patch.object(mi, 'load_manifest', return_value=manifest()), \
             mock.patch.object(mi, 'run_operation', side_effect=mi.InstallError('verification failed')), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mi.main(['install', 'sample', str(self.dest)]), 1)
        self.assertFalse(self.dest.exists())

    def test_cli_failure_codes(self):
        script = Path(mi.__file__)
        for args in (['install', '../evil', str(self.dest)], ['verify', 'missing-model', str(self.dest)], ['import']):
            result = subprocess.run([sys.executable, '-B', str(script), *args], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_schema(self):
        for key, value in [('revision', 'main'), ('repository', '../x'), ('schema', True)]:
            m = manifest()
            m[key] = value
            with self.subTest(key=key), self.assertRaises(mi.InstallError):
                mi.validate_manifest(m)


class WrapperTest(unittest.TestCase):
    def test_existing_model_is_verified_without_network_or_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            scripts = root / 'scripts'
            (scripts / 'model_manifests').mkdir(parents=True)
            shutil.copy(Path(__file__).with_name('download_model.sh'), scripts)
            shutil.copy(Path(mi.__file__), scripts)
            fixture = manifest()
            fixture['model'] = 'cosyvoice3'
            (scripts / 'model_manifests/cosyvoice3.json').write_text(json.dumps(fixture))
            destination = root / 'models/CosyVoice3/weights'
            destination.mkdir(parents=True)
            (destination / 'model.bin').write_bytes(DATA)
            result = subprocess.run(['bash', str(scripts / 'download_model.sh'), 'cosyvoice3'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('verified existing destination', result.stdout)
            self.assertEqual((destination / 'model.bin').read_bytes(), DATA)
            invalid = subprocess.run(['bash', str(scripts / 'download_model.sh'), 'wrong'], capture_output=True)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertEqual((destination / 'model.bin').read_bytes(), DATA)


if __name__ == '__main__':
    unittest.main()
