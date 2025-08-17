# task.py (Modified: Copy selected videos to ASCII-named temp files in task_dir to avoid Unicode path issues)
import math
import os.path
import re
from os import path
import gc  # Added for memory management
import random  # 추가: 랜덤 선택
import unicodedata  # 추가: Unicode normalization
import shutil  # 추가: 파일 복사

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoConcatMode, VideoParams, MaterialInfo
from app.services import llm, subtitle, video, voice
from app.utils import utils
from pathlib import Path


def generate_script(task_id, params):
    logger.info("\n\n## generating video script")
    video_script = params.video_script.strip()
    if not video_script:
        # positional args로 llm.generate_script 호출
        video_script = llm.generate_script(
            video_subject=params.video_subject,
            language=params.video_language,
            paragraph_number=params.paragraph_number or 1,
        )
    else:
        logger.debug(f"video script: \n{video_script}")

    if not video_script:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video script.")
        return None

    return video_script


def generate_terms(task_id, params, video_script):
    logger.info("\n\n## generating video terms")
    video_terms = params.video_terms
    if not video_terms:
        # positional 인자로 llm.generate_terms 호출
        video_terms = llm.generate_terms(
            video_subject=params.video_subject,
            video_script=video_script,
            amount=max(1, params.paragraph_number or 5),
        )
    else:
        if isinstance(video_terms, str):
            video_terms = [term.strip() for term in re.split(r"[,，]", video_terms)]
        elif isinstance(video_terms, list):
            video_terms = [term.strip() for term in video_terms]
        else:
            raise ValueError("video_terms must be a string or a list of strings.")

        logger.debug(f"video terms: {utils.to_json(video_terms)}")

    if not video_terms:
        sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
        logger.error("failed to generate video terms.")
        return None

    return video_terms


def save_script_data(task_id, video_script, video_terms, params):
    script_file = path.join(utils.task_dir(task_id), "script.json")
    script_data = {
        "script": video_script,
        "search_terms": video_terms,
        "params": params,
    }

    with open(script_file, "w", encoding="utf-8") as f:
        f.write(utils.to_json(script_data))


def generate_audio(task_id, params, video_script):
    logger.info("\n\n## generating audio")
    audio_file = path.join(utils.task_dir(task_id), "audio.mp3")
    # voice.tts가 파일만 반환
    voice_file = voice.tts(
        text=video_script,
        voice_name=voice.parse_voice_name(params.voice_name),
        voice_rate=params.voice_rate,
        voice_file=audio_file,
    )
    if not voice_file:
        raise ValueError("TTS 생성 실패")
    audio_duration = math.ceil(voice.get_audio_duration(audio_file))
    return audio_file, audio_duration


def generate_subtitle(task_id, params, video_script, audio_file):
    if not params.subtitle_enabled:
        return ""

    subtitle_path = path.join(utils.task_dir(task_id), "subtitle.srt")
    subtitle_provider = config.app.get("subtitle_provider", "whisper").strip().lower()  # 기본 Whisper
    logger.info(f"\n\n## generating subtitle, provider: {subtitle_provider}")

    subtitle.create(audio_file=audio_file, subtitle_file=subtitle_path)
    subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)

    return subtitle_path if os.path.exists(subtitle_path) else ""


def get_video_materials(task_id, params, video_terms, audio_duration, subtitle_segments):
    if params.video_source == "local":
        logger.info("\n\n## preprocess local materials")
        local_dir = str(utils.media_dir())
        if not os.path.exists(local_dir):
            logger.error(f"Local dir does not exist: {local_dir}")
            return None
        subdirs = [d for d in os.listdir(local_dir) if os.path.isdir(os.path.join(local_dir, d))]
        logger.info(f"Found subdirectories: {subdirs}")

        selected_videos = []
        for seg_idx, segment in enumerate(subtitle_segments):
            seg_duration = segment['end'] - segment['start']
            term = video_terms[seg_idx % len(video_terms)]
            # Normalize term and subdirs for matching
            norm_term = unicodedata.normalize('NFD', term.lower())
            norm_subdirs = [unicodedata.normalize('NFD', d.lower()) for d in subdirs]

            matching_dirs = [subdirs[i] for i, nd in enumerate(norm_subdirs) if any(word in nd for word in norm_term.split())]
            if not matching_dirs:
                matching_dirs = [random.choice(subdirs)] if subdirs else []
                logger.warning(f"No matching dir for term '{term}', using random: {matching_dirs}")

            if matching_dirs:
                selected_dir = random.choice(matching_dirs)
                dir_path = os.path.abspath(os.path.join(local_dir, selected_dir))
                if not os.path.exists(dir_path):
                    logger.error(f"Directory does not exist: {dir_path}")
                    continue
                files = [f for f in os.listdir(dir_path) if f.lower().endswith((".mp4", ".png", ".jpg"))]
                if files:
                    selected_file = random.choice(files)
                    full_path = os.path.join(dir_path, selected_file)
                    # 변경: 복사하지 않고 원본 경로를 직접 사용
                    item = MaterialInfo(url=full_path, duration=min(6, seg_duration or 6))
                    selected_videos.append(item)
                    logger.info(f"Selected for segment {seg_idx}: {full_path}")
                else:
                    logger.warning(f"No files in dir: {selected_dir}")
            else:
                logger.error("No directories available")

        materials = video.preprocess_video(
            materials=selected_videos,
            clip_duration=params.video_clip_duration,
        )
        if not materials:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("no valid materials found, please check the materials and try again.")
            return None
        return [material_info.url for material_info in materials]
    else:
        # 온라인 소스 (생략, local만)
        pass


