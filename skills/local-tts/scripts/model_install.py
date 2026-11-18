#!/usr/bin/env python3
"""Install a model from a locally trusted, pinned manifest; stdlib only."""
import argparse
import ctypes
import errno
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import sys
import urllib.parse
import urllib.request

MANIFESTS = Path(__file__).resolve().parent / 'model_manifests'
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')
HEX40 = re.compile(r'[0-9a-fA-F]{40}\Z')
HEX64 = re.compile(r'[0-9a-fA-F]{64}\Z')
HF_SOURCES = ('https://huggingface.co', 'https://hf-mirror.com')


class InstallError(Exception):
    pass


def validate_manifest(m, expected=None):
    if not isinstance(m, dict) or set(m) not in (
            {'schema', 'model', 'repository', 'revision', 'files'},
            {'schema', 'model', 'repository', 'revision', 'files', 'source'}):
        raise InstallError('invalid manifest fields')
    if 'source' in m and m['source'] != 'huggingface':
        raise InstallError('unsupported model source')
    if type(m['schema']) is not int or m['schema'] != 1:
        raise InstallError('unsupported manifest schema')
    model, repo, rev = m['model'], m['repository'], m['revision']
    if not isinstance(model, str) or not ID.fullmatch(model) or model in ('.', '..') or (expected is not None and model != expected):
        raise InstallError('invalid model ID')
    if not isinstance(repo, str) or len(repo.split('/')) != 2 or any(not ID.fullmatch(x) or x in ('.', '..') for x in repo.split('/')):
        raise InstallError('invalid repository')
    if not isinstance(rev, str) or not HEX40.fullmatch(rev):
        raise InstallError('revision must be a full 40-digit hex commit')
    if not isinstance(m['files'], list) or not m['files']:
        raise InstallError('files must be a nonempty list')
    paths = set()
    for item in m['files']:
        if not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256'}:
            raise InstallError('invalid file entry')
        p, size, digest = item['path'], item['size'], item['sha256']
        if not isinstance(p, str) or not p or '\\' in p or '\x00' in p or p.startswith('/') or any(x in ('', '.', '..') for x in p.split('/')):
            raise InstallError('unsafe file path')
        if any(x.endswith('.part') or x == '.manifest-id' for x in p.split('/')):
            raise InstallError('reserved path')
        if type(size) is not int or size < 0 or not isinstance(digest, str) or not HEX64.fullmatch(digest):
            raise InstallError('invalid file size or SHA-256')
        if p in paths:
            raise InstallError('duplicate path')
        paths.add(p)
    for p in paths:
        if any('/'.join(p.split('/')[:i]) in paths for i in range(1, len(p.split('/')))):
            raise InstallError('file/directory path conflict')
    return m


def load_manifest(model):
    if not isinstance(model, str) or not ID.fullmatch(model) or model in ('.', '..'):
        raise InstallError('invalid model ID')
    path = MANIFESTS / (model + '.json')
    # A built-in manifest is a local trust root, never a remote manifest.
    with open(path, 'rb') as f:
        return validate_manifest(json.load(f), model)


def checked_dir(path):
    """Require an existing absolute directory path with no symlink components."""
    p = Path(os.path.abspath(path))
    cur = Path(p.anchor)
    for part in p.parts[1:]:
        cur = cur / part
        if not stat.S_ISDIR(os.lstat(cur).st_mode):
            raise InstallError('not a real directory: ' + str(cur))
    return p


def regular_open(path, flags=os.O_RDONLY, mode=0o600):
    truncate = bool(flags & os.O_TRUNC)
    fd = os.open(path, (flags & ~os.O_TRUNC) | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), mode)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise InstallError('not a regular unlinked file: ' + str(path))
    if truncate:
        os.ftruncate(fd, 0)
    return fd


def digest_file(path, entry):
    fd = regular_open(path)
    h = hashlib.sha256()
    n = 0
    with os.fdopen(fd, 'rb') as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            n += len(block)
            h.update(block)
    if n != entry['size'] or h.hexdigest() != entry['sha256'].lower():
        raise InstallError('size or SHA-256 mismatch: ' + entry['path'])


def verify_tree(directory, manifest):
    directory = checked_dir(directory)
    expected = {e['path'] for e in manifest['files']}
    actual = set()
    for root, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs:
            if not stat.S_ISDIR(os.lstat(Path(root) / name).st_mode):
                raise InstallError('symlink or non-directory in model tree')
        for name in files:
            path = Path(root) / name
            if not stat.S_ISREG(os.lstat(path).st_mode):
                raise InstallError('non-regular file in model tree')
            actual.add(path.relative_to(directory).as_posix())
    if actual != expected:
        raise InstallError('model directory contents differ from manifest')
    for entry in manifest['files']:
        digest_file(directory / entry['path'], entry)


def mkdir_parents(root, relative):
    current = root
    for name in relative.split('/')[:-1]:
        current = current / name
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            if not stat.S_ISDIR(os.lstat(current).st_mode):
                raise InstallError('unsafe staging directory')


