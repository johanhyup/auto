import asyncio
import os
import re
from datetime import datetime
from typing import Union
from xml.sax.saxutils import unescape

import edge_tts
try:
    from elevenlabs import ElevenLabs
except ImportError:
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        ElevenLabs = None

from elevenlabs.core.api_error import ApiError
from loguru import logger

from app.config import config
from app.utils import utils
from app.services import subtitle  # 수정: Whisper fallback 위해 import 추가

def parse_voice_name(voice_name: str) -> str:
    """
    Azure/Streamlit용 음성 이름을 ElevenLabs voice_id로 변환합니다.
    실패 시, 첫번째 음성 ID나 원본 문자열을 반환합니다.
    """
    if ElevenLabs is None:
        return voice_name
    client = get_elevenlabs_client()
    try:
        resp = client.voices.search(include_total_count=True, voice_type="personal")
        voices = getattr(resp, "voices", [])
    except Exception:
        voices = []
    for v in voices:
        vid = getattr(v, "voice_id", "") or getattr(v, "id", "")
        name = getattr(v, "name", "")
        if voice_name.lower() in (vid.lower(), name.lower()):
            return vid
    if voices:
        first = voices[0]
        return getattr(first, "voice_id", voice_name)
    return voice_name

def get_elevenlabs_client():
    api_key = config.app.get("elevenlabs_api_key")
    if not api_key:
        raise ValueError("ElevenLabs API 키가 설정되지 않았습니다.")
    return ElevenLabs(api_key=api_key)

def get_audio_duration(audio_source) -> float:
    """
    오디오 파일 경로를 받아 길이를 초 단위로 반환합니다.
    """
    try:
        from moviepy.editor import AudioFileClip  # 동적 임포트
        clip = AudioFileClip(audio_source)
        duration = clip.duration
        clip.close()
        return duration
    except Exception as e:
        logger.warning(f"오디오 길이 계산 실패: {e}")
        return 0.0

def tts(text: str, voice_name: str = "ko-KR-InJoonNeural-Male", voice_rate: float = 0.0, voice_file: str = "") -> str:
    if ElevenLabs is None:
        raise ImportError("ElevenLabs 패키지가 설치되어 있지 않습니다. TTS를 수행할 수 없습니다.")

    voice_id = parse_voice_name(voice_name)
    client = get_elevenlabs_client()
    logger.info("ElevenLabs TTS 생성 중...")
    try:
        response = client.text_to_speech.convert(
            voice_id=voice_id,
            text=text,
            model_id="eleven_multilingual_v2",
            output_format="mp3_44100_128"
        )
    except ApiError as e:
        msg = e.body.get("detail", {}).get("message", str(e))
        raise ValueError(f"ElevenLabs TTS 변환 실패 (status={e.status_code}): {msg}")

    if not voice_file:
        voice_file = utils.task_dir() + "/tts-output.mp3"
    with open(voice_file, "wb") as f:
        try:
            for chunk in response:
                f.write(chunk)
        except ApiError as e:
            msg = e.body.get("detail", {}).get("message", str(e))
            raise ValueError(f"ElevenLabs TTS 스트리밍 실패 (status={e.status_code}): {msg}")

    # Whisper로 자막 생성
    subtitle_path = voice_file + ".srt"
    subtitle.create(audio_file=voice_file, subtitle_file=subtitle_path)
    return voice_file  # subtitle_path는 task.py에서 별도 처리