def parse_subtitle_segments(subtitle_path):
    # subtitle.py의 file_to_subtitles 사용, but simplify
    segments = []
    if subtitle_path and os.path.exists(subtitle_path):
        with open(subtitle_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        i = 0
        while i < len(lines):
            if lines[i].strip().isdigit():
                time_line = lines[i+1].strip()
                start, end = time_line.split(' --> ')
                start_sec = utils.srt_time_to_seconds(start)
                end_sec = utils.srt_time_to_seconds(end)
                text = lines[i+2].strip()
                segments.append({'start': start_sec, 'end': end_sec, 'text': text})
                i += 4  # skip empty line
            else:
                i += 1
    return segments


def generate_final_videos(
    task_id, params, downloaded_videos, audio_file, subtitle_path
):
    final_video_paths = []
    combined_video_paths = []
    video_concat_mode = (
        params.video_concat_mode if params.video_count == 1 else VideoConcatMode.random
    )
    video_transition_mode = params.video_transition_mode

    _progress = 50
    for i in range(params.video_count):
        index = i + 1
        combined_video_path = path.join(
            utils.task_dir(task_id), f"combined-{index}.mp4"  # combined-1.mp4, combined-2.mp4 등
        )
        logger.info(f"\n\n## combining video: {index} => {combined_video_path}")
        video.combine_videos(
            combined_video_path=combined_video_path,
            video_paths=downloaded_videos,
            audio_file=audio_file,
            video_aspect=params.video_aspect,
            video_concat_mode=video_concat_mode,
            video_transition_mode=video_transition_mode,
            max_clip_duration=params.video_clip_duration,
            threads=params.n_threads,
        )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_path = path.join(utils.task_dir(task_id), f"final-{index}.mp4")

        logger.info(f"\n\n## generating video: {index} => {final_video_path}")
        # 여기서 비디오+오디오 합치기 수행
        video.generate_video(
            video_path=combined_video_path,
            audio_path=audio_file,
            subtitle_path=subtitle_path,
            output_file=final_video_path,
            params=params,
        )

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_paths.append(final_video_path)
        combined_video_paths.append(combined_video_path)

    return final_video_paths, combined_video_paths


def start(task_id, params: VideoParams, stop_at: str = "video"):
    num_videos = params.video_count
    logger.info(f"전체 영상 수: {num_videos}개, 생성 시작")
    results = []
    for idx in range(num_videos):
        logger.info(f"{idx+1}/{num_videos}번째 영상 생성 시작")
        video_script = generate_script(task_id, params)
        video_terms = generate_terms(task_id, params, video_script)
        audio_file, audio_duration = generate_audio(task_id, params, video_script)
        if not audio_file or not os.path.exists(audio_file) or audio_duration == 0:
            sm.state.update_task(task_id, state=const.TASK_STATE_FAILED)
            logger.error("오디오 파일 생성에 실패하여 작업을 중단합니다.")
            return None
        subtitle_path = generate_subtitle(task_id, params, video_script, audio_file)
        subtitle_segments = parse_subtitle_segments(subtitle_path)  # 추가: 자막 segments 파싱
        downloaded_videos = get_video_materials(task_id, params, video_terms, audio_duration, subtitle_segments)  # 수정: segments 전달
        final_video_paths, _ = generate_final_videos(
            task_id, params, downloaded_videos, audio_file, subtitle_path
        )
        final_video_path = final_video_paths[0] if final_video_paths else ""
        results.append({"video": final_video_path})
        logger.info(f"{idx+1}/{num_videos}번째 영상 생성 완료: {final_video_path}")
    logger.success("모든 영상 생성 완료")
    return results


def pick_local_videos(
    search_terms: list[str],
    max_clip_duration: int = 5,
    limit: int = 20,
) -> list[str]:
    media_root = Path("local_media")
    if not media_root.is_dir():
        return []

    candidates: list[Path] = []
    for term in search_terms:
        term_dir = media_root / term
        if term_dir.is_dir():
            candidates.extend(term_dir.glob("*.mp4"))

    random.shuffle(candidates)
    return [str(p) for p in candidates[:limit]]


if __name__ == "__main__":
    task_id = "task_id"
    params = VideoParams(
        video_subject="금전의 역할",
        voice_name="zh-CN-XiaoyiNeural-Female",
        voice_rate=1.0,
    )
    start(task_id, params, stop_at="video")