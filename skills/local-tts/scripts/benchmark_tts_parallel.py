#!/usr/bin/env python3
"""Isolated CosyVoice3/MPS throughput experiment; --mock never loads a model."""
import argparse
import contextlib
import io
import json
import math
import multiprocessing as mp
from multiprocessing.connection import wait
import os
import re
import subprocess
from pathlib import Path
import sys
import time
import traceback
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def peak_rss_bytes():
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == "darwin" else value * 1024)
    except (ImportError, AttributeError, OSError, ValueError):
        return None


def memory_free_percent():
    if sys.platform != 'darwin':
        return None
    try:
        result = subprocess.run(['memory_pressure', '-Q'], capture_output=True, text=True, timeout=5, check=True)
        match = re.search(r'System-wide memory free percentage:\s*(\d+)%', result.stdout)
        return int(match.group(1)) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def wav_info(data):
    with wave.open(io.BytesIO(data), "rb") as wav:
        if wav.getcomptype() != "NONE" or wav.getnchannels() < 1:
            raise ValueError("unsupported WAV encoding")
        rate, frames = wav.getframerate(), wav.getnframes()
        if rate <= 0 or frames <= 0:
            raise ValueError("empty or invalid WAV")
        expected = frames * wav.getnchannels() * wav.getsampwidth()
        if len(wav.readframes(frames)) != expected:
            raise ValueError("truncated WAV")
    return rate, frames / rate


def worker(conn, output_dir, speed, mock, ref_wav, prompt, voice):
    try:
        os.chdir(ROOT)  # engine configuration uses project-relative paths
        with contextlib.redirect_stdout(sys.stderr):
            from app.engines.base import TTSRequest
            if mock:
                from app.engines.mock_engine import MockEngine
                engine = MockEngine()
            else:
                import torch
                if not torch.backends.mps.is_available():
                    raise RuntimeError("MPS unavailable; refusing CPU fallback")
                from app.engines.cosyvoice.engine import CosyVoice3Engine
                engine = CosyVoice3Engine(device="mps")
            engine.load()
            engine.clone_voice(ref_wav, voice, prompt)
        conn.send({"type": "ready", "pid": os.getpid(), "peak_rss_bytes": peak_rss_bytes()})
        while True:
            task = conn.recv()
            if task is None:
                break
            index, chunk = task
            started = time.monotonic()
            try:
                req = TTSRequest(text=chunk["text"], phoneme_overrides=chunk["overrides"],
                                 breaks=chunk["breaks"], emphasis_spans=chunk["emphasis_spans"],
                                 voice_id=voice, speed=speed)
                with contextlib.redirect_stdout(sys.stderr):
                    data = engine.synthesize(req)
                rate, duration = wav_info(data)
                filename = f"chunk_{index:06d}.wav"
                # Exclusive creation within a fresh, private experiment directory.
                with open(Path(output_dir) / filename, "xb") as out:
                    out.write(data)
                result = {"status": "success", "file": filename, "sample_rate": rate,
                          "duration_seconds": duration}
            except Exception as exc:
                result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            result.update(type="result", index=index, pid=os.getpid(),
                          wall_seconds=time.monotonic() - started, peak_rss_bytes=peak_rss_bytes())
            conn.send(result)
    except BaseException as exc:
        try:
            conn.send({"type": "worker_error", "pid": os.getpid(),
                       "error": f"{type(exc).__name__}: {exc}"})
        except (BrokenPipeError, EOFError, OSError):
            pass
        traceback.print_exc(file=sys.stderr)
    finally:
        conn.close()


