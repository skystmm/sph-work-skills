"""把带数字声调的拼音转成 CosyVoice3 的 pronunciation-inpainting token。

CosyVoice3 tokenizer 的拼音 special token 表（cosyvoice/tokenizer/tokenizer.py:280+）
按「声母 + 带调韵母」拆分，例如
    chǎng -> [ch][ǎng]      jiàn -> [j][iàn]      jǐ -> [j][ǐ]
    ān    -> [ān]           yī   -> [y][ī]       lǜ -> [l][ǜ]
用户标签写的是数字声调（[阊=chang1]），本模块负责换写成上表的形式。
CosyVoice2 的 tokenizer 没有这些 token，不支持注音。
"""

VOWELS = "aoeiuü"

TONE_MARKS = {
    "a": "āáǎà",
    "o": "ōóǒò",
    "e": "ēéěè",
    "i": "īíǐì",
    "u": "ūúǔù",
    "ü": "ǖǘǚǜ",
}

# 最长优先，避免 zh 被切成 z
INITIALS = ("zh", "ch", "sh") + tuple("bpmfdtnlgkhjqxrzcsyw")


def numeric_to_tone(pinyin: str) -> str:
    """chang1 -> chǎng, lv4 -> lǜ, de0 -> de"""
    py = pinyin.strip().lower().replace("v", "ü").replace("u:", "ü")
    tone = ""
    if py and py[-1].isdigit():
        tone = py[-1]
        py = py[:-1]
    if tone in ("", "0", "5"):
        return py

    idx = _tone_position(py)
    if idx is None:
        return py
    vowel = py[idx]
    marked = TONE_MARKS[vowel][int(tone) - 1]
    return py[:idx] + marked + py[idx + 1:]


def _tone_position(py: str) -> int | None:
    """按《汉语拼音方案》标调规则定位要标调的元音"""
    for v in ("a", "o", "e"):
        i = py.find(v)
        if i != -1:
            return i
    if "iu" in py:
        return py.find("iu") + 1
    if "ui" in py:
        return py.find("ui") + 1
    for i in range(len(py) - 1, -1, -1):
        if py[i] in VOWELS:
            return i
    return None


def split_initial(rime: str) -> tuple[str, str]:
    """chǎng -> ('ch', 'ǎng')；ān -> ('', 'ān')"""
    for ini in INITIALS:
        if rime.startswith(ini):
            rest = rime[len(ini):]
            # y/w 后面必须有韵母才是声母，单独的 y 不是音节
            if rest:
                return ini, rest
    return "", rime


def to_tokens(pinyin: str) -> str:
    """chang1 -> '[ch][ǎng]'；an1 -> '[ān]'"""
    syllable = numeric_to_tone(pinyin)
    if not syllable:
        return ""
    ini, rime = split_initial(syllable)
    if not ini:
        return f"[{rime}]"
    return f"[{ini}][{rime}]"
