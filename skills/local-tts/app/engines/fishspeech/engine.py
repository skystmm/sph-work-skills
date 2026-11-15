"""Fish-Speech 引擎适配器 - 插槽已预留"""
from pathlib import Path
from app.engines import TTSEngine
from app.engines.base import TTSRequest, TTSCapability
from app.engines.registry import register


@register("fishspeech")
class FishSpeechEngine(TTSEngine):
    name = "fishspeech"
    implemented = False  # 占位插槽，synthesize 尚未实现
    capability = TTSCapability(
        supports_phoneme=True,
        supports_break_ms=True,
        supports_emphasis=True,
        supports_streaming=False,
        supports_chinese=True,
        clone_mode="zero_shot",
        sample_rate=44100,
    )

    def __init__(self, model_path: str = "./models/FishSpeech-1.5"):
        self.model_path = Path(model_path)
        self._loaded = False

    def load(self) -> None:
        if not self.model_path.exists():
            raise FileNotFoundError(f"Fish-Speech 模型未找到: {self.model_path}")
        print(f"[Fish-Speech] loading from {self.model_path} ...")
        self._loaded = True

    def is_loaded(self) -> bool:
        return self._loaded

    def clone_voice(self, ref_wav_path: str, voice_id: str, prompt_text: str = "") -> str:
        print(f"[Fish-Speech] clone_voice {voice_id}")
        return voice_id

    def synthesize(self, req: TTSRequest) -> bytes:
        # Fish-Speech 原生支持 SSML，可直接透传标签
        raise NotImplementedError("Fish-Speech 插槽已就绪，安装后实现即可")

    def unload(self) -> None:
        self._loaded = False
