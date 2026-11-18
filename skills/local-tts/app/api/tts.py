import json
import re
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from dataclasses import asdict

from app.parser.ssml import apply_confirmed_drops, parse_ssml, to_engine_request
from app.engines.registry import get_engine, REGISTRY
from app.config import load_config
from app.voice_store import VoiceStore

router = APIRouter(tags=["tts"])

class TTSBody(BaseModel):
    text: str  # 支持 SSML 标签
    voice_id: str = "default"
    speed: float = 1.0
    reference_mode: str = 'reference'
    accept_controls: list[str] = Field(default_factory=list)
    drop_controls: list[str] = Field(default_factory=list)

class ParseBody(TTSBody):
    model: str | None = None


@router.get("/engines")
def get_engines():
    result = []
    for name in REGISTRY:
        inst = get_engine(name)
        result.append({"name": name, "implemented": inst.implemented,
                       "capability": inst.get_capability().__dict__,
                       "capability_report": inst.capabilities(),
                       "capability_context": inst.capability_context(),
                       "readiness": inst.readiness()})
    cfg = load_config()
    return {"current": cfg.get("engine", "mock"), "engines": result}

@router.post("/tts")
def tts(body: TTSBody):
    if not body.text.strip():
        raise HTTPException(400, "text 不能为空")

    cfg = load_config()
    engine_name = cfg.get("engine", "mock")

    # 1. 解析 SSML
    try:
        parsed = parse_ssml(body.text)
        if not set(body.accept_controls) <= {'phoneme', 'emotion'}:
            raise ValueError('无法识别的实验性控制类型')
        if set(body.accept_controls) & set(body.drop_controls):
            raise ValueError('不能同时确认和忽略同一类型的控制')
        parsed, dropped_controls = apply_confirmed_drops(parsed, body.drop_controls)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # 2. 取引擎
    try:
        engine = get_engine(engine_name)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # 3. voice 校验（预置音色引擎如 macsay 跳过）
    if body.voice_id != "default" and engine.voice_from_store:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', body.voice_id):
            raise HTTPException(400, "音色 ID 无效")
        store = VoiceStore(cfg.get("voices_dir", "./voices"))
        folder = store.base / body.voice_id
        wav = folder / 'ref.wav'
        if (folder.is_symlink() or folder.resolve().parent != store.base.resolve()
                or wav.is_symlink() or not wav.is_file() or not store.get(body.voice_id)):
            raise HTTPException(404, f"voice_id '{body.voice_id}' 不存在，请先上传参考音频")

    # 4. 转换为引擎请求
    req = to_engine_request(parsed, voice_id=body.voice_id, speed=body.speed,
                            accepted_controls=tuple(sorted(set(body.accept_controls))),
                            reference_mode=body.reference_mode)
    try:
        engine.prepare_voice(body.voice_id)
        engine.validate(req)
        if req.reference_mode == 'reference_plus_prompt':
            req.reference_fingerprint = engine.reference_identity(body.voice_id)
        plan = engine.compile_input(req)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError:
        raise HTTPException(503, '参考音频未就绪')

    # 5. 推理
    try:
        wav_bytes = engine.execute_plan(plan)
    except FileNotFoundError as e:
        raise HTTPException(503, f"模型未就绪: {e}，请先下载模型或将 config.yaml 的 engine 改为 mock")
    except NotImplementedError as e:
        raise HTTPException(501, str(e))
    except Exception as e:
        raise HTTPException(500, f"合成失败: {e}")

    # 5. 返回 wav
    import urllib.parse
    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={
            "X-Plain-Text": urllib.parse.quote(parsed.plain_text),
            "X-Engine": engine_name,
            "X-TTS-Reference-Mode": body.reference_mode,
            "X-TTS-Dropped-Controls": urllib.parse.quote(json.dumps(dropped_controls, ensure_ascii=False)),
        },
    )

@router.post("/parse")
def parse_preview(body: ParseBody):
    """仅解析不合成，用于前端预览标签是否生效"""
    try:
        parsed = parse_ssml(body.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    result = {
        "plain_text": parsed.plain_text,
        "phoneme_overrides": parsed.phoneme_overrides,
        "breaks": parsed.breaks,
        "emphasis_spans": parsed.emphasis_spans,
        "emotion_spans": parsed.emotion_spans,
        "control_spans": [asdict(span) for span in parsed.control_spans],
    }
    model = getattr(body, 'model', None)
    if body.reference_mode != 'reference' and not model:
        raise HTTPException(400, '预览参考音频模式时必须指定模型')
    if model:
        try:
            engine = get_engine(model)
            if not engine.implemented:
                raise ValueError(f'模型尚未实现: {model}')
            from app.engines import preview_input
            result['provider_preview'] = preview_input(engine, parsed, body.reference_mode)
        except ValueError as e:
            raise HTTPException(400, str(e))
    return result
