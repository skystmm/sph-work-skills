#!/usr/bin/env python3
import argparse
import contextlib
import hashlib
import stat as stat_module
import json
import os
import secrets
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'SKILL.md'
MARK = '<!-- local-tts-install sha256='

# Exact pre-rename sources; only this release can be migrated automatically.
LEGACY_SOURCES = {'main': '---\n'
         'name: local-tts\n'
         'description: 使用本地中文 TTS 生成语音、管理音色、设置默认音色和注册授权的自定义声音。\n'
         '---\n'
         '\n'
         '# 本地中文语音生成\n'
         '\n'
         '在 TTS 项目根目录使用 `.venv/bin/python scripts/tts_cli.py`；跨项目时必须使用 TTS 项目自身的 Python 和 CLI '
         '绝对路径（用户级安装会在下方记录该路径，可由 `VOICE_TTS_ROOT` 覆盖）。输入、参考音频和用户指定的输出文件都必须使用绝对路径：CLI 内部会切换到 TTS '
         '项目目录。项目移动或依赖缺失时报告错误，不猜测其他位置。结果是 JSON，进度和错误输出到 stderr。绝不把 mock 结果当成真实语音。\n'
         '\n'
         '## 模型与离线使用\n'
         '\n'
         '首次安装或模型缺失时，先解释模型来源、磁盘开销和网络需求，获得用户同意后才运行安装器。CosyVoice 使用 `bash scripts/download_model.sh '
         'cosyvoice3`；Qwen/VoxCPM 分别使用 `scripts/model_install.py install qwen3tts 新目标绝对路径` 和 `install '
         'voxcpm2 新目标绝对路径`。后两者使用清单固定版本，官方 Hugging Face 请求失败时自动尝试 HF-Mirror；下载器不使用 Hugging Face '
         'Token、不修改全局配置，所有文件经清单大小及 SHA-256 验证后才发布。镜像为第三方，不要向镜像提供私有模型或凭据。已有目标只读检查，不覆盖或自动修复。若用户提供可信本地备份，可用 '
         '`scripts/model_install.py import 模型ID 新目标绝对路径 备份目录绝对路径` 导入；现有目录用 `verify` '
         '只读核对，校验要求目录文件集合与清单一致。若安装到新目录，提示用户自行确认配置中的模型路径，不自动切换。\n'
         '\n'
         '用户要求离线时使用 `synth --offline`，目前该选项只支持 `cosyvoice2` 和 `cosyvoice3`；事先确保依赖和本地模型已准备好。Qwen/VoxCPM '
         '要求已配置的本地模型和独立环境，其运行时禁用 Hugging Face 模型下载，但未验证为全进程网络沙箱，不能使用 `--offline` 声称已通过离线验证。\n'
         '\n'
         '可用 `models` 检查模型 ID、能力及文件是否存在；`qwen3tts` 和 `voxcpm2` 使用独立 `.venv-mlx`。Skill 默认选择 `voxcpm2`；直接使用 '
         'CLI 时仍使用项目配置的默认模型。Qwen 需要参考录音的逐字文本，目前不支持注音、停顿或情绪；VoxCPM '
         '支持额外数字静音，注音和情绪为实验能力，重音、变速和流式不支持。`readiness` 中的 `present` '
         '仅表示文件路径存在，不证明依赖、权重完整或实际合成可用。缺少模型或运行时须告知用户并征得同意后再准备，不能自动下载或切换引擎。\n'
         '\n'
         '仅当用户明确选择时，VoxCPM 可在 `parse` 和 `synth` 使用 `--reference-mode reference_plus_prompt`；默认模式仍为 '
         '`reference`。新模式使用同一份已授权参考音频及其非空逐字稿，确认逐字稿与录音一致；系统不能自动证明对应关系。新模式与情绪标签冲突，须由用户选择原 `reference` '
         '模式，或明确允许本次 `--drop-control emotion`；不得静默丢弃或切换。该模式暂不支持 `--diagnostic-log`，音色是否更接近仍需试听。\n'
         '\n'
         '## 对话流程\n'
         '\n'
         '用户可先提供文字或先选声音。检查 `voices '
         'list`，显式指定的声音仅用于本次；未指定时使用已设置的默认音色。没有默认音色时列出选项并请用户选择，不要猜测，也不要擅自更改默认。音色名称匹配不唯一时询问。用户要求修改默认音色时运行 '
         '`voices default <voice_id>`。\n'
         '\n'
         '用户要求创建自己的音色时，要求本人或已获授权的录音和对应逐字文本，确认授权后运行 `voices add --id ID --name 名称 --audio 路径 --prompt-text '
         '逐字文本 --authorized`。注册只保存资料；提议生成样音让用户试听，不表示音色质量已验证。不要删除或覆盖已有音色。\n'
         '\n'
         '## 生成\n'
         '\n'
         '合成前确认宿主 Agent 的目标产出目录：使用绝对路径传入 `--output-dir`，或由宿主通过 `VOICE_TTS_OUTPUT_DIR` '
         '提供；若用户/宿主没有给出且无法从工作流确定，先询问，不要擅自选择其他项目目录。用户指定完整文件名时改用 `--output`，不要同时传 '
         '`--output-dir`。优先级为显式文件或目录、环境变量、项目默认输出目录。完成后以 JSON 中 `output` 为准。多个 Agent 共用 `VOICE_TTS_ROOT` 下的 '
         '`models/` 与 `voices/`；默认音色也是共享的，声音与模型不随音频复制到产出目录。当前 `--workers auto` '
         '仅安全回退到单路并保存可恢复任务，尚未进行真实模型校准或启用多路；手动指定 2 或 4 路会报错。不可声称已实现自动并发。默认按 40–80 个字符的目标长度切分长文；显式停顿和文末可短于 40 '
         '字。长文或易中断任务使用 `--task-dir 任务目录绝对路径` '
         '保存分块；失败后用相同输入、音色、模型和参数重跑并保留同一任务目录。已完成分块会校验后复用，缺失或损坏的块会重算；身份不匹配不得强行复用。任务目录含文本和音频，未经用户许可不要删除。\n'
         '\n'
         '先检查模型与音色能力。用户未指定模型时，每次 `synth` 显式传入 `--model '
         'voxcpm2`；用户指定模型时仅传入用户指定的模型，不附加默认模型参数。模型不可用时报告原因，不静默切换；用户要求 `--offline` 时说明 VoxCPM '
         '不支持该选项，并由用户决定是否选择支持的其他模型。使用 `parse --text 文字` 或 `parse --file 路径` 检查标注；然后运行 `synth --text 文字 '
         '--voice ID --model voxcpm2` 或 `synth --file 路径 --voice ID --model voxcpm2`，也支持 '
         '`--stdin`、`--speed`、`--output`。长文优先使用 UTF-8 文件或 stdin，原文必须完整，不得改写、截断或将用户文字拼成未经转义的 shell '
         '命令。仅在用户要求时添加注音或停顿。\n'
         '\n'
         '支持 `[重=zhong4]`、`[break 500]`、`<emphasis>词</emphasis>` 以及 `<emotion type="happy">文字</emotion>`；情绪 '
         '`type` 可写简短中英文描述，控制标签不可嵌套。`[break 500]` 是额外插入 500ms 数字静音，不保证总听感停顿；这是项目支持的标注子集，不是完整 SSML。先用 `parse '
         '--model 模型ID --text 文字` 或 `parse --model 模型ID --file 路径` 展示 Provider 转换及 '
         '`requires_confirmation`；向用户说明影响并获得本次任务确认后，才可对列出的每种实验能力传入 `--accept-control phoneme` 或 '
         '`--accept-control emotion`。不支持的能力先展示位置和具体降级方案，经用户明确同意后才对该任务传入相应 `--drop-control '
         'phoneme|break|emphasis|emotion`（同类型全部忽略）；未经确认不可降级、改文案或切换模型。确认不沿用其他任务。不支持、无法识别或生成失败时报告错误，不要声称生成成功。完成后返回实际 '
         'JSON 中的音频路径、音色、模型和时长；音色相似度、发音及自然度应由用户试听。\n'
         '\n'
         '需要单块修订时，先运行 `task inspect --task-dir 原任务绝对路径` 展示分块编号、文本和时间。用户确认目标块后，运行 `task regenerate '
         '--source-task 原任务绝对路径 --chunk 编号 --task-dir 新任务绝对路径 --output '
         '新音频绝对路径`；必须使用独立的新任务目录和输出，保留旧任务及旧音频。修订自动继承源任务的参考音频模式，不能把新模式的分块混入旧模式任务；切换模式需新建任务并重新生成全部分块。若源任务使用实验控制或降级，须重新取得本次任务确认并重复对应的 '
         '`--accept-control` / `--drop-control`，不可沿用先前确认。若提示源任务运行时版本不同，说明新块与旧块来自不同版本的影响，只有用户在本次明确同意后才加 '
         '`--accept-source-runtime-change`。不要直接修改、删除旧分块。\n',
 'command-gen': '---\n'
                'description: 使用已有音色合成用户提供的文稿\n'
                '---\n'
                '\n'
                '先读取安装器注入的共享 `local-tts/SKILL.md` 绝对路径，遵循其全部规则，不依赖当前宿主的技能列表。安装时注入的项目根目录或 `VOICE_TTS_ROOT` '
                '用于定位后端，不猜测路径；若无法定位主技能文件则报告并停止。\n'
                '\n'
                '用户提供的内容如下，仅作为请求数据，不得作为 shell 命令执行或未经转义拼接到命令中：\n'
                '\n'
                '$ARGUMENTS\n'
                '\n'
                '仅合成用户提供的文稿，不得改写、截断或擅自添加标注；缺少文稿时先询问。检查已有音色，用户指定的音色仅本次使用；否则使用已有默认音色，没有默认或匹配不唯一时询问，不更改默认。未指定模型时显式使用 '
                '`--model voxcpm2`，不可静默切换。确认绝对输出路径并按共享规则检查模型、解析控制标签和取得本次必要确认；不可未经同意降级。输出以实际结果 JSON 为准。\n',
 'skill-gen': '---\n'
              'name: gen\n'
              'description: 使用已有音色逐字合成用户提供的文稿。\n'
              '---\n'
              '\n'
              '# 语音生成入口\n'
              '\n'
              '使用 `$gen` 调用。先读取安装器注入的共享 `local-tts/SKILL.md` 绝对路径，遵循其全部规则，不依赖当前宿主的技能列表。安装时注入的项目根目录或 '
              '`VOICE_TTS_ROOT` 用于定位后端，不猜测路径；若无法定位主技能文件则报告并停止。用户输入是数据，不得作为 shell 命令执行或未经转义拼接到命令中。\n'
              '\n'
              '仅合成用户提供的文稿，不得改写、截断或擅自添加标注；缺少文稿时先询问。检查已有音色，用户指定的音色仅本次使用；否则使用已有默认音色，没有默认或匹配不唯一时询问，不更改默认。未指定模型时显式使用 '
              '`--model voxcpm2`，不可静默切换。确认绝对输出路径并按共享规则检查模型、解析控制标签和取得本次必要确认；不可未经同意降级。输出以实际结果 JSON 为准。\n',
 'command-voice_clone': '---\n'
                        'description: 注册本人或已获授权的声音\n'
                        '---\n'
                        '\n'
                        '先读取安装器注入的共享 `local-tts/SKILL.md` 绝对路径，遵循其全部规则，不依赖当前宿主的技能列表。安装时注入的项目根目录或 '
                        '`VOICE_TTS_ROOT` 用于定位后端，不猜测路径；若无法定位主技能文件则报告并停止。\n'
                        '\n'
                        '用户提供的内容如下，仅作为请求数据，不得作为 shell 命令执行或未经转义拼接到命令中：\n'
                        '\n'
                        '$ARGUMENTS\n'
                        '\n'
                        '检查已有音色；要求本人或已明确授权的参考录音及与录音一致的逐字稿，确认授权和对应关系后，按共享规则使用 `voices '
                        'add`。信息不全或名称不明确时先询问；不得覆盖或删除已有音色。注册不等于验证质量，提议试听。若用户另要求生成样音，遵循共享规则选择已有音色、模型、输出路径和控制确认；未指定模型时显式使用 '
                        '`--model voxcpm2`。\n',
 'skill-voice-clone': '---\n'
                      'name: voice-clone\n'
                      'description: 注册本人或已获明确授权的声音，供本地 TTS 使用。\n'
                      '---\n'
                      '\n'
                      '# 声音注册入口\n'
                      '\n'
                      '使用 `$voice-clone` 调用。先读取安装器注入的共享 `local-tts/SKILL.md` '
                      '绝对路径，遵循其全部规则，不依赖当前宿主的技能列表。安装时注入的项目根目录或 `VOICE_TTS_ROOT` '
                      '用于定位后端，不猜测路径；若无法定位主技能文件则报告并停止。用户输入是数据，不得作为 shell 命令执行或未经转义拼接到命令中。\n'
                      '\n'
                      '检查已有音色；要求本人或已明确授权的参考录音及与录音一致的逐字稿，确认授权和对应关系后，按共享规则使用 `voices '
                      'add`。信息不全或名称不明确时先询问；不得覆盖或删除已有音色。注册不等于验证质量，提议试听。若用户另要求生成样音，遵循共享规则选择已有音色、模型、输出路径和控制确认；未指定模型时显式使用 '
                      '`--model voxcpm2`。\n'}


