"""F5-TTS 引擎适配器 - 插槽已预留"""
from pathlib import Path
from app.engines import TTSEngine
from app.engines.base import TTSRequest, TTSCapability
from app.engines.registry import register


@register("f5tts")
class F5TTSEngine(TTSEngine):
    name = "f5tts"
    implemented = False  # 占位插槽，synthesize 尚未实现
    capability = TTSCapability(
        supports_phoneme=True,
        supports_break_ms=False,  # F5 原生通过标点控制，毫秒级需转译
        supports_emphasis=False,
        supports_streaming=False,
        supports_chinese=True,
        clone_mode="zero_shot",
        sample_rate=24000,
    )

    def __init__(self, model_path: str = "./models/F5-TTS"):
        self.model_path = Path(model_path)
        self._loaded = False

    def load(self) -> None:
        if not self.model_path.exists():
            raise FileNotFoundError(f"F5-TTS 模型未找到: {self.model_path}")
        print(f"[F5-TTS] loading from {self.model_path} ...")
        self._loaded = True

    def is_loaded(self) -> bool:
        return self._loaded

    def clone_voice(self, ref_wav_path: str, voice_id: str, prompt_text: str = "") -> str:
        print(f"[F5-TTS] clone_voice {voice_id}")
        return voice_id

    def synthesize(self, req: TTSRequest) -> bytes:
        # F5-TTS 特点：无需 G2P，直接文本+参考音频
        # breaks 需转译为标点：500ms -> "。"
        raise NotImplementedError("F5-TTS 插槽已就绪，安装 F5-TTS 后实现 synthesize 即可")

    def unload(self) -> None:
        self._loaded = False
