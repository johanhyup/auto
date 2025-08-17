from moviepy.video.VideoClip import VideoClip  # 수정: Clip 정의
from moviepy.video.fx import all as vfx  # 수정: vfx를 정확한 경로로 가져옴


# FadeIn
def fadein_transition(clip: VideoClip, t: float) -> VideoClip:
    return clip.with_effects([vfx.FadeIn(t)])


# FadeOut
def fadeout_transition(clip: VideoClip, t: float) -> VideoClip:
    return clip.with_effects([vfx.FadeOut(t)])


# SlideIn
def slidein_transition(clip: VideoClip, t: float, side: str) -> VideoClip:
    return clip.with_effects([vfx.SlideIn(t, side)])


# SlideOut
def slideout_transition(clip: VideoClip, t: float, side: str) -> VideoClip:
    return clip.with_effects([vfx.SlideOut(t, side)])
