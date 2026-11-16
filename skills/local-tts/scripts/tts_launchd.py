"""Mock-only launchd adapter; real model execution is forbidden.

No job is registered unless a caller explicitly invokes run with a real adapter.
This module never signals a PID or process group.
"""
from __future__ import annotations

import hmac
import json
import math
import os
import subprocess
from pathlib import Path
import secrets
import socket
import stat
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tts_workers import _run_managed, ParallelTTSError

MAX_FRAME = 1024 * 1024


class LaunchdError(RuntimeError):
    def __init__(self, message, completed_indices=(), cleanup_confirmed=False):
        self.completed_indices = tuple(completed_indices)
        self.cleanup_confirmed = cleanup_confirmed is True
        super().__init__(message)


class LaunchctlAdapter:
    """Fail-closed launchctl prototype; production bootstrap is disabled.

    launchctl has no atomic compare-job-identity-and-bootout operation exposed
    here. A print/bootout pair can unload a replacement job. Therefore this
    adapter refuses bootstrap before making any system call rather than leave a
    job that cannot safely be removed. No injected runner can waive that rule.
    """
    LAUNCHCTL = '/bin/launchctl'
    PYTHON = '/usr/bin/python3'

    def __init__(self, runner=subprocess.run, *, command_timeout=5):
        if not callable(runner) or isinstance(command_timeout, bool) or not isinstance(command_timeout, (int, float)) or not math.isfinite(command_timeout) or command_timeout <= 0:
            raise ValueError('invalid runner or timeout')
        self._runner = runner
        self._timeout = command_timeout
        self.domain = f'gui/{os.getuid()}'

    def bootstrap(self, label, config_path):
        raise RuntimeError('launchctl bootstrap disabled: atomic job ownership and peer identity cannot be verified')

    def authenticate(self, label, pid, pgid):
        raise RuntimeError('launchctl job identity unavailable')

    def bootout(self, label):
        raise RuntimeError('launchctl bootout disabled: atomic job ownership cannot be verified')


def _peer_pid(sock):
    if sys.platform != 'darwin':
        raise RuntimeError('kernel peer PID verification is unavailable on this platform')
    pid = sock.getsockopt(0, 0x002)
    if pid <= 1:
        raise RuntimeError('invalid kernel peer PID')
    return pid


def _send(sock, value):
    data = json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode() + b'\n'
    if len(data) > MAX_FRAME:
        raise ValueError('frame too large')
    sock.sendall(data)


def _recv(sock, deadline):
    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('IPC timeout')
        sock.settimeout(min(.1, remaining))
        try:
            part = sock.recv(1)
        except socket.timeout:
            continue
        if not part:
            raise EOFError('IPC disconnected')
        if part == b'\n':
            return json.loads(data)
        data.extend(part)
        if len(data) >= MAX_FRAME:
            raise ValueError('frame too large')


def _mock(chunk):
    # Deliberately not an engine: no model loading or device access.
    return chunk['text'].encode('utf-8')


def _job(config_path):
    """Entry point for an adapter-owned process; never called by launchctl here."""
    try:
        with open(config_path, encoding='utf-8') as f:
            config = json.load(f)
        pid, pgid = os.getpid(), os.getpgrp()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(config['socket'])
            _send(conn, {'type': 'hello', 'token': config['token'], 'pid': pid, 'pgid': pgid})
            deadline = time.monotonic() + config['timeout']
            if _recv(conn, deadline) != {'type': 'start'} or pid != pgid:
                return 2
            def health():
                # Peek only: a command or disconnect cancels dispatch.
                conn.setblocking(False)
                try:
                    data = conn.recv(1, socket.MSG_PEEK)
                except BlockingIOError:
                    return
                finally:
                    conn.setblocking(True)
                raise RuntimeError('controller disconnected or cancelled')
            def result(index, data):
                _send(conn, {'type': 'result', 'index': index, 'data': data.decode('utf-8')})
                if _recv(conn, deadline) != {'type': 'ack', 'index': index}:
                    raise RuntimeError('controller rejected result')
                if time.monotonic() >= deadline:
                    raise TimeoutError('callback acknowledgement timed out')
            try:
                _run_managed([(x[0], x[1]) for x in config['chunks']], 'mock',
                             'mock', 1.0, config['workers'], result,
                             config['timeout'], _worker_fn=_mock, _health=health)
                _send(conn, {'type': 'done'})
            except BaseException as exc:
                _send(conn, {'type': 'error', 'message': str(exc),
                             'cleanup_confirmed': isinstance(exc, ParallelTTSError) and exc.cleanup_confirmed})
                return 1
        return 0
    except BaseException:
        return 1


