"""Disposable, offline Whisper worker producing timed cues for a final video.

Runs in the configured Transformers environment. Parent process handles audio
extraction and process cancellation. No model downloads or text-model rewriting.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import sys
import wave


class CaptionTimingError(ValueError):
    pass


def validate_offsets(offsets, *, chunk_start, chunk_duration):
    """Validate native timestamp tokens; never invent evenly spaced speech times."""
    result = []
    previous_end = 0.0
    for item in offsets:
        text = re.sub(r'\s+', ' ', str(item.get('text', ''))).strip()
        if not text:
            continue
        stamps = item.get('timestamp')
        if not isinstance(stamps, (tuple, list)) or len(stamps) != 2:
            raise CaptionTimingError('Whisper 未返回有效句子时间戳')
        try:
            begin, end = float(stamps[0]), float(stamps[1])
        except (ValueError, TypeError) as exc:
            raise CaptionTimingError('Whisper 时间戳不完整') from exc
        if (not all(math.isfinite(x) for x in (begin, end)) or begin < 0 or end <= begin
                or end > chunk_duration + .12 or begin < previous_end - .021):
            raise CaptionTimingError('Whisper 返回越界或重叠时间戳，未写入猜测字幕')
        # Token timestamps have 20 ms granularity; only tolerate endpoint rounding.
        begin, end = max(begin, previous_end), min(end, chunk_duration)
        if end <= begin:
            raise CaptionTimingError('字幕时长无效')
        if len(text) > 500 or re.search(r'(.{2,12})\1{4,}', text):
            raise CaptionTimingError('语音识别出现异常重复，请重新识别')
        result.append({'start': round(chunk_start+begin, 3),
                       'end': round(chunk_start+end, 3), 'text': text})
        previous_end = end
    return result


def main():
    import numpy as np
    import torch
    from transformers import WhisperProcessor, WhisperForConditionalGeneration

    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    torch.set_num_threads(2)
    if not torch.cuda.is_available() or torch.cuda.mem_get_info()[0] < 5*1024**3:
        raise RuntimeError('字幕识别需要至少 5 GB 空闲 GPU 显存')
    with wave.open(request['audio']) as wav:
        if wav.getframerate()!=16000 or wav.getnchannels()!=1 or wav.getsampwidth()!=2:
            raise ValueError('Expected mono 16 kHz PCM audio')
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16).astype(np.float32)/32768
    processor = WhisperProcessor.from_pretrained(request['model'], local_files_only=True)
    model = WhisperForConditionalGeneration.from_pretrained(request['model'],
        torch_dtype=torch.float16, local_files_only=True, attn_implementation='eager').to('cuda').eval()
    model.generation_config.forced_decoder_ids = None
    segments = []
    # Non-overlapping short-form chunks have explicit global offsets. Segment
    # end timestamps must fit the actual last chunk rather than its padded input.
    for index in range(0, len(samples), 30*16000):
        audio = samples[index:index+30*16000]
        if len(audio)<1600 or float(np.sqrt(np.mean(audio**2))) < .002:
            continue
        inputs = processor(audio, sampling_rate=16000, return_tensors='pt', return_attention_mask=True)
        with torch.inference_mode():
            predicted = model.generate(input_features=inputs.input_features.to('cuda',dtype=torch.float16),
                attention_mask=inputs.attention_mask.to('cuda'), language='zh', task='transcribe',
                return_timestamps=True, num_beams=3, max_new_tokens=440,
                condition_on_prev_tokens=False)
        if isinstance(predicted, dict):
            predicted = predicted['sequences']
        decoded = processor.tokenizer.decode(predicted[0], skip_special_tokens=True, output_offsets=True)
        if not isinstance(decoded, dict) or 'offsets' not in decoded:
            raise CaptionTimingError('Whisper 没有返回句子时间轴')
        if decoded.get('text', '').strip() and not decoded['offsets']:
            raise CaptionTimingError('识别文字缺少时间戳，不能生成猜测字幕')
        segments.extend(validate_offsets(decoded['offsets'], chunk_start=index/16000,
                                         chunk_duration=len(audio)/16000))
    result = {'version':1,'clock':'final_video_seconds','duration':len(samples)/16000,
              'segments':segments,'model':'local_transformers_whisper','human_verified':False}
    Path(request['output']).write_text(json.dumps(result,ensure_ascii=False,allow_nan=False),encoding='utf-8')


if __name__ == '__main__':
    main()
