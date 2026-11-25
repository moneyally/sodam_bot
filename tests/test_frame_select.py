"""🎬 영상·GIF 원본에서 '어느 장면'을 쓸지 고르기 — python tests/run_all.py frame_select

2026-11-25 오너: 영상에 답글+사진/스티커/움프 만들 때 늘 가운데 프레임만 썼음 → 시작·끝·N초·N%·N번째 등
어느 장면이든 고를 수 있게(부품+값 틀). make_image·make_sticker·make_profile_video 공통(vision.Attached.pick_frame)."""
from types import SimpleNamespace

from fakes import fake_user, runner

from sodam.vision import Attached

test, run_all = runner()

# 장면 4개: (초, 이미지바이트). data(기본) 는 일부러 프레임과 다른 값으로 둬서 '선택이 실제로 바뀌는지' 확인
FRAMES = [(0.0, b"F0"), (1.0, b"F1"), (2.0, b"F2"), (3.0, b"F3")]


def vid(data=b"MID"):
    return Attached(data, "image/jpeg", frames=list(FRAMES))


@test
async def start_middle_end():
    a = vid()
    assert a.pick_frame("시작") == b"F0" and a.pick_frame("처음") == b"F0" and a.pick_frame("start") == b"F0"
    assert a.pick_frame("끝") == b"F3" and a.pick_frame("마지막") == b"F3" and a.pick_frame("end") == b"F3"
    assert a.pick_frame("가운데") == b"F2" and a.pick_frame("중간") == b"F2"   # len//2 = 2


@test
async def nth_frame():
    a = vid()
    assert a.pick_frame("1번째") == b"F0"
    assert a.pick_frame("2번째") == b"F1"
    assert a.pick_frame("4번째") == b"F3"
    assert a.pick_frame("99번째") == b"F3"      # 범위 넘으면 마지막


@test
async def percent():
    a = vid()
    assert a.pick_frame("0%") == b"F0"
    assert a.pick_frame("100%") == b"F3"
    assert a.pick_frame("150%") == b"F3"        # 넘치면 마지막 (clamp)


@test
async def seconds_closest():
    a = vid()
    assert a.pick_frame("0초") == b"F0"
    assert a.pick_frame("1초") == b"F1"
    assert a.pick_frame("3초") == b"F3"
    assert a.pick_frame("2.4초") == b"F2"        # 2.0 에 더 가까움
    assert a.pick_frame("10초") == b"F3"         # 길이 넘으면 가장 가까운(끝)


@test
async def default_and_no_frames():
    a = vid()
    assert a.pick_frame(None) == b"MID"          # 안 고르면 원래(가운데) 그대로
    assert a.pick_frame("아무장면이나") == b"MID"  # 못 알아들으면 원래
    assert Attached(b"ONLY", "image/jpeg", frames=[]).pick_frame("끝") == b"ONLY"  # 프레임 없으면 원래


@test
async def use_frame_mutates_data():
    a = vid()
    ret = a.use_frame("끝")
    assert ret is a and a.data == b"F3"          # data 가 고른 장면으로 바뀜 (편집 원본)
    a.use_frame(None)                            # None 이면 그대로(이미 F3)
    assert a.data == b"F3"


@test
async def source_photo_uses_selected_frame():
    from sodam.panels.avatar import source_photo
    ctx = SimpleNamespace(image=vid(), bot=None, caller=fake_user(5))
    data, err = await source_photo(ctx, {"frame": "끝"})
    assert err is None and data == b"F3"         # 스티커·움프 공통 경로도 고른 장면 사용
    ctx2 = SimpleNamespace(image=vid(), bot=None, caller=fake_user(5))
    data2, _ = await source_photo(ctx2, {})      # frame 안 주면 가운데(기본)
    assert data2 == b"MID"


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_all())