def run(chunks, on_result, adapter, *, model='mock', workers=1, timeout=30.0, temp_root=None):
    """Run mock text chunks; callback receives (index, UTF-8 bytes).

    adapter.bootstrap(label, config_path), adapter.authenticate(label, pid, pgid)
    and adapter.bootout(label) return None on success and raise on failure.
    authenticate must compare against an independent, trusted job identity.
    Bootstrap must launch this file as
    ``python tts_launchd.py --job CONFIG`` in its own process group. Bootout
    must target only label and return only after the job has stopped. No retry
    is safe when LaunchdError.cleanup_confirmed is false. Callback time is not
    interruptible. The optional temp_root must already exist.
    """
    if model != 'mock':
        raise LaunchdError('real model execution is disabled')
    if not callable(on_result) or type(workers) is not int or workers not in (1, 2, 4):
        raise ValueError('invalid callback or worker count')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('invalid timeout')
    if not isinstance(chunks, list) or any(not isinstance(x, tuple) or len(x) != 2 or type(x[0]) is not int or not isinstance(x[1], dict) or not isinstance(x[1].get('text'), str) for x in chunks):
        raise ValueError('invalid chunks')
    if len({x[0] for x in chunks}) != len(chunks):
        raise ValueError('duplicate index')
    # Validate serialization and limit configuration before any job creation.
    encoded_chunks = json.dumps(chunks, ensure_ascii=True).encode()
    if len(encoded_chunks) > MAX_FRAME // 2:
        raise ValueError('chunks too large')
    label = 'local.tts.mock.' + secrets.token_hex(16)
    token = secrets.token_hex(32)
    directory = None
    directory_identity = None
    created = {}
    server = conn = None
    attempted = False
    completed = []
    terminal = False
    terminal_cleanup = False
    failure = None
    cleanup_ok = True
    deadline = time.monotonic() + timeout
    try:
        directory = tempfile.mkdtemp(prefix='tts-', dir=temp_root)
        info = os.lstat(directory)
        directory_identity = (info.st_dev, info.st_ino)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError('unsafe private directory')
        config_path = os.path.join(directory, 'config.json')
        socket_path = os.path.join(directory, 'ipc.sock')
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(socket_path)
        info = os.lstat(socket_path)
        created[socket_path] = (info.st_dev, info.st_ino)
        os.chmod(socket_path, 0o600)
        server.listen(1)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(config_path, flags, 0o600)
        info = os.fstat(fd)
        created[config_path] = (info.st_dev, info.st_ino)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            os.close(fd)
            raise RuntimeError('unsafe config file')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'socket': socket_path, 'token': token, 'chunks': chunks,
                       'workers': workers, 'timeout': timeout}, f)
        adapter.bootstrap(label, config_path)
        attempted = True
        server.settimeout(max(0.001, deadline - time.monotonic()))
        conn, _ = server.accept()
        hello = _recv(conn, deadline)
        if (not isinstance(hello, dict) or hello.get('type') != 'hello'
                or not isinstance(hello.get('token'), str)
                or not hmac.compare_digest(hello['token'], token)
                or type(hello.get('pid')) is not int or hello['pid'] <= 1
                or _peer_pid(conn) != hello['pid']
                or type(hello.get('pgid')) is not int or hello['pid'] != hello['pgid']
                or os.getpgid(hello['pid']) != hello['pgid']):
            raise RuntimeError('invalid authenticated pid/pgid handshake')
        adapter.authenticate(label, hello['pid'], hello['pgid'])
        if time.monotonic() >= deadline:
            raise TimeoutError('identity validation timed out')
        _send(conn, {'type': 'start'})
        expected = {index for index, _ in chunks}
        while True:
            msg = _recv(conn, deadline)
            if not isinstance(msg, dict):
                raise RuntimeError('invalid message')
            if msg.get('type') == 'result':
                index, data = msg.get('index'), msg.get('data')
                if type(index) is not int or index not in expected or index in completed or not isinstance(data, str):
                    raise RuntimeError('invalid result')
                on_result(index, data.encode('utf-8'))
                completed.append(index)
                if time.monotonic() >= deadline:
                    raise TimeoutError('callback exceeded deadline')
                _send(conn, {'type': 'ack', 'index': index})
            elif msg == {'type': 'done'} and len(completed) == len(chunks):
                terminal = terminal_cleanup = True
                break
            elif msg.get('type') == 'error' and isinstance(msg.get('message'), str):
                terminal = True
                terminal_cleanup = msg.get('cleanup_confirmed') is True
                raise RuntimeError(msg['message'])
            else:
                raise RuntimeError('unexpected message')
    except BaseException as exc:
        failure = exc
    finally:
        if conn is not None:
            conn.close()
        if server is not None:
            server.close()
        if attempted:
            try:
                adapter.bootout(label)
            except BaseException as exc:
                cleanup_ok = False
                if failure is None:
                    failure = exc
        if directory is not None:
            try:
                info = os.lstat(directory)
                if (info.st_dev, info.st_ino) != directory_identity or not stat.S_ISDIR(info.st_mode):
                    raise RuntimeError('private directory replaced')
                for path, identity in created.items():
                    try:
                        info = os.lstat(path)
                        if (info.st_dev, info.st_ino) == identity:
                            os.unlink(path)
                        else:
                            cleanup_ok = False
                    except FileNotFoundError:
                        pass
                    except OSError:
                        cleanup_ok = False
                os.rmdir(directory)
            except (OSError, RuntimeError):
                cleanup_ok = False
        if not cleanup_ok and failure is None:
            failure = RuntimeError('job or private workspace cleanup failed')
    if failure is not None:
        raise LaunchdError(f'{type(failure).__name__}: {failure}', completed,
                           cleanup_ok and terminal and terminal_cleanup) from failure
    return tuple(completed)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--job':
        raise SystemExit(_job(sys.argv[2]))
    raise SystemExit('mock job entry only')
