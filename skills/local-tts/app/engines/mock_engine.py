"""Mock 引擎：无权重即可跑通全链路，用于开发/演示"""
import io
import struct
import math
import time
from app.engines import TTSEngine
from app.engines.base import TTSRequest, TTSCapability
from app.engines.registry import register


@register("mock")
class MockEngine(TTSEngine):
    name = "mock"
    capability = TTSCapability(
        supports_phoneme=True,
        supports_break_ms=True,
        supports_emphasis=True,
        supports_streaming=False,
        supports_chinese=True,
        clone_mode="zero_shot",
        sample_rate=22050,
    )

    def __init__(self):
        self._loaded = False
        self._voices: dict[str, str] = {}

    def load(self) -> None:
        self._loaded = True
        print("[MockEngine] loaded (no weights needed)")

    def is_loaded(self) -> bool:
        return self._loaded

    def clone_voice(self, ref_wav_path: str, voice_id: str, prompt_text: str = "") -> str:
        self._voices[voice_id] = ref_wav_path
        print(f"[MockEngine] cloned voice {voice_id} from {ref_wav_path}")
        return voice_id

    def synthesize(self, req: TTSRequest) -> bytes:
        if not self._loaded:
            self.load()
        # 生成一段演示用正弦波，时长与文本长度相关，便于验证链路
        # 实际接入真实引擎时，这里替换为模型推理
        duration = max(1.0, len(req.text) * 0.15 + sum(ms for _, ms in req.breaks) / 1000.0)
        # 重音段稍微提升频率以示区分
        wav = self._sine_wav(duration, freq=220, sample_rate=self.capability.sample_rate)
        # 在返回前打印解析结果，便于验证 SSML 是否生效
        print(f"[MockEngine] text='{req.text}' overrides={req.phoneme_overrides} breaks={req.breaks} emphasis={req.emphasis_spans} -> {duration:.1f}s wav")
        time.sleep(0.1)
        return wav

    def unload(self) -> None:
        self._loaded = False

    def _sine_wav(self, duration: float, freq: float = 220, sample_rate: int = 22050) -> bytes:
        n_samples = int(sample_rate * duration)
        buf = io.BytesIO()
        # WAV header
        buf.write(b"RIFF")
        buf.write(struct.pack("<I", 36 + n_samples * 2))
        buf.write(b"WAVE")
        buf.write(b"fmt ")
        buf.write(struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16))
        buf.write(b"data")
        buf.write(struct.pack("<I", n_samples * 2))
        for i in range(n_samples):
            # 简单正弦波 + 淡入淡出
            t = i / sample_rate
            envelope = 1.0
            if i < sample_rate * 0.05:
                envelope = i / (sample_rate * 0.05)
            elif i > n_samples - sample_rate * 0.05:
                envelope = (n_samples - i) / (sample_rate * 0.05)
            sample = int(16000 * envelope * math.sin(2 * math.pi * freq * t))
            buf.write(struct.pack("<h", sample))
        return buf.getvalue()
