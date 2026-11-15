from dataclasses import dataclass, field

@dataclass
class TTSRequest:
    text: str  # 纯文本，已去除标签
    phoneme_overrides: dict = field(default_factory=dict)  # {char_index: "zhong4"}
    breaks: list = field(default_factory=list)  # [(char_pos, ms)]
    emphasis_spans: list = field(default_factory=list)  # [(start, end)]
    voice_id: str = "default"
    speed: float = 1.0
    # 中间表示：解析后用于调试
    raw_ssml: str = ""

@dataclass
class TTSCapability:
    supports_phoneme: bool = True
    supports_break_ms: bool = True
    supports_emphasis: bool = True
    supports_streaming: bool = False
    supports_chinese: bool = True
    clone_mode: str = "zero_shot"  # zero_shot | finetune
    sample_rate: int = 22050

@dataclass
class VoiceInfo:
    voice_id: str
    name: str
    ref_wav: str
    created_at: str
    engine: str
