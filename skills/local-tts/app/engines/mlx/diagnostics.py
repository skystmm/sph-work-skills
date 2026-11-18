import json
import os
import re
import stat
import uuid
from pathlib import Path


class DiagnosticLog:
    _events = {'run_start', 'step_start', 'step_finish', 'step_silence', 'chunk_reused',
               'chunk_saved', 'diagnostic_finish'}
    _fields = {'chunk_index', 'chunk_number', 'step_index', 'step_number', 'attempt_id',
               'worker_request_id', 'outcome', 'before_sha256', 'after_sha256',
               'elapsed_ms', 'milliseconds', 'observation'}

    def __init__(self, path, voice_id, reference_sha256):
        path = Path(path).expanduser()
        if not path.is_absolute() or not path.name:
            raise ValueError('诊断日志必须使用绝对文件路径')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', voice_id):
            raise ValueError('诊断音色 ID 无效')
        if not re.fullmatch(r'[0-9a-f]{64}', reference_sha256):
            raise ValueError('诊断参考音频身份无效')
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        parent_fd = os.open(path.parent, flags)
        try:
            info = os.fstat(parent_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError('诊断日志目录必须由当前用户拥有且仅用户可访问')
            self.fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                              0o600, dir_fd=parent_fd)
            try:
                os.fchmod(self.fd, 0o600)
            except BaseException:
                os.close(self.fd)
                raise
        finally:
            os.close(parent_fd)
        self.run_id = uuid.uuid4().hex
        self.voice_id = voice_id
        self.reference_sha256 = reference_sha256
        try:
            self.record('run_start', observation='worker_path_hash_before_after')
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def record(self, event, **fields):
        if event not in self._events or not set(fields) <= self._fields:
            raise ValueError('诊断日志字段无效')
        for key, value in fields.items():
            if key in {'chunk_index', 'step_index', 'elapsed_ms', 'milliseconds'}:
                valid = type(value) is int and value >= 0
            elif key in {'chunk_number', 'step_number', 'worker_request_id'}:
                valid = type(value) is int and value > 0
            elif key == 'attempt_id':
                valid = isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value)
            elif key in {'before_sha256', 'after_sha256'}:
                valid = value is None or isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)
            elif key == 'outcome':
                valid = value in {'success', 'error', 'unknown', 'reference_mismatch'}
            elif key == 'observation':
                valid = value in {'worker_path_hash_before_after', 'reused_not_observed'}
            else:
                valid = False
            if not valid:
                raise ValueError('诊断日志字段无效')
        record = {'schema': 1, 'event': event, 'run_id': self.run_id, 'model': 'voxcpm2',
                  'backend': 'mlx-audio', 'voice_id': self.voice_id,
                  'task_reference_sha256': self.reference_sha256, **fields}
        payload = (json.dumps(record, ensure_ascii=True, separators=(',', ':')) + '\n').encode('utf-8')
        if len(payload) > 4096:
            raise ValueError('诊断日志记录过大')
        try:
            view = memoryview(payload)
            while view:
                written = os.write(self.fd, view)
                if written <= 0:
                    raise OSError('short write')
                view = view[written:]
            os.fsync(self.fd)
        except OSError as exc:
            raise RuntimeError('诊断日志写入失败') from exc

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.record('diagnostic_finish', outcome='success' if exc_type is None else 'error')
        finally:
            self.close()
