import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from tts_auto import choose_workers


class AutoWorkersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cache.json"
        self.profile = {"host": "host-hash", "model": "model-hash", "runtime": "v1",
                        "supervisor_protocol": "v1"}
        self.chunks = ["secret text " + str(i) for i in range(5)]
        self.memory = Mock(return_value=50)
        self.trial = Mock(side_effect=self.results(10, 7))

    @staticmethod
    def results(one, two, minimum=40):
        return [{"success": True, "seconds": one, "minimum_free_percent": minimum},
                {"success": True, "seconds": two, "minimum_free_percent": minimum}]

    def choose(self, profile=None, chunks=None):
        return choose_workers(self.profile if profile is None else profile,
                              self.chunks if chunks is None else chunks,
                              self.trial, self.memory, self.path)

    def test_fast_trials_and_cache(self):
        self.assertEqual(self.choose()[0], 2)
        self.assertEqual(self.trial.call_args_list[0].args, (1, self.chunks[:4]))
        self.assertEqual(self.trial.call_args_list[1].args, (2, self.chunks[:4]))
        self.trial.reset_mock(side_effect=True)
        result, reason = self.choose()
        self.assertEqual(result, 2)
        self.assertIn("缓存", reason)
        self.trial.assert_not_called()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_privacy(self):
        self.profile["voice"] = "private voice name"
        self.choose()
        raw = self.path.read_text()
        for secret in self.chunks + ["private voice name", "host-hash", "model-hash"]:
            self.assertNotIn(secret, raw)
        self.assertEqual(set(json.loads(raw)), {"schema", "profile_hash", "timestamp", "trials"})

    def test_preconditions(self):
        for value in (None, -1, 101, float("nan"), 29.9, True):
            self.memory.return_value = value
            self.assertEqual(self.choose()[0], 1)
        self.memory.side_effect = RuntimeError()
        self.assertEqual(self.choose()[0], 1)
        self.assertEqual(self.choose(chunks=self.chunks[:3])[0], 1)
        for profile in ({}, {"host": "h", "model": "", "runtime": "v"},
                        {"host": "h", "model": "m", "runtime": None}):
            self.assertEqual(self.choose(profile=profile)[0], 1)
        self.trial.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_thresholds(self):
        for seconds, minimum, expected in [(8, 25, 2), (8.0001, 25, 1),
                                            (7, 24.99, 1), (10, 50, 1)]:
            with self.subTest(seconds=seconds, minimum=minimum):
                self.path.unlink(missing_ok=True)
                self.trial = Mock(side_effect=self.results(10, seconds, minimum))
                self.assertEqual(self.choose()[0], expected)

    def test_invalid_or_failed_trials_not_cached(self):
        for bad in ({"success": False, "seconds": 1, "minimum_free_percent": 50},
                    {"success": True, "seconds": 0, "minimum_free_percent": 50},
                    {"success": True, "seconds": float("nan"), "minimum_free_percent": 50},
                    {"success": True, "seconds": 1}, None, RuntimeError("bad")):
            with self.subTest(bad=bad):
                self.trial = Mock(side_effect=[self.results(10, 7)[0], bad])
                self.assertEqual(self.choose()[0], 1)
                self.assertFalse(self.path.exists())

    def test_cached_resources_checked_and_post_trial_pressure(self):
        self.assertEqual(self.choose()[0], 2)
        self.memory.return_value = 29
        self.trial.reset_mock()
        self.assertEqual(self.choose()[0], 1)
        self.trial.assert_not_called()
        self.path.unlink()
        self.memory.side_effect = [50, 50, 50, 29]
        self.trial.side_effect = self.results(10, 7)
        self.assertEqual(self.choose()[0], 1)
        self.assertEqual(self.trial.call_count, 1)
        self.assertFalse(self.path.exists())

    def test_profile_and_expiry(self):
        self.choose()
        self.trial = Mock(side_effect=self.results(10, 9))
        self.assertEqual(self.choose(profile={**self.profile, "runtime": "v2"})[0], 1)
        self.assertEqual(self.trial.call_count, 2)
        cache = json.loads(self.path.read_text())
        cache["timestamp"] = time.time() - 7 * 86400 - 1
        self.path.write_text(json.dumps(cache))
        self.trial = Mock(side_effect=self.results(10, 7))
        self.assertEqual(self.choose(profile={**self.profile, "runtime": "v2"})[0], 2)
        self.assertEqual(self.trial.call_count, 2)

    def test_corrupt_cache_never_overwritten(self):
        self.path.write_text("user content")
        self.assertEqual(self.choose()[0], 2)
        self.assertEqual(self.path.read_text(), "user content")

    def test_lock_contention(self):
        lock = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.choose()[0], 1)
            self.trial.assert_not_called()
        finally:
            os.close(fd)
        self.assertTrue(lock.is_file())
        self.assertEqual(self.choose()[0], 2)
        self.assertTrue(lock.is_file())

    def test_protocol_and_legacy_cache(self):
        self.assertEqual(self.choose()[0], 2)
        old = json.loads(self.path.read_text())
        old["schema"] = "tts-auto-v1"
        self.path.write_text(json.dumps(old))
        original = self.path.read_bytes()
        self.trial = Mock(side_effect=self.results(10, 9))
        self.assertEqual(self.choose()[0], 1)
        self.assertEqual(self.trial.call_count, 2)
        self.assertEqual(self.path.read_bytes(), original)
        self.trial.reset_mock()
        for protocol in (None, "", "  ", 3):
            self.assertEqual(self.choose(profile={**self.profile, "supervisor_protocol": protocol})[0], 1)
        self.assertEqual(self.choose(profile={k: v for k, v in self.profile.items()
                                              if k != "supervisor_protocol"})[0], 1)
        self.trial.assert_not_called()

    def test_protocol_isolation(self):
        self.assertEqual(self.choose()[0], 2)
        self.trial = Mock(side_effect=self.results(10, 9))
        self.assertEqual(self.choose(profile={**self.profile, "supervisor_protocol": "v2"})[0], 1)
        self.assertEqual(self.trial.call_count, 2)

    def test_readings_before_and_after_every_trial(self):
        for readings, calls in (([50, None], 0), ([50, 50, None], 1),
                                ([50, 50, 50, 29], 1),
                                ([50, 50, 50, 30, None], 2),
                                ([50, 50, 50, 30, 24], 2)):
            with self.subTest(readings=readings):
                self.path.unlink(missing_ok=True)
                self.memory = Mock(side_effect=readings)
                self.trial = Mock(side_effect=self.results(10, 7))
                self.assertEqual(self.choose()[0], 1)
                self.assertEqual(self.trial.call_count, calls)
                self.assertFalse(self.path.exists())
        self.memory = Mock(return_value=50)
        self.trial = Mock(side_effect=RuntimeError("failure"))
        self.assertEqual(self.choose()[0], 1)
        self.assertEqual(self.memory.call_count, 3)

    def test_unsafe_locks_are_never_changed(self):
        lock = self.path.with_name(self.path.name + ".lock")
        target = Path(self.tmp.name) / "target"
        target.write_text("keep")
        lock.symlink_to(target)
        self.assertEqual(self.choose()[0], 1)
        self.assertTrue(lock.is_symlink())
        self.assertEqual(target.read_text(), "keep")
        lock.unlink()
        lock.mkdir()
        self.assertEqual(self.choose()[0], 1)
        self.assertTrue(lock.is_dir())
        lock.rmdir()
        os.link(target, lock)
        self.assertEqual(self.choose()[0], 1)
        self.assertEqual(target.read_text(), "keep")
        self.trial.assert_not_called()

    def test_nonprivate_cache_directory_is_not_modified(self):
        os.chmod(self.tmp.name, 0o755)
        self.assertEqual(self.choose()[0], 2)
        self.assertFalse(self.path.exists())
        self.assertEqual(os.stat(self.tmp.name).st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
