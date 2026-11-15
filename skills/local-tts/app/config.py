from pathlib import Path
import yaml


def load_config(path: str = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        return {"engine": "mock", "voices_dir": "./voices", "models_dir": "./models"}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
