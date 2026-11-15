"""长文本分块。

模型对单次输入长度有限制，长文必须切开逐块合成。分块在这里做而不是交给引擎，
是为了让调用方能保存进度、失败后从断点续跑。

切分规则：
  1. 显式 [break] 处必须切，并记住该处要插入的静音时长
  2. 剩下的长段落按句末标点切，尽量不超过 max_len
"""

SENT_END = "。！？!?…"
CLAUSE = "，,、；;：:"


def _split_sentences(text: str, max_len: int, min_len: int) -> list[str]:
    """切出的片段拼接后必须等于 text —— 调用方依赖长度累加来重映射注音下标"""
    pieces: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in SENT_END and len(buf) >= min_len:
            pieces.append(buf)
            buf = ""
        elif len(buf) >= max_len:
            cut = _last_clause(buf)
            if cut:
                pieces.append(buf[:cut])
                buf = buf[cut:]
            else:
                pieces.append(buf)
                buf = ""
    if buf:
        pieces.append(buf)
    return pieces


def _last_clause(buf: str) -> int:
    """在 buf 后半段找最后一个逗号类标点作为断点，找不到返回 0"""
    half = len(buf) // 2
    for i in range(len(buf) - 1, half - 1, -1):
        if buf[i] in CLAUSE:
            return i + 1
    return 0


def chunk_text_with_metadata(
    text: str,
    overrides: dict | None = None,
    breaks: list | None = None,
    emphasis_spans: list | None = None,
    max_len: int = 120,
    min_len: int = 12,
) -> list[dict]:
    """按原文坐标分块并映射全部元数据。

    返回的 breaks 和 emphasis_spans 均使用各块内的半开区间坐标。
    停顿坐标位于两个块的边界时只归前一块；位置 0 归首块。
    同一位置的停顿时长相加。空文本有停顿时返回一个空文本块。
    """
    if max_len <= 0 or min_len < 0:
        raise ValueError("max_len must be positive and min_len nonnegative")

    overrides = overrides or {}
    emphasis_spans = emphasis_spans or []
    break_at: dict[int, int] = {}
    for pos, ms in breaks or []:
        pos = max(0, min(pos, len(text)))
        break_at[pos] = break_at.get(pos, 0) + ms

    bounds = [0, *sorted(p for p in break_at if 0 < p < len(text)), len(text)]
    chunks: list[dict] = []
    for start, end in zip(bounds, bounds[1:]):
        offset = start
        for piece in _split_sentences(text[start:end], max_len, min_len):
            stop = offset + len(piece)
            local_breaks = []
            if offset == 0 and 0 in break_at:
                local_breaks.append((0, break_at[0]))
            if stop in break_at:
                local_breaks.append((len(piece), break_at[stop]))
            chunks.append({
                "text": piece,
                "overrides": {i - offset: value for i, value in overrides.items()
                              if offset <= i < stop},
                "breaks": local_breaks,
                "emphasis_spans": [(max(a, offset) - offset, min(b, stop) - offset)
                                   for a, b in emphasis_spans
                                   if max(a, offset) < min(b, stop)],
            })
            offset = stop

    if not chunks and break_at:
        chunks.append({"text": "", "overrides": {},
                       "breaks": [(0, break_at[0])], "emphasis_spans": []})
    return chunks


def chunk_text(
    text: str,
    overrides: dict | None = None,
    breaks: list | None = None,
    max_len: int = 120,
    min_len: int = 12,
) -> list[tuple[str, dict, int]]:
    """-> [(chunk_text, 重映射后的注音, chunk 后要插入的静音毫秒)]"""
    overrides = dict(overrides or {})
    break_at: dict[int, int] = {}
    for pos, ms in breaks or []:
        pos = max(0, min(pos, len(text)))
        break_at[pos] = max(break_at.get(pos, 0), ms)

    hard = sorted(p for p in break_at if 0 < p < len(text))
    bounds = [0] + hard + [len(text)]

    chunks: list[tuple[str, dict, int]] = []
    for a, b in zip(bounds, bounds[1:]):
        gap_after_segment = break_at.get(b, 0)
        pieces = _split_sentences(text[a:b], max_len, min_len)
        off = a
        for k, piece in enumerate(pieces):
            ov = {i - off: py for i, py in overrides.items() if off <= i < off + len(piece)}
            is_last = k == len(pieces) - 1
            gap = gap_after_segment if is_last else 0
            if not piece.strip() and chunks:
                # 纯空白（换行等）并入前一块，避免产生空音频块；下标不受影响
                prev_t, prev_ov, prev_gap = chunks[-1]
                chunks[-1] = (prev_t + piece, prev_ov, prev_gap or gap)
            else:
                chunks.append((piece, ov, gap))
            off += len(piece)
    return chunks
