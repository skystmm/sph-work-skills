---
name: local-tts
description: 使用本地中文 TTS 生成语音、管理音色、设置默认音色和注册授权的自定义声音。
---

# 本地中文语音生成

在 TTS 项目根目录使用 `.venv/bin/python scripts/tts_cli.py`；跨项目时必须使用 TTS 项目自身的 Python 和 CLI 绝对路径（用户级安装会在下方记录该路径，可由 `VOICE_TTS_ROOT` 覆盖）。输入、参考音频和用户指定的输出文件都必须使用绝对路径：CLI 内部会切换到 TTS 项目目录。项目移动或依赖缺失时报告错误，不猜测其他位置。结果是 JSON，进度和错误输出到 stderr。绝不把 mock 结果当成真实语音。

## 模型与离线使用

首次安装或模型缺失时，先解释模型来源、磁盘开销和网络需求，获得用户同意后才运行安装器。CosyVoice 使用 `bash scripts/download_model.sh cosyvoice3`；Qwen/VoxCPM 分别使用 `scripts/model_install.py install qwen3tts 新目标绝对路径` 和 `install voxcpm2 新目标绝对路径`。后两者使用清单固定版本，官方 Hugging Face 请求失败时自动尝试 HF-Mirror；下载器不使用 Hugging Face Token、不修改全局配置，所有文件经清单大小及 SHA-256 验证后才发布。镜像为第三方，不要向镜像提供私有模型或凭据。已有目标只读检查，不覆盖或自动修复。若用户提供可信本地备份，可用 `scripts/model_install.py import 模型ID 新目标绝对路径 备份目录绝对路径` 导入；现有目录用 `verify` 只读核对，校验要求目录文件集合与清单一致。若安装到新目录，提示用户自行确认配置中的模型路径，不自动切换。

用户要求离线时使用 `synth --offline`，目前该选项只支持 `cosyvoice2` 和 `cosyvoice3`；事先确保依赖和本地模型已准备好。Qwen/VoxCPM 要求已配置的本地模型和独立环境，其运行时禁用 Hugging Face 模型下载，但未验证为全进程网络沙箱，不能使用 `--offline` 声称已通过离线验证。

可用 `models` 检查模型 ID、能力及文件是否存在；`qwen3tts` 和 `voxcpm2` 使用独立 `.venv-mlx`。Skill 默认选择 `voxcpm2`；直接使用 CLI 或 API 时仍使用项目配置的默认模型。Qwen 需要参考录音的逐字文本，目前不支持注音、停顿或情绪；VoxCPM 支持额外数字静音，注音和情绪为实验能力，重音、变速和流式不支持。`readiness` 中的 `present` 仅表示文件路径存在，不证明依赖、权重完整或实际合成可用。缺少模型或运行时须告知用户并征得同意后再准备，不能自动下载或切换引擎。

Skill 为 VoxCPM2 默认选择 `reference_plus_prompt`：所选模型为 `voxcpm2` 且用户未指定参考模式时，在 `parse --model voxcpm2` 和 `synth --model voxcpm2` 都显式传入 `--reference-mode reference_plus_prompt`。用户明确指定 `reference` 或 `reference_plus_prompt` 时，两步均遵循用户选择。其他引擎不传 `--reference-mode`，也不将该模式强加给其他引擎；用户提出不兼容模式时先说明并询问。直接调用 CLI/API 的参考模式默认仍为 `reference`，不修改配置或后端默认值。

`reference_plus_prompt` 使用同一份已授权参考音频及其非空逐字稿。先核实所选音色的 `prompt_text` 非空，并向用户确认逐字稿与录音一致；系统不能自动证明对应关系。缺少逐字稿或对应关系不确定时，先向用户索取或核对，暂停生成；不得猜写逐字稿、静默改用其他音色或自动回退 `reference`。新模式与情绪标签冲突，必须由用户明确选择切换到 `reference`，或明确允许本次 `--drop-control emotion`（同类型全部忽略）；不得静默丢弃或切换。新模式不支持 `--diagnostic-log`；若用户要求诊断日志，先说明限制并询问，不得自动改模式或忽略日志要求。音色是否更接近仍需试听。不删除或覆盖用户的录音、音色、逐字稿、旧任务及音频。

## 对话流程

用户可先提供文字或先选声音。检查 `voices list`，显式指定的声音仅用于本次；未指定时使用已设置的默认音色。没有默认音色时列出选项并请用户选择，不要猜测，也不要擅自更改默认。音色名称匹配不唯一时询问。用户要求修改默认音色时运行 `voices default <voice_id>`。

