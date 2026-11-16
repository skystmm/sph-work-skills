#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'SKILL.md'
MARK = '<!-- local-tts-install sha256='


def content():
    body = SOURCE.read_text(encoding='utf-8').rstrip() + '\n\n'
    body += ('## 本机安装位置\n\n'
             f'本次安装的 TTS 项目根目录为 `{ROOT}`。'
             '如设置了 VOICE_TTS_ROOT，优先使用该变量；否则使用此目录。'
             '使用根目录下的 .venv/bin/python 执行 scripts/tts_cli.py。'
             '运行时同时检查 Python、CLI 和模型配置是否存在，不执行依赖安装。\n\n'
             '```bash\n'
             f'VOICE_TTS_ROOT=${{VOICE_TTS_ROOT:-{shlex.quote(str(ROOT))}}}\n'
             '"$VOICE_TTS_ROOT/.venv/bin/python" "$VOICE_TTS_ROOT/scripts/tts_cli.py" voices list\n'
             '```\n')
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    return body + f'\n{MARK}{digest} -->\n'


def owned(path):
    if path.is_symlink() or not path.is_file():
        return False
    text = path.read_text(encoding='utf-8')
    body, separator, suffix = text.rpartition('\n' + MARK)
    if not separator or not suffix.endswith(' -->\n'):
        return False
    digest = suffix[:-len(' -->\n')]
    return len(digest) == 64 and hashlib.sha256(body.encode('utf-8')).hexdigest() == digest


def main():
    p = argparse.ArgumentParser(description='安装本地 TTS Skill 到用户级 Agent 技能目录')
    p.add_argument('action', choices=['check', 'install', 'uninstall'])
    p.add_argument('--home', type=Path, default=Path.home(), help='用户目录（测试或自定义环境）')
    args = p.parse_args()
    home = args.home.expanduser().resolve()
    targets = {'agents': home / '.agents/skills/local-tts', 'workbuddy': home / '.codebuddy/skills/local-tts'}
    desired = content()
    report = []
    for name, dest in targets.items():
        skill = dest / 'SKILL.md'
        if dest.is_symlink():
            state = 'conflict'
        elif not dest.exists():
            state = 'absent'
        elif not dest.is_dir() or not owned(skill):
            state = 'conflict'
        elif skill.read_text(encoding='utf-8') == desired:
            state = 'installed'
        else:
            state = 'outdated'
        report.append({'target': name, 'directory': str(dest), 'state': state})
    print(json.dumps({'source': str(SOURCE), 'root': str(ROOT), 'action': args.action, 'targets': report}, ensure_ascii=False), flush=True)
    if args.action == 'check':
        return 0 if all(item['state'] == 'installed' for item in report) else 1
    if args.action == 'install':
        if any(item['state'] in ('conflict', 'outdated') for item in report):
            print('存在同名或已变更技能，未执行安装；请先检查目标目录。', file=__import__('sys').stderr)
            return 1
        for item in report:
            if item['state'] == 'installed':
                continue
            dest = Path(item['directory'])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.mkdir()
            try:
                with (dest / 'SKILL.md').open('x', encoding='utf-8') as f:
                    f.write(desired)
            except Exception:
                dest.rmdir()
                raise
        return 0
    if any(item['state'] == 'conflict' for item in report):
        print('存在非本安装器创建或已修改的技能，未执行卸载。', file=__import__('sys').stderr)
        return 1
    for item in report:
        if item['state'] in ('installed', 'outdated'):
            dest = Path(item['directory'])
            if sorted(p.name for p in dest.iterdir()) != ['SKILL.md']:
                print(f'目录包含其他文件，拒绝卸载: {dest}', file=__import__('sys').stderr)
                return 1
    for item in report:
        if item['state'] in ('installed', 'outdated'):
            dest = Path(item['directory'])
            (dest / 'SKILL.md').unlink()
            dest.rmdir()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
