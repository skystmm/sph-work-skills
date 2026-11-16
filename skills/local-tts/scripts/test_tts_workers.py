import fcntl
import functools
import multiprocessing as mp
from multiprocessing.reduction import DupFd
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tts_workers as tw


def pressure_value(value):
    return value


class PressureSequence:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1
        return 70 if self.count == 1 else 10


def fake_synth(chunk):
    if chunk.get("crash_worker"):
        os._exit(83)
    if chunk.get("crash_supervisor"):
        os.kill(os.getppid(), 9)
        time.sleep(10)
    time.sleep(chunk.get("delay", 0))
    if chunk.get("fail"):
        raise RuntimeError("injected failure")
    return str(chunk["value"]).encode()


def wait_for_file(path, timeout=5):
    end = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() >= end:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.01)


def parent_crash(lock_path, marker):
    def done(index, data):
        Path(marker).write_bytes(data)
        os._exit(0)
    tw.run_parallel([(0, {"value": 1}), (1, {"value": 2, "delay": 20})],
                    "mock", "voice", 1, 2, done, timeout=30,
                    _worker_fn=fake_synth, _lock_path=lock_path)


class WorkerTests(unittest.TestCase):
    def run_fake(self, chunks, **kwargs):
        received = []
        tw.run_parallel(chunks, "mock", "voice", 1.0, kwargs.pop("workers", 2),
                        lambda i, data: received.append((i, data)),
                        timeout=kwargs.pop("timeout", 10), _worker_fn=fake_synth, **kwargs)
        return received

    def test_out_of_order_and_noncontiguous_indices(self):
        actual = self.run_fake([(19, {"value": "slow", "delay": .5}),
                                (4, {"value": "fast"}),
                                (90, {"value": "last"})])
        self.assertEqual(actual[0], (4, b"fast"))
        self.assertEqual(dict(actual), {19: b"slow", 4: b"fast", 90: b"last"})

    def test_error_reports_completed_and_stops(self):
        got = []
        with self.assertRaises(tw.ParallelTTSError) as cm:
            tw.run_parallel([(0, {"value": 0}), (1, {"fail": True, "delay": .4}),
                             (2, {"value": 2, "delay": 3})], "mock", "v", 1, 2,
                            lambda i, b: got.append(i), timeout=8, _worker_fn=fake_synth)
        self.assertEqual(got, [0])
        self.assertEqual(cm.exception.completed_indices, (0,))
        self.assertIn("injected failure", str(cm.exception))

    def test_actual_worker_crash_is_reported_and_lock_released(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "lock")
            with self.assertRaisesRegex(tw.ParallelTTSError, "worker exited") as cm:
                self.run_fake([(0, {"crash_worker": True})], _lock_path=path)
            self.assertTrue(cm.exception.cleanup_confirmed)
            fd = tw._device_lock(path)
            os.close(fd)

    def test_supervisor_crash_reports_unverified_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "lock")
            completed = []
            with self.assertRaisesRegex(tw.ParallelTTSError, "cleanup unverified") as cm:
                tw.run_parallel([(5, {"value": "saved"}), (7, {"crash_supervisor": True})],
                                "mock", "v", 1, 1, lambda i, data: completed.append((i, data)),
                                _worker_fn=fake_synth, _lock_path=path, timeout=10)
            self.assertEqual(completed, [(5, b"saved")])
            self.assertEqual(cm.exception.completed_indices, (5,))
            self.assertFalse(cm.exception.cleanup_confirmed)
            # This mock sleeps and releases the GIL, so its worker watchdog can
            # exit. This is not evidence for a GIL-holding native model call.
            end = time.monotonic() + 5
            while True:
                try:
                    fd = tw._device_lock(path)
                    os.close(fd)
                    break
                except BlockingIOError:
                    if time.monotonic() >= end:
                        self.fail("mock worker retained lock after supervisor crash")
                    time.sleep(.02)

    def test_timeout_kills_children(self):
        before = {p.pid for p in mp.active_children()}
        with self.assertRaisesRegex(tw.ParallelTTSError, "timed out"):
            self.run_fake([(1, {"value": 1, "delay": 5})], workers=1, timeout=.7)
        self.assertEqual({p.pid for p in mp.active_children()} - before, set())

    def test_pressure_unavailable_and_low(self):
        for value in (None, 14, float("nan")):
            with self.subTest(value=value), self.assertRaisesRegex(tw.ParallelTTSError, "memory pressure"):
                self.run_fake([(0, {"value": 1})], memory_check=functools.partial(pressure_value, value))
        self.assertEqual(self.run_fake([(0, {"value": 1})], memory_check=functools.partial(pressure_value, 50)), [(0, b"1")])

    def test_pressure_rechecked_while_work_is_running(self):
        with self.assertRaisesRegex(tw.ParallelTTSError, "memory pressure"):
            self.run_fake([(0, {"value": 1, "delay": 5})],
                          memory_check=PressureSequence(), timeout=9)

    def test_four_mock_workers(self):
        chunks = [(i, {"value": i}) for i in range(8)]
        self.assertEqual(dict(self.run_fake(chunks, workers=4)),
                         {i: str(i).encode() for i in range(8)})

    def test_callback_failure_completed_indices(self):
        def cb(index, data):
            if index == 1:
                raise RuntimeError("sink failed")
        with self.assertRaises(tw.ParallelTTSError) as cm:
            tw.run_parallel([(0, {"value": 0}), (1, {"value": 1})], "mock", "v", 1, 1, cb,
                            _worker_fn=fake_synth, timeout=5)
        self.assertEqual(cm.exception.completed_indices, (0,))
        self.assertIn("sink failed", str(cm.exception))

    def test_real_is_fail_closed(self):
        for count in (1, 2, 4):
            with self.assertRaisesRegex(tw.ParallelTTSError, "disabled"):
                tw.run_parallel([(0, {})], "cosyvoice3", "v", 1, count, lambda *_: None)

    def test_invalid_and_empty(self):
        with self.assertRaises(ValueError):
            self.run_fake([(0, {}), (0, {})])
        with self.assertRaises(ValueError):
            self.run_fake([(0, {})], workers=3)
        self.assertEqual(self.run_fake([]), [])

    def test_lock_is_held_by_worker_after_parent_closes_fd(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "lock")
            fd = tw._device_lock(path)
            ctx = mp.get_context("spawn")
            parent, child = ctx.Pipe()
            rd, wr = ctx.Pipe(False)
            proc = ctx.Process(target=tw._worker, args=(child, rd, DupFd(fd), "mock", "v", 1, fake_synth))
            proc.start()
            child.close(); rd.close()
            self.assertTrue(parent.poll(5))
            self.assertEqual(parent.recv(), ("ready",))
            os.close(fd)
            with self.assertRaises(BlockingIOError):
                tw._device_lock(path)
            parent.send(None)
            proc.join(5)
            self.assertEqual(proc.exitcode, 0)
            fd = tw._device_lock(path)
            os.close(fd)
            wr.close(); parent.close(); proc.close()

    def test_parent_abrupt_exit_ends_mock_workers_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = os.path.join(directory, "lock")
            marker = os.path.join(directory, "done")
            proc = subprocess.Popen([sys.executable, __file__, "--crash-parent", lock, marker])
            try:
                wait_for_file(marker, 10)
                self.assertEqual(proc.wait(timeout=5), 0)
                end = time.monotonic() + 5
                while True:
                    try:
                        fd = tw._device_lock(lock)
                        os.close(fd)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= end:
                            self.fail("lock still held after parent exited")
                        time.sleep(.02)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def test_mock_engine_runs_in_spawn(self):
        chunk = {"text": "test", "overrides": {}, "breaks": [], "emphasis_spans": []}
        result = []
        tw.run_parallel([(7, chunk)], "mock", "v", 1, 1,
                        lambda i, b: result.append((i, b)), timeout=15)
        self.assertEqual(result[0][0], 7)
        self.assertTrue(result[0][1].startswith(b"RIFF"))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--crash-parent":
        parent_crash(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
