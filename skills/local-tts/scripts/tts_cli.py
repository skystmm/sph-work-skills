#!/usr/bin/env python3
import argparse
import contextlib
import fcntl
import hashlib
import importlib.metadata
import io
import json
import os
import re
import sys
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import numpy as np
import soundfile as sf

from app.config import load_config
from app.engines.base import TTSRequest
from app.engines.registry import get_engine, REGISTRY
from app.parser.ssml import apply_confirmed_drops, parse_ssml, TOKEN_RE
from app.voice_store import VoiceStore

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def voice_id(value):
    if not ID_RE.fullmatch(value) or value == "default":
        raise ValueError("音色 ID 只能包含字母、数字、下划线或连字符，最长 64 字符")
    return value


def store_and_settings():
    if not Path("config.yaml").is_file():
        raise ValueError("未找到项目 config.yaml，无法确定模型和音色库")
    config = load_config()
    store = VoiceStore(config.get("voices_dir", "./voices"))
    return config, store, store.base / ".tts-cli.json"


def settings_read(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def settings_write(path, data):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def lookup_voice(store, value):
    voice_id(value)
    path = store.base / value
    if path.is_symlink() or path.resolve().parent != store.base.resolve():
        raise ValueError("音色路径不安全")
    meta = store.get(value)
    if not meta or (path / "ref.wav").is_symlink() or not (path / "ref.wav").is_file():
        raise ValueError(f"音色不存在或参考音频不可用: {value}")
    return meta


def text_input(args):
    if args.text is not None:
        return args.text
    if args.file is not None:
        return Path(args.file).read_text(encoding="utf-8-sig")
    return sys.stdin.read()


def parsed_input(text):
    if not text.strip():
        raise ValueError("文字不能为空")
    parsed = parse_ssml(text)
    for match in TOKEN_RE.finditer(text):
        annotated = match.group("tag_char") if match.group("tag_ph") else match.group("in_char")
        if annotated is not None and len(annotated) != 1:
            raise ValueError("注音标记须逐字指定")
    for py in parsed.phoneme_overrides.values():
        if not re.fullmatch(r"[a-züv]+[0-4]", py):
            raise ValueError(f"拼音必须包含声调数字 0-4: {py}")
    if not parsed.plain_text.strip():
        raise ValueError("没有可合成的文字")
    return parsed


def engine_for(name):
    if name not in REGISTRY:
        raise ValueError(f"未知模型: {name}")
    engine = get_engine(name)
    if not engine.implemented:
        raise ValueError(f"模型尚未实现: {name}")
    return engine


# Bump whenever chunking, synthesis or the on-disk contract changes.
TASK_VERSION = 2


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def model_identity(config, model, engine):
    cfg = config.get('engines', {}).get(model, {})
    model_path = cfg.get('model_path') or getattr(engine, 'default_model_dir', None)
    if not model_path:
        return None
    root = Path(model_path)
    if not root.exists():
        raise ValueError(f'模型文件不存在: {root}')
    if root.is_file():
        files = [(root.name, file_hash(root))]
    else:
        files = []
        for directory, dirs, names in os.walk(root, followlinks=False):
            for name in sorted(dirs):
                if (Path(directory) / name).is_symlink():
                    raise ValueError('模型目录不能包含目录符号链接')
            for name in sorted(names):
                path = Path(directory) / name
                if not path.is_file():
                    raise ValueError(f'模型文件不可读取: {path}')
                files.append((path.relative_to(root).as_posix(), file_hash(path)))
        files.sort()
    return {'path': str(root.resolve()), 'files': files}


def runtime_identity():
    source = [Path(__file__), ROOT / 'app/parser/chunker.py', ROOT / 'app/parser/ssml.py',
              ROOT / 'app/engines/__init__.py', ROOT / 'app/engines/base.py',
              ROOT / 'app/engines/registry.py', ROOT / 'app/engines/mock_engine.py',
              ROOT / 'app/engines/cosyvoice/engine.py', ROOT / 'app/engines/cosyvoice/pinyin_tokens.py']
    upstream = ROOT / 'third_party/CosyVoice/cosyvoice'
    if upstream.is_dir():
        source.extend(sorted(upstream.rglob('*.py')))
    packages = {}
    for name in ('torch', 'torchaudio', 'transformers', 'numpy', 'soundfile', 'onnxruntime'):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {'python': sys.version, 'packages': packages,
            'source_files': [(str(path.relative_to(ROOT)), file_hash(path)) for path in source]}


def atomic_bytes(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_json(path, data):
    atomic_bytes(path, json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'))


def read_task_json(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'任务记录缺失或不安全: {path}')
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError(f'任务记录过大: {path}')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise ValueError(f'任务记录损坏: {path}') from exc


def checked_audio(data):
    try:
        samples, rate = sf.read(io.BytesIO(data), dtype='float32', always_2d=True)
    except Exception as exc:
        raise ValueError('音频损坏或格式无效') from exc
    if not len(samples) or rate <= 0 or not np.isfinite(samples).all():
        raise ValueError('生成的音频为空或无效')
    return samples, rate


@contextlib.contextmanager
def task_lock(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('任务目录不能是符号链接')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / '.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('任务目录正被其他进程使用') from exc
        yield directory
    finally:
        os.close(fd)


def synthesize_piece(engine, chunk, selected, speed, accepted_controls=(), reference_mode='reference',
                     reference_fingerprint=None):
    req = TTSRequest(text=chunk['text'], phoneme_overrides=chunk['overrides'], breaks=chunk['breaks'],
                     emphasis_spans=chunk['emphasis_spans'], voice_id=selected, speed=speed,
                     emotion_spans=chunk.get('emotion_spans', []), accepted_controls=accepted_controls,
                     reference_mode=reference_mode, reference_fingerprint=reference_fingerprint)
    with contextlib.redirect_stdout(sys.stderr):
        audio = engine.synthesize_standard(req)
    return checked_audio(audio)


def task_pieces(directory, identity, chunks, engine, selected, speed, processor=None, accepted_controls=(),
                allowed_pending=None, diagnostic=None, reference_mode='reference', reference_fingerprint=None):
    if diagnostic is not None and processor is not None:
        raise ValueError('诊断模式不支持并行处理器')
    manifest = directory / 'task.json'
    if not manifest.exists() and not manifest.is_symlink():
        if any(p.name != '.lock' for p in directory.iterdir()):
            raise ValueError('任务目录不是空目录且没有任务记录')
        atomic_json(manifest, identity)
    elif read_task_json(manifest) != identity:
        raise ValueError('任务身份不兼容，拒绝恢复')
    pieces, pending = {}, []
    for i, chunk in enumerate(chunks):
        record_path = directory / f'{i:06d}.json'
        data = None
        if record_path.exists() or record_path.is_symlink():
            record = read_task_json(record_path)
            if (not isinstance(record, dict) or set(record) != {'index', 'sha256', 'frames', 'sample_rate', 'channels'}
                    or type(record['index']) is not int or record['index'] != i
                    or not isinstance(record['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', record['sha256'])
                    or any(type(record[key]) is not int or record[key] <= 0 for key in ('frames', 'sample_rate', 'channels'))):
                raise ValueError(f'任务分块记录损坏: {record_path}')
            wav = directory / (record['sha256'] + '.wav')
            if wav.is_symlink() or (wav.exists() and not wav.is_file()):
                raise ValueError(f'任务音频路径不安全: {wav}')
            if wav.exists():
                if wav.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError(f'任务分块音频过大: {wav}')
                payload = wav.read_bytes()
                corrupt = hashlib.sha256(payload).hexdigest() != record['sha256']
                if not corrupt:
                    try:
                        data, sr = checked_audio(payload)
                    except ValueError:
                        corrupt = True
                if corrupt:
                    quarantine = directory / f'{wav.name}.corrupt-{uuid.uuid4().hex}'
                    os.link(wav, quarantine, follow_symlinks=False)
                    wav.unlink()
                    data = None
                elif (len(data), sr, data.shape[1]) != (record['frames'], record['sample_rate'], record['channels']):
                    raise ValueError(f'任务分块记录属性不匹配: {record_path}')
        if data is None:
            pending.append((i, chunk))
        else:
            pieces[i] = (data, sr)
            if diagnostic is not None:
                diagnostic.record('chunk_reused', chunk_index=i, chunk_number=i + 1,
                                  observation='reused_not_observed')

    expected = {i for i, _ in pending}
    if allowed_pending is not None and not expected <= set(allowed_pending):
        raise ValueError('单块重生成发现其他分块缺失或损坏，拒绝额外生成')
    def save(index, audio):
        if index not in expected or index in pieces:
            raise ValueError(f'重复或未知的分块结果: {index}')
        data, sr = checked_audio(audio)
        buf = io.BytesIO()
        sf.write(buf, data, sr, format='WAV', subtype='FLOAT')
        payload = buf.getvalue()
        digest = hashlib.sha256(payload).hexdigest()
        wav = directory / (digest + '.wav')
        if wav.is_symlink() or (wav.exists() and (not wav.is_file() or file_hash(wav) != digest)):
            raise ValueError(f'任务音频路径冲突: {wav}')
        if not wav.exists():
            atomic_bytes(wav, payload)
        atomic_json(directory / f'{index:06d}.json', {'index': index, 'sha256': digest, 'frames': len(data), 'sample_rate': sr, 'channels': data.shape[1]})
        if diagnostic is not None:
            diagnostic.record('chunk_saved', chunk_index=index, chunk_number=index + 1, outcome='success')
        pieces[index] = (data, sr)

    if processor:
        processor(pending, save)
    else:
        for i, chunk in pending:
            print(f'正在合成 {i + 1}/{len(chunks)}', file=sys.stderr)
            context = engine.diagnostic_chunk(diagnostic, i) if diagnostic is not None else contextlib.nullcontext()
            with context:
                data, sr = synthesize_piece(engine, chunk, selected, speed, accepted_controls,
                                            reference_mode, reference_fingerprint)
            buf = io.BytesIO()
            sf.write(buf, data, sr, format='WAV', subtype='FLOAT')
            save(i, buf.getvalue())
    if len(pieces) != len(chunks):
        raise ValueError('尚有分块未完成')
    ordered = [pieces[i] for i in range(len(chunks))]
    sample_rate, channels = ordered[0][1], ordered[0][0].shape[1]
    if any(sr != sample_rate or data.shape[1] != channels for data, sr in ordered):
        raise ValueError('生成的音频采样率或声道数不一致')
    return [data for data, _ in ordered], sample_rate


def read_verified_chunk(directory, index):
    record = read_task_json(directory / f'{index:06d}.json')
    if (not isinstance(record, dict) or set(record) != {'index', 'sha256', 'frames', 'sample_rate', 'channels'}
            or type(record['index']) is not int or record['index'] != index
            or not isinstance(record['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', record['sha256'])
            or any(type(record[key]) is not int or record[key] <= 0
                   for key in ('frames', 'sample_rate', 'channels'))):
        raise ValueError(f'任务分块记录无效: {index + 1}')
    wav = directory / (record['sha256'] + '.wav')
    if wav.is_symlink() or not wav.is_file():
        raise ValueError(f'任务分块音频缺失或不安全: {index + 1}')
    if wav.stat().st_size > 64 * 1024 * 1024:
        raise ValueError(f'任务分块音频过大: {index + 1}')
    payload = wav.read_bytes()
    if hashlib.sha256(payload).hexdigest() != record['sha256']:
        raise ValueError(f'任务分块音频损坏: {index + 1}')
    data, rate = checked_audio(payload)
    if (len(data), rate, data.shape[1]) != (record['frames'], record['sample_rate'], record['channels']):
        raise ValueError(f'任务分块音频损坏: {index + 1}')
    return record


def task_reference_mode(identity):
    if not isinstance(identity, dict) or type(identity.get('version')) is not int or identity['version'] not in (1, TASK_VERSION):
        raise ValueError('任务版本无效或不支持')
    mode = identity.get('reference_mode', 'reference')
    if mode not in ('reference', 'reference_plus_prompt'):
        raise ValueError('任务参考音频模式无效')
    fingerprint = identity.get('reference_fingerprint')
    if mode == 'reference_plus_prompt':
        if identity['version'] == 1:
            raise ValueError('旧版任务不支持参考音频加逐字稿模式')
        if (identity.get('model') != 'voxcpm2' or not isinstance(fingerprint, dict)
                or set(fingerprint) != {'audio_sha256', 'prompt_sha256'}
                or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)
                       for value in fingerprint.values())
                or fingerprint['audio_sha256'] != identity.get('voice_wav_sha256')):
            raise ValueError('任务参考音频身份无效')
        meta = identity.get('voice_metadata')
        text = meta.get('prompt_text') if isinstance(meta, dict) else None
        if (not isinstance(text, str) or not text.strip()
                or hashlib.sha256(text.encode('utf-8')).hexdigest() != fingerprint['prompt_sha256']):
            raise ValueError('任务逐字稿身份无效')
    elif fingerprint is not None:
        raise ValueError('任务参考音频身份与模式不匹配')
    return mode


def inspect_task(task_dir):
    directory = Path(task_dir)
    if (directory.is_symlink() or not directory.is_dir() or (directory / '.lock').is_symlink()
            or not (directory / '.lock').is_file()):
        raise ValueError('任务目录不存在或不安全')
    with task_lock(directory):
        identity = read_task_json(directory / 'task.json')
        chunks = identity.get('chunks') if isinstance(identity, dict) else None
        if not isinstance(chunks, list) or not chunks:
            raise ValueError('任务分块记录无效')
        result, elapsed = [], 0.0
        for i, chunk in enumerate(chunks):
            if not isinstance(chunk, dict) or not isinstance(chunk.get('text'), str):
                raise ValueError(f'任务分块文本无效: {i + 1}')
            controls = {'phoneme_overrides': chunk.get('overrides', {}),
                        'breaks': chunk.get('breaks', []), 'emphasis_spans': chunk.get('emphasis_spans', []),
                        'emotion_spans': chunk.get('emotion_spans', [])}
            path = directory / f'{i:06d}.json'
            if not path.exists() and not path.is_symlink():
                result.append({'index': i + 1, 'text': chunk['text'], 'controls': controls, 'status': 'missing',
                               'start_seconds': elapsed, 'end_seconds': None})
                elapsed = None
                continue
            record = read_verified_chunk(directory, i)
            end = None if elapsed is None else elapsed + record['frames'] / record['sample_rate']
            result.append({'index': i + 1, 'text': chunk['text'], 'controls': controls, 'status': 'verified',
                           'start_seconds': None if elapsed is None else round(elapsed, 3),
                           'end_seconds': None if end is None else round(end, 3),
                           'sha256': record['sha256']})
            elapsed = end
        return {'task_dir': str(directory.resolve()), 'version': identity.get('version'),
                'model': identity.get('model'), 'voice_id': identity.get('voice_id'),
                'reference_mode': task_reference_mode(identity), 'chunks': result}


def seed_regenerated_task(directory, source_task, chunk_index, identity, allow_runtime_change):
    source = Path(source_task)
    if source.is_symlink() or not source.is_dir() or not (source / 'task.json').is_file():
        raise ValueError('源任务目录不存在或不安全')
    source = source.resolve()
    target = Path(directory).resolve()
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError('源任务和新任务目录必须相互独立')
    if (source / '.lock').is_symlink() or not (source / '.lock').is_file():
        raise ValueError('源任务锁不存在或不安全')
    with task_lock(source):
        prior = read_task_json(source / 'task.json')
        if not isinstance(prior, dict):
            raise ValueError('源任务身份无效')
        if (task_reference_mode(prior) != task_reference_mode(identity)
                or prior.get('reference_fingerprint') != identity.get('reference_fingerprint')):
            raise ValueError('源任务参考音频模式或身份不匹配，拒绝混用分块')
        comparable = ('text', 'plain_text', 'overrides', 'breaks', 'emphasis_spans', 'chunks',
                      'voice_id', 'voice_metadata', 'voice_wav_sha256', 'model', 'config',
                      'model_files', 'speed')
        if any(prior.get(key) != identity.get(key) for key in comparable):
            raise ValueError('源任务文本、分块、声音、模型或配置不匹配，拒绝重生成')
        for key in ('accepted_controls', 'dropped_controls', 'emotion_spans'):
            if prior.get(key, []) != identity.get(key, []):
                raise ValueError('源任务控制项或确认不匹配，拒绝重生成')
        changed_fields = [key for key in ('version', 'runtime', 'adapter_runtime')
                          if prior.get(key) != identity.get(key)]
        runtime_changed = bool(changed_fields)
        if runtime_changed and not allow_runtime_change:
            raise ValueError('源任务与当前运行环境不同（' + ', '.join(changed_fields)
                             + '）；其余分块来自旧环境，新块来自当前环境。确认后使用 --accept-source-runtime-change')
        chunks = identity['chunks']
        if chunk_index < 0 or chunk_index >= len(chunks):
            raise ValueError('重生成分块编号超出范围')
        records = [read_verified_chunk(source, i) for i in range(len(chunks))]
        parent = {'source_task': str(source), 'task_sha256': file_hash(source / 'task.json'),
                  'chunk_hashes': [record['sha256'] for record in records],
                  'regenerate_chunk': chunk_index + 1,
                  'runtime_changed_fields': changed_fields,
                  'runtime_change_confirmed': bool(runtime_changed and allow_runtime_change),
                  'runtime_change_acceptance': bool(allow_runtime_change)}
        identity = dict(identity, parent=parent)
        manifest = directory / 'task.json'
        if not manifest.exists() and not manifest.is_symlink():
            if any(path.name != '.lock' for path in directory.iterdir()):
                raise ValueError('新任务目录不是空目录')
            atomic_json(manifest, identity)
        elif read_task_json(manifest) != identity:
            raise ValueError('新任务身份不兼容，拒绝恢复')
        for i, record in enumerate(records):
            if i == chunk_index:
                continue
            record_path = directory / f'{i:06d}.json'
            wav_path = directory / (record['sha256'] + '.wav')
            record_exists = record_path.exists() or record_path.is_symlink()
            if record_exists and read_task_json(record_path) != record:
                raise ValueError(f'新任务已有不同分块: {i + 1}')
            if (wav_path.is_symlink() or (wav_path.exists()
                    and (not wav_path.is_file() or file_hash(wav_path) != record['sha256']))):
                raise ValueError(f'新任务音频路径冲突: {i + 1}')
            if not wav_path.exists():
                payload = (source / (record['sha256'] + '.wav')).read_bytes()
                if hashlib.sha256(payload).hexdigest() != record['sha256']:
                    raise ValueError(f'源任务音频在复制时变化: {i + 1}')
                atomic_bytes(wav_path, payload)
            if not record_exists:
                atomic_json(record_path, record)
    return identity


def run(args):
    if args.command != 'synth' or not getattr(args, 'offline', False):
        return _run(args)

    from app.model_preflight import offline_environment, validate_model
    with offline_environment():
        config = load_config()
        model = args.model or config.get('engine', 'cosyvoice3')
        if model not in ('cosyvoice2', 'cosyvoice3'):
            raise ValueError('--offline 仅支持 cosyvoice2 和 cosyvoice3')
        model_path = config.get('engines', {}).get(model, {}).get('model_path')
        if not isinstance(model_path, str) or not model_path.strip():
            raise ValueError('离线模式需要在配置中指定本地 model_path')
        engine = engine_for(model)
        if engine.model_dir.expanduser().resolve() != Path(model_path).expanduser().resolve():
            raise ValueError('缓存的模型路径与配置不一致；请使用新进程')
        if engine.is_loaded():
            raise ValueError('模型已加载，无法确认离线状态；请在新进程中重试')
        validate_model(engine.model_dir, model)
        previous = engine.offline
        engine.offline = True
        try:
            return _run(args)
        finally:
            engine.offline = previous


def _run(args):
    config, store, settings = store_and_settings()
    if args.command == "models":
        result = []
        for name in REGISTRY:
            engine = get_engine(name)
            if engine.implemented:
                result.append({"id": name, "capabilities": vars(engine.get_capability()),
                               "capability_report": engine.capabilities(),
                               "capability_context": engine.capability_context(),
                               "readiness": engine.readiness()})
        return {"configured": config.get("engine", "cosyvoice3"), "models": result,
                "note": "已实现不代表模型权重可加载；mock 仅用于测试"}
    if args.command == "voices":
        if args.action == "list":
            default = settings_read(settings).get("default_voice")
            return {"default_voice": default, "voices": [{"voice_id": v["voice_id"], "name": v["name"]} for v in store.list()]}
        if args.action == "default":
            if args.voice:
                lookup_voice(store, args.voice)
                data = settings_read(settings)
                data["default_voice"] = args.voice
                settings_write(settings, data)
            return {"default_voice": settings_read(settings).get("default_voice")}
        if not args.authorized:
            raise ValueError("请确认参考声音属于本人或已获得授权，并传入 --authorized")
        voice_id(args.id)
        path = store.base / args.id
        if path.exists() or path.is_symlink():
            raise ValueError(f"音色 ID 已存在: {args.id}")
        if not args.prompt_text.strip():
            raise ValueError("请提供与参考音频逐字一致的文本")
        audio, sr = sf.read(args.audio, dtype="float32", always_2d=True)
        if len(audio) == 0 or sr < 16000:
            raise ValueError("参考音频为空或采样率低于 16kHz")
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
            sf.write(tmp.name, audio.mean(axis=1), sr)
            meta = store.save(args.id, tmp.name, name=args.name, prompt_text=args.prompt_text.strip())
        return {"voice_id": meta["voice_id"], "name": meta["name"], "status": "saved", "duration_seconds": len(audio) / sr,
                "note": "音色资料已保存；需生成样音并试听确认效果"}
    if args.command == 'task':
        if args.action == 'inspect':
            return inspect_task(args.task_dir)
        source = Path(args.source_task)
        if source.is_symlink() or not source.is_dir():
            raise ValueError('源任务目录不存在或不安全')
        prior = read_task_json(source / 'task.json')
        if (not isinstance(prior, dict) or not isinstance(prior.get('text'), str)
                or not isinstance(prior.get('voice_id'), str) or not isinstance(prior.get('model'), str)
                or type(prior.get('speed')) not in (int, float)):
            raise ValueError('源任务身份无效')
        derived = argparse.Namespace(command='synth', text=prior['text'], file=None, stdin=False,
                                     voice=prior['voice_id'], model=prior['model'], speed=prior['speed'],
                                     output=args.output, output_dir=None, task_dir=args.task_dir,
                                     source_task=args.source_task, regenerate_chunk=args.chunk,
                                     accept_source_runtime_change=args.accept_source_runtime_change,
                                     accept_control=args.accept_control, drop_control=args.drop_control,
                                     reference_mode=task_reference_mode(prior), workers='1', offline=False)
        return _run(derived)
    if args.command == "parse":
        parsed = parsed_input(text_input(args))
        result = {"plain_text": parsed.plain_text, "phoneme_overrides": parsed.phoneme_overrides,
                  "breaks": parsed.breaks, "emphasis_spans": parsed.emphasis_spans,
                  "emotion_spans": parsed.emotion_spans,
                  "control_spans": [asdict(span) for span in parsed.control_spans]}
        preview_model = getattr(args, 'model', None)
        preview_mode = getattr(args, 'reference_mode', 'reference')
        if preview_mode != 'reference' and not preview_model:
            raise ValueError('预览参考音频模式时必须指定模型')
        if preview_model:
            from app.engines import preview_input
            result['provider_preview'] = preview_input(engine_for(preview_model), parsed, preview_mode)
        return result
    source_text = text_input(args)
    parsed = parsed_input(source_text)
    accepted_controls = tuple(sorted(set(getattr(args, 'accept_control', None) or ())))
    if not set(accepted_controls) <= {'phoneme', 'emotion'}:
        raise ValueError('无法识别的实验性控制类型')
    if set(accepted_controls) & set(getattr(args, 'drop_control', None) or ()):
        raise ValueError('不能同时确认和忽略同一类型的控制')
    parsed, dropped_controls = apply_confirmed_drops(parsed, getattr(args, 'drop_control', None))
    if dropped_controls:
        print('已按本次任务确认忽略控制项: ' + json.dumps(dropped_controls, ensure_ascii=False), file=sys.stderr)
    selected = args.voice or settings_read(settings).get("default_voice")
    if not selected:
        raise ValueError("尚未指定音色且没有默认音色；请先列出音色并选择")
    lookup_voice(store, selected)
    model = args.model or config.get("engine", "cosyvoice3")
    engine = engine_for(model)
    if not engine.voice_from_store:
        raise ValueError(f"模型 {model} 不支持使用本地克隆音色，请选择兼容模型")
    engine.prepare_voice(selected)
    reference_mode = getattr(args, 'reference_mode', 'reference')
    engine.validate(TTSRequest(text=parsed.plain_text,
                                       phoneme_overrides=parsed.phoneme_overrides,
                                       breaks=parsed.breaks, emphasis_spans=parsed.emphasis_spans,
                                       emotion_spans=parsed.emotion_spans, accepted_controls=accepted_controls,
                                       voice_id=selected, speed=args.speed, reference_mode=reference_mode))
    reference_fingerprint = (engine.reference_identity(selected)
                             if reference_mode == 'reference_plus_prompt' else None)
    chunks = engine.plan_chunks(parsed)
    from app.parser.chunker import validate_chunk_plan
    validate_chunk_plan(parsed, chunks)
    if any(not chunk['text'].strip() and (chunk['overrides'] or chunk['emphasis_spans']
                                         or chunk.get('emotion_spans')) for chunk in chunks):
        raise ValueError('不能对无可读文本的分块施加注音、重音或情绪')
    chunks = [chunk for chunk in chunks if chunk["text"].strip() or chunk["breaks"]]
    requested_workers = str(getattr(args, 'workers', '1'))
    if requested_workers not in ('1', '2', '4', 'auto'):
        raise ValueError('workers 必须是 auto、1、2 或 4')
    if requested_workers in ('2', '4'):
        raise ValueError('真实多路执行尚未通过进程生命周期安全验证，当前不可用')
    worker_reason = None
    if requested_workers == 'auto':
        worker_reason = '真实多路安全验证尚未完成，保守使用单路；未执行校准'
        print(worker_reason, file=sys.stderr)
    output_file = getattr(args, 'output', None)
    output_dir = getattr(args, 'output_dir', None)
    if output_file and output_dir:
        raise ValueError('--output 与 --output-dir 不能同时使用')
    configured_dir = output_dir or (os.environ.get('VOICE_TTS_OUTPUT_DIR') if not output_file else None)
    if configured_dir:
        output_root = Path(configured_dir).expanduser()
        if not output_root.is_absolute():
            raise ValueError('输出目录必须是绝对路径')
    else:
        output_root = ROOT / 'output'
    out = Path(output_file) if output_file else output_root / f'tts_{uuid.uuid4().hex}.wav'
    if out.exists() or out.is_symlink():
        raise ValueError(f"输出文件已存在: {out}")
    task_dir = getattr(args, 'task_dir', None)
    source_task = getattr(args, 'source_task', None)
    diagnostic_path = getattr(args, 'diagnostic_log', None)
    if diagnostic_path is not None:
        diagnostic_path = Path(diagnostic_path).expanduser()
        if not diagnostic_path.is_absolute() or not diagnostic_path.name:
            raise ValueError('诊断日志必须使用绝对文件路径')
        if model != 'voxcpm2' or not task_dir or source_task:
            raise ValueError('诊断模式仅支持指定新日志和任务目录的单路 VoxCPM 合成')
        if reference_mode == 'reference_plus_prompt':
            raise ValueError('参考音频加逐字稿模式暂不支持诊断日志')
        diagnostic_resolved = diagnostic_path.resolve()
        if (diagnostic_resolved.is_relative_to(Path(task_dir).resolve())
                or diagnostic_resolved == out.resolve()
                or any((parent / 'task.json').exists() and (parent / '.lock').exists()
                       for parent in diagnostic_resolved.parents)):
            raise ValueError('诊断日志必须独立于任务目录和输出文件')
    if source_task and not task_dir:
        raise ValueError('分块重生成需要显式指定新的 --task-dir')
    if requested_workers == 'auto' and not task_dir:
        cache_root = out.parent
        cache_root.mkdir(parents=True, exist_ok=True)
        task_dir = tempfile.mkdtemp(prefix='tts-task-', dir=cache_root)
        print(f'可恢复任务目录: {task_dir}', file=sys.stderr)
    regenerate_chunk = getattr(args, 'regenerate_chunk', None)
    allow_runtime_change = bool(getattr(args, 'accept_source_runtime_change', False))
    if (source_task is None) != (regenerate_chunk is None):
        raise ValueError('--source-task 与 --regenerate-chunk 必须同时提供')
    if allow_runtime_change and not source_task:
        raise ValueError('--accept-source-runtime-change 仅用于分块重生成')
    if source_task:
        if type(regenerate_chunk) is not int or regenerate_chunk < 1:
            raise ValueError('重生成分块编号必须是从 1 开始的整数')
        if not task_dir:
            raise ValueError('分块重生成需要指定新的 --task-dir')
        source_path, target_path = Path(source_task).resolve(), Path(task_dir).resolve()
        if (source_path == target_path or source_path.is_relative_to(target_path)
                or target_path.is_relative_to(source_path) or out.resolve().is_relative_to(source_path)):
            raise ValueError('源任务、新任务和输出文件必须相互独立')
    if task_dir and out.resolve().is_relative_to(Path(task_dir).resolve()):
        raise ValueError('输出文件不能位于任务目录内')
    lock = task_lock(task_dir) if task_dir else contextlib.nullcontext(None)
    with lock as directory:
        if directory is not None:
            meta = lookup_voice(store, selected)
            adapter_identity = engine.runtime_identity()
            identity = {'version': TASK_VERSION, 'text': source_text, 'plain_text': parsed.plain_text,
                        'overrides': parsed.phoneme_overrides, 'breaks': parsed.breaks,
                        'emphasis_spans': parsed.emphasis_spans, 'chunks': chunks,
                        'voice_id': selected, 'voice_metadata': meta,
                        'voice_wav_sha256': file_hash(store.base / selected / 'ref.wav'),
                        'model': model, 'config': config, 'model_files': model_identity(config, model, engine),
                        'runtime': runtime_identity(), 'speed': args.speed, 'reference_mode': reference_mode,
                        'accepted_controls': accepted_controls, 'dropped_controls': dropped_controls,
                        'emotion_spans': parsed.emotion_spans,
                        'control_spans': [asdict(span) for span in parsed.control_spans]}
            if adapter_identity is not None:
                identity['adapter_runtime'] = adapter_identity
            if reference_fingerprint is not None:
                identity['reference_fingerprint'] = reference_fingerprint
            task_reference_mode(identity)
            # Normalize tuples and integer keys to the exact persisted JSON representation.
            identity = json.loads(json.dumps(identity, ensure_ascii=False, sort_keys=True))
            if source_task:
                identity = seed_regenerated_task(directory, source_task, regenerate_chunk - 1,
                                                 identity, allow_runtime_change)
            if diagnostic_path is not None:
                from app.engines.mlx.diagnostics import DiagnosticLog
                diagnostic_context = DiagnosticLog(diagnostic_path, selected, identity['voice_wav_sha256'])
            else:
                diagnostic_context = contextlib.nullcontext(None)
            with diagnostic_context as diagnostic:
                pieces, sample_rate = task_pieces(directory, identity, chunks, engine, selected, args.speed,
                                                  accepted_controls=accepted_controls,
                                                  allowed_pending={regenerate_chunk - 1} if source_task else None,
                                                  diagnostic=diagnostic, reference_mode=reference_mode,
                                                  reference_fingerprint=reference_fingerprint)
        else:
            pieces, sample_rate, channels = [], None, None
            for i, chunk in enumerate(chunks):
                print(f"正在合成 {i + 1}/{len(chunks)}", file=sys.stderr)
                data, sr = synthesize_piece(engine, chunk, selected, args.speed, accepted_controls,
                                            reference_mode, reference_fingerprint)
                if sample_rate is not None and (sr != sample_rate or data.shape[1] != channels):
                    raise ValueError("生成的音频采样率或声道数不一致")
                sample_rate, channels = sr, data.shape[1]
                pieces.append(data)
        result = np.concatenate(pieces, axis=0)
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".wav", dir=out.parent, delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            sf.write(str(temp_path), result, sample_rate, format="WAV")
            os.link(temp_path, out)
        finally:
            temp_path.unlink(missing_ok=True)
    return {"output": str(out.resolve()), "voice_id": selected, "model": model, "sample_rate": sample_rate,
            "duration_seconds": round(len(result) / sample_rate, 3), "chunks": len(chunks),
            "requested_workers": requested_workers, "actual_workers": 1, "worker_reason": worker_reason,
            "task_dir": str(Path(task_dir).resolve()) if task_dir else None,
            "dropped_controls": dropped_controls, "accepted_controls": list(accepted_controls),
            "source_task": str(Path(source_task).resolve()) if source_task else None,
            "regenerated_chunk": regenerate_chunk, "reference_mode": reference_mode}


def add_input(parser):
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text")
    group.add_argument("--file")
    group.add_argument("--stdin", action="store_true")


def main():
    parser = argparse.ArgumentParser(description="本地中文 TTS")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("models")
    voices = sub.add_parser("voices").add_subparsers(dest="action", required=True)
    voices.add_parser("list")
    default = voices.add_parser("default")
    default.add_argument("voice", nargs="?")
    add = voices.add_parser("add")
    add.add_argument("--id", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--audio", required=True)
    add.add_argument("--prompt-text", required=True)
    add.add_argument("--authorized", action="store_true")
    preview = sub.add_parser("parse")
    add_input(preview)
    preview.add_argument('--model', help='预览指定模型转换；不加载模型或合成')
    preview.add_argument('--reference-mode', choices=('reference', 'reference_plus_prompt'), default='reference',
                         help='参考音频模式；reference_plus_prompt 仅支持 VoxCPM2')
    task = sub.add_parser('task')
    task_actions = task.add_subparsers(dest='action', required=True)
    task_inspect = task_actions.add_parser('inspect')
    task_inspect.add_argument('--task-dir', required=True)
    regenerate = task_actions.add_parser('regenerate')
    regenerate.add_argument('--source-task', required=True)
    regenerate.add_argument('--chunk', type=int, required=True, help='分块编号，从 1 开始')
    regenerate.add_argument('--task-dir', required=True, help='独立的新任务目录')
    regenerate.add_argument('--output', required=True, help='新的完整音频输出路径')
    regenerate.add_argument('--accept-source-runtime-change', action='store_true',
                            help='仅本次确认沿用源任务旧运行时生成的其他分块')
    regenerate.add_argument('--accept-control', action='append', choices=('phoneme', 'emotion'))
    regenerate.add_argument('--drop-control', action='append', choices=('phoneme', 'break', 'emphasis', 'emotion'))
    synth = sub.add_parser("synth")
    add_input(synth)
    synth.add_argument("--voice")
    synth.add_argument("--model")
    synth.add_argument("--offline", action="store_true", help="仅从完整的本地 CosyVoice 模型目录加载")
    synth.add_argument("--speed", type=float, default=1.0)
    synth.add_argument('--reference-mode', choices=('reference', 'reference_plus_prompt'), default='reference',
                       help='VoxCPM2 可选 reference_plus_prompt：同一录音加逐字稿；与情绪控制冲突')
    synth.add_argument('--accept-control', action='append', choices=('phoneme', 'emotion'),
                       help='仅本次任务确认使用指定实验性控制；可重复')
    synth.add_argument('--drop-control', action='append', choices=('phoneme', 'break', 'emphasis', 'emotion'),
                       help='仅本次任务确认忽略指定类型的全部控制；先用 parse 查看位置，可重复')
    output = synth.add_mutually_exclusive_group()
    output.add_argument("--output", help="指定输出文件路径")
    output.add_argument("--output-dir", help="指定绝对输出目录并自动命名 WAV")
    synth.add_argument("--task-dir", help="显式指定可恢复任务目录")
    synth.add_argument('--diagnostic-log', help='可选：写入新的私有 VoxCPM 逐块诊断日志绝对路径')
    synth.add_argument('--source-task', help='只读原任务目录；重生成使用独立的新任务目录')
    synth.add_argument('--regenerate-chunk', type=int, help='仅重生成这个分块（从 1 开始）')
    synth.add_argument('--accept-source-runtime-change', action='store_true',
                       help='仅本次确认沿用源任务旧运行时生成的其他分块')
    synth.add_argument("--workers", choices=('auto', '1', '2', '4'), default='1',
                       help="auto 暂时保守使用单路；真实多路尚不可用")
    args = parser.parse_args()
    try:
        try:
            result = run(args)
        finally:
            from app.engines.registry import release_all
            release_all()
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
