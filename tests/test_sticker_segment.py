"""🧍 실사 사진 '사람만 오려내기' (sodam/stickerforge/segment.py · keying ai 모드).
색 키잉은 흰·검·단색 배경만 지운다 → 카페 같은 복잡한 실사 배경은 AI 세그로 사람만 따냄
(2026-11 송도현 카페 사진: '인물만 보이게' 했는데 통째로 스티커됨).
실제 rembg(ONNX)는 서버에서만 — 여기선 가짜 세그로 연결·폴백만 검증 (오프라인)."""
import io
import os
import tempfile

from PIL import Image

from fakes import runner

from sodam.stickerforge import keying, segment
from sodam.panels.sticker import _PERSON_CUT

test, run_all = runner()


def _photo(color=(240, 240, 240)):
    p = tempfile.mktemp(suffix=".png")
    Image.new("RGB", (60, 60), color).save(p)
    return p


@test
async def person_cut_matches_only_cutout_requests():
    for s in ("인물만 보이게 만들어줘", "배경 지우고 얼굴만", "누끼 따줘", "사람만 오려", "배경 제거해줘", "배경 투명하게"):
        assert _PERSON_CUT.search(s), s                       # 사람 따기 요청은 다 잡음
    for s in ("귀여운 스티커 만들어", "흔들리게 해줘", "글자 넣어줘", "네온 효과"):
        assert not _PERSON_CUT.search(s), s                   # 일반 요청은 안 잡음 (뮤테이션: 정규식 지우면 실패)


@test
async def key_image_ai_uses_segment_result():
    """keying ai 모드 → segment 결과(원본 보존 투명 PNG)를 그대로 쓴다."""
    orig = segment.segment_sync
    buf = io.BytesIO()
    Image.new("RGBA", (40, 40), (0, 200, 0, 120)).save(buf, "PNG")
    segment.segment_sync = lambda data: buf.getvalue()
    p = _photo()
    try:
        out, mode = keying.key_image(p, mode="ai")
        assert mode == "ai" and out.mode == "RGBA" and out.size == (40, 40)   # 세그 결과 사용 (뮤테이션: ai 분기 빼면 실패)
    finally:
        segment.segment_sync = orig
        os.remove(p)


@test
async def key_image_ai_falls_back_to_color_when_segment_fails():
    """세그가 꺼졌거나 터지면 색 키잉으로 폴백 (스티커가 아예 안 나오는 것보단 낫다)."""
    orig = segment.segment_sync

    def boom(data):
        raise RuntimeError("seg off")

    segment.segment_sync = boom
    p = _photo((255, 255, 255))                               # 흰 배경 → 색 키잉 white 로 폴백
    try:
        out, mode = keying.key_image(p, mode="ai")
        assert mode != "ai", mode                            # 폴백됨 (뮤테이션: except 지우면 터져서 실패)
    finally:
        segment.segment_sync = orig
        os.remove(p)


@test
async def sanitize_allows_ai_keying_mode():
    """spec keying 에 'ai' 가 허용돼야 소담이 쓸 수 있다."""
    from sodam.stickerforge import sanitize
    spec, err = sanitize({"mode": "photo", "keying": {"mode": "ai"}})
    assert err is None and spec["keying"]["mode"] == "ai", (err, spec)   # 뮤테이션: 화이트리스트에서 ai 빼면 실패