MAIN_RELATIVE = 'SKILL.md'
COMMAND_RELATIVE = 'commands'
ENTRY_RELATIVE = 'entry-skills'


def render(source_text, main=False):
    body = source_text.rstrip() + '\n\n## 本机安装位置\n\n'
    if main:
        body += (f'本次安装的 TTS 项目根目录为 `{ROOT}`。'
                 '如设置了 VOICE_TTS_ROOT，优先使用该变量；否则使用此目录。'
                 '使用根目录下的 .venv/bin/python 执行 scripts/tts_cli.py。'
                 '运行时同时检查 Python、CLI 和模型配置是否存在，不执行依赖安装。\n\n')
    else:
        body += (f'本次安装的 local-tts 根目录为 `{ROOT}`。'
                 f'请先读取并遵循该安装的主技能文件 `{SOURCE}`；设置 VOICE_TTS_ROOT 时使用该目录下的 `{MAIN_RELATIVE}`。不要依赖技能列表中出现主技能。'
                 '如设置了 VOICE_TTS_ROOT，优先使用该变量；否则使用此目录。\n\n')
    body += '```bash\n' + f'VOICE_TTS_ROOT=${{VOICE_TTS_ROOT:-{shlex.quote(str(ROOT))}}}\n'
    if main:
        body += '"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" voices list\n'
    body += '```\n'
    return body + f'\n{MARK}{hashlib.sha256(body.encode("utf-8")).hexdigest()} -->\n'


