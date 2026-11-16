# local-tts 安装指南

本技能包含 Agent 指令和 TTS 后端源码，**不包含模型权重、参考录音、用户音色、生成音频、虚拟环境或上游 CosyVoice 源码**。需要本机有足够磁盘、可用的 Python 环境，并按上游说明准备 CosyVoice 依赖。以下为安装路径，干净环境的一键安装尚未经验证；不同系统和硬件的依赖可能不同。

## 1. 获取技能

克隆本仓库，并将 `skills/local-tts` 作为后端根目录。为避免重复复制模型，建议保留一份固定位置的技能目录，将其绝对路径设为 `VOICE_TTS_ROOT`。Agent 必须能读取该目录并获准执行本地命令；远程或容器中的 Agent 不会自动访问你电脑上的文件。

## 2. 准备 CosyVoice 和环境

按 [CosyVoice 上游项目](https://github.com/FunAudioLLM/CosyVoice)说明，下载其源码到 `$VOICE_TTS_ROOT/third_party/CosyVoice` 并初始化 Matcha-TTS 子模块。当前开发环境使用 CosyVoice 提交 `074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc`，Matcha-TTS 提交 `bd4d90d93214b37f7a159cf205ae85762c2c10aa`；请同时检查上游许可和模型使用条款。

在 `$VOICE_TTS_ROOT/.venv` 创建 Python 环境，安装本目录 `requirements.txt` 以及上游 CosyVoice 推理依赖。**本目录 requirements 不是完整的 CosyVoice 依赖锁文件**；尤其 PyTorch、torchaudio 和运行设备相关依赖需要按上游及目标平台选择。当前开发环境的 macOS/MPS 合成曾成功，其他系统和全新安装未验证。

经用户同意后，在技能根目录下载模型：

```bash
bash "$VOICE_TTS_ROOT/scripts/download_model.sh" cosyvoice3
```

脚本从 ModelScope 拉取 `FunAudioLLM/Fun-CosyVoice3-0.5B-2512` 到 `$VOICE_TTS_ROOT/models/CosyVoice3/`；需准备网络和足够磁盘空间。开发机器上的 CosyVoice3 目录约占 5.1 GB，实际占用和临时空间可能因版本与文件系统而异。若需要 CosyVoice2，可运行：

```bash
bash "$VOICE_TTS_ROOT/scripts/download_model.sh" cosyvoice2
```

CosyVoice2 的模型 ID 为 `iic/CosyVoice2-0.5B`，保存在 `models/CosyVoice2/`；不传参数时脚本默认下载 CosyVoice2，但本项目的拼音注音只支持 CosyVoice3。模型文件还需要上游源码及对应推理依赖，下载权重本身不代表模型已能运行。

下载后检查每个文件和终端是否出现 `SOME_FAILED`；脚本会跳过非空文件，**不检查完整性或哈希，不能仅凭退出码判断成功**。下载中断时请对照模型发布方的文件清单及校验信息检查，勿将不完整文件当作可用权重。恢复可信备份时可先恢复原模型文件并核对配置，不必重复下载。

下载前检查上游来源、许可和模型文件完整性。模型、音色和生成音频不应提交到 GitHub。

## 3. 安装 Agent 技能入口

本目录提供用户级入口安装器，会使用当前后端根目录生成技能说明，并写入 `~/.agents/skills/local-tts` 和 `~/.codebuddy/skills/local-tts`；不会复制模型、音色或后端。运行前应检查目标路径和冲突，已有同名或用户修改过的技能不会被覆盖：

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/install_agent_skill.py" check
```

若显示 `absent` 是尚未安装，可在确认目标目录后运行：

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/install_agent_skill.py" install
```

也可以按目标 Agent 官方文档手动添加 `SKILL.md` 入口，并确保进程能读取后端根目录的 `install-guide.md`。不同产品的发现目录、环境变量传递和权限可能不同；安装入口不代表客户端已发现或允许执行。用户级默认音色存于共享后端，多个 Agent 会共用。

普通豆包 App 尚未确认支持加载本机 Skill 或执行本地 CLI，不能保证直接接入。安装了入口不代表客户端已发现技能、允许执行命令或可访问音频输出。

## 4. 生成位置、断点与备份

宿主 Agent 指定绝对 `--output-dir` 或 `VOICE_TTS_OUTPUT_DIR` 即可将 WAV 放入自己的产出目录；若要指定完整文件名可用 `--output`，两种参数不能同时使用。CLI 的 JSON 返回实际路径。未指定输出目录时使用后端根目录的 `output/`。

长文可传 `--task-dir /绝对路径/任务目录`，已完成分块会保存供中断后复用；任务目录包含原文和音频，不要未经许可删除。默认按 40–80 个字符目标切分；手动停顿和末尾可能短于 40 字。当前 `--workers auto` 仍保守使用单路、不会执行真实自动校准；手动 2/4 路不可用。`benchmark_tts_parallel.py` 是独立的实验脚本，不属于生产自动模式；它会启动多个模型副本，可能造成严重内存压力，不建议在资源不足的机器运行。

所有 Agent 共用后端的 `models/` 和 `voices/`，不会将其复制到音频产出目录。备份前停止合成与音色写入，备份模型、完整音色库（含隐藏设置文件）及 `config.yaml`；参考录音含个人敏感信息，请使用受控或加密介质。恢复到新机器时 Python 环境和上游依赖可能需重建；任务身份含路径和环境，跨机器可能无法续跑，保留原任务数据。

## 5. 验证与注册音色

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" voices list
```

首次生成须选用已有音色，或通过 `voices add --id ID --name 名称 --audio 录音绝对路径 --prompt-text 逐字文本 --authorized` 注册本人或已获授权的声音；创建仅表示资料已保存，声音效果须试听。使用 `voices default 音色ID` 可设置默认，`synth --file 文章绝对路径 --voice 音色ID` 可生成长文音频。

配置安装入口后，可向支持本机技能和命令的 Agent 发送：

> 按照 `https://github.com/skystmm/sph-work-skills` 仓库的 `skills/local-tts/install-guide.md` 安装 `local-tts` skill。

请 Agent 先说明拟执行的依赖安装和模型下载，并在执行前获得你的同意。
