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

脚本下载第三方模型文件到本地 `models/`；下载需要网络和磁盘空间。下载前检查上游来源、许可和模型文件完整性。模型和录音不应提交到 GitHub。

## 3. 安装 Agent 技能入口

将本目录的 `SKILL.md` 及本 `install-guide.md` 复制到目标 Agent 的技能目录，例如用户级共享目录中的 `local-tts/`，或者在客户端中通过支持的方式添加。实际发现目录和权限因产品与版本而异；Codex 可使用 `~/.agents/skills/local-tts/`，其他客户端请核对其文档。安装入口后，为该 Agent 进程设置 `VOICE_TTS_ROOT` 指向第 1 步的后端根目录。不要覆盖同名技能或自动更改 Agent 权限。

普通豆包 App 尚未确认支持加载本机 Skill 或执行本地 CLI，不能保证直接接入。安装了入口不代表客户端已发现技能、允许执行命令或可访问音频输出。

## 4. 验证与注册音色

```bash
"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" voices list
```

首次生成须选用已有音色，或通过 `voices add --id ID --name 名称 --audio 录音绝对路径 --prompt-text 逐字文本 --authorized` 注册本人或已获授权的声音；创建仅表示资料已保存，声音效果须试听。使用 `voices default 音色ID` 可设置默认，`synth --file 文章绝对路径 --voice 音色ID` 可生成长文音频。

配置安装入口后，可向支持本机技能和命令的 Agent 发送：

> 按照 `https://github.com/skystmm/sph-work-skills` 仓库的 `skills/local-tts/install-guide.md` 安装 `local-tts` skill。

请 Agent 先说明拟执行的依赖安装和模型下载，并在执行前获得你的同意。
