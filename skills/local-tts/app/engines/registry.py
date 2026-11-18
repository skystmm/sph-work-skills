import importlib
from pathlib import Path
from typing import Dict, Type
from . import TTSEngine

REGISTRY: Dict[str, Type[TTSEngine]] = {}
# 引擎实例缓存：模型加载昂贵，同一进程内复用；换引擎时先 unload 旧的
_INSTANCES: Dict[str, TTSEngine] = {}

def register(name: str):
    def decorator(cls: Type[TTSEngine]):
        if name in REGISTRY:
            raise ValueError(f"重复的引擎 ID: {name}")
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
    root = Path(__file__).parent
    modules = ["app.engines.macsay", "app.engines.mock_engine"]
    modules.extend(
        f"app.engines.{path.name}.engine"
        for path in root.iterdir()
        if path.is_dir() and path.name.isidentifier()
        and not path.is_symlink() and (path / "engine.py").is_file()
        and not (path / "engine.py").is_symlink()
    )
    for module in sorted(modules):
        importlib.import_module(module)

_import_engines()
