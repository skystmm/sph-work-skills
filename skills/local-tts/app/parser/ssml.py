import re
from dataclasses import dataclass, field

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
    r'<phoneme\s+ph="(?P<tag_ph>[^"]+)"\s*>(?P<tag_char>[^<]*)</phoneme>'
    r'|<break\s+time="(?P<tag_break>\d+)ms"\s*/>'
    r'|<emphasis>(?P<em_text>[^<]*)</emphasis>'
    r'|\[(?P<in_char>[^\[\]<>=]+)=(?P<in_py>[a-zA-Züv]+[0-4]?)\]'
    r'|\[break\s*(?P<short_break>\d+)(?:ms)?\]'
    r'|(?P<suf_char>[^\s\[\]<>=])\[(?P<suf_py>[a-zA-Züv]+[0-4]?)\]',
    re.IGNORECASE,
)


@dataclass
class ParseResult:
    plain_text: str
    phoneme_overrides: dict = field(default_factory=dict)  # {char_index: pinyin}
    breaks: list = field(default_factory=list)  # [(char_pos, ms)]
    emphasis_spans: list = field(default_factory=list)  # [(start, end)]
    raw: str = ""


def parse_ssml(ssml: str) -> ParseResult:
    raw = ssml
    text = ssml.lstrip(BOM)

    chars: list[str] = []
    phoneme_overrides: dict[int, str] = {}
    breaks: list[tuple[int, int]] = []
    emphasis_spans: list[tuple[int, int]] = []

    pos = 0
    for m in TOKEN_RE.finditer(text):
        chars.extend(text[pos:m.start()])

        if m.group("tag_ph") is not None:
            _emit(chars, m.group("tag_char"), m.group("tag_ph"), phoneme_overrides)
        elif m.group("tag_break") is not None:
            breaks.append((len(chars), int(m.group("tag_break"))))
        elif m.group("em_text") is not None:
            start = len(chars)
            content = m.group("em_text")
            chars.extend(content)
            emphasis_spans.append((start, start + len(content)))
        elif m.group("in_char") is not None:
            _emit(chars, m.group("in_char"), m.group("in_py"), phoneme_overrides)
        elif m.group("short_break") is not None:
            breaks.append((len(chars), int(m.group("short_break"))))
        elif m.group("suf_char") is not None:
            _emit(chars, m.group("suf_char"), m.group("suf_py"), phoneme_overrides)

        pos = m.end()

    chars.extend(text[pos:])

    return ParseResult(
        plain_text="".join(chars),
        phoneme_overrides=phoneme_overrides,
        breaks=breaks,
        emphasis_spans=emphasis_spans,
        raw=raw,
    )


def _emit(chars: list, char: str, pinyin: str, overrides: dict) -> None:
    """把字写入 plain_text，并在其真实下标处登记拼音覆盖"""
    if not char:
        return
    start = len(chars)
    chars.extend(char)
    overrides[start] = pinyin.strip().lower()


def _is_chinese(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def to_engine_request(parsed: ParseResult, voice_id: str = "default", speed: float = 1.0):
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
    )
