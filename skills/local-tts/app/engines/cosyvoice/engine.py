"""CosyVoice2 / CosyVoice3 引擎适配器。

两者共用同一套 CLI（AutoModel 按目录下的 yaml 自动分发），差异在于：
  - CosyVoice3 的 tokenizer 带拼音 special token，支持「发音改写」(pronunciation inpainting)
  - CosyVoice2 的 tokenizer 没有拼音 token，注音标签对它无效

适配层要处理三件事：
  1. torchaudio>=2.11 移除了 load(backend='soundfile')，用 soundfile 直读替换
  2. 拼音标签 -> CosyVoice3 的 [声母][韵母] token（见 pinyin_tokens.py）
  3. 毫秒停顿：模型不支持内嵌静音，按 break 位置切段合成后拼静音
"""
import sys
import io
import time
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

from app.engines import TTSEngine
from app.engines.base import TTSRequest, TTSCapability
from app.engines.registry import register
from app.engines.cosyvoice.pinyin_tokens import to_tokens

REPO = Path(__file__).resolve().parents[3] / "third_party" / "CosyVoice"
DEFAULT_PROMPT_WAV = REPO / "asset" / "zero_shot_prompt.wav"
DEFAULT_PROMPT_TXT = "希望你以后能够做的比我还好呦。"
_MODEL_LOAD_LOCK = threading.RLock()


def _install_paths() -> None:
    for p in (str(REPO), str(REPO / "third_party" / "Matcha-TTS")):
        if p not in sys.path:
            sys.path.insert(0, p)


def _patch_load_wav() -> None:
    """cosyvoice.utils.file_utils.load_wav 依赖已被 torchaudio 移除的 soundfile 后端"""
    import torch
    import cosyvoice.utils.file_utils as fu

    def load_wav(wav, target_sr, min_sr=16000):
        import torchaudio
        data, sr = sf.read(wav, dtype="float32", always_2d=True)
        speech = torch.from_numpy(data.mean(axis=1, keepdims=True)).transpose(0, 1)
        if sr != target_sr:
            assert sr >= min_sr, f"wav sample rate {sr} must be >= {min_sr}"
            speech = torchaudio.transforms.Resample(orig_freq=sr, new_freq=target_sr)(speech)
        return speech

    fu.load_wav = load_wav
    try:
        import cosyvoice.cli.frontend as fe
        fe.load_wav = load_wav
    except ImportError:
        pass


def _patch_hifigan_f0() -> None:
    """MPS 不支持 float64，而声码器的 f0 预测强制用 double。

    源码注释即建议把 f0_predictor 放 CPU；它只有几层卷积，挪到 CPU 的开销可忽略，
    精度与官方实现完全一致。
    """
    import torch
    import numpy as np
    from cosyvoice.hifigan.generator import CausalHiFTGenerator

    if getattr(CausalHiFTGenerator.inference, "_f0_on_cpu", False):
        return

    @torch.inference_mode()
    def inference(self, speech_feat, finalize=True):
        if next(self.f0_predictor.parameters()).device.type == "mps":
            self.f0_predictor.to("cpu")
        self.f0_predictor.to(torch.float64)
        f0 = self.f0_predictor(speech_feat.cpu().to(torch.float64), finalize=finalize).to(speech_feat)
        s = self.f0_upsamp(f0[:, None]).transpose(1, 2)
        s, _, _ = self.m_source(s)
        s = s.transpose(1, 2)
        if finalize is True:
            generated_speech = self.decode(x=speech_feat, s=s, finalize=finalize)
        else:
            generated_speech = self.decode(
                x=speech_feat[:, :, :-self.f0_predictor.condnet[0].causal_padding], s=s, finalize=finalize)
        return generated_speech, s

    inference._f0_on_cpu = True
    CausalHiFTGenerator.inference = inference


def apply_pinyin_inpaint(text: str, overrides: dict) -> str:
    """把指定下标的汉字换成 [声母][韵母] 形式的拼音 token"""
    if not overrides:
        return text
    parts = []
    for i, ch in enumerate(text):
        py = overrides.get(i)
        if not py:
            parts.append(ch)
            continue
        tok = to_tokens(py)
        parts.append(tok or ch)
    return "".join(parts)


def apply_emphasis(text: str, spans: list) -> str:
    """重音段用模型内置 <strong></strong> 包裹（从后往前插，避免下标漂移）"""
    if not spans:
        return text
    # 合并相交区间，避免重叠标签交错；所有坐标均基于传入文本。
    merged = []
    for start, end in sorted(spans):
        start, end = max(0, start), min(len(text), end)
        if start >= end:
            continue
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    out = text
    for start, end in reversed(merged):
        out = out[:start] + "<strong>" + out[start:end] + "</strong>" + out[end:]
    return out


