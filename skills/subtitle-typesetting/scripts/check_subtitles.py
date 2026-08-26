#!/usr/bin/env python3
"""
短视频字幕排版校验脚本
用法：
    python3 check_subtitles.py <字幕文件.txt>
    cat 字幕.txt | python3 check_subtitles.py

校验规则（与 SKILL.md 核心约束一致）：
1. 每行 ≤8 字符（汉字、数字、英文均按 1 字符计）
2. 无标点符号（只允许汉字、数字、英文、% 、·）
3. 不以"的"开头
4. 无空行
5. 启发式：常见词组未被跨行拆开
"""

import sys
import re

MAX_LEN = 8

# 允许的字符集：汉字、数字、大小写英文、% 、·（外国人名音译分隔符）
ALLOWED = re.compile(r"^[\u4e00-\u9fff0-9A-Za-z%\u00b7]+$")

# 常见不可拆分词组表（按需扩充）
PHRASES = [
    "中华人民共和国", "互联网", "社会主义", "世界观", "人生观", "价值观",
    "生活方式", "短视频", "视频号", "直播间", "出版社", "畅销书",
    "读后感", "文学奖", "诺贝尔", "主人公", "这本书", "一句话",
    "一辈子", "了不起", "第一次", "一下子", "刚开始", "到最后",
    "尤其是", "特别是", "实际上", "事实上", "不知不觉", "无论如何",
    "加油站", "理财书", "口播稿", "带货", "下单", "橱窗", "点个赞",
    "这本书", "那本书", "每个人", "所有人", "年轻人", "老年人",
    "读后感", "知识改变", "命运", "财富自由", "人生三修",
]


def check_line(line: str) -> list:
    """返回该行的违规原因列表，空列表表示合规。"""
    errors = []
    if not line.strip():
        return ["空行"]
    if len(line) > MAX_LEN:
        errors.append(f"超长({len(line)}字>8)")
    if not ALLOWED.match(line):
        errors.append("含标点或非法字符")
    if line.startswith("的"):
        errors.append('以"的"开头')
    return errors


def check_phrase_breaks(lines: list) -> list:
    """启发式检测常见词组是否被跨行拆开（词组前半在行尾、后半在下一行行首）。"""
    issues = []
    for i in range(len(lines) - 1):
        tail = lines[i]
        head = lines[i + 1]
        for ph in PHRASES:
            if len(ph) < 2:
                continue
            # 词组在行尾截断：该行以词组前缀结尾
            for cut in range(1, len(ph)):
                prefix, suffix = ph[:cut], ph[cut:]
                if tail.endswith(prefix) and head.startswith(suffix):
                    issues.append(f"第{i+1}-{i+2}行疑似拆分词组「{ph}」：{tail} | {head}")
                    break
    return issues


def main() -> None:
    if len(sys.argv) >= 2:
        with open(sys.argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    lines = [ln.strip() for ln in text.splitlines()]
    problems = 0

    for idx, line in enumerate(lines, 1):
        for err in check_line(line):
            problems += 1
            print(f"第{idx}行 [{line}] → {err}")

    for msg in check_phrase_breaks(lines):
        problems += 1
        print(msg)

    if problems == 0:
        print("PASS: 全部行符合规则（每行≤8字、无标点、的-不顶行、无空行、常见词组完整）")
    else:
        print(f"FAIL: 共 {problems} 处问题，请修正后重跑校验")


if __name__ == "__main__":
    main()