def content():
    return render(SOURCE.read_text(encoding='utf-8'), main=True)


def entry_content(source):
    return render(source.read_text(encoding='utf-8'))


def marked(text):
    body, separator, suffix = text.rpartition('\n' + MARK)
    if not separator or not suffix.endswith(' -->\n'):
        return False
    digest = suffix[:-len(' -->\n')]
    return len(digest) == 64 and hashlib.sha256(body.encode('utf-8')).hexdigest() == digest


def identity(info):
    return info.st_dev, info.st_ino


@contextlib.contextmanager
def directory_fd(directory, home, created_dirs=None):
    """Resolve below the selected home without following any directory symlinks."""
    relative = directory.relative_to(home)
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current = home
    try:
        for part in relative.parts:
            current = current / part
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if created_dirs is None:
                    raise
                os.mkdir(part, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                created_dirs.append((current, identity(os.fstat(child))))
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_file(parent_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        if not stat_module.S_ISREG(before.st_mode):
            raise OSError('目标不是普通文件')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read()
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise OSError('读取期间目标被修改')
        return data, identity(after)
    finally:
        os.close(fd)


def targets_for(home):
    main = content()
    old_main = render(LEGACY_SOURCES['main'], main=True)
    targets = []
    for name, relative in (('agents', '.agents/skills/local-tts/SKILL.md'),
                           ('workbuddy', '.codebuddy/skills/local-tts/SKILL.md')):
        targets.append(dict(target=name, path=home / relative, desired=main,
                            legacy=old_main, skill_directory=True))
    for name, source_dir, destination_dir, extension in (
            ('voice_clone', COMMAND_RELATIVE, '.claude/commands', '.md'),
            ('voice_gen', COMMAND_RELATIVE, '.claude/commands', '.md'),
            ('voice-clone', ENTRY_RELATIVE, '.agents/skills', '/SKILL.md'),
            ('voice-gen', ENTRY_RELATIVE, '.agents/skills', '/SKILL.md')):
        kind = 'command' if extension == '.md' else 'skill'
        old_name = 'gen' if name in ('voice_gen', 'voice-gen') else name
        old = render(LEGACY_SOURCES[f'{kind}-{old_name}'])
        targets.append(dict(target=f'{kind}-{name}', path=home / f'{destination_dir}/{name}{extension}',
                            desired=entry_content(ROOT / f'{source_dir}/{name}{extension}'),
                            legacy=old if old_name == name else None, skill_directory=kind == 'skill'))
    legacy = []
    for kind, relative in (('command', '.claude/commands/gen.md'), ('skill', '.agents/skills/gen/SKILL.md')):
        legacy.append(dict(target=f'{kind}-gen', path=home / relative, desired=None,
                           legacy=render(LEGACY_SOURCES[f'{kind}-gen']), skill_directory=kind == 'skill'))
    return targets, legacy


def inspect_target(item, home):
    path = item['path']
    try:
        with directory_fd(path.parent, home) as fd:
            parent_identity = identity(os.fstat(fd))
            # Extra files (including hidden files or links) make a skill directory foreign.
            if item['skill_directory'] and os.listdir(fd) != ['SKILL.md']:
                return dict(item, state='conflict', snapshot=None)
            try:
                data, inode = read_file(fd, path.name)
            except FileNotFoundError:
                return dict(item, state='absent', snapshot=None)
        text = data.decode('utf-8')
        state = 'conflict'
        if marked(text):
            if item['desired'] is not None and text == item['desired']:
                state = 'installed'
            elif item['legacy'] is not None and text == item['legacy']:
                state = 'migratable' if item['desired'] is not None else 'legacy'
            else:
                # A self-supplied digest is NOT provenance, even for uninstall.
                state = 'outdated'
        return dict(item, state=state, snapshot=(data, inode), parent_identity=parent_identity)
    except FileNotFoundError:
        return dict(item, state='absent', snapshot=None)
    except (OSError, UnicodeError, ValueError):
        return dict(item, state='conflict', snapshot=None)


def public_report(items):
    return [dict(target=item['target'], directory=str(item['path'].parent),
                 path=str(item['path']), state=item['state']) for item in items]


def verify_target(item, home):
    current = inspect_target(item, home)
    if current['snapshot'] != item['snapshot'] or (
            item['snapshot'] is not None and current.get('parent_identity') != item.get('parent_identity')) or (
            item['snapshot'] is None and current['state'] != 'absent'):
        raise OSError(f'预检后目标发生变化: {item["path"]}')


def remove_directory(path, home, expected_identity=None):
    with directory_fd(path.parent, home) as fd:
        info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        if expected_identity is not None and identity(info) != expected_identity:
            raise OSError(f'目录已被替换: {path}')
        os.rmdir(path.name, dir_fd=fd)


class Transaction:
    """Exclusive publication, recoverable originals, and no-overwrite rollback.

    This is not a cross-file atomic transaction. Every detached original is retained
    before publishing a replacement. Concurrent edits cause failure, never force.
    """
    def __init__(self, home):
        self.home = home
        self.created_dirs = []
        self.published = []
        self.detached = []
        self.backup = None
        self.backup_identity = None
        self.fd = None

    def stage(self, changes):
        # Validate home without resolving it through a symlink.
        with directory_fd(self.home, self.home) as home_fd:
            name = '.local-tts-backup-' + secrets.token_hex(8)
            os.mkdir(name, mode=0o700, dir_fd=home_fd)
            self.backup = self.home / name
            self.fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home_fd)
            self.backup_identity = identity(os.fstat(self.fd))
        manifest = dict(root=str(ROOT),
                        note='恢复目录仅保留已移出的 original-*；失败时清单可能还包含尚未移动的目标。',
                        originals=[dict(path=str(item['path']), file=f'original-{i}')
                                   for i, item in enumerate(changes) if item['snapshot'] is not None],
                        desired=[dict(path=str(item['path']), file=f'desired-{i}')
                                 for i, item in enumerate(changes) if item['desired'] is not None])
        self.write_stage('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        for i, item in enumerate(changes):
            if item['desired'] is not None:
                self.write_stage(f'desired-{i}', item['desired'].encode('utf-8'))

    def write_stage(self, name, data):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def journal(self, operation, path, name):
        # Write intent first so even interruption between rename and verification is recoverable.
        fd = os.open('operations.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(operation=operation, path=str(path), file=name), ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def detach(self, item, i):
        verify_target(item, self.home)
        path = item['path']
        name = f'original-{i}'
        self.journal('detach', path, name)
        with directory_fd(path.parent, self.home) as fd:
            os.rename(path.name, name, src_dir_fd=fd, dst_dir_fd=self.fd)
        # Journal immediately: even a race at rename leaves the moved file recoverable.
        self.detached.append((item, name))
        if read_file(self.fd, name) != item['snapshot']:
            raise OSError(f'移出期间目标发生变化，原文件已保留: {path}')

    def publish(self, item, i):
        path = item['path']
        staged = f'desired-{i}'
        self.journal('publish', path, staged)
        if item['skill_directory'] and item['snapshot'] is None:
            with directory_fd(path.parent.parent, self.home, self.created_dirs) as fd:
                # Even an empty concurrently created skill directory is foreign.
                os.mkdir(path.parent.name, dir_fd=fd)
                info = os.stat(path.parent.name, dir_fd=fd, follow_symlinks=False)
                self.created_dirs.append((path.parent, identity(info)))
        with directory_fd(path.parent, self.home, self.created_dirs) as fd:
            inode = identity(os.stat(staged, dir_fd=self.fd, follow_symlinks=False))
            os.link(staged, path.name, src_dir_fd=self.fd, dst_dir_fd=fd, follow_symlinks=False)
            self.published.append((item, inode))

    def apply(self, changes):
        for i, item in enumerate(changes):
            if item['snapshot'] is not None:
                self.detach(item, i)
            else:
                verify_target(item, self.home)
            if item['desired'] is not None:
                self.publish(item, i)
        for item in changes:
            if item['desired'] is None and item['skill_directory']:
                remove_directory(item['path'].parent, self.home, item.get('parent_identity'))

    def restore(self, name, path):
        with directory_fd(path.parent, self.home, self.created_dirs) as fd:
            # EEXIST is a conflict, not permission to overwrite a concurrent edit.
            os.link(name, path.name, src_dir_fd=self.fd, dst_dir_fd=fd, follow_symlinks=False)

    def rollback(self):
        failures = []
        for i, (item, inode) in enumerate(reversed(self.published)):
            path = item['path']
            quarantine = f'rollback-{i}'
            try:
                self.journal('rollback-withdraw', path, quarantine)
                with directory_fd(path.parent, self.home) as fd:
                    try:
                        os.rename(path.name, quarantine, src_dir_fd=fd, dst_dir_fd=self.fd)
                    except FileNotFoundError:
                        continue
                try:
                    unchanged = read_file(self.fd, quarantine) == (item['desired'].encode('utf-8'), inode)
                except OSError:
                    unchanged = False
                if not unchanged:
                    self.restore(quarantine, path)
                    raise OSError('目标已被外部修改，保留修改版及原始备份')
                # Retain quarantine until rollback finishes; no foreign file is unlinked.
            except OSError as exc:
                failures.append(f'{path}: {exc}')
        for item, name in reversed(self.detached):
            try:
                self.restore(name, item['path'])
            except OSError as exc:
                failures.append(f'{item["path"]}: {exc}；原文件保存在 {self.backup / name}')
        for path, inode in reversed(self.created_dirs):
            try:
                remove_directory(path, self.home, inode)
            except FileNotFoundError:
                pass
            except OSError as exc:
                # Restored originals or concurrent files keep their directories.
                if not any(item['path'].is_relative_to(path) for item, _ in self.detached):
                    failures.append(f'{path}: {exc}')
        return failures

    def finish(self, keep_originals):
        if self.fd is None:
            return
        try:
            for name in os.listdir(self.fd):
                if keep_originals and (name.startswith('original-') or name in ('manifest.json', 'operations.jsonl')):
                    continue
                os.unlink(name, dir_fd=self.fd)
        finally:
            os.close(self.fd)
            self.fd = None
        if keep_originals:
            print(f'原文件备份（请保留，需要时手动恢复）: {self.backup}', file=sys.stderr)
        else:
            remove_directory(self.backup, self.home, self.backup_identity)


def run(action, home):
    targets, legacy = targets_for(home)
    current = [inspect_target(item, home) for item in targets]
    old = [inspect_target(item, home) for item in legacy]
    print(json.dumps(dict(source=str(SOURCE), root=str(ROOT), action=action,
                          targets=public_report(current), legacy_targets=public_report(old)), ensure_ascii=False), flush=True)
    if action == 'check':
        return 0 if all(x['state'] == 'installed' for x in current) and all(x['state'] == 'absent' for x in old) else 1
    if any(item['state'] in ('conflict', 'outdated') for item in current + old):
        print('存在同名、未知版本或已修改的技能，未执行操作；请先检查目标目录。', file=sys.stderr)
        return 1
    if action == 'install':
        changes = [item for item in current if item['state'] != 'installed']
        changes += [dict(item, desired=None) for item in old if item['state'] == 'legacy']
    else:
        changes = [dict(item, desired=None) for item in current + old if item['state'] != 'absent']
    if not changes:
        return 0
    transaction = Transaction(home)
    try:
        transaction.stage(changes)
        # Revalidate the entire preflight after staging, not just files being changed.
        for item in current + old:
            verify_target(item, home)
        transaction.apply(changes)
        for item in targets:
            if action == 'install' and inspect_target(item, home)['state'] != 'installed':
                raise OSError(f'发布后目标发生变化: {item["path"]}')
        for item in legacy if action == 'install' else targets + legacy:
            if inspect_target(item, home)['state'] != 'absent':
                raise OSError(f'移除后目标发生变化: {item["path"]}')
    except Exception as exc:
        print(f'操作失败: {exc}', file=sys.stderr)
        failures = transaction.rollback()
        for failure in failures:
            print(f'清理失败（未覆盖冲突文件）: {failure}', file=sys.stderr)
        try:
            if failures or transaction.detached or transaction.published:
                if transaction.fd is not None:
                    os.close(transaction.fd)
                    transaction.fd = None
                status = '回滚存在冲突' if failures else '回滚完成'
                print(f'{status}；完整恢复目录已保留: {transaction.backup}', file=sys.stderr)
            else:
                transaction.finish(keep_originals=False)
        except OSError as cleanup_exc:
            print(f'清理失败；保留恢复目录 {transaction.backup}: {cleanup_exc}', file=sys.stderr)
        return 1
    try:
        transaction.finish(keep_originals=bool(transaction.detached))
    except OSError as exc:
        print(f'操作已完成，但清理失败；保留恢复目录 {transaction.backup}: {exc}', file=sys.stderr)
        return 1
    return 0


def main():
    p = argparse.ArgumentParser(description='安装本地 TTS Skill 到用户级 Agent 技能目录')
    p.add_argument('action', choices=['check', 'install', 'uninstall'])
    p.add_argument('--home', type=Path, default=Path.home(), help='用户目录（测试或自定义环境）')
    args = p.parse_args()
    home = Path(os.path.abspath(os.path.expanduser(args.home)))
    try:
        return run(args.action, home)
    except (OSError, UnicodeError) as exc:
        print(f'安装器失败（未覆盖文件）: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
