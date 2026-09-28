"""Canvas and timing constants shared by every module.

89 frames at 30 fps is 2.9667 s. Telegram caps stickers at 3.000 s and an exact
3.000 s file can round up to 3.033 s in the container and get rejected, so the
loop stops one frame short on purpose.
"""
S = 512            # sticker canvas
FPS = 30
NF = 89
D = NF / FPS       # loop length in seconds; every periodic motion uses multiples of 1/D
MASTER = 1024      # working resolution of the source before the single 1024->512 resample


def ffmpeg() -> str:
    """imageio-ffmpeg 정적 바이너리(libvpx-vp9 포함) → 없으면 PATH 의 ffmpeg (sodam.avatar 와 같은 규칙)."""
    from ..avatar import ffmpeg_path
    path = ffmpeg_path()
    if not path:
        raise RuntimeError("ffmpeg 없음")
    return path
