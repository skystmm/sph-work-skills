# local-tts

面向 Agent 的本地中文语音生成技能和后端。当前主要介绍在 **Apple Silicon macOS 本地使用 VoxCPM2**；模型、音色、录音、音频和虚拟环境不包含在仓库中。

## 能力概览

- VoxCPM2 声音克隆：使用本人或已获授权的参考录音；Skill 默认使用“参考音频＋对应逐字稿”模式。
- 中文注音与情绪为实验能力，须在每次任务预览并确认；数字静音停顿按指定时间额外插入。
- 长文单路分块、任务恢复及单块修订；不自动切换模型或覆盖已有输出。
- 保留 CosyVoice、Qwen3-TTS 和 macOS 系统语音适配接口；各自依赖、功能及硬件要求不同，详见技能说明。

Skill 在用户未指定模型时选择 `voxcpm2`；所选模型为 VoxCPM2 时默认使用 `reference_plus_prompt`，在 `parse --model voxcpm2` 和 `synth` 都显式传入 `--reference-mode reference_plus_prompt`。用户明确选择参考模式时两步一致遵循用户选择，其他引擎不传此参数。直接调用 CLI/API 的参考模式默认仍为 `reference`；模型仍由 `config.yaml` 决定，默认配置保留 `cosyvoice3`，不修改后端默认值。要直接用 CLI 测试 VoxCPM2，请显式传入 `--model voxcpm2`。本技能发布包面向 CLI，不包含独立启动的 HTTP 服务。

## 安装与模型下载

请先阅读 [VoxCPM2 安装指南](install-guide.md)，按步骤准备主 Python 环境、独立 MLX 环境、授权音色和模型。VoxCPM2 模型安装器先尝试 Hugging Face，失败时尝试第三方 HF-Mirror，按固定清单校验大小和 SHA-256；安装前请确认网络、磁盘空间和模型许可。

也可以将下面这句话发送给支持本地技能和命令的 Agent：

> 请按照 `https://github.com/skystmm/sph-work-skills` 仓库的 `skills/local-tts/install-guide.md` 安装 `local-tts` 技能，并在下载 VoxCPM2 模型前先征得我的同意。

参考音频＋逐字稿模式由 Skill 显式传参；直接 CLI 请求要使用该模式也须加 `--reference-mode reference_plus_prompt`。逐字稿须非空且经用户确认与所选参考录音一致；缺失或不确定时先询问并暂停生成，不猜写、不自动回退。遇情绪标签时，必须由用户明确选择 `reference` 模式或本次 `--drop-control emotion`，不得静默降级；新模式不支持 `--diagnostic-log`。实际音色、注音和稳定性须试听验证，不删除或覆盖用户录音、音色、旧任务或音频。

## 声音注册与生成示例

将占位路径替换为真实绝对路径。只有确认录音属于本人或已获明确授权，且逐字稿与录音一致后，才运行注册；已有音色不会被覆盖。

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" voices add --id my_voice --name '我的音色' --audio /绝对路径/reference.wav --prompt-text '这里是录音中逐字说出的内容' --authorized
```

生成短句（以下两个示例直接调用 CLI，保持 `reference` 默认）：

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" synth --text '你好，这是一段测试。' --voice my_voice --model voxcpm2 --output-dir /绝对路径/产出目录
```

生成文稿文件：

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" synth --file /绝对路径/文稿.txt --voice my_voice --model voxcpm2 --output-dir /绝对路径/产出目录
```

按 Skill 默认使用参考音频＋逐字稿时，在确认逐字稿后用相同模式预览并合成：

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" parse --model voxcpm2 --reference-mode reference_plus_prompt --text '你好，这是一段测试。'
```

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" synth --text '你好，这是一段测试。' --voice my_voice --model voxcpm2 --reference-mode reference_plus_prompt --output-dir /绝对路径/产出目录
```

对应入口模板在支持并安装这些入口的宿主中使用：Claude Code 输入 `/voice_clone` 或 `/voice_gen`，Codex 输入 `$voice-clone` 或 `$voice-gen`，后接请求内容；这些是宿主对话输入，不是 shell 命令。未指定音色时使用已有默认音色；没有默认音色会先询问。模板需由安装器注入共享技能路径，入口安装和实际客户端发现情况以安装器及宿主验证为准。

技能的选择、确认和安全规则见 [SKILL.md](SKILL.md)。音频、音色和任务文案可能包含个人敏感信息，不应提交到仓库。
