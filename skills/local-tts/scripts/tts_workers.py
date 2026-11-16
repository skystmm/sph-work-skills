"""Bounded spawn-based TTS scheduling.  Mock output is not real speech.

Real model execution is deliberately disabled: supervisor failure can leave a
worker orphaned, and a Python watchdog cannot guarantee termination while native
code holds the GIL. A held flock is not proof that an orphan has exited.
"""
from __future__ import annotations

import contextlib
import fcntl
import math
import multiprocessing as mp
from multiprocessing.connection import wait
from multiprocessing.reduction import DupFd
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
MEMORY_MIN_PERCENT = 15
MEMORY_INTERVAL = 3.0
DEFAULT_TIMEOUT = 600.0


class ParallelTTSError(RuntimeError):
    """A failed run; completed_indices contains only successful callbacks."""

    def __init__(self, message, completed_indices=(), *, cleanup_confirmed=False):
        self.completed_indices = tuple(completed_indices)
        # False means unverified, not necessarily that any process is alive.
        # Callers must not retry automatically when this is false.
        self.cleanup_confirmed = cleanup_confirmed is True
        super().__init__(f"{message}; completed indices: {list(self.completed_indices)}; "
                         f"cleanup confirmed: {self.cleanup_confirmed}")


def memory_free_percent():
    """macOS system-wide free percentage, or None when unavailable."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(["memory_pressure", "-Q"], capture_output=True,
                                text=True, timeout=5, check=True)
        match = re.search(r"System-wide memory free percentage:\s*(\d+)%", result.stdout)
        return int(match.group(1)) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def _device_lock(path):
    # Never unlink: otherwise existing holders and newcomers can lock different
    # inodes. The sticky temporary directory protects against other users.
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RuntimeError("unsafe device lock file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Reject a replaced pathname, including between open and flock.
        named = os.lstat(path)
        if (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
            raise RuntimeError("device lock path changed")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _watch_parent(parent_pipe):
    try:
        parent_pipe.recv_bytes()
    except (EOFError, OSError):
        os._exit(71)
    # No messages are sent; any message is also a shutdown request.
    os._exit(71)


def _worker(conn, parent_pipe, lock_dup, model, voice_id, speed, worker_fn):
    lock_fd = None
    try:
        # Adopt before doing any model work. If the parent died during spawn,
        # DupFd.detach fails and no work starts. Never explicitly LOCK_UN.
        if lock_dup is not None:
            lock_fd = lock_dup.detach()
        threading.Thread(target=_watch_parent, args=(parent_pipe,), daemon=True).start()
        os.chdir(ROOT)
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        if worker_fn is None:
            from app.engines.registry import get_engine
            from app.engines.base import TTSRequest
            from app.config import load_config
            import json
            with contextlib.redirect_stdout(sys.stderr):
                engine = get_engine(model)
                engine.load()
                # Read existing metadata only; do not instantiate VoiceStore,
                # which creates directories. mock may use an arbitrary voice ID.
                cfg = load_config()
                base = Path(cfg.get("voices_dir", "./voices"))
                if not base.is_absolute():
                    base = ROOT / base
                meta_path = base / voice_id / "meta.json"
                ref = base / voice_id / "ref.wav"
                if meta_path.is_file() and ref.is_file():
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    engine.clone_voice(str(ref), voice_id, meta.get("prompt_text", ""))
                elif model != "mock":
                    raise ValueError("voice reference or metadata missing")
                else:
                    engine.clone_voice("", voice_id, "")
            def synth(chunk):
                req = TTSRequest(text=chunk["text"], phoneme_overrides=chunk["overrides"],
                                 breaks=chunk["breaks"], emphasis_spans=chunk["emphasis_spans"],
                                 voice_id=voice_id, speed=speed)
                with contextlib.redirect_stdout(sys.stderr):
                    return engine.synthesize(req)
        else:
            synth = worker_fn
        conn.send(("ready",))
        while True:
            item = conn.recv()
            if item is None:
                break
            index, chunk = item
            data = synth(chunk)
            if not isinstance(data, bytes):
                raise TypeError("worker must return bytes")
            conn.send(("result", index, data))
    except BaseException as exc:
        try:
            conn.send(("error", f"{type(exc).__name__}: {exc}"))
        except (OSError, EOFError, BrokenPipeError):
            pass
    finally:
        conn.close()
        # The daemon watchdog owns parent_pipe until process exit; closing it
        # here can wake it and turn a graceful shutdown into an emergency exit.
        if lock_fd is not None:
            os.close(lock_fd)


def _run_managed(chunks: list[tuple[int, dict]], model: str, voice_id: str,
                 speed: float, workers: int, on_result, timeout: float = DEFAULT_TIMEOUT,
                 memory_check=None, *, _worker_fn=None, _lock_path=None, _health=None):
    """Run mock chunks in spawned processes; call on_result(index, bytes).

    `_worker_fn` and `_lock_path` are private test-only hooks, accepted only
    for model='mock'. memory_check returns an integer/float percentage or None;
    it runs in the parent. The timeout spans startup, work and graceful exit,
    but cannot interrupt a blocking user callback or memory_check callable.
    The caller must use the usual __main__ guard when spawning processes.
    """
    if type(workers) is not int or workers not in (1, 2, 4):
        raise ValueError("workers must be 1, 2 or 4")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if not isinstance(speed, (int, float)) or isinstance(speed, bool) or not math.isfinite(speed) or not 0.5 <= speed <= 2:
        raise ValueError("speed must be between 0.5 and 2")
    if not callable(on_result) or (memory_check is not None and not callable(memory_check)):
        raise ValueError("on_result and memory_check must be callable")
    if not isinstance(model, str) or not isinstance(voice_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", voice_id):
        raise ValueError("invalid model or voice_id")
    if model != "mock":
        raise ParallelTTSError("real model execution disabled: parent-death termination cannot be guaranteed")
    if _worker_fn is not None and not callable(_worker_fn):
        raise ValueError("_worker_fn must be callable")
    if not isinstance(chunks, list) or any(not isinstance(x, tuple) or len(x) != 2 or type(x[0]) is not int or not isinstance(x[1], dict) for x in chunks):
        raise ValueError("chunks must be a list of (int, dict) tuples")
    if len({x[0] for x in chunks}) != len(chunks):
        raise ValueError("duplicate chunk index")
    if not chunks:
        return
    deadline = time.monotonic() + timeout
    ctx = mp.get_context("spawn")
    states = []
    completed = []
    lock_fd = None
    failure = None
    cleanup_confirmed = True
    next_chunk = 0
    next_check = 0.0
    try:
        # Mock does not claim device access. A private test path exercises the
        # same lock-transfer protocol without touching the production lock.
        if _lock_path is not None:
            lock_fd = _device_lock(os.fspath(_lock_path))
        for _ in range(min(workers, len(chunks))):
            if _health is not None:
                _health()
            if time.monotonic() >= deadline:
                raise TimeoutError("run timed out during startup")
            parent, child = ctx.Pipe()
            watch_read, watch_write = ctx.Pipe(duplex=False)
            proc = ctx.Process(target=_worker, args=(child, watch_read,
                               DupFd(lock_fd) if lock_fd is not None else None,
                               model, voice_id, speed, _worker_fn))
            try:
                proc.start()
            except BaseException:
                # Process.start can fail after platform-dependent partial startup.
                cleanup_confirmed = False
                parent.close(); child.close(); watch_read.close(); watch_write.close()
                raise
            child.close(); watch_read.close()
            states.append({"proc": proc, "conn": parent, "watch": watch_write,
                           "ready": False, "index": None})
        while len(completed) < len(chunks):
            if _health is not None:
                _health()
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError("run timed out")
            if memory_check is not None and now >= next_check:
                pressure = memory_check()
                if (isinstance(pressure, bool) or not isinstance(pressure, (int, float))
                        or not math.isfinite(pressure) or not MEMORY_MIN_PERCENT <= pressure <= 100):
                    raise RuntimeError(f"memory pressure unavailable or insufficient: {pressure!r}")
                next_check = time.monotonic() + MEMORY_INTERVAL
            ready = wait([s["conn"] for s in states] + [s["proc"].sentinel for s in states],
                         timeout=min(0.1, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                raise TimeoutError("run timed out")
            # Drain all ready messages before sending any new work. If a worker
            # failed, no further chunks are dispatched in this iteration.
            results = []
            for s in states:
                if s["conn"] in ready:
                    try:
                        msg = s["conn"].recv()
                    except EOFError:
                        raise RuntimeError("worker exited unexpectedly")
                    if not isinstance(msg, tuple) or not msg:
                        raise RuntimeError("invalid worker message")
                    if msg[0] == "error":
                        raise RuntimeError(f"worker failed: {msg[1]}")
                    if msg == ("ready",) and not s["ready"]:
                        s["ready"] = True
                    elif len(msg) == 3 and msg[0] == "result" and s["index"] == msg[1] and isinstance(msg[2], bytes):
                        results.append((s, msg[1], msg[2]))
                    else:
                        raise RuntimeError("unexpected worker message")
                if s["proc"].sentinel in ready:
                    raise RuntimeError(f"worker exited unexpectedly: {s['proc'].exitcode}")
            for s, index, data in results:
                on_result(index, data)
                completed.append(index)
                s["index"] = None
            for s in states:
                if all(w["ready"] for w in states) and s["ready"] and s["index"] is None and next_chunk < len(chunks):
                    index, chunk = chunks[next_chunk]
                    s["conn"].send((index, chunk))
                    s["index"] = index
                    next_chunk += 1
        for s in states:
            s["conn"].send(None)
        for s in states:
            while s["proc"].is_alive() and time.monotonic() < deadline:
                if _health is not None:
                    _health()
                s["proc"].join(min(.1, max(0, deadline - time.monotonic())))
            if s["proc"].is_alive():
                raise TimeoutError("run timed out during graceful shutdown")
            if s["proc"].exitcode != 0:
                raise RuntimeError(f"worker exited with code {s['proc'].exitcode}")
    except BaseException as exc:
        failure = exc
    finally:
        if failure is not None:
            for s in states:
                try:
                    if s["proc"].is_alive():
                        s["proc"].kill()
                except (OSError, ValueError):
                    cleanup_confirmed = False
            cleanup_deadline = time.monotonic() + 4
            for s in states:
                s["proc"].join(max(0, cleanup_deadline - time.monotonic()))
            alive = [s["proc"].pid for s in states if s["proc"].is_alive()]
            if alive:
                cleanup_confirmed = False
                failure = RuntimeError(f"could not confirm worker termination: {alive}; original: {failure}")
        for s in states:
            s["conn"].close()
            s["watch"].close()
            if not s["proc"].is_alive():
                s["proc"].close()
        if lock_fd is not None:
            os.close(lock_fd)
    if failure is not None:
        raise ParallelTTSError(f"{type(failure).__name__}: {failure}", completed,
                               cleanup_confirmed=cleanup_confirmed) from failure


def _supervisor(conn, chunks, model, voice_id, speed, workers, timeout,
                memory_check, worker_fn, lock_path):
    """Own and reap workers; a dead CLI is observed through its sole pipe."""
    deadline = time.monotonic() + timeout
    next_check = 0.0

    def check_pressure():
        nonlocal next_check
        if memory_check is not None and time.monotonic() >= next_check:
            pressure = memory_check()
            if (isinstance(pressure, bool) or not isinstance(pressure, (int, float))
                    or not math.isfinite(pressure) or not MEMORY_MIN_PERCENT <= pressure <= 100):
                raise RuntimeError(f"memory pressure unavailable or insufficient: {pressure!r}")
            next_check = time.monotonic() + MEMORY_INTERVAL

    def health():
        check_pressure()
        if conn.poll():
            try:
                command = conn.recv()
            except EOFError:
                raise RuntimeError("CLI disconnected")
            raise RuntimeError(f"CLI cancelled: {command!r}")

    def result(index, data):
        conn.send(("result", index, data))
        while True:
            check_pressure()
            if time.monotonic() >= deadline:
                raise TimeoutError("timeout waiting for callback")
            if conn.poll(min(.1, max(0, deadline - time.monotonic()))):
                try:
                    message = conn.recv()
                except EOFError:
                    raise RuntimeError("CLI disconnected")
                if message != ("ack", index):
                    raise RuntimeError("CLI cancelled callback")
                return

    try:
        _run_managed(chunks, model, voice_id, speed, workers, result, timeout,
                     None, _worker_fn=worker_fn, _lock_path=lock_path,
                     _health=health)
        conn.send(("done",))
    except BaseException as exc:
        try:
            conn.send(("error", f"{type(exc).__name__}: {exc}",
                       isinstance(exc, ParallelTTSError) and exc.cleanup_confirmed))
        except (OSError, EOFError):
            pass
    finally:
        conn.close()


def run_parallel(chunks: list[tuple[int, dict]], model: str, voice_id: str,
                 speed: float, workers: int, on_result, timeout: float = DEFAULT_TIMEOUT,
                 memory_check=None, *, _worker_fn=None, _lock_path=None):
    """Schedule mock work through a spawned supervisor and spawned workers.

    The supervisor owns the workers and optional test lock. memory_check and
    private _worker_fn must be spawn-picklable; on_result runs in the caller.
    Timeout cannot interrupt a blocking callback. If the supervisor itself dies,
    child exit cannot be verified by the caller; no retry or fallback is safe.
    """
    if type(workers) is not int or workers not in (1, 2, 4):
        raise ValueError("workers must be 1, 2 or 4")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if not isinstance(speed, (int, float)) or isinstance(speed, bool) or not math.isfinite(speed) or not 0.5 <= speed <= 2:
        raise ValueError("speed must be between 0.5 and 2")
    if not callable(on_result) or (memory_check is not None and not callable(memory_check)):
        raise ValueError("on_result and memory_check must be callable")
    if not isinstance(model, str) or not isinstance(voice_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", voice_id):
        raise ValueError("invalid model or voice_id")
    if model != "mock":
        raise ParallelTTSError("real model execution disabled: supervisor crash can orphan workers")
    if _worker_fn is not None and not callable(_worker_fn):
        raise ValueError("_worker_fn must be callable")
    if not isinstance(chunks, list) or any(not isinstance(x, tuple) or len(x) != 2 or type(x[0]) is not int or not isinstance(x[1], dict) for x in chunks):
        raise ValueError("chunks must be a list of (int, dict) tuples")
    if len({x[0] for x in chunks}) != len(chunks):
        raise ValueError("duplicate chunk index")
    if not chunks:
        return
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    deadline = time.monotonic() + timeout
    process = ctx.Process(target=_supervisor, args=(child, chunks, model, voice_id,
                          speed, workers, timeout, memory_check, _worker_fn, _lock_path))
    completed = []
    failure = None
    started = False
    done = False
    cleanup_confirmed = False
    try:
        process.start()
        started = True
        child.close()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("run timed out")
            ready = wait([parent, process.sentinel], timeout=min(.1, remaining))
            if parent in ready:
                try:
                    msg = parent.recv()
                except EOFError:
                    raise RuntimeError("supervisor exited; worker cleanup unverified")
                if msg[0] == "result":
                    _, index, data = msg
                    on_result(index, data)
                    completed.append(index)
                    parent.send(("ack", index))
                elif msg == ("done",):
                    done = True
                    break
                elif len(msg) == 3 and msg[0] == "error":
                    cleanup_confirmed = msg[2] is True
                    raise RuntimeError(msg[1])
                else:
                    raise RuntimeError("invalid supervisor message")
            elif process.sentinel in ready:
                raise RuntimeError("supervisor exited; worker cleanup unverified")
        process.join(max(0, deadline - time.monotonic()))
        if process.is_alive() or process.exitcode != 0:
            raise RuntimeError("supervisor did not exit cleanly; worker cleanup unverified")
    except BaseException as exc:
        failure = exc
    finally:
        child.close()
        if started:
            if failure is not None and process.is_alive():
                try:
                    parent.send(("cancel",))
                except (OSError, EOFError):
                    pass
                process.join(7)
            if process.is_alive():
                cleanup_confirmed = False
                process.kill()
                process.join(2)
                failure = RuntimeError(f"supervisor unresponsive; worker cleanup unverified; original: {failure}")
            if process.exitcode not in (0, None) and not done:
                cleanup_confirmed = False
                failure = RuntimeError(f"supervisor died; worker cleanup unverified; original: {failure}")
            if not process.is_alive():
                process.close()
        parent.close()
    if failure is not None:
        raise ParallelTTSError(f"{type(failure).__name__}: {failure}", completed,
                               cleanup_confirmed=cleanup_confirmed) from failure
