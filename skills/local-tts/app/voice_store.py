import shutil
import time
from pathlib import Path
from datetime import datetime

class VoiceStore:
    def __init__(self, base_dir: str = "./voices"):
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def save(self, voice_id: str, ref_wav_path: str, name: str = "", prompt_text: str = "") -> dict:
        dest_dir = self.base / voice_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_wav = dest_dir / "ref.wav"
        shutil.copy(ref_wav_path, dest_wav)
        meta = {
            "voice_id": voice_id,
            "name": name or voice_id,
            "ref_wav": str(dest_wav),
            # 参考音频的逐字文本。零样本克隆靠它给 LLM 对齐音色，
            # 留空则退化为纯音频条件（近似 cross-lingual），音色相似度会下降。
            "prompt_text": prompt_text,
            "created_at": datetime.now().isoformat(),
        }
        # 简单用文本存 meta
        import json
        (dest_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta

    def list(self) -> list[dict]:
        import json
        voices = []
        for d in self.base.iterdir():
            meta_path = d / "meta.json"
            if meta_path.exists():
                voices.append(json.loads(meta_path.read_text(encoding="utf-8")))
        return sorted(voices, key=lambda x: x["created_at"], reverse=True)

    def get(self, voice_id: str) -> dict | None:
        import json
        p = self.base / voice_id / "meta.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return None

    def delete(self, voice_id: str) -> bool:
        p = self.base / voice_id
        if p.exists():
            shutil.rmtree(p)
            return True
        return False

    def ref_wav(self, voice_id: str) -> str | None:
        p = self.base / voice_id / "ref.wav"
        return str(p) if p.exists() else None