def split_by_breaks(plain: str, overrides: dict, breaks: list):
    """按停顿位置切段 -> ([(段文本, 该段重映射后的注音)], [段间静音秒数])"""
    if not breaks:
        return [(plain, dict(overrides))], []

    break_at = {}
    for pos, ms in breaks:
        pos = min(max(pos, 0), len(plain))
        break_at[pos] = break_at.get(pos, 0) + ms
    cuts = sorted(break_at)
    bounds = [0] + cuts + [len(plain)]
    segs = [(plain[a:b], {i - a: py for i, py in overrides.items() if a <= i < b})
            for a, b in zip(bounds, bounds[1:])]
    gaps = [break_at[pos] / 1000.0 for pos in cuts]
    return segs, gaps


class CosyVoiceEngine(TTSEngine):
    version = "cosyvoice2"
    default_model_dir = "./models/CosyVoice2"
    supports_pinyin_inpaint = False
    prompt_prefix = ""

    def __init__(self, model_dir: str = "", device: str = ""):
        from app.config import load_config
        cfg = load_config().get("engines", {}).get(self.version, {})
        self.model_dir = Path(model_dir or cfg.get("model_path", "") or self.default_model_dir)
        self.device = device or cfg.get("device", "")  # '' = 自动（有 MPS 用 MPS）
        self._model = None
        self._loaded = False
        self.offline = False
        self._spk_ids: set[str] = set()       # 已注册进 spk2info 的音色
        self._voice_prompt: dict[str, str] = {}  # voice_id -> spk_id

    @property
    def capability(self) -> TTSCapability:
        return TTSCapability(
            supports_phoneme=self.supports_pinyin_inpaint,
            supports_break_ms=True,
            supports_emphasis=True,
            supports_streaming=False,
            supports_chinese=True,
            clone_mode="zero_shot",
            sample_rate=24000,
        )

    # ---- 生命周期 ----

    def load(self) -> None:
        if self._loaded:
            return
        if self.offline:
            from app.model_preflight import assert_offline_ready, validate_model
            assert_offline_ready()
            validate_model(self.model_dir, self.version)
        if not self.offline and not self.model_dir.exists():
            raise FileNotFoundError(
                f"{self.version} 权重未找到: {self.model_dir}\n"
                f"请执行: bash scripts/download_model.sh {self.version}"
            )
        _install_paths()
        _patch_load_wav()

        import torch
        import cosyvoice.cli.cosyvoice as cosyvoice_cli

        t0 = time.time()
        print(f"[{self.version}] 加载权重 {self.model_dir} ...")
        with _MODEL_LOAD_LOCK:
            if self.offline:
                from app.model_preflight import block_snapshot_download, validate_model
                model_dir = validate_model(self.model_dir, self.version)
                with block_snapshot_download(cosyvoice_cli):
                    self._model = cosyvoice_cli.AutoModel(model_dir=str(model_dir))
            else:
                self._model = cosyvoice_cli.AutoModel(model_dir=str(self.model_dir))
        dev = self._pick_device()
        if dev.type == "mps":
            _patch_hifigan_f0()
        self._move_to(dev)
        print(f"[{self.version}] 就绪 {time.time()-t0:.1f}s device={dev} sr={self._model.sample_rate}")
        self._loaded = True

    def _pick_device(self):
        import torch
        if self.device:
            return torch.device(self.device)
        return torch.device("mps") if torch.backends.mps.is_available() else torch.device("cpu")

    def _move_to(self, dev) -> None:
        """模型在 __init__ 里把权重搬到了 CPU，这里整体搬到目标设备"""
        import torch
        if dev.type == "cpu":
            return
        m = self._model
        m.model.device = dev
        for sub in ("llm", "flow", "hift"):
            mod = getattr(m.model, sub, None)
            if mod is not None:
                mod.to(dev)
        m.frontend.device = dev

    def is_loaded(self) -> bool:
        return self._loaded

    def unload(self) -> None:
        self._model = None
        self._loaded = False
        self._spk_ids.clear()
        try:
            import torch
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except (ImportError, AttributeError):
            pass

    # ---- 音色 ----

    def clone_voice(self, ref_wav_path: str, voice_id: str, prompt_text: str = "") -> str:
        """登记参考音色；提示词与特征在首次合成时算一次并缓存进 spk2info"""
        self.forget_voice(voice_id)  # 同 id 换音频时要让旧特征失效
        self.register_prompt(voice_id, ref_wav_path, prompt_text)
        return voice_id

    def forget_voice(self, voice_id: str) -> None:
        spk_id = self._voice_prompt.pop(voice_id, None)
        if spk_id and self._model is not None:
            self._spk_ids.discard(spk_id)
            self._model.frontend.spk2info.pop(spk_id, None)

    def register_prompt(self, voice_id: str, ref_wav_path, prompt_text: str) -> str:
        """首次使用时把参考音频编码一次存进 spk2info，避免每次合成都重算"""
        if not self._loaded:
            self.load()
        spk_id = self._voice_prompt.get(voice_id)
        if spk_id:
            return spk_id
        spk_id = f"vc_{voice_id}"
        self._model.add_zero_shot_spk(self.prompt_prefix + prompt_text, str(ref_wav_path), spk_id)
        self._spk_ids.add(spk_id)
        self._voice_prompt[voice_id] = spk_id
        return spk_id

    # ---- 合成 ----

    def synthesize(self, req: TTSRequest) -> bytes:
        if not self._loaded:
            self.load()

        segs, gaps = split_by_breaks(req.text, req.phoneme_overrides, req.breaks)
        offsets = _offsets(segs)
        spk_id = self._resolve_voice(req.voice_id)

        chunks = []
        for i, ((seg_text, seg_ov), base) in enumerate(zip(segs, offsets)):
            if seg_text.strip():
                spans = [(max(a, base) - base, min(b, base + len(seg_text)) - base)
                         for a, b in req.emphasis_spans
                         if max(a, base) < min(b, base + len(seg_text))]
                if self.supports_pinyin_inpaint:
                    # 先逐字替换，再将原文坐标映射到 token 文本，避免标签或 token 改变下标。
                    parts = [apply_pinyin_inpaint(ch, {0: seg_ov.get(j)})
                             for j, ch in enumerate(seg_text)]
                    positions = [0]
                    for part in parts:
                        positions.append(positions[-1] + len(part))
                    seg_text = "".join(parts)
                    spans = [(positions[a], positions[b]) for a, b in spans]
                chunks.append(self._infer(apply_emphasis(seg_text, spans), spk_id, req.speed))
            if i < len(gaps):
                chunks.append(self._silence(gaps[i]))

        chunks = [c for c in chunks if len(c)]
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        buf = io.BytesIO()
        sf.write(buf, audio, self._model.sample_rate, format="WAV")
        return buf.getvalue()

    def _resolve_voice(self, voice_id: str) -> str:
        if voice_id in self._voice_prompt:
            return self._voice_prompt[voice_id]
        if voice_id != "default":
            from app.config import load_config
            from app.voice_store import VoiceStore
            store = VoiceStore(load_config().get("voices_dir", "./voices"))
            wav = store.ref_wav(voice_id)
            if wav:
                meta = store.get(voice_id) or {}
                # 用户没填参考文本就传空串。塞默认提示词的文本是错的：
                # prompt_text 必须与音频内容一致，对不上反而比空着更差。
                return self.register_prompt(voice_id, wav, meta.get("prompt_text", ""))
        return self.register_prompt("default", DEFAULT_PROMPT_WAV, DEFAULT_PROMPT_TXT)

    def _infer(self, text: str, spk_id: str, speed: float) -> np.ndarray:
        outs = []
        for out in self._model.inference_zero_shot(text, "", "", zero_shot_spk_id=spk_id,
                                                   stream=False, speed=speed):
            outs.append(out["tts_speech"].squeeze(0).cpu().numpy())
        return np.concatenate(outs) if outs else np.zeros(0, dtype=np.float32)

    def _silence(self, seconds: float) -> np.ndarray:
        return np.zeros(round(self._model.sample_rate * seconds), dtype=np.float32)


@register("cosyvoice2")
class CosyVoice2Engine(CosyVoiceEngine):
    version = "cosyvoice2"
    default_model_dir = "./models/CosyVoice2"
    supports_pinyin_inpaint = False


@register("cosyvoice3")
class CosyVoice3Engine(CosyVoiceEngine):
    version = "cosyvoice3"
    default_model_dir = "./models/CosyVoice3"
    supports_pinyin_inpaint = True
    # CosyVoice3 的提示词必须带 system 前缀才会被当成指令
    prompt_prefix = "You are a helpful assistant.<|endofprompt|>"


def _offsets(segs):
    off, res = 0, []
    for t, _ in segs:
        res.append(off)
        off += len(t)
    return res
