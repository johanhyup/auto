# Main.py (No change needed, but confirm local_media_dir in config.toml is set to "/Users/joko/youtube/MoneyPrinterTurbo/local_media")
import os
from pathlib import Path
import platform
import sys
from uuid import uuid4

import streamlit as st
from loguru import logger

# ----------------------------------------
# 경로 설정 (config.paths.* → root_dir 기준)
# ----------------------------------------
root_dir = Path(__file__).resolve().parents[1]

# FFmpeg 경로
ffmpeg_rel = config.paths.get("ffmpeg", "bin/ffmpeg")
os.environ["IMAGEIO_FFMPEG_EXE"] = str((root_dir / ffmpeg_rel).resolve())

# 프로젝트 루트(sys.path 편입)
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))
    print("******** sys.path ********")
    print(sys.path)
    print("")

from app.config import config
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services import llm, voice
from app.services import task as tm
from app.utils import utils

st.set_page_config(
    page_title="코인 뉴스 영상 생성기",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        "Report a bug": "https://github.com/harry0703/MoneyPrinterTurbo/issues",
        "About": "# 코인 뉴스 영상 생성기\n키워드를 입력하면 자동으로 뉴스 기반 영상을 생성합니다.\n\nhttps://github.com/harry0703/MoneyPrinterTurbo",
    },
)


streamlit_style = """
<style>
h1 {
    padding-top: 0 !important;
}
</style>
"""
st.markdown(streamlit_style, unsafe_allow_html=True)

# 정의 자원 디렉토리
font_dir   = utils.font_dir()
song_dir   = utils.song_dir()
i18n_dir   = root_dir / "webui/i18n"
config_file = root_dir / "webui/.streamlit/webui.toml"
system_locale = utils.get_system_locale()


if "video_subject" not in st.session_state:
    st.session_state["video_subject"] = ""
if "video_script" not in st.session_state:
    st.session_state["video_script"] = ""
if "video_terms" not in st.session_state:
    st.session_state["video_terms"] = ""
if "ui_language" not in st.session_state:
    st.session_state["ui_language"] = config.ui.get("language", system_locale)

# 언어 파일 로드
locales = utils.load_locales(i18n_dir)

# 상단 바 생성, 제목만 표시
st.title(f"코인 뉴스 영상 생성기 v{config.project_version}")

support_locales = [
    "ko-KR",  # 한국어 우선
    "zh-CN",
    "zh-HK",
    "zh-TW",
    "de-DE",
    "en-US",
    "fr-FR",
    "vi-VN",
    "th-TH",
]


def get_all_fonts():
    fonts = []
    for root, dirs, files in os.walk(font_dir):
        for file in files:
            if file.endswith(".ttf") or file.endswith(".ttc"):
                fonts.append(file)
    fonts.sort()
    return fonts


def get_all_songs():
    songs = []
    for root, dirs, files in os.walk(song_dir):
        for file in files:
            if file.endswith(".mp3"):
                songs.append(file)
    return songs


def open_task_folder(task_id):
    try:
        sys = platform.system()
        tasks_dir = root_dir / config.paths.get("storage_tasks_dir", "storage/tasks")
        path = tasks_dir / task_id
        if os.path.exists(path):
            if sys == "Windows":
                os.system(f"start {path}")
            if sys == "Darwin":
                os.system(f"open {path}")
    except Exception as e:
        logger.error(e)


def scroll_to_bottom():
    js = """
    <script>
        console.log("scroll_to_bottom");
        function scroll(dummy_var_to_force_repeat_execution){
            var sections = parent.document.querySelectorAll('section.main');
            console.log(sections);
            for(let index = 0; index<sections.length; index++) {
                sections[index].scrollTop = sections[index].scrollHeight;
            }
        }
        scroll(1);
    </script>
    """
    st.components.v1.html(js, height=0, width=0)


def init_log():
    logger.remove()
    _lvl = "DEBUG"

    def format_record(record):
        # 로그 기록 파일 경로
        file_path = record["file"].path
        # 절대 경로를 프로젝트 루트 기준 상대 경로로 변환
        relative_path = os.path.relpath(file_path, root_dir)
        # 기록 업데이트
        record["file"].path = f"./{relative_path}"
        # 메시지 루트 디렉토리 대체
        record["message"] = record["message"].replace(root_dir, ".")

        _format = (
            "<green>{time:%Y-%m-%d %H:%M:%S}</> | "
            + "<level>{level}</> | "
            + '"{file.path}:{line}":<blue> {function}</> '
            + "- <level>{message}</>"
            + "\n"
        )
        return _format

    logger.add(
        sys.stdout,
        level=_lvl,
        format=format_record,
        colorize=True,
    )


