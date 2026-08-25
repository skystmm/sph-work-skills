# sph-work-skills

图书带货短视频内容创作工作流配套的 WorkBuddy / CodeBuddy Skill 集。

本仓库收录短视频口播文案从「合规审查 → 合规改写 → 照读优化」全链路所需的技能，供多设备 / 多环境复用。

## 技能清单

| Skill | 作用 | 核心能力 |
|-------|------|----------|
| [short-video-compliance-judge](skills/short-video-compliance-judge/) | 短视频口播文案合规判定与改写 | 七类违规扫描（诱导互动 / 恐吓诅咒 / 道德绑架 / 悬念强迫 / 承诺诱导 / 无法验证的承诺 / 健康功效绝对化）→ 严重程度标注（🔴致命 / 🟡风险 / 🟢安全）→ 合规改写方案 → 账号处理建议（删 / 改 / 重发） |
| [pronunciation-annotation](skills/pronunciation-annotation/) | 口播稿注音标注（照读优化） | 多音字 + 生僻字注音（`[字=pinyin+声调]` 格式，如 `[长=chang2]`、顾[恺=kai3]之）→ 注音版口播稿（含一键复制）→ 阿拉伯数字年份转中文（1948 年 → 一九四八年） |

## 典型工作流

```
口播文案
  │
  ├─→ short-video-compliance-judge  ① 合规审查：定位触发句、标严重程度
  │        │
  │        └─→ ② 合规改写：降施压感、去诱导指令
  │
  └─→ pronunciation-annotation      ③ 照读优化：多音字/生僻字注音 + 年份转中文
          │
          └─→ 注音版口播稿（可直接照读、一键复制）
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
    ├── short-video-compliance-judge/
    │   ├── SKILL.md                      # 合规判定 + 改写主流程
    │   └── references/
    │       └── violation-rules.md        # 七类违规规则库与改写范式
    └── pronunciation-annotation/
        ├── SKILL.md                      # 注音标注主流程
        └── references/
            ├── polyphone-words.md        # 口播高频易错多音字库
            └── rare-words.md             # 口播易错生僻字库（人名/地名/书名/器物/雅词）
```

## Skill 协作关系

两个 skill 职责互补、可独立使用：

- **short-video-compliance-judge** 管「能不能发」：判定是否踩线、给出合规改写与账号处理建议。
- **pronunciation-annotation** 管「怎么读顺」：只做注音与年份转汉字，不含违规判定。
- 组合用法：先合规审查与改写，再将定稿交给注音 skill 产出可照读的注音版口播稿。

## 安全说明

- 技能内不含任何密钥、token 或敏感信息，可放心公开。
- 各 skill 均标注 `agent_created: true`，可被 WorkBuddy 正常加载使用。
