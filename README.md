# sph-work-skills

图书带货短视频内容创作工作流配套的 WorkBuddy / CodeBuddy Skill 集。

本仓库收录图书带货短视频口播文案从「二创生成 → 合规审查 → 合规改写 → 照读优化 → 字幕排版」全链路所需的技能，供多设备 / 多环境复用。

## 技能清单

| Skill | 作用 | 核心能力 |
|-------|------|----------|
| [book-video-script-rewrite](skills/book-video-script-rewrite/) | 图书类短视频文案深度二创与原创生成 | 掐头去尾（黄金钩子 + 转化闭环一字不动）、重塑中段（六策略工具箱组合深度重写，相似度 <20%）→ 多模式交付（二创 / 原创 / 口吻适配 / 标题简介 / 购买引导评论 / 框架对照补强） |
| [short-video-compliance-judge](skills/short-video-compliance-judge/) | 短视频口播文案合规判定与改写 | 七类违规扫描（诱导互动 / 恐吓诅咒 / 道德绑架 / 悬念强迫 / 承诺诱导 / 无法验证的承诺 / 健康功效绝对化）→ 严重程度标注（🔴致命 / 🟡风险 / 🟢安全）→ 合规改写方案 → 账号处理建议（删 / 改 / 重发） |
| [pronunciation-annotation](skills/pronunciation-annotation/) | 口播稿注音标注（照读优化） | 多音字 + 生僻字注音（`[字=pinyin+声调]` 格式，如 `[长=chang2]`、顾[恺=kai3]之）→ 注音版口播稿（含一键复制）→ 阿拉伯数字年份转中文（1948 年 → 一九四八年） |
| [subtitle-typesetting](skills/subtitle-typesetting/) | 短视频字幕排版 | 按语义与口语节奏拆分每行 ≤8 字的无标点纯文本短句（可直接导入剪映 / PR / 必剪字幕轨道）→ 程序化校验脚本终检（每行字数 / 标点 / "的"不顶行 / 词组完整） |

## 典型工作流

```
图书素材 / 对标文案
  │
  ├─→ book-video-script-rewrite       ① 二创生成：掐头去尾、重塑中段，产出口播文案
  │        │
  │        └─→ 口播文案
  │
  ├─→ short-video-compliance-judge    ② 合规审查：定位触发句、标严重程度
  │        │
  │        └─→ ③ 合规改写：降施压感、去诱导指令
  │
  ├─→ pronunciation-annotation        ④ 照读优化：多音字/生僻字注音 + 年份转中文
  │        │
  │        └─→ 注音版口播稿（可直接照读、一键复制）
  │
  └─→ subtitle-typesetting            ⑤ 字幕排版：每行≤8字无标点短句 → 导入剪映/PR/必剪
```

## 安装方式

将所需 skill 目录（如 `skills/short-video-compliance-judge/`）复制到本机 WorkBuddy 技能目录：

```bash
# 用户级技能（推荐，所有项目可用）
cp -R skills/<skill-name> ~/.workbuddy/skills/

# 或项目级技能（仅当前项目可用）
cp -R skills/<skill-name> <workspace>/.workbuddy/skills/
```

## 目录结构

```
sph-work-skills/
├── README.md
└── skills/
    ├── book-video-script-rewrite/
    │   └── SKILL.md                      # 二创 + 原创生成主流程（六策略工具箱）
    ├── short-video-compliance-judge/
    │   ├── SKILL.md                      # 合规判定 + 改写主流程
    │   └── references/
    │       └── violation-rules.md        # 七类违规规则库与改写范式
    ├── pronunciation-annotation/
    │   ├── SKILL.md                      # 注音标注主流程
    │   └── references/
    │       ├── polyphone-words.md        # 口播高频易错多音字库
    │       └── rare-words.md             # 口播易错生僻字库（人名/地名/书名/器物/雅词）
    └── subtitle-typesetting/
        ├── SKILL.md                      # 字幕排版主流程（每行≤8字、无标点）
        └── scripts/
            └── check_subtitles.py        # 字幕规则程序化校验脚本
```

## Skill 协作关系

四个 skill 职责互补、可独立使用：

- **book-video-script-rewrite** 管「怎么产出」：深度二创 / 原创生成口播文案，输出标题、简介、购买引导评论等全套交付物。
- **short-video-compliance-judge** 管「能不能发」：判定是否踩线、给出合规改写与账号处理建议。
- **pronunciation-annotation** 管「怎么读顺」：只做注音与年份转汉字，不含违规判定。
- **subtitle-typesetting** 管「怎么上字幕」：将定稿口播稿拆成每行 ≤8 字的无标点短句，供剪辑软件直接导入。
- 组合用法：二创生成文案 → 合规审查与改写 → 注音 skill 产出可照读的注音版口播稿 → 字幕排版 skill 产出可导入剪映的字幕文本。

## 安全说明

- 技能内不含任何密钥、token 或敏感信息，可放心公开。
- 各 skill 均标注 `agent_created: true`，可被 WorkBuddy 正常加载使用。
