from typing import Dict, Type
from . import TTSEngine

REGISTRY: Dict[str, Type[TTSEngine]] = {}
# 引擎实例缓存：模型加载昂贵，同一进程内复用；换引擎时先 unload 旧的
_INSTANCES: Dict[str, TTSEngine] = {}

def register(name: str):
    def decorator(cls: Type[TTSEngine]):
        REGISTRY[name] = cls
        cls.name = name
        return cls
    return decorator

def get_engine(name: str) -> TTSEngine:
    if name not in REGISTRY:
        available = ", ".join(REGISTRY.keys())
        raise ValueError(f"未知引擎 '{name}'，可用: {available}")
    inst = _INSTANCES.get(name)
    if inst is None:
        inst = REGISTRY[name]()
        _INSTANCES[name] = inst
    return inst

def release_engine(name: str) -> None:
    """释放并丢弃缓存的实例，用于热切换基座"""
    inst = _INSTANCES.pop(name, None)
    if inst is not None:
        inst.unload()

def release_all() -> None:
    for name in list(_INSTANCES):
        release_engine(name)

def list_engines() -> list[str]:
    return list(REGISTRY.keys())

def _import_engines():
    # 触发各引擎模块的 @register 装饰器
    import app.engines.cosyvoice.engine  # noqa: F401  注册 cosyvoice2 / cosyvoice3
    import app.engines.f5tts.engine  # noqa: F401
    import app.engines.fishspeech.engine  # noqa: F401
    # mock 引擎始终可用，用于无权重开发
    import app.engines.mock_engine  # noqa: F401
    # macOS say，无需模型即可出真实语音
    import app.engines.macsay  # noqa: F401

_import_engines()
