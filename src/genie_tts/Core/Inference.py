import onnxruntime as ort
import numpy as np
from typing import List, Optional
import threading

from ..Audio.ReferenceAudio import ReferenceAudio
from ..GetPhonesAndBert import get_phones_and_bert

MAX_T2S_LEN = 1000


class GENIE:
    def __init__(self):
        self.stop_event: threading.Event = threading.Event()

    def tts(
            self,
            text: str,
            prompt_audio: ReferenceAudio,
            encoder: ort.InferenceSession,
            first_stage_decoder: ort.InferenceSession,
            stage_decoder: ort.InferenceSession,
            vocoder: ort.InferenceSession,
            prompt_encoder: Optional[ort.InferenceSession],
            language: str = 'japanese',
    ) -> Optional[np.ndarray]:
        text = '。' + text  # 防止漏第一句。
        text_seq, text_bert = get_phones_and_bert(text, language=language)

        semantic_tokens: np.ndarray = self.t2s_cpu(
            ref_seq=prompt_audio.phonemes_seq,
            ref_bert=prompt_audio.text_bert,
            text_seq=text_seq,
            text_bert=text_bert,
            ssl_content=prompt_audio.ssl_content,
            encoder=encoder,
            first_stage_decoder=first_stage_decoder,
            stage_decoder=stage_decoder,
        )

        # stop_event 被设置时 t2s_cpu 返回 None（退出中）
        if semantic_tokens is None:
            return None

        # 过滤无效 token（>=1024 为 EOS/特殊 token，VITS codebook 为 0-1023）
        semantic_tokens = np.where(semantic_tokens >= 1024, 0, semantic_tokens)

        if prompt_encoder is None:
            return vocoder.run(None, {
                "text_seq": text_seq,
                "pred_semantic": semantic_tokens,
                "ref_audio": prompt_audio.audio_32k
            })[0]
        else:
            # V2ProPlus 新增。
            prompt_audio.update_global_emb(prompt_encoder=prompt_encoder)
            audio_chunk = vocoder.run(None, {
                "text_seq": text_seq,
                "pred_semantic": semantic_tokens,
                "ge": prompt_audio.global_emb,
                "ge_advanced": prompt_audio.global_emb_advanced,
            })[0]
            return audio_chunk

    def t2s_cpu(
            self,
            ref_seq: np.ndarray,
            ref_bert: np.ndarray,
            text_seq: np.ndarray,
            text_bert: np.ndarray,
            ssl_content: np.ndarray,
            encoder: ort.InferenceSession,
            first_stage_decoder: ort.InferenceSession,
            stage_decoder: ort.InferenceSession,
    ) -> Optional[np.ndarray]:
        """在CPU上运行T2S模型"""
        # Encoder
        x, prompts = encoder.run(
            None,
            {
                "ref_seq": ref_seq,
                "text_seq": text_seq,
                "ref_bert": ref_bert,
                "text_bert": text_bert,
                "ssl_content": ssl_content,
            },
        )

        # First Stage Decoder
        y, y_emb, *present_key_values = first_stage_decoder.run(
            None, {"x": x, "prompts": prompts}
        )

        # Stage Decoder
        input_names: List[str] = [inp.name for inp in stage_decoder.get_inputs()]
        MIN_TOKENS = 8  # 最少生成 token 数，防止短文本几乎静音
        idx: int = 0
        for idx in range(0, 500):
            if self.stop_event.is_set():
                return None
            input_feed = {
                name: data
                for name, data in zip(input_names, [y, y_emb, *present_key_values])
            }
            outputs = stage_decoder.run(None, input_feed)
            y, y_emb, stop_condition_tensor, *present_key_values = outputs

            # 至少生成 MIN_TOKENS 个 token 才允许 stop
            if stop_condition_tensor and idx >= MIN_TOKENS - 1:
                break
            if idx >= 200:  # 安全上限
                break

        y[0, -1] = 0
        # 修正: 使用 max(1, idx) 防止 idx=0 时 y[:, -0:] 返回全部参考 token
        num_new_tokens = max(1, idx)
        return np.expand_dims(y[:, -num_new_tokens:], axis=0)


tts_client: GENIE = GENIE()
