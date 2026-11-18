# local-tts

面向 Agent 的本地中文语音生成技能和后端。当前主要介绍在 **Apple Silicon macOS 本地使用 VoxCPM2**；模型、音色、录音、音频和虚拟环境不包含在仓库中。

## 能力概览

- VoxCPM2 声音克隆：使用本人或已获授权的参考录音；可选“参考音频＋对应逐字稿”模式。
- 中文注音与情绪为实验能力，须在每次任务预览并确认；数字静音停顿按指定时间额外插入。
- 长文单路分块、任务恢复及单块修订；不自动切换模型或覆盖已有输出。
- 保留 CosyVoice、Qwen3-TTS 和 macOS 系统语音适配接口；各自依赖、功能及硬件要求不同，详见技能说明。

Skill 在用户未指定模型时选择 `voxcpm2`；直接调用 CLI 则使用 `config.yaml` 的模型配置，默认配置保留 `cosyvoice3`。要直接用 CLI 测试 VoxCPM2，请显式传入 `--model voxcpm2`。本技能发布包面向 CLI，不包含独立启动的 HTTP 服务。

## 安装与模型下载

请先阅读 [VoxCPM2 安装指南](install-guide.md)，按步骤准备主 Python 环境、独立 MLX 环境、授权音色和模型。VoxCPM2 模型安装器先尝试 Hugging Face，失败时尝试第三方 HF-Mirror，按固定清单校验大小和 SHA-256；安装前请确认网络、磁盘空间和模型许可。

也可以将下面这句话发送给支持本地技能和命令的 Agent：

> 请按照 `https://github.com/skystmm/sph-work-skills` 仓库的 `skills/local-tts/install-guide.md` 安装 `local-tts` 技能，并在下载 VoxCPM2 模型前先征得我的同意。

参考音频＋逐字稿模式通过 `--reference-mode reference_plus_prompt` 显式启用；逐字稿须与所选参考录音一致，该模式与情绪控制冲突。实际音色、注音和稳定性须试听验证。

技能的选择、确认和安全规则见 [SKILL.md](SKILL.md)。音频、音色和任务文案可能包含个人敏感信息，不应提交到仓库。
