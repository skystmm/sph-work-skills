from abc import ABC, abstractmethod
from dataclasses import asdict
from typing import Iterator
from .base import TTSRequest, TTSCapability

class TTSEngine(ABC):
    contract_version: int = 1
    backend: str = "unspecified"
    name: str = "base"

    @property
    def id(self) -> str:
        return self.name
    capability: TTSCapability = TTSCapability()
    # voice_id 是否必须来自本地音色库（克隆类引擎为 True，预置音色引擎为 False）
    voice_from_store: bool = True
    # 占位插槽尚未实现 synthesize，能力矩阵对它没有意义
    implemented: bool = True

    @abstractmethod
    def load(self) -> None:
        """加载模型到显存/内存"""
        ...

    @abstractmethod
    def is_loaded(self) -> bool:
        ...

    @abstractmethod
    def clone_voice(self, ref_wav_path: str, voice_id: str, prompt_text: str = "") -> str:
        """从参考音频克隆音色，返回 voice_id。prompt_text 是音频的逐字文本，
        零样本类引擎靠它对齐音色；预置音色引擎（如 macsay）可忽略。"""
        ...

    @abstractmethod
    def synthesize(self, req: TTSRequest) -> bytes:
        """同步合成，返回 wav bytes"""
        ...

    def compile_input(self, req: TTSRequest):
        from .base import ProviderPlan, ProviderStep
        self.validate_request(req)
        return ProviderPlan(version=1, steps=(ProviderStep(kind="legacy"),), request=req)

    def execute_plan(self, plan):
        return self.synthesize(plan.request)

    def synthesize_standard(self, req: TTSRequest) -> bytes:
        self.validate(req)
        return self.execute_plan(self.compile_input(req))

    def synthesize_stream(self, req: TTSRequest) -> Iterator[bytes]:
        """流式合成，默认降级为一次性返回"""
        yield self.synthesize(req)

    def unload(self) -> None:
        """释放资源，用于热切换"""
        pass

    def forget_voice(self, voice_id: str) -> None:
        """音色被删除或替换时清掉引擎内缓存的参考特征（无缓存则无需实现）"""
        pass

    def get_capability(self) -> TTSCapability:
        return self.capability

    def capability_report(self) -> dict:
        capability = self.get_capability()
        supported = {'phoneme': capability.supports_phoneme, 'break': capability.supports_break_ms,
                     'emphasis': capability.supports_emphasis, 'emotion': capability.supports_emotion,
                     'speed': capability.supports_speed, 'streaming': capability.supports_streaming}
        return {name: {'status': ('unsupported' if not available else
                                  'experimental' if name in capability.experimental_controls else 'unknown'),
                       'precision': ('exact' if name == 'break' else 'best_effort') if available else None}
                for name, available in supported.items()}

    def capability_context(self) -> dict:
        from pathlib import Path
        result = {'provider': self.id, 'backend': self.backend,
                  'mode': 'reference' if self.voice_from_store else 'preset',
                  'model_revision': None, 'model_sha256': None, 'runtime_version': None}
        model = getattr(self, 'model_dir', None)
        runtime = getattr(self, 'python', None)
        if model is not None:
            result['model_path'] = str(Path(model).absolute())
        if runtime is not None:
            result['runtime_path'] = str(Path(runtime).absolute())
        return result

    def capabilities(self, context=None) -> dict:
        selected = self.capability_context()
        if context is not None:
            mode = context.get('mode') if isinstance(context, dict) else None
            if mode in self.get_capability().reference_modes:
                selected['mode'] = mode
            if not isinstance(context, dict) or any(key not in selected or selected[key] != value
                                                    for key, value in context.items()):
                raise ValueError('模型、后端或模式的能力上下文不匹配')
        report = self.capability_report()
        if selected['mode'] == 'reference_plus_prompt':
            report['emotion'] = {'status': 'unsupported', 'precision': None}
        return report

    def validate(self, req: TTSRequest, context=None) -> None:
        if context is not None:
            self.capabilities(context)
        self.validate_request(req)

    def prepare_voice(self, voice_id: str, options=None) -> str:
        if options is not None:
            raise ValueError('当前 Provider 不支持额外的音色准备选项')
        self.validate_voice(voice_id)
        return voice_id

    def plan_chunks(self, document, context=None) -> list[dict]:
        if context is not None:
            self.capabilities(context)
        from app.parser.chunker import chunk_text_with_metadata
        return chunk_text_with_metadata(document.plain_text, document.phoneme_overrides,
                                        document.breaks, document.emphasis_spans,
                                        min_len=40, max_len=80,
                                        emotion_spans=document.emotion_spans)

    def open_session(self, context=None):
        if context is not None:
            self.capabilities(context)
        if not self.is_loaded():
            self.load()
        return self

    def runtime_identity(self):
        return self.resume_identity()

    def close(self) -> None:
        self.unload()

    def validate_voice(self, voice_id: str) -> None:
        pass

    def reference_identity(self, voice_id: str) -> dict:
        raise ValueError(f'模型 {self.name} 不支持参考音频加逐字稿模式')

    def validate_request(self, req: TTSRequest) -> None:
        from .base import validate_request
        validate_request(req, self.get_capability())

    def readiness(self) -> dict:
        return {"runtime": "unknown", "model": "unknown"}

    def resume_identity(self) -> dict | None:
        raise ValueError(f"模型 {self.name} 未实现可恢复任务身份")


def preview_input(engine: TTSEngine, document, reference_mode='reference') -> dict:
    capability = engine.get_capability()
    experimental = [name for name, present in (('phoneme', document.phoneme_overrides),
                                                ('emotion', document.emotion_spans))
                    if present and name in capability.experimental_controls]
    req = TTSRequest(text=document.plain_text, phoneme_overrides=document.phoneme_overrides,
                     breaks=document.breaks, emphasis_spans=document.emphasis_spans,
                     emotion_spans=document.emotion_spans, accepted_controls=tuple(experimental),
                     reference_mode=reference_mode)
    try:
        plan = engine.compile_input(req)
        return {'model': engine.name, 'reference_mode': reference_mode, 'plan_version': plan.version,
                'steps': [asdict(step) for step in plan.steps],
                'requires_confirmation': experimental}
    except ValueError as exc:
        return {'model': engine.name, 'reference_mode': reference_mode, 'error': str(exc), 'requires_confirmation': experimental}
