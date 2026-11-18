"""macOS say 引擎：无需下载模型即可产出真实语音，用于快速验证链路。

限制：say 不接受拼音输入，因此不支持 phoneme 覆盖（需 CosyVoice3 等真实引擎）。
停顿通过分段合成及精确数字静音实现。
"""
import os
import shutil
import subprocess
import tempfile

import numpy as np
import soundfile as sf

from app.engines import TTSEngine
from app.engines.base import ProviderPlan, ProviderStep, TTSRequest, TTSCapability
from app.engines.registry import register

BASE_RATE = 180  # say 默认语速


@register("macsay")
class MacSayEngine(TTSEngine):
    backend = 'macos-say'
    name = "macsay"
    voice_from_store = False
    capability = TTSCapability(
        supports_phoneme=False,
        supports_break_ms=True,
        supports_emphasis=False,
        supports_streaming=False,
        supports_chinese=True,
        clone_mode="preset",
        sample_rate=22050,
        supports_speed=True,
    )

    def __init__(self, default_voice: str = ""):
        from app.config import load_config
        cfg = load_config().get("engines", {}).get("macsay", {})
        self.default_voice = default_voice or cfg.get("voice", "Tingting")
        self._loaded = False

    def load(self) -> None:
        if shutil.which("say") is None:
            raise FileNotFoundError("未找到系统 say 命令（仅 macOS 可用）")
        self._loaded = True

    def is_loaded(self) -> bool:
        return self._loaded

    def clone_voice(self, ref_wav_path: str, voice_id: str, prompt_text: str = "") -> str:
        raise NotImplementedError("macsay 使用系统预置音色，不支持克隆")

    def compile_input(self, req: TTSRequest) -> ProviderPlan:
        self.validate_request(req)
        break_at = {}
        for pos, ms in req.breaks:
            if not isinstance(pos, int) or not 0 <= pos <= len(req.text) or not isinstance(ms, int) or not 0 <= ms <= 60000:
                raise ValueError('停顿位置或时长无效')
            break_at[pos] = break_at.get(pos, 0) + ms
        cuts = sorted({0, len(req.text), *break_at})
        steps = []
        if 0 in break_at:
            steps.append(ProviderStep(kind='silence', milliseconds=break_at[0]))
        for start, end in zip(cuts, cuts[1:]):
            if req.text[start:end].strip():
                mapping = tuple((i, i + 1, i - start, i - start + 1) for i in range(start, end))
                steps.append(ProviderStep(kind='speak', text=req.text[start:end], source_start=start,
                                          source_end=end, source_map=mapping))
            if end in break_at:
                steps.append(ProviderStep(kind='silence', milliseconds=break_at[end],
                                          source_start=end, source_end=end))
        return ProviderPlan(1, tuple(steps), req)

    def synthesize(self, req: TTSRequest) -> bytes:
        return self.execute_plan(self.compile_input(req))

    def execute_plan(self, plan: ProviderPlan) -> bytes:
        self.open_session()
        req = plan.request
        voice = self.default_voice if req.voice_id == "default" else req.voice_id
        rate = max(80, int(BASE_RATE * req.speed))
        pieces = []
        with tempfile.TemporaryDirectory() as td:
            for index, step in enumerate(plan.steps):
                if step.kind == 'silence':
                    pieces.append(np.zeros(round(self.capability.sample_rate * step.milliseconds / 1000), dtype=np.float32))
                    continue
                aiff = os.path.join(td, f'{index}.aiff')
                wav = os.path.join(td, f'{index}.wav')
                subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", aiff, step.text],
                               check=True, capture_output=True)
                subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{self.capability.sample_rate}", aiff, wav],
                               check=True, capture_output=True)
                samples, sample_rate = sf.read(wav, dtype='float32', always_2d=True)
                if sample_rate != self.capability.sample_rate or samples.shape[1] != 1 or not len(samples):
                    raise ValueError('系统语音返回了无效音频')
                pieces.append(samples[:, 0])
        if not pieces or not sum(map(len, pieces)):
            raise ValueError('没有可合成的音频')
        import io
        output = io.BytesIO()
        sf.write(output, np.concatenate(pieces), self.capability.sample_rate, format='WAV')
        return output.getvalue()

    def unload(self) -> None:
        self._loaded = False
