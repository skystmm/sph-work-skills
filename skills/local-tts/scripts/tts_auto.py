"""Conservative, injectable worker selection for local TTS.

``memory_check()`` returns available memory as a percentage (0..100).
``trial(workers, chunks)`` runs the supplied real chunks and returns success,
seconds, and minimum_free_percent. Neither callable is implemented here.
"""

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import stat
import time

_MAX_AGE = 7 * 24 * 60 * 60
_MAX_CACHE_BYTES = 16384


def _number(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value))
    except (OverflowError, ValueError):
        return False


def _profile_hash(profile):
    if not isinstance(profile, dict):
        return None
    if any(not isinstance(profile.get(key), str) or not profile[key].strip()
           for key in ("host", "model", "runtime", "supervisor_protocol")):
        return None
    # JSON object keys must be strings, including nested objects, to avoid
    # collisions caused by JSON's implicit conversion of non-string keys.
    def valid(value):
        if isinstance(value, dict):
            return all(isinstance(k, str) and valid(v) for k, v in value.items())
        if isinstance(value, list):
            return all(valid(v) for v in value)
        return value is None or isinstance(value, (str, bool, int, float))
    try:
        if not valid(profile):
            return None
        data = json.dumps(profile, sort_keys=True, ensure_ascii=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        return None
    return hashlib.sha256(data).hexdigest()


def _memory(memory_check):
    try:
        value = memory_check()
        if _number(value) and 0 <= value <= 100:
            return float(value)
    except Exception:
        pass
    return None


def _result(value):
    if not isinstance(value, dict) or value.get("success") is not True:
        return None
    seconds = value.get("seconds")
    free = value.get("minimum_free_percent")
    if not _number(seconds) or seconds <= 0 or not _number(free) or not 0 <= free <= 100:
        return None
    return {"seconds": float(seconds), "minimum_free_percent": float(free)}


def _private_directory(path):
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077)
    except OSError:
        return False


def _acquire_lock(path):
    """Hold a nonblocking advisory lock on a private, stable regular file."""
    if not hasattr(os, "O_NOFOLLOW"):
        return None
    fd = None
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            os.close(fd)
            return None
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = os.lstat(path)
        if (not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
                or current.st_nlink != 1 or current.st_mode & 0o077
                or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)):
            os.close(fd)
            return None
        return fd
    except OSError:
        if fd is not None:
            os.close(fd)
        return None


def _read_cache(path):
    """Return (record, replaceable). Unknown files are never overwritten."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return None, False
            raw = stream.read(_MAX_CACHE_BYTES + 1)
        if len(raw) > _MAX_CACHE_BYTES:
            return None, False
        obj = json.loads(raw)
        if not isinstance(obj, dict) or set(obj) != {"schema", "profile_hash", "timestamp", "trials"}:
            return None, False
        if obj["schema"] != "tts-auto-v2" or not isinstance(obj["profile_hash"], str):
            return None, False
        if len(obj["profile_hash"]) != 64 or any(c not in "0123456789abcdef" for c in obj["profile_hash"]):
            return None, False
        if not _number(obj["timestamp"]) or not isinstance(obj["trials"], dict) or set(obj["trials"]) != {"1", "2"}:
            return None, False
        for entry in obj["trials"].values():
            if not isinstance(entry, dict) or set(entry) != {"seconds", "minimum_free_percent"}:
                return None, False
            if _result({"success": True, **entry}) is None:
                return None, False
        return obj, True
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return None, False


def _decision(trials):
    one, two = trials["1"], trials["2"]
    if min(one["minimum_free_percent"], two["minimum_free_percent"]) < 25:
        return 1, "试验期间可用内存低于25%"
    if two["seconds"] <= one["seconds"] * 0.8:
        return 2, "双路试验耗时不超过单路的80%"
    return 1, "双路试验加速不足20%"


def _write_cache(path, record):
    temp = path.parent / (".tts-auto-" + secrets.token_hex(16))
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
    except OSError:
        pass


def choose_workers(profile: dict, chunks: list, trial: callable,
                   memory_check: callable, cache_path: Path) -> tuple:
    """Return (1 or 2, reason); failures always choose one worker.

    A private cache directory and a non-waiting descriptor lock protect
    cooperating cache operations. Lock files are never removed. Unrecognized
    cache files are never overwritten. Trials can run without a usable cache.
    """
    if len(chunks) < 4:
        return 1, "分块少于4个"
    digest = _profile_hash(profile)
    if digest is None:
        return 1, "profile未知或缺少host、model、runtime、supervisor_protocol"
    free = _memory(memory_check)
    if free is None or free < 30:
        return 1, "实时可用内存不可用或低于30%"

    path = Path(cache_path)
    lock_fd = None
    if _private_directory(path.parent):
        lock_fd = _acquire_lock(path.with_name(path.name + ".lock"))
        if lock_fd is None:
            return 1, "缓存锁不可用或正在使用"
    try:
        replaceable = False
        if lock_fd is not None:
            cached, replaceable = _read_cache(path)
            now = time.time()
            if (cached is not None and cached["profile_hash"] == digest
                    and 0 <= now - cached["timestamp"] <= _MAX_AGE):
                workers, reason = _decision(cached["trials"])
                return workers, "缓存命中：" + reason
        trials = {}
        for workers in (1, 2):
            before = _memory(memory_check)
            # Never start a dual-worker trial below 30%; unknown is unsafe.
            if before is None or before < (30 if workers == 2 else 25):
                return 1, "试验前可用内存不足或不可用"
            try:
                result = _result(trial(workers, list(chunks[:4])))
            except Exception:
                result = None
            after = _memory(memory_check)
            if (result is None or after is None or after < 25
                    or result["minimum_free_percent"] < 25):
                return 1, "试验失败或期间可用内存不足或不可用"
            trials[str(workers)] = result
        workers, reason = _decision(trials)
        if workers == 2 and after < 30:
            return 1, "试验后实时可用内存低于30%"
        if lock_fd is not None and replaceable:
            _write_cache(path, {"schema": "tts-auto-v2", "profile_hash": digest,
                                "timestamp": time.time(), "trials": trials})
        return workers, "试验结果：" + reason
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
