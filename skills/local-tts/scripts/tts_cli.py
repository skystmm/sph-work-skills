#!/usr/bin/env python3
import argparse
import contextlib
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


def run(args):
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
    parsed = parsed_input(text_input(args))
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
    chunks = chunk_text_with_metadata(parsed.plain_text, parsed.phoneme_overrides, parsed.breaks, parsed.emphasis_spans)
    pieces = []
    sample_rate = None
    for i, chunk in enumerate(chunks):
        print(f"正在合成 {i + 1}/{len(chunks)}", file=sys.stderr)
        req = TTSRequest(text=chunk["text"], phoneme_overrides=chunk["overrides"], breaks=chunk["breaks"],
                         emphasis_spans=chunk["emphasis_spans"], voice_id=selected, speed=args.speed)
        with contextlib.redirect_stdout(sys.stderr):
            audio = engine.synthesize(req)
        data, sr = sf.read(io.BytesIO(audio), dtype="float32", always_2d=True)
        if not len(data) or (sample_rate is not None and sr != sample_rate):
            raise ValueError("生成的音频为空或采样率不一致")
        sample_rate = sr
        pieces.append(data)
    result = np.concatenate(pieces, axis=0)
    out = Path(args.output) if args.output else ROOT / "output" / f"tts_{uuid.uuid4().hex}.wav"
    if out.exists():
        raise ValueError(f"输出文件已存在: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".wav", dir=out.parent, delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        sf.write(str(temp_path), result, sample_rate, format="WAV")
        os.link(temp_path, out)
    finally:
        temp_path.unlink(missing_ok=True)
    return {"output": str(out.resolve()), "voice_id": selected, "model": model, "sample_rate": sample_rate,
            "duration_seconds": round(len(result) / sample_rate, 3), "chunks": len(chunks)}


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
    synth.add_argument("--speed", type=float, default=1.0)
    synth.add_argument("--output")
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
