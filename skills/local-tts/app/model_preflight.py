"""离线模型加载的前置条件；不提供全进程网络隔离。"""
import contextlib
import contextvars
import os
import sys
from pathlib import Path

_OFFLINE_VARIABLES = ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE')
_ACTIVE = contextvars.ContextVar('tts_offline_active', default=False)


@contextlib.contextmanager
def offline_environment():
    # 两个库可能在 import 时缓存环境值，即使现在的环境变量为 1 也不能验证历史状态。
    loaded = sorted(name for name in sys.modules
                    if name == 'huggingface_hub' or name.startswith('huggingface_hub.')
                    or name == 'transformers' or name.startswith('transformers.'))
    if loaded:
        raise RuntimeError('Hugging Face/Transformers 已导入，无法安全启用离线模式；请使用新进程')
    saved = {key: os.environ.get(key) for key in _OFFLINE_VARIABLES}
    token = _ACTIVE.set(True)
    try:
        for key in _OFFLINE_VARIABLES:
            os.environ[key] = '1'
        yield
    finally:
        _ACTIVE.reset(token)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def assert_offline_ready():
    if not _ACTIVE.get() or any(os.environ.get(key) != '1' for key in _OFFLINE_VARIABLES):
        raise RuntimeError('离线环境未在模型加载前启用；请通过 synth --offline 在新进程运行')


def validate_model(model_dir, version):
    if version not in ('cosyvoice2', 'cosyvoice3'):
        raise ValueError(f'不支持离线模型版本: {version}')
    root = Path(model_dir).expanduser()
    if not root.is_dir():
        raise ValueError(f'离线模式需要实际存在的模型目录: {root}')
    # AutoModel 依次优先选择 1、2、3，拒绝混合布局以防加载错误版本。
    other = [name for name in ('cosyvoice.yaml', 'cosyvoice2.yaml', 'cosyvoice3.yaml')
             if name != f'{version}.yaml' and (root / name).exists()]
    if other:
        raise ValueError(f'模型版本布局冲突: {root} 包含 {", ".join(other)}')
    required = [f'{version}.yaml', 'llm.pt', 'flow.pt', 'hift.pt', 'campplus.onnx',
                f'speech_tokenizer_v{version[-1]}.onnx']
    required += [f'CosyVoice-BlankEN/{name}' for name in
                 ('config.json', 'model.safetensors', 'tokenizer_config.json', 'vocab.json', 'merges.txt')]
    invalid = []
    for name in required:
        path = root / name
        try:
            if not path.is_file() or path.stat().st_size == 0:
                invalid.append(name)
            else:
                with path.open('rb') as stream:
                    if not stream.read(1):
                        invalid.append(name)
        except OSError:
            invalid.append(name)
    if invalid:
        raise ValueError(f'离线模型缺少文件或文件为空 ({root}): {", ".join(invalid)}')
    return root.resolve()


@contextlib.contextmanager
def block_snapshot_download(module):
    if not hasattr(module, 'snapshot_download'):
        raise RuntimeError('无法保护 CosyVoice snapshot_download 绑定，拒绝离线加载')
    original = module.snapshot_download

    def blocked(*args, **kwargs):
        raise RuntimeError('离线模式拒绝 CosyVoice snapshot_download 网络回退')

    module.snapshot_download = blocked
    try:
        yield
    finally:
        module.snapshot_download = original
