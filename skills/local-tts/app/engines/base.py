import re
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
    emotion_spans: list = field(default_factory=list)  # [(start, end, instruction)]
    accepted_controls: tuple = ()
    reference_mode: str = 'reference'
    reference_fingerprint: dict | None = None


@dataclass(frozen=True)
class ProviderStep:
    kind: str  # speak | silence | legacy
    text: str = ""
    instruction: str = ""
    milliseconds: int = 0
    source_start: int = 0
    source_end: int = 0
    source_map: tuple = ()  # (source_start, source_end, output_start, output_end)


@dataclass(frozen=True)
class ProviderPlan:
    version: int
    steps: tuple[ProviderStep, ...]
    request: TTSRequest

@dataclass
class TTSCapability:
    supports_phoneme: bool = False
    supports_break_ms: bool = False
    supports_emphasis: bool = False
    supports_streaming: bool = False
    supports_chinese: bool = False
    clone_mode: str = "zero_shot"  # zero_shot | finetune
    sample_rate: int = 22050
    supports_speed: bool = False
    supports_emotion: bool = False
    experimental_controls: tuple[str, ...] = ()
    reference_modes: tuple[str, ...] = ('reference',)


def validate_request(req: TTSRequest, capability: TTSCapability) -> None:
    if not isinstance(req.text, str):
        raise ValueError('文本必须是字符串')
    if not isinstance(req.reference_mode, str) or req.reference_mode not in capability.reference_modes:
        raise ValueError('模型不支持该参考音频模式')
    if req.reference_mode == 'reference_plus_prompt' and req.emotion_spans:
        raise ValueError('参考音频加逐字稿模式与情绪控制冲突；请明确丢弃情绪或使用 reference 模式')
    for pos, value in req.phoneme_overrides.items():
        if type(pos) is not int or not 0 <= pos < len(req.text) or not isinstance(value, str) or not re.fullmatch(r'[a-züv]+[0-4]', value):
            raise ValueError('注音位置或格式无效')
    totals = {}
    for pos, milliseconds in req.breaks:
        if (type(pos) is not int or not 0 <= pos <= len(req.text) or type(milliseconds) is not int
                or not 0 <= milliseconds <= 60000):
            raise ValueError('停顿位置或时长无效')
        totals[pos] = totals.get(pos, 0) + milliseconds
        if totals[pos] > 60000:
            raise ValueError('同一位置累计停顿不能超过 60000 毫秒')
    for requested, supported, label in (
        (req.phoneme_overrides, capability.supports_phoneme, "注音"),
        (req.breaks, capability.supports_break_ms, "停顿"),
        (req.emphasis_spans, capability.supports_emphasis, "重音"),
        (req.emotion_spans, capability.supports_emotion, "情绪"),
    ):
        if requested and not supported:
            raise ValueError(f"模型不支持{label}")
    for control, requested in (("phoneme", req.phoneme_overrides), ("emotion", req.emotion_spans)):
        if requested and control in capability.experimental_controls and control not in req.accepted_controls:
            raise ValueError(f"{control} 为实验性控制，请确认后在本次任务使用 --accept-control {control}")
    if not 0.5 <= req.speed <= 2.0:
        raise ValueError("语速必须在 0.5 到 2.0 之间")
    if req.speed != 1.0 and not capability.supports_speed:
        raise ValueError("模型不支持调整语速")

@dataclass
class VoiceInfo:
    voice_id: str
    name: str
    ref_wav: str
    created_at: str
    engine: str
