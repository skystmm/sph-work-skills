import base64
import hashlib
import io
import json
import os
import struct
import sys
import threading
import time

MAX_MESSAGE = 32 * 1024 * 1024


def send(fd, message):
    payload = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(payload) > MAX_MESSAGE:
        raise ValueError('response too large')
    frame = struct.pack('!I', len(payload)) + payload
    while frame:
        count = os.write(fd, frame)
        if count <= 0:
            raise BrokenPipeError()
        frame = frame[count:]


def reference_hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    fd = int(sys.argv[1])
    model_path = sys.argv[2]
    kind = sys.argv[3]
    parent_pid = int(sys.argv[4])

    def watch_parent():
        while os.getppid() == parent_pid:
            time.sleep(1)
        os._exit(1)

    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        import mlx.core as mx
        import numpy as np
        import soundfile as sf
        from mlx_audio.tts.utils import load_model
        model = load_model(model_path)
        send(fd, {'version': 1, 'id': 0, 'ok': True})
    except Exception as exc:
        send(fd, {'version': 1, 'id': 0, 'ok': False, 'error': type(exc).__name__})
        return 1

    for line in sys.stdin.buffer:
        if len(line) > 1024 * 1024:
            return 1
        request = None
        observation = None
        started = None
        try:
            request = json.loads(line)
            if request.get('version') != 1:
                raise ValueError('protocol version')
            if request['operation'] == 'close':
                send(fd, {'version': 1, 'id': request['id'], 'ok': True})
                return 0
            if request['operation'] != 'synthesize':
                raise ValueError('operation')
            mode = request.get('reference_mode', 'reference')
            if not isinstance(mode, str) or mode not in ('reference', 'reference_plus_prompt'):
                raise ValueError('reference mode')
            if 'prompt_audio' in request:
                raise ValueError('prompt audio is derived from reference')
            if mode == 'reference_plus_prompt':
                prompt = request.get('prompt_text')
                if (kind != 'vox' or not isinstance(request.get('ref_audio'), str)
                        or not request['ref_audio'].strip() or not isinstance(prompt, str) or not prompt.strip()
                        or len(prompt.encode('utf-8')) > 65536 or request.get('instruct')
                        or request.get('diagnostic') is not None):
                    raise ValueError('reference plus prompt parameters')
            elif 'prompt_text' in request:
                raise ValueError('prompt text requires reference plus prompt')
            context = request.get('diagnostic')
            if context is not None:
                import re
                if (kind != 'vox' or not isinstance(context, dict)
                        or set(context) != {'version', 'run_id', 'attempt_id', 'chunk_index', 'step_index'}
                        or type(context['version']) is not int or context['version'] != 1
                        or any(not isinstance(context[key], str) or not re.fullmatch(r'[0-9a-f]{32}', context[key])
                               for key in ('run_id', 'attempt_id'))
                        or any(type(context[key]) is not int or context[key] < 0
                               for key in ('chunk_index', 'step_index'))):
                    raise ValueError('diagnostic context')
                observation = {'context': context, 'before_sha256': None, 'after_sha256': None,
                               'elapsed_ms': None}
                observation['before_sha256'] = reference_hash(request['ref_audio'])
                started = time.monotonic()
            args = {'text': request['text'], 'ref_audio': request['ref_audio']}
            if kind == 'qwen':
                args['ref_text'] = request['ref_text']
            if mode == 'reference_plus_prompt':
                args['prompt_audio'] = request['ref_audio']
                args['prompt_text'] = request['prompt_text']
            if kind == 'vox' and request.get('instruct'):
                args['instruct'] = request['instruct']
            results = list(model.generate(**args))
            if not results:
                raise ValueError('empty audio')
            rate = results[0].sample_rate
            if not isinstance(rate, int) or rate <= 0:
                raise ValueError('invalid sample rate')
            pieces = []
            for result in results:
                if result.sample_rate != rate:
                    raise ValueError('inconsistent sample rate')
                mx.eval(result.audio)
                audio = np.asarray(result.audio)
                if audio.ndim != 1 or not audio.size or not np.isfinite(audio).all():
                    raise ValueError('invalid audio')
                pieces.append(audio)
            audio = np.concatenate(pieces)
            output = io.BytesIO()
            sf.write(output, audio, rate, format='WAV')
            response = {'version': 1, 'id': request['id'], 'ok': True,
                        'audio': base64.b64encode(output.getvalue()).decode('ascii')}
            if observation is not None:
                observation['after_sha256'] = reference_hash(request['ref_audio'])
                observation['elapsed_ms'] = round((time.monotonic() - started) * 1000)
                response['diagnostic'] = observation
            send(fd, response)
        except Exception as exc:
            if observation is not None and started is not None:
                try:
                    observation['after_sha256'] = reference_hash(request['ref_audio'])
                except Exception:
                    observation['after_sha256'] = None
                observation['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            response = {'version': 1, 'id': request.get('id') if isinstance(request, dict) else None,
                        'ok': False, 'error': type(exc).__name__}
            if observation is not None:
                response['diagnostic'] = observation
            send(fd, response)
    return 0


if __name__ == '__main__':
    sys.exit(main())