def run(args):
    from app.config import load_config
    from app.parser.ssml import parse_ssml
    from app.parser.chunker import chunk_text_with_metadata

    source = Path(args.file).resolve(strict=True)
    text = source.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ValueError("input is empty")
    parsed = parse_ssml(text)
    chunks = chunk_text_with_metadata(parsed.plain_text, parsed.phoneme_overrides,
                                      parsed.breaks, parsed.emphasis_spans,
                                      max_len=args.max_len, min_len=args.min_len)
    chunks = [c for c in chunks if c["text"].strip() or c["breaks"]]
    if not chunks:
        raise ValueError("no synthesizable chunks")
    total_chunks = len(chunks)
    if args.limit is not None:
        chunks = chunks[:args.limit]
    cfg = load_config(str(ROOT / "config.yaml"))
    voice_dir = Path(cfg.get("voices_dir", "./voices"))
    if not voice_dir.is_absolute():
        voice_dir = ROOT / voice_dir
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.voice) or args.voice == 'default':
        raise ValueError('invalid voice ID')
    voice_dir = voice_dir / args.voice
    if voice_dir.is_symlink():
        raise ValueError('voice directory must not be a symlink')
    ref = voice_dir / "ref.wav"
    meta = json.loads((voice_dir / "meta.json").read_text(encoding="utf-8"))
    if not ref.is_file():
        raise ValueError("voice reference WAV not found")
    prompt = meta.get("prompt_text", "")
    if not isinstance(prompt, str):
        raise ValueError("invalid voice prompt")

    out = Path(args.output_dir).absolute()
    # Never create parent directories or reuse an existing trial.
    out.mkdir(mode=0o700, exist_ok=False)
    started = time.monotonic()
    deadline = started + args.timeout
    results = [{"index": i, "status": "not_started"} for i in range(len(chunks))]
    workers = []
    errors = []
    timed_out = False
    minimum_free_percent = None
    last_memory_check = 0.0
    context = mp.get_context("spawn")
    next_index = 0
    try:
        for _ in range(min(args.workers, len(chunks))):
            if time.monotonic() >= deadline:
                timed_out = True
                break
            parent, child = context.Pipe()
            process = context.Process(target=worker, args=(child, str(out), args.speed, args.mock,
                                                            str(ref.resolve()), prompt, args.voice))
            try:
                process.start()
            except BaseException:
                parent.close()
                child.close()
                raise
            child.close()
            workers.append({"process": process, "conn": parent, "ready": False, "index": None})
        # At most one outstanding chunk per process, no unbounded task queue.
        while workers and any(r["status"] in ("not_started", "running") for r in results):
            if not args.mock and time.monotonic() - last_memory_check >= 3:
                free = memory_free_percent()
                last_memory_check = time.monotonic()
                if free is None:
                    errors.append({"error": "system memory pressure unavailable; stopping for safety"})
                    break
                minimum_free_percent = free if minimum_free_percent is None else min(minimum_free_percent, free)
                if free < 15:
                    errors.append({"error": f"system memory free percentage {free}% below 15%; stopping for safety"})
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for w in workers:
                if w["ready"] and w["index"] is None and next_index < len(chunks):
                    w["conn"].send((next_index, chunks[next_index]))
                    w["index"] = next_index
                    results[next_index]["status"] = "running"
                    next_index += 1
            active = [w["conn"] for w in workers if not w.get("dead")]
            if not active:
                break
            ready_connections = wait(active, timeout=min(remaining, 0.1))
            if time.monotonic() >= deadline:
                timed_out = True
                break
            for conn in ready_connections:
                w = next(w for w in workers if w["conn"] is conn)
                try:
                    message = conn.recv()
                except (EOFError, OSError):
                    message = {"type": "worker_error", "error": "worker exited or pipe closed"}
                if message["type"] == "ready":
                    w["ready"] = True
                    w["initial_peak_rss_bytes"] = message["peak_rss_bytes"]
                elif message["type"] == "result":
                    idx = message["index"]
                    if idx != w["index"]:
                        raise RuntimeError("worker returned unexpected chunk index")
                    message.pop("type")
                    results[idx] = message
                    w["index"] = None
                else:
                    errors.append({"pid": w["process"].pid, "error": message.get("error", "worker error")})
                    if w["index"] is not None:
                        results[w["index"]] = {"index": w["index"], "status": "failed",
                                                "error": message.get("error", "worker error"),
                                                "pid": w["process"].pid}
                    w["dead"] = True
            if not any(not w.get("dead") for w in workers):
                break
    except BaseException as exc:
        errors.append({"error": f"{type(exc).__name__}: {exc}"})
    finally:
        for w in workers:
            if w["process"].is_alive() and w["index"] is None and w["ready"]:
                try:
                    w["conn"].send(None)
                except (BrokenPipeError, OSError):
                    pass
        for w in workers:
            w["process"].join(timeout=5 if w["index"] is None else 0)
            if w["process"].is_alive():
                w["process"].terminate()
        for w in workers:
            w["process"].join(timeout=1)
            if w["process"].is_alive():
                w["process"].kill()
        for w in workers:
            w["process"].join()
            w["conn"].close()
        for r in results:
            if r["status"] == "running":
                r.update(status="failed", error="deadline exceeded" if timed_out else "worker interrupted")
        report = {"mode": "mock" if args.mock else "mps", "model": "mock" if args.mock else "cosyvoice3",
                  "voice": args.voice, "speed": args.speed, "workers_requested": args.workers,
                  "workers_started": len(workers), "timeout_seconds": args.timeout,
                  "timed_out": timed_out, "wall_seconds": time.monotonic() - started,
                  "minimum_system_memory_free_percent": minimum_free_percent,
                  "input": str(source), "total_chunks": total_chunks, "selected_chunks": len(chunks),
                  "min_len": args.min_len, "max_len": args.max_len,
                  "chunk_lengths": [len(c['text']) for c in chunks],
                  "results": results, "worker_errors": errors,
                  "workers": [{"pid": w["process"].pid, "exitcode": w["process"].exitcode,
                               "initial_peak_rss_bytes": w.get("initial_peak_rss_bytes")} for w in workers]}
        report["success"] = not timed_out and not errors and all(r["status"] == "success" for r in results)
        with (out / "report.json").open("x", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(json.dumps({"success": report["success"], "report": str(out / "report.json"),
                          "mode": report["mode"]}, ensure_ascii=False))
    return 0 if report["success"] else 1


def positive_int(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return n


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="UTF-8 source text file")
    parser.add_argument("--output-dir", required=True, help="new experiment directory; parent must exist")
    parser.add_argument("--voice", required=True, help="existing authorized voice ID")
    parser.add_argument("--workers", type=int, choices=(1, 2, 4), default=1)
    parser.add_argument("--limit", type=positive_int, help="only benchmark the first N chunks")
    parser.add_argument("--min-len", type=positive_int, default=12)
    parser.add_argument("--max-len", type=positive_int, default=120)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=600, help="overall deadline in seconds, including model loading")
    parser.add_argument("--mock", action="store_true", help="test orchestration only; produces synthetic tones")
    args = parser.parse_args()
    if args.min_len > args.max_len:
        parser.error("min-len must not exceed max-len")
    if not math.isfinite(args.speed) or not 0.5 <= args.speed <= 2.0:
        parser.error("speed must be between 0.5 and 2.0")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be positive and finite")
    try:
        return run(args)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