用户要求创建自己的音色时，要求本人或已获授权的录音和对应逐字文本，确认授权后运行 `voices add --id ID --name 名称 --audio 路径 --prompt-text 逐字文本 --authorized`。注册只保存资料；提议生成样音让用户试听，不表示音色质量已验证。不要删除或覆盖已有音色。

## 生成

合成前确认宿主 Agent 的目标产出目录：使用绝对路径传入 `--output-dir`，或由宿主通过 `VOICE_TTS_OUTPUT_DIR` 提供；若用户/宿主没有给出且无法从工作流确定，先询问，不要擅自选择其他项目目录。用户指定完整文件名时改用 `--output`，不要同时传 `--output-dir`。优先级为显式文件或目录、环境变量、项目默认输出目录。完成后以 JSON 中 `output` 为准。多个 Agent 共用 `VOICE_TTS_ROOT` 下的 `models/` 与 `voices/`；默认音色也是共享的，声音与模型不随音频复制到产出目录。当前 `--workers auto` 仅安全回退到单路并保存可恢复任务，尚未进行真实模型校准或启用多路；手动指定 2 或 4 路会报错。不可声称已实现自动并发。默认按 40–80 个字符的目标长度切分长文；显式停顿和文末可短于 40 字。长文或易中断任务使用 `--task-dir 任务目录绝对路径` 保存分块；失败后用相同输入、音色、模型和参数重跑并保留同一任务目录。已完成分块会校验后复用，缺失或损坏的块会重算；身份不匹配不得强行复用。任务目录含文本和音频，未经用户许可不要删除。

先检查模型与音色能力。用户未指定模型时，每次 `synth` 显式传入 `--model voxcpm2`；用户指定模型时仅传入用户指定的模型，不附加默认模型参数。模型不可用时报告原因，不静默切换；用户要求 `--offline` 时说明 VoxCPM 不支持该选项，并由用户决定是否选择支持的其他模型。使用 `parse --model voxcpm2 --reference-mode reference_plus_prompt --text 文字` 或 `parse --model voxcpm2 --reference-mode reference_plus_prompt --file 路径` 检查默认 VoxCPM2 模式的标注；然后运行 `synth --text 文字 --voice ID --model voxcpm2 --reference-mode reference_plus_prompt` 或 `synth --file 路径 --voice ID --model voxcpm2 --reference-mode reference_plus_prompt`。这些是 Skill 默认模式的示意，用户明确选择其他模式时同步替换两步的模式值；选择其他模型时使用该模型并省略 `--reference-mode`。也支持 `--stdin`、`--speed`、`--output`。长文优先使用 UTF-8 文件或 stdin，原文必须完整，不得改写、截断或将用户文字拼成未经转义的 shell 命令。仅在用户要求时添加注音或停顿。

支持 `[重=zhong4]`、`[break 500]`、`<emphasis>词</emphasis>` 以及 `<emotion type="happy">文字</emotion>`；情绪 `type` 可写简短中英文描述，控制标签不可嵌套。`[break 500]` 是额外插入 500ms 数字静音，不保证总听感停顿；这是项目支持的标注子集，不是完整 SSML。按上文选定的模型及参考模式运行 `parse`，展示 Provider 转换及 `requires_confirmation`；VoxCPM2 的预览与合成必须传入相同的显式 `--reference-mode`，其他引擎两步都不传该参数；向用户说明影响并获得本次任务确认后，才可对列出的每种实验能力传入 `--accept-control phoneme` 或 `--accept-control emotion`。不支持的能力先展示位置和具体降级方案，经用户明确同意后才对该任务传入相应 `--drop-control phoneme|break|emphasis|emotion`（同类型全部忽略）；未经确认不可降级、改文案或切换模型。确认不沿用其他任务。不支持、无法识别或生成失败时报告错误，不要声称生成成功。完成后返回实际 JSON 中的音频路径、音色、模型和时长；音色相似度、发音及自然度应由用户试听。

需要单块修订时，先运行 `task inspect --task-dir 原任务绝对路径` 展示分块编号、文本和时间。用户确认目标块后，运行 `task regenerate --source-task 原任务绝对路径 --chunk 编号 --task-dir 新任务绝对路径 --output 新音频绝对路径`；必须使用独立的新任务目录和输出，保留旧任务及旧音频。修订自动继承源任务的参考音频模式，不能把新模式的分块混入旧模式任务；切换模式需新建任务并重新生成全部分块。若源任务使用实验控制或降级，须重新取得本次任务确认并重复对应的 `--accept-control` / `--drop-control`，不可沿用先前确认。若提示源任务运行时版本不同，说明新块与旧块来自不同版本的影响，只有用户在本次明确同意后才加 `--accept-source-runtime-change`。不要直接修改、删除旧分块。