def publish_noreplace(source, destination):
    """Atomic same-filesystem rename, with kernel-enforced no replacement."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        fn = libc.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        args = (os.fsencode(source), os.fsencode(destination), 0x4)  # RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        args = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    else:
        raise InstallError('atomic no-replace rename is unsupported on this OS')
    fn.restype = ctypes.c_int
    if fn(*args) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(destination))


def download(entry, path, manifest, base_url):
    if manifest.get('source') == 'huggingface':
        suffix = '/' + manifest['repository'] + '/resolve/' + manifest['revision'] + '/'
        suffix += urllib.parse.quote(entry['path'], safe='/')
        for index, origin in enumerate(HF_SOURCES):
            try:
                return download_url(entry, path, origin + suffix)
            except (InstallError, OSError, http.client.HTTPException) as exc:
                if index + 1 == len(HF_SOURCES):
                    raise InstallError('official and mirror downloads failed: ' + entry['path']) from exc
                print('official model download failed; trying mirror: ' + entry['path'], file=sys.stderr)
    else:
        query = urllib.parse.urlencode({'Revision': manifest['revision'], 'FilePath': entry['path']})
        url = base_url.rstrip('/') + '/api/v1/models/' + manifest['repository'] + '/repo?' + query
        return download_url(entry, path, url)


def download_url(entry, path, url):
    fd = regular_open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'r+b') as out:
        size = os.fstat(out.fileno()).st_size
        if size > entry['size']:
            raise InstallError('partial file exceeds expected size: ' + entry['path'])
        if size == entry['size']:
            # A completed interrupted transfer can be reused after verification.
            try:
                digest_file(path, entry)
                return
            except InstallError:
                out.seek(0)
                out.truncate()
                size = 0
        request = urllib.request.Request(url, headers={'Range': 'bytes=%d-' % size} if size else {})
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            if status == 206:
                header = response.headers.get('Content-Range', '')
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', header)
                if not match or int(match[1]) != size or int(match[2]) != entry['size'] - 1 or int(match[3]) != entry['size']:
                    raise InstallError('invalid Content-Range: ' + header)
            elif status == 200:
                # Server ignored Range: restart rather than append.
                out.seek(0)
                out.truncate()
                size = 0
            else:
                raise InstallError('unexpected HTTP status: ' + str(status))
            out.seek(size)
            while True:
                block = response.read1(min(1024 * 1024, entry['size'] - size + 1))
                if not block:
                    break
                if size + len(block) > entry['size']:
                    raise InstallError('HTTP response exceeds expected size')
                out.write(block)
                size += len(block)
    digest_file(path, entry)


def run_operation(operation, manifest, destination, source=None, base_url='https://www.modelscope.cn'):
    """Internal API: manifest/base_url injection is for controlled tests only."""
    validate_manifest(manifest)
    destination = Path(os.path.abspath(destination))
    parent = checked_dir(destination.parent)
    if destination.name in ('', '.', '..'):
        raise InstallError('invalid destination')
    if os.path.lexists(destination):
        verify_tree(destination, manifest)
        return 'verified existing destination'
    if operation == 'verify':
        raise InstallError('destination does not exist')
    if operation not in ('install', 'import'):
        raise InstallError('unknown operation')
    source_root = checked_dir(source) if operation == 'import' and source is not None else None
    if operation == 'import' and source_root is None:
        raise InstallError('import requires a source directory')
    lock_path = parent / ('.' + destination.name + '.install.lock')
    lockfd = regular_open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError('another installer holds the destination lock')
        if os.path.lexists(destination):
            verify_tree(destination, manifest)
            return 'verified existing destination'
        stage = parent / ('.' + destination.name + '.install-staging')
        try:
            os.mkdir(stage, 0o700)
        except FileExistsError:
            if not stat.S_ISDIR(os.lstat(stage).st_mode):
                raise InstallError('unsafe staging path')
        stage_info = os.lstat(stage)
        if stage_info.st_uid != os.geteuid() or stat.S_IMODE(stage_info.st_mode) & 0o077:
            raise InstallError('staging directory must be private and owned by current user')
        marker = stage / '.manifest-id'
        identity = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest().encode()
        try:
            fd = regular_open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(identity)
        except FileExistsError:
            fd = regular_open(marker)
            with os.fdopen(fd, 'rb') as f:
                if f.read(65) != identity:
                    raise InstallError('staging manifest mismatch')
        for entry in manifest['files']:
            mkdir_parents(stage, entry['path'])
            final = stage / entry['path']
            part = Path(str(final) + '.part')
            if os.path.lexists(final):
                digest_file(final, entry)
                continue
            if operation == 'install':
                download(entry, part, manifest, base_url)
            else:
                source_file = source_root / entry['path']
                checked_dir(source_file.parent)
                infd = regular_open(source_file)
                try:
                    outfd = regular_open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(infd, 'rb') as inp, os.fdopen(outfd, 'wb') as out:
                        infd = -1
                        while True:
                            block = inp.read(1024 * 1024)
                            if not block:
                                break
                            out.write(block)
                finally:
                    if infd != -1:
                        os.close(infd)
                digest_file(part, entry)
            os.rename(part, final)
        # Do not publish unlisted files or links left in an interrupted staging tree.
        os.unlink(marker)
        try:
            verify_tree(stage, manifest)
            publish_noreplace(stage, destination)
        except Exception:
            # Restore identity for safe retry; never touch a newly published destination.
            if os.path.isdir(stage) and not os.path.lexists(marker):
                fd = regular_open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as f:
                    f.write(identity)
            raise
        return 'installed'
    finally:
        os.close(lockfd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('install', 'import', 'verify'):
        p = sub.add_parser(command)
        p.add_argument('model', help='built-in manifest model ID')
        p.add_argument('destination', help='explicit destination directory')
        if command == 'import':
            p.add_argument('source', help='local model directory')
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.model)
        print(run_operation(args.command, manifest, args.destination, getattr(args, 'source', None)))
        return 0
    except (InstallError, OSError, ValueError, http.client.HTTPException) as exc:
        print('model installer: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