init_log()

# 기존 로그 UI 복원
log_container = st.empty()
log_records: list[str] = []
def log_received(msg):
    if config.ui.get("hide_log", False):
        return
    log_records.append(msg)
    if len(log_records) > 100:  # 로그 제한 (메모리 누수 방지)
        log_records.pop(0)
    log_container.code("\n".join(log_records))

# 예전처럼 전역으로 등록
logger.add(log_received)

locales = utils.load_locales(i18n_dir)


def tr(key):
    loc = locales.get(st.session_state["ui_language"], {})
    return loc.get(key, key)  # 키 없으면 키 자체 반환

# UI 컴포넌트 한글화
st.markdown("<h2>코인 키워드 입력</h2>", unsafe_allow_html=True)
keyword = st.text_input("코인 키워드 입력 (예: Bitcoin)", value="")

# 수정: video_count UI 입력 추가
video_count = st.number_input("생성할 영상 수", min_value=1, max_value=20, value=1)

# ElevenLabs 개인 음성 목록 불러오기 및 선택
voices_list = []
if voice.ElevenLabs is not None:
    try:
        client = voice.get_elevenlabs_client()
        # v2 /v2/voices 개인(personal) 음성만 조회
        resp = client.voices.search(include_total_count=True, voice_type="personal")
        voices_list = getattr(resp, "voices", [])  # Pydantic 모델의 속성
    except Exception as e:
        st.error(f"ElevenLabs 음성 목록을 불러올 수 없습니다: {e}. API 키를 확인하세요.")
        st.stop()  # 생성 중단
else:
    st.error("ElevenLabs 패키지가 설치되어 있지 않습니다. requirements.txt 확인.")
    st.stop()

if voices_list:
    # Pydantic Voice 객체는 .voice_id, .name 속성 사용
    voice_labels = [f"{v.name} ({v.voice_id})" for v in voices_list]
    selected_label = st.selectbox("ElevenLabs 음성 선택", options=voice_labels, key="eleven_voice")
    sel_idx = voice_labels.index(selected_label)
    selected_voice_id = voices_list[sel_idx].voice_id
else:
    selected_voice_id = config.app.get("voice_name", "")
    st.warning("사용 가능한 음성이 없습니다. 기본 음성을 사용합니다.")

if st.button("영상 생성", use_container_width=True, type="primary"):
    # 원래 로그 컨테이너 초기화
    log_records.clear()
    log_container.empty()

    if not keyword:
        st.error("키워드를 입력하세요.")
        scroll_to_bottom()
        st.stop()

    params = VideoParams(
        video_subject=keyword,
        video_language="ko-KR",
        paragraph_number=5,  # 예시
        voice_name=selected_voice_id,  # ElevenLabs에서 선택된 음성
        voice_rate=1.0,
        subtitle_enabled=True,
        font_name=config.app.get("font_names", ["NotoSansKR-Regular.ttf"])[0],
        subtitle_position="bottom",
        text_fore_color="#FFFFFF",
        font_size=60,
        stroke_color="#000000",
        stroke_width=1.5,
        bgm_type="random",
        bgm_volume=0.2,
        voice_volume=1.0,
        video_aspect=VideoAspect.portrait,
        video_concat_mode=VideoConcatMode.random,
        video_transition_mode=VideoTransitionMode.none,
        video_clip_duration=6,  # 수정: 모든 비디오 6초로 고정
        video_count=video_count,  # 수정: UI 입력 값 사용
        video_source="local",
        n_threads=2,
    )

    config.save_config()
    task_id = str(uuid4())

    st.toast("영상 생성 중...")  # 수정: 진행 상태 toast 강화
    logger.info("영상 생성 시작")
    logger.info(utils.to_json(params))
    scroll_to_bottom()

    try:
        result = tm.start(task_id=task_id, params=params)
    except Exception as e:
        st.error(f"영상 생성 실패: {e}")
        logger.error(f"영상 생성 실패: {e}")
        scroll_to_bottom()
        st.stop()

# 수정: result가 리스트이고, 각 항목에 "video" 키가 있는지 확인
    if not result or not isinstance(result, list) or not all("video" in item for item in result):
        st.error("영상 생성 실패")
        scroll_to_bottom()
        st.stop()

    video_files = [item["video"] for item in result]
    st.success("영상 생성 완료")
    try:
        if video_files:
            player_cols = st.columns(len(video_files) * 2 + 1)
            for i, url in enumerate(video_files):
                player_cols[i * 2 + 1].video(url)
    except Exception:
        pass
    open_task_folder(task_id)
    logger.info("영상 생성 완료")
    scroll_to_bottom()
    config.save_config()