"""校验拼音标签 -> CosyVoice3 token 的转换，并确认每个 token 都在模型词表中。

不加载模型，纯静态检查。用法: .venv/bin/python scripts/verify_pinyin_tokens.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engines.cosyvoice.pinyin_tokens import to_tokens

TOKENIZER_PY = ROOT / "third_party/CosyVoice/cosyvoice/tokenizer/tokenizer.py"


def load_vocab() -> set[str]:
    """从 CosyVoice3Tokenizer 的 special token 表里抠出所有 [x] 形式的拼音 token"""
    src = TOKENIZER_PY.read_text(encoding="utf-8")
    start = src.index("class CosyVoice3Tokenizer")
    end = src.index("class ", start + 10) if "class " in src[start + 10:] else len(src)
    block = src[start:end]
    return set(re.findall(r'"(\[[^\[\]]+\])"', block))


ANNOT_RE = re.compile(r"\[([^\[\]<>=]+)=([a-zA-Züv]+[0-4]?)\]")

CASES = [
    ("chang1", "[ch][āng]"),
    ("chang3", "[ch][ǎng]"),
    ("he2", "[h][é]"),
    ("xian4", "[x][iàn]"),
    ("jian4", "[j][iàn]"),
    ("chong2", "[ch][óng]"),
    ("zhong4", "[zh][òng]"),
    ("an1", "[ān]"),
    ("yi1", "[y][ī]"),
    ("lv4", "[l][ǜ]"),
    ("liu2", "[l][iú]"),
    ("gui1", "[g][uī]"),
    ("jun1", "[j][ūn]"),
    ("er2", "[ér]"),
    ("nv3", "[n][ǚ]"),
    ("de0", "[d][e]"),
    ("wu3", "[w][ǔ]"),
    ("zhi1", "[zh][ī]"),
    ("jiong3", "[j][iǒng]"),
]


def main() -> int:
    vocab = load_vocab()
    print(f"词表拼音 token 数: {len(vocab)}")

    fails = 0
    print("\n-- 单元用例 --")
    for py, expect in CASES:
        got = to_tokens(py)
        ok = got == expect
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {py:10s} -> {got:16s} expect {expect}")

    art = ROOT / "examples/yuwenkai.txt"
    print(f"\n-- 文章注音 {art.name} --")
    pairs = ANNOT_RE.findall(art.read_text(encoding="utf-8"))
    print(f"注音条数: {len(pairs)}")
    bad = []
    for _, py in pairs:
        for tok in re.findall(r"\[[^\[\]]+\]", to_tokens(py)):
            if tok not in vocab:
                bad.append((py, tok))
    if bad:
        fails += 1
        print("词表缺失 token:")
        for py, tok in sorted(set(bad)):
            print(f"  {py} -> {tok}")
    else:
        print("全部 token 命中词表")

    print("\nRESULT:", "PASS" if fails == 0 else f"FAIL ({fails})")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
