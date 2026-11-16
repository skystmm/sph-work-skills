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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import numpy as np
import soundfile as sf

from app.config import load_config
from app.engines.base import TTSRequest
from app.engines.registry import get_engine, REGISTRY
from app.parser.ssml import parse_ssml, TOKEN_RE
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
    remaining = TOKEN_RE.sub("", text)
    if re.search(r"</?[A-Za-z][^>]*>|</?(?:phoneme|break|emphasis)\b|\[(?:break\b|[^\]\n]*=)", remaining, re.I):
        raise ValueError("存在无法识别的标注；请先检查注音、停顿或重音格式")
    parsed = parse_ssml(text)
    for match in TOKEN_RE.finditer(text):
        annotated = match.group("tag_char") if match.group("tag_ph") else match.group("in_char")
        if annotated is not None and len(annotated) != 1:
            raise ValueError("注音标记须逐字指定")
    if parsed.phoneme_overrides:
        from app.engines.cosyvoice.pinyin_tokens import to_tokens
        from verify_pinyin_tokens import load_vocab
        vocab = load_vocab()
        for py in parsed.phoneme_overrides.values():
            if not re.fullmatch(r"[a-züv]+[0-4]", py):
                raise ValueError(f"拼音必须包含声调数字 0-4: {py}")
            tokens = re.findall(r"\[[^\[\]]+\]", to_tokens(py))
            if not tokens or any(token not in vocab for token in tokens):
                raise ValueError(f"模型词表不支持该拼音: {py}")
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
TASK_VERSION = 1


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


def synthesize_piece(engine, chunk, selected, speed):
    req = TTSRequest(text=chunk['text'], phoneme_overrides=chunk['overrides'], breaks=chunk['breaks'],
                     emphasis_spans=chunk['emphasis_spans'], voice_id=selected, speed=speed)
    with contextlib.redirect_stdout(sys.stderr):
        audio = engine.synthesize(req)
    return checked_audio(audio)


def task_pieces(directory, identity, chunks, engine, selected, speed, processor=None):
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

    expected = {i for i, _ in pending}
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
        pieces[index] = (data, sr)

    if processor:
        processor(pending, save)
    else:
        for i, chunk in pending:
            print(f'正在合成 {i + 1}/{len(chunks)}', file=sys.stderr)
            data, sr = synthesize_piece(engine, chunk, selected, speed)
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
                result.append({"id": name, "capabilities": vars(engine.get_capability())})
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
    if args.command == "parse":
        parsed = parsed_input(text_input(args))
        return {"plain_text": parsed.plain_text, "phoneme_overrides": parsed.phoneme_overrides,
                "breaks": parsed.breaks, "emphasis_spans": parsed.emphasis_spans}
    source_text = text_input(args)
    parsed = parsed_input(source_text)
    selected = args.voice or settings_read(settings).get("default_voice")
    if not selected:
        raise ValueError("尚未指定音色且没有默认音色；请先列出音色并选择")
    lookup_voice(store, selected)
    model = args.model or config.get("engine", "cosyvoice3")
    engine = engine_for(model)
    if not engine.voice_from_store:
        raise ValueError(f"模型 {model} 不支持使用本地克隆音色，请选择兼容模型")
    cap = engine.get_capability()
    for requested, supported, label in ((parsed.phoneme_overrides, cap.supports_phoneme, "注音"),
                                        (parsed.breaks, cap.supports_break_ms, "停顿"),
                                        (parsed.emphasis_spans, cap.supports_emphasis, "重音")):
        if requested and not supported:
            raise ValueError(f"模型 {model} 不支持{label}")
    if not 0.5 <= args.speed <= 2.0:
        raise ValueError("语速必须在 0.5 到 2.0 之间")
    from app.parser.chunker import chunk_text_with_metadata
    chunks = chunk_text_with_metadata(parsed.plain_text, parsed.phoneme_overrides, parsed.breaks,
                                      parsed.emphasis_spans, min_len=40, max_len=80)
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
    if requested_workers == 'auto' and not task_dir:
        cache_root = out.parent
        cache_root.mkdir(parents=True, exist_ok=True)
        task_dir = tempfile.mkdtemp(prefix='tts-task-', dir=cache_root)
        print(f'可恢复任务目录: {task_dir}', file=sys.stderr)
    if task_dir and out.resolve().is_relative_to(Path(task_dir).resolve()):
        raise ValueError('输出文件不能位于任务目录内')
    lock = task_lock(task_dir) if task_dir else contextlib.nullcontext(None)
    with lock as directory:
        if directory is not None:
            meta = lookup_voice(store, selected)
            identity = {'version': TASK_VERSION, 'text': source_text, 'plain_text': parsed.plain_text,
                        'overrides': parsed.phoneme_overrides, 'breaks': parsed.breaks,
                        'emphasis_spans': parsed.emphasis_spans, 'chunks': chunks,
                        'voice_id': selected, 'voice_metadata': meta,
                        'voice_wav_sha256': file_hash(store.base / selected / 'ref.wav'),
                        'model': model, 'config': config, 'model_files': model_identity(config, model, engine),
                        'runtime': runtime_identity(), 'speed': args.speed}
            # Normalize tuples and integer keys to the exact persisted JSON representation.
            identity = json.loads(json.dumps(identity, ensure_ascii=False, sort_keys=True))
            pieces, sample_rate = task_pieces(directory, identity, chunks, engine, selected, args.speed)
        else:
            pieces, sample_rate, channels = [], None, None
            for i, chunk in enumerate(chunks):
                print(f"正在合成 {i + 1}/{len(chunks)}", file=sys.stderr)
                data, sr = synthesize_piece(engine, chunk, selected, args.speed)
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
            "task_dir": str(Path(task_dir).resolve()) if task_dir else None}


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
    synth = sub.add_parser("synth")
    add_input(synth)
    synth.add_argument("--voice")
    synth.add_argument("--model")
    synth.add_argument("--offline", action="store_true", help="仅从完整的本地 CosyVoice 模型目录加载")
    synth.add_argument("--speed", type=float, default=1.0)
    output = synth.add_mutually_exclusive_group()
    output.add_argument("--output", help="指定输出文件路径")
    output.add_argument("--output-dir", help="指定绝对输出目录并自动命名 WAV")
    synth.add_argument("--task-dir", help="显式指定可恢复任务目录")
    synth.add_argument("--workers", choices=('auto', '1', '2', '4'), default='1',
                       help="auto 暂时保守使用单路；真实多路尚不可用")
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
