"""macOS say 引擎：无需下载模型即可产出真实语音，用于快速验证链路。

限制：say 不接受拼音输入，因此不支持 phoneme 覆盖（需 CosyVoice3 等真实引擎）。
停顿通过 [[slnc ms]] 内嵌指令实现。
"""
import os
import shutil
import subprocess
import tempfile

from app.engines import TTSEngine
from app.engines.base import TTSRequest, TTSCapability
from app.engines.registry import register

BASE_RATE = 180  # say 默认语速


@register("macsay")
class MacSayEngine(TTSEngine):
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

    def synthesize(self, req: TTSRequest) -> bytes:
        if not self._loaded:
            self.load()
        text = inject_breaks(req.text, req.breaks)
        voice = self.default_voice if req.voice_id == "default" else req.voice_id
        rate = max(80, int(BASE_RATE * req.speed))

        with tempfile.TemporaryDirectory() as td:
            aiff = os.path.join(td, "out.aiff")
            wav = os.path.join(td, "out.wav")
            subprocess.run(
                ["say", "-v", voice, "-r", str(rate), "-o", aiff, text],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["afconvert", "-f", "WAVE", "-d", f"LEI16@{self.capability.sample_rate}", aiff, wav],
                check=True, capture_output=True,
            )
            with open(wav, "rb") as f:
                return f.read()

    def unload(self) -> None:
        self._loaded = False


def inject_breaks(text: str, breaks: list) -> str:
    """按字符下标插入 [[slnc ms]] 静音指令"""
    if not breaks:
        return text
    parts = []
    last = 0
    for pos, ms in sorted(breaks):
        pos = min(pos, len(text))
        parts.append(text[last:pos])
        parts.append(f"[[slnc {ms}]]")
        last = pos
    parts.append(text[last:])
    return "".join(parts)
