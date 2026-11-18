import re
from dataclasses import dataclass, field, replace

BOM = "﻿"

# 支持的标签：
#   标准 SSML
#     <phoneme ph="zhong4">重</phoneme>
#     <break time="500ms"/>
#     <emphasis>重点词</emphasis>
#   简写
#     [重=chong2]      内联注音（字在括号内）
#     重[zhong4]       后缀注音
#     [break 500]      停顿
TOKEN_RE = re.compile(
    r'<emotion\s+type="(?P<emotion_type>[^"<>\r\n]{1,160})"\s*>(?P<emotion_text>[^<>]*)</emotion>'
    r'|<phoneme\s+ph="(?P<tag_ph>[^"<>\r\n]+)"\s*>(?P<tag_char>[^<]*)</phoneme>'
    r'|<break\s+time="(?P<tag_break>\d+)ms"\s*/>'
    r'|<emphasis>(?P<em_text>[^<]*)</emphasis>'
    r'|\[(?P<in_char>[^\[\]<>=]+)=(?P<in_py>[a-zA-Züv]+[0-4]?)\]'
    r'|\[break\s*(?P<short_break>\d+)(?:ms)?\]'
    r'|(?P<suf_char>[^\s\[\]<>=])\[(?P<suf_py>[a-zA-Züv]+[0-4]?)\]',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ControlSpan:
    kind: str
    start: int
    end: int
    source_start: int
    source_end: int
    value: object


@dataclass(frozen=True)
class SpeechDocument:
    plain_text: str
    phoneme_overrides: dict = field(default_factory=dict)
    breaks: list = field(default_factory=list)
    emphasis_spans: list = field(default_factory=list)
    raw: str = ""
    emotion_spans: list = field(default_factory=list)
    control_spans: list[ControlSpan] = field(default_factory=list)
    version: int = 1


ParseResult = SpeechDocument


def parse_ssml(ssml: str) -> SpeechDocument:
    raw = ssml
    text = ssml.lstrip(BOM)
    prefix = len(raw) - len(text)
    chars = []
    phoneme_overrides = {}
    breaks = []
    emphasis_spans = []
    emotion_spans = []
    controls = []

    pos = 0
    for m in TOKEN_RE.finditer(text):
        gap = text[pos:m.start()]
        bad = re.search(r'</?[A-Za-z][^>]*>|</?(?:phoneme|break|emphasis|emotion)\b|\[(?:break\b|[^\]\n]*=)', gap, re.I)
        if bad:
            raise ValueError(f'存在无法识别或嵌套的标注，位置 {pos + bad.start() + prefix}')
        chars.extend(gap)
        start = len(chars)
        kind = None
        value = None
        if m.group('emotion_type') is not None:
            value = m.group('emotion_type').strip()
            content = m.group('emotion_text')
            if not value or any(c in value for c in '(){}[]') or any(ord(c) < 32 for c in value):
                raise ValueError(f'情绪描述无效，位置 {m.start() + prefix}')
            if not content.strip() or TOKEN_RE.search(content) or re.search(r'\[(?:break\b|[^\]\n]*=)', content, re.I):
                raise ValueError(f'情绪标签不支持嵌套控制或空内容，位置 {m.start() + prefix}')
            chars.extend(content)
            emotion_spans.append((start, len(chars), value))
            kind = 'emotion'
        elif m.group('tag_ph') is not None:
            char, value = m.group('tag_char'), m.group('tag_ph').strip().lower()
            if len(char) != 1 or not re.fullmatch(r'[a-züv]+[0-4]', value):
                raise ValueError(f'注音格式无效，位置 {m.start() + prefix}')
            _emit(chars, char, value, phoneme_overrides)
            kind = 'phoneme'
        elif m.group('tag_break') is not None or m.group('short_break') is not None:
            value = int(m.group('tag_break') or m.group('short_break'))
            if value > 60000:
                raise ValueError(f'停顿不能超过 60000 毫秒，位置 {m.start() + prefix}')
            breaks.append((start, value))
            kind = 'break'
        elif m.group('em_text') is not None:
            content = m.group('em_text')
            if not content or TOKEN_RE.search(content) or re.search(r'\[(?:break\b|[^\]\n]*=)', content, re.I):
                raise ValueError(f'重音标签不支持嵌套控制或空内容，位置 {m.start() + prefix}')
            chars.extend(content)
            emphasis_spans.append((start, len(chars)))
            kind = 'emphasis'
        elif m.group('in_char') is not None or m.group('suf_char') is not None:
            char = m.group('in_char') or m.group('suf_char')
            value = (m.group('in_py') or m.group('suf_py')).strip().lower()
            if len(char) != 1 or not re.fullmatch(r'[a-züv]+[0-4]', value):
                raise ValueError(f'注音格式无效，位置 {m.start() + prefix}')
            _emit(chars, char, value, phoneme_overrides)
            kind = 'phoneme'
        if kind is not None:
            controls.append(ControlSpan(kind, start, len(chars), m.start() + prefix, m.end() + prefix, value))
        pos = m.end()

    gap = text[pos:]
    bad = re.search(r'</?[A-Za-z][^>]*>|</?(?:phoneme|break|emphasis|emotion)\b|\[(?:break\b|[^\]\n]*=)', gap, re.I)
    if bad:
        raise ValueError(f'存在无法识别或嵌套的标注，位置 {pos + bad.start() + prefix}')
    chars.extend(gap)
    return SpeechDocument(plain_text=''.join(chars), phoneme_overrides=phoneme_overrides,
                          breaks=breaks, emphasis_spans=emphasis_spans, raw=raw,
                          emotion_spans=emotion_spans, control_spans=controls)


def apply_confirmed_drops(parsed: SpeechDocument, controls) -> tuple[SpeechDocument, list]:
    requested = set(controls or ())
    allowed = {'phoneme', 'break', 'emphasis', 'emotion'}
    if not requested <= allowed:
        raise ValueError('无法识别的降级控制类型')
    present = {span.kind for span in parsed.control_spans}
    if requested - present:
        raise ValueError('确认的降级类型不在当前输入中')
    report = [{'kind': span.kind, 'source_start': span.source_start, 'source_end': span.source_end,
               'action': 'drop'} for span in parsed.control_spans if span.kind in requested]
    updated = replace(parsed,
                      phoneme_overrides={} if 'phoneme' in requested else parsed.phoneme_overrides,
                      breaks=[] if 'break' in requested else parsed.breaks,
                      emphasis_spans=[] if 'emphasis' in requested else parsed.emphasis_spans,
                      emotion_spans=[] if 'emotion' in requested else parsed.emotion_spans)
    return updated, report


def _emit(chars: list, char: str, pinyin: str, overrides: dict) -> None:
    """把字写入 plain_text，并在其真实下标处登记拼音覆盖"""
    if not char:
        return
    start = len(chars)
    chars.extend(char)
    overrides[start] = pinyin.strip().lower()


def _is_chinese(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def to_engine_request(parsed: ParseResult, voice_id: str = "default", speed: float = 1.0,
                      accepted_controls: tuple = (), reference_mode: str = 'reference',
                      reference_fingerprint: dict | None = None):
    """转换为引擎通用请求"""
    from app.engines.base import TTSRequest

    return TTSRequest(
        text=parsed.plain_text,
        phoneme_overrides=parsed.phoneme_overrides,
        breaks=parsed.breaks,
        emphasis_spans=parsed.emphasis_spans,
        voice_id=voice_id,
        speed=speed,
        raw_ssml=parsed.raw,
        emotion_spans=parsed.emotion_spans,
        accepted_controls=accepted_controls,
        reference_mode=reference_mode,
        reference_fingerprint=reference_fingerprint,
    )
