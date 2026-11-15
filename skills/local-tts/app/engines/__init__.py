from abc import ABC, abstractmethod
from typing import Iterator
from .base import TTSRequest, TTSCapability

class TTSEngine(ABC):
    name: str = "base"
    capability: TTSCapability = TTSCapability()
    # voice_id 是否必须来自本地音色库（克隆类引擎为 True，预置音色引擎为 False）
    voice_from_store: bool = True
    # 占位插槽尚未实现 synthesize，能力矩阵对它没有意义
    implemented: bool = True

    @abstractmethod
    def load(self) -> None:
        """加载模型到显存/内存"""
        ...

    @abstractmethod
    def is_loaded(self) -> bool:
        ...

    @abstractmethod
    def clone_voice(self, ref_wav_path: str, voice_id: str, prompt_text: str = "") -> str:
        """从参考音频克隆音色，返回 voice_id。prompt_text 是音频的逐字文本，
        零样本类引擎靠它对齐音色；预置音色引擎（如 macsay）可忽略。"""
        ...

    @abstractmethod
    def synthesize(self, req: TTSRequest) -> bytes:
        """同步合成，返回 wav bytes"""
        ...

    def synthesize_stream(self, req: TTSRequest) -> Iterator[bytes]:
        """流式合成，默认降级为一次性返回"""
        yield self.synthesize(req)

    def unload(self) -> None:
        """释放资源，用于热切换"""
        pass

    def forget_voice(self, voice_id: str) -> None:
        """音色被删除或替换时清掉引擎内缓存的参考特征（无缓存则无需实现）"""
        pass

    def get_capability(self) -> TTSCapability:
        return self.capability
