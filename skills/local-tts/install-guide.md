# local-tts 安装指南：VoxCPM2

本仓库包含技能指令和 TTS 后端源码，**不包含模型权重、虚拟环境、参考录音、个人音色或生成音频**。以下主要说明本项目已在 Apple Silicon macOS 上验证的 VoxCPM2 路径；Windows、Linux 和全新机器的端到端安装尚未验证。

## 1. 前提与目录

需要 Apple Silicon Mac、可用磁盘空间、网络、Python 3.11/3.12 和 `uv`。项目固定使用 `mlx-community/VoxCPM2-8bit`：主权重约 3.22 GB，依赖与下载暂存还需额外空间。安装前请确认模型使用条款；不要将模型、音色、任务记录或音频提交到 GitHub。

先克隆本仓库（若已克隆可跳过）：

```bash
git clone https://github.com/skystmm/sph-work-skills.git
```

将 `VOICE_TTS_ROOT` 设置为其中 `skills/local-tts` 的**真实绝对路径**。请先确认检出包含本指南的分支或版本；以下示例中的路径需要替换：

```bash
export VOICE_TTS_ROOT="/绝对路径/sph-work-skills/skills/local-tts"
```

下方所有命令使用这一绝对路径。安装入口不授予 Agent 文件访问或运行权限；远程 Agent 也不会自动访问本机。

## 2. 准备两个隔离环境

主环境运行 CLI 和技能安装器，MLX 模型使用独立环境；不会覆盖已有环境。以下以 `uv` 为例，可能需要下载对应 Python 和依赖：

```bash
uv venv --python 3.12 "$VOICE_TTS_ROOT/.venv"
```

```bash
uv pip install --python "$VOICE_TTS_ROOT/.venv/bin/python" -r "$VOICE_TTS_ROOT/requirements.txt"
```

```bash
uv venv --python 3.11 "$VOICE_TTS_ROOT/.venv-mlx"
```

```bash
uv pip install --python "$VOICE_TTS_ROOT/.venv-mlx/bin/python" 'mlx-audio==0.6.5' 'mlx==0.32.4' 'soundfile==0.14.0'
```

以上为当前项目验证过的依赖组合，不保证未来版本或所有硬件相同。`.venv` 与 `.venv-mlx` 已被忽略，不要提交。

## 3. 下载 VoxCPM2 模型

获得用户对模型下载、磁盘占用及网络访问的同意后，先确保模型父目录存在：

```bash
mkdir -p "$VOICE_TTS_ROOT/models"
```

安装到默认的 `models/VoxCPM2`：

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/model_install.py" install voxcpm2 "$VOICE_TTS_ROOT/models/VoxCPM2"
```

安装器先访问 Hugging Face；失败时尝试第三方 **HF-Mirror**。它不读取或发送 Hugging Face Token，也不修改全局镜像配置。内置清单固定仓库 revision `d52725898a0675703f7f9ddc5a4d1a3cdbb99032` 和各文件大小、SHA-256；下载到私有暂存区并全部校验通过后才发布。校验可发现文件损坏或版本不符，但不是独立签名，无法排除发布方或托管平台被攻陷。

如果 Hugging Face 和镜像均不可访问，请检查网络或在获授权后从可信备份导入；不要关闭 TLS 校验或向镜像提供私人凭据。中断后用同样命令可尝试继续；已有目标只做只读校验，失败不会覆盖或自动修复。校验已有完整目录：

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/model_install.py" verify voxcpm2 "$VOICE_TTS_ROOT/models/VoxCPM2"
```

也可导入符合内置清单的可信备份；目标目录必须不存在，父目录必须已存在：

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/model_install.py" import voxcpm2 /绝对路径/新模型目录 /绝对路径/可信备份目录
```

导入/下载到自定义目录不会自动更改 `config.yaml`。目录校验要求文件集合与清单一致；额外文件也可能导致校验不通过，但不会被删除。

## 4. 安装技能入口

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/install_agent_skill.py" check
```

确认目标目录无冲突后安装：

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/install_agent_skill.py" install
```

安装器将技能入口写入 `~/.agents/skills/local-tts` 与 `~/.codebuddy/skills/local-tts`，不会复制后端、模型或音色。它不会覆盖用户修改或过期的同名技能；需要更新时先检查，再按照安装器的保护规则卸载、重装。实际客户端是否发现技能以及是否允许执行，需在对应客户端验证。

## 5. 音色、测试与模式

先查看可用模型与音色；`present` 仅表示路径存在，不证明模型能加载：

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" models
```

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" voices list
```

只使用本人声音或取得声音所有者明确授权的录音。没有已登记音色时，用参考 WAV 和**与录音逐字一致**的文本注册：

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" voices add --id my_voice --name '我的音色' --audio /绝对路径/reference.wav --prompt-text '与参考录音逐字一致的内容' --authorized
```

注册只保存资料，需生成样音并试听。普通参考模式的短句测试：

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" synth --text '你好，这是一段测试。' --voice my_voice --model voxcpm2 --output-dir /绝对路径/产出目录
```

若用户明确希望尝试同一参考录音加对应逐字稿的可选模式：

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" synth --text '你好，这是一段测试。' --voice my_voice --model voxcpm2 --reference-mode reference_plus_prompt --output-dir /绝对路径/产出目录
```

该模式要求非空且与录音相符的逐字稿，系统不能自动验证文字是否匹配。它与情绪标签冲突；用户须明确选择原模式或确认本次丢弃情绪，不能静默降级。两种模式的音色和稳定性均需试听；新模式暂不支持逐块诊断日志。

Skill 未指定模型时显式选择 `voxcpm2`；**直接使用 CLI 时仍由 `config.yaml` 的 `engine` 决定**，仓库配置保留 `cosyvoice3`。直接 CLI 测试 VoxCPM 时请显式传 `--model voxcpm2`。本技能发布包面向 CLI，不包含独立启动的 HTTP 服务。其他引擎的依赖另行准备；CosyVoice 需要其上游源码与推理依赖。

## 6. 注音、长文与安全

VoxCPM2 的 `[重=chong2]` 注音和 `<emotion type="happy">文字</emotion>` 情绪控制是实验能力。先预览转换；用户确认本次风险后，才分别添加 `--accept-control phoneme` 或 `--accept-control emotion`：

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" parse --model voxcpm2 --text '请[重=chong2]新开始。'
```

`[break 500]` 表示额外插入 500 毫秒数字静音，不保证听感上的总停顿恰好 500 毫秒。标签不能嵌套，VoxCPM 当前不支持精确变速和流式；不支持的控制不得静默删除或自动切换模型。具体控制方式见 [SKILL.md](SKILL.md)。

长文可指定独立 `--task-dir /绝对路径/新任务目录`，逐块保存以供恢复；原文、音色、模型、模式或运行时变化时可能拒绝续跑，不要强行复用。当前 `--workers auto` 仍为单路，手动多路不可用。任务目录和音频包含敏感信息，请妥善保管，未经许可不要删除。VoxCPM 的 `--offline` 选项尚未支持；本地模型加载不等于操作系统级网络隔离。
