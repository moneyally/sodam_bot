"""🎬 AI 영상 만들기 — 제공자 어댑터 (Google Veo 3.1 · xAI Grok Imagine Video). 키가 있을 때만 켜짐.

오너 결정 2026-09-30: 키는 오너가 직접 받음, 방마다 한 주 6개(video_weekly, 한국시간 월요일 0시 초기화). 도구·배경 작업·한도는 `panels/videogen.py`.

켜는 법 (.env, 비밀값 — git 에 올리지 말 것):
- `GEMINI_API_KEY` = Google AI Studio 키 (결제 연결 필요 — Veo 는 무료 등급 없음) → Veo 3.1 Lite (720p, 소리 포함)
- `XAI_API_KEY` = xAI 콘솔 키 → grok-imagine-video
- 둘 다 있으면 `VIDEO_PROVIDER=gemini|xai` 로 고름 (없으면 gemini 먼저). `VIDEO_MODEL` 로 모델 바꿈 (그 회사 모델 이름일 때만).

공식 문서 (2026-09-30 확인, 추측 없음):
- Veo REST: https://ai.google.dev/gemini-api/docs/veo (md: …/veo.md.txt)
  POST {BASE}/models/{model}:predictLongRunning  헤더 x-goog-api-key
    {"instances":[{"prompt", "image":{"bytesBase64Encoded","mimeType"}}], "parameters":{"durationSeconds":4|6|8,"aspectRatio":"16:9"|"9:16","personGeneration"}}
    → {"name": "models/…/operations/…"}
  GET {BASE}/{name} → {"done": true, "response":{"generateVideoResponse":{"generatedSamples":[{"video":{"uri"}}],
    "raiMediaFilteredCount","raiMediaFilteredReasons"}}, "error":{code,message}}
  uri 는 같은 키 헤더로 내려받음(리다이렉트). 이미지 입력 모양·int durationSeconds 는 공식 SDK(googleapis/python-genai
  models.py `_Image_to_mldev` → bytesBase64Encoded/mimeType, types.py duration_seconds: int) 그대로.
  image-to-video 는 personGeneration "allow_adult" 만 (문서 표). 요금 https://ai.google.dev/gemini-api/docs/pricing :
  Lite 720p $0.05/초 · Fast $0.10 · Standard $0.40 — **만들어진 영상만 청구** (막힌 건 청구 안 함).
- xAI: https://docs.x.ai/developers/model-capabilities/video/generation · https://docs.x.ai/developers/rest-api-reference/inference/videos
  POST https://api.x.ai/v1/videos/generations (Bearer) {"model","prompt","duration":1~15,"aspect_ratio","resolution","image":{"url": data URI}}
    → {"request_id"}
  GET https://api.x.ai/v1/videos/{request_id} → {"status": pending|done|expired|failed, "video":{"url","duration","respect_moderation"},
    "error":{"code","message"}} (moderation 으로 막히면 respect_moderation=false·url 빔, 또는 error.code invalid_argument)
  요금 https://docs.x.ai/developers/models/grok-imagine-video ($0.05/초) · …/grok-imagine-video-1.5 ($0.08/초) ·
  …/grok-imagine-video-1.5-lite ($0.02/초, 글·사진 → 영상만). 해상도는 값에 영향 없음(모델·초로만 청구, 480p 는 빠르기만).
  영상 고치기 POST /v1/videos/edits {"model","prompt","video":{"url"}} (결과 = 원본 길이, 최대 720p) ·
  이어 붙이기 POST /v1/videos/extensions {"model","prompt","video":{"url"},"duration"(늘릴 초만)} — 영상 입력은 grok-imagine-video 만
  (모델 페이지 'text, image, video → video'). 둘 다 같은 GET /v1/videos/{id} 로 기다림 (2026-10-03 문서 확인).

키는 로그·오류 글에 절대 안 남김(`redact`). 다운로드 주소가 다른 호스트로 넘어가면 키 헤더를 떼고 따라감.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger(__name__)

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
XAI_BASE = "https://api.x.ai/v1"
GEMINI_DEFAULT = "veo-3.1-lite-generate-preview"
XAI_DEFAULT = "grok-imagine-video"
XAI_VIDEO_IN = "grok-imagine-video"     # 영상을 입력으로 받는 모델 (고치기·이어 붙이기)
MAX_IN_BYTES = 20 * 1024 * 1024         # 봇이 받을 수 있는 파일 = 우리가 넘길 수 있는 원본 영상
TIMEOUT_SEC = float(os.getenv("VIDEO_TIMEOUT_SEC", "") or 300)   # 시작→완성 전체 (Veo 문서: 보통 11초~최대 6분)
POLL_GEMINI = 10.0      # 문서 예제 간격
POLL_XAI = 5.0
MAX_BYTES = 50 * 1024 * 1024   # 봇이 보낼 수 있는 파일 한도 (Bot API sendVideo 50MB)
TRANSPORT: httpx.AsyncBaseTransport | None = None   # 테스트가 httpx.MockTransport 를 끼움


class VideoError(Exception):
    """kind: policy(안전 정책) · timeout · auth(키 틀림) · quota(요금·속도 한도) · failed(그 밖)."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(kind, detail)
        self.kind, self.detail = kind, detail


def _keys() -> list[str]:
    return [k for k in (os.getenv("GEMINI_API_KEY", "").strip(), os.getenv("XAI_API_KEY", "").strip()) if k]


def redact(text: str) -> str:
    """오류 글에서 키를 지움 (로그·사용자 안내에 절대 안 나가게)."""
    text = str(text)
    for k in _keys():
        text = text.replace(k, "***")
    return text[:500]


_POLICY_WORDS = ("safety", "blocked", "policy", "moderat", "prohibited", "responsible ai", "sensitive", "violat")


def _is_policy(msg: str) -> bool:
    low = msg.lower()
    return any(w in low for w in _POLICY_WORDS)


def _http_error(r: httpx.Response) -> VideoError:
    try:
        body = r.text
    except Exception:
        body = ""
    msg = redact(f"HTTP {r.status_code} {body[:300]}")
    if r.status_code in (401, 403):
        return VideoError("auth", msg)
    if r.status_code in (402, 429) or "RESOURCE_EXHAUSTED" in body or "quota" in body.lower() or "billing" in body.lower():
        return VideoError("quota", msg)
    if r.status_code == 400 and _is_policy(body):
        return VideoError("policy", msg)
    return VideoError("failed", msg)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), transport=TRANSPORT, follow_redirects=False)


async def _download(c: httpx.AsyncClient, url: str, auth: dict[str, str], auth_host: str) -> bytes:
    """스트리밍으로 받되 MAX_BYTES 넘으면 끊음. 리다이렉트는 직접 따라가며 키 헤더는 auth_host 에만."""
    for _ in range(5):
        host = urlsplit(url).hostname or ""
        headers = auth if host == auth_host else {}
        async with c.stream("GET", url, headers=headers) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code >= 400:
                await r.aread()
                raise _http_error(r)
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_BYTES:
                    raise VideoError("failed", "영상 파일이 50MB 를 넘음")
            if not buf:
                raise VideoError("failed", "빈 영상")
            return bytes(buf)
    raise VideoError("failed", "리다이렉트가 너무 많음")


@dataclass(frozen=True)
class Provider:
    name: str            # gemini · xai
    key: str
    model: str
    seconds: tuple[int, ...]   # 고를 수 있는 길이
    aspects: tuple[str, ...]

    @property
    def label(self) -> str:
        return {"gemini": "Google Veo", "xai": "xAI Grok Imagine"}[self.name]

    def clamp_seconds(self, want: int) -> int:
        """want 이하 중 가장 긴 길이 (없으면 가장 짧은 것) — 비용이 요청보다 커지지 않게."""
        ok = [s for s in self.seconds if s <= want]
        return max(ok) if ok else min(self.seconds)

    def aspect(self, want: str | None) -> str | None:
        return want if want in self.aspects else None

    async def generate(self, prompt: str, image: tuple[bytes, str] | None, seconds: int, aspect: str | None) -> bytes:
        deadline = time.monotonic() + TIMEOUT_SEC
        async with _client() as c:
            if self.name == "gemini":
                return await self._gemini(c, prompt, image, seconds, aspect, deadline)
            return await self._xai(c, prompt, image, seconds, aspect, deadline)

    # ── Google Veo (Gemini API) ──
    async def _gemini(self, c, prompt, image, seconds, aspect, deadline) -> bytes:
        auth = {"x-goog-api-key": self.key}
        inst: dict = {"prompt": prompt}
        params: dict = {"durationSeconds": int(seconds)}
        if image:
            inst["image"] = {"bytesBase64Encoded": base64.b64encode(image[0]).decode(), "mimeType": image[1]}
            params["personGeneration"] = "allow_adult"   # image-to-video 는 allow_adult 만 (문서 표)
        if aspect:
            params["aspectRatio"] = aspect
        r = await c.post(f"{GEMINI_BASE}/models/{self.model}:predictLongRunning", headers=auth,
                         json={"instances": [inst], "parameters": params})
        if r.status_code >= 400:
            raise _http_error(r)
        name = (r.json() or {}).get("name")
        if not name:
            raise VideoError("failed", "작업 이름이 없음")
        while True:
            if time.monotonic() > deadline:
                raise VideoError("timeout", name)
            await asyncio.sleep(POLL_GEMINI)
            r = await c.get(f"{GEMINI_BASE}/{name}", headers=auth)
            if r.status_code >= 400:
                if r.status_code >= 500:
                    continue   # 잠깐 장애 → 다음 확인에서 다시
                raise _http_error(r)
            d = r.json() or {}
            if d.get("done"):
                break
        if d.get("error"):
            msg = redact(str(d["error"].get("message", "")))
            raise VideoError("policy" if _is_policy(msg) else "failed", msg)
        resp = (d.get("response") or {}).get("generateVideoResponse") or {}
        samples = resp.get("generatedSamples") or []
        if not samples:
            if resp.get("raiMediaFilteredCount") or resp.get("raiMediaFilteredReasons"):
                raise VideoError("policy", redact(str(resp.get("raiMediaFilteredReasons") or "")))
            raise VideoError("failed", "영상이 없음")
        video = samples[0].get("video") or {}
        if video.get("encodedVideo"):
            return base64.b64decode(video["encodedVideo"])
        if not video.get("uri"):
            raise VideoError("failed", "영상 주소가 없음")
        return await _download(c, video["uri"], auth, urlsplit(GEMINI_BASE).hostname)

    @property
    def edits(self) -> bool:
        """영상 고치기·이어 붙이기가 되는 제공자 (xAI 만)."""
        return self.name == "xai"

    async def edit(self, video_mp4: bytes, prompt: str) -> bytes:
        return await self._xai_video("edits", {"prompt": prompt}, video_mp4)

    async def extend(self, video_mp4: bytes, prompt: str, seconds: int) -> bytes:
        return await self._xai_video("extensions", {"prompt": prompt, "duration": int(seconds)}, video_mp4)

    async def _xai_video(self, path: str, body: dict, video_mp4: bytes) -> bytes:
        if not self.edits:
            raise VideoError("unsupported", self.name)
        if len(video_mp4) > MAX_IN_BYTES:
            raise VideoError("failed", "원본 영상이 너무 큼")
        deadline = time.monotonic() + TIMEOUT_SEC
        auth = {"Authorization": f"Bearer {self.key}"}
        body = {"model": XAI_VIDEO_IN, **body,
                "video": {"url": "data:video/mp4;base64," + base64.b64encode(video_mp4).decode()}}
        async with _client() as c:
            r = await c.post(f"{XAI_BASE}/videos/{path}", headers=auth, json=body)
            return await self._xai_wait(c, r, auth, deadline)

    # ── xAI Grok Imagine Video ──
    async def _xai(self, c, prompt, image, seconds, aspect, deadline) -> bytes:
        auth = {"Authorization": f"Bearer {self.key}"}
        body: dict = {"model": self.model, "prompt": prompt, "duration": int(seconds), "resolution": "720p"}
        if aspect:
            body["aspect_ratio"] = aspect
        if image:
            body["image"] = {"url": f"data:{image[1]};base64,{base64.b64encode(image[0]).decode()}"}
        r = await c.post(f"{XAI_BASE}/videos/generations", headers=auth, json=body)
        return await self._xai_wait(c, r, auth, deadline)

    async def _xai_wait(self, c, r, auth: dict, deadline: float) -> bytes:
        """시작 응답 r → request_id 로 끝날 때까지 기다려 영상 bytes (만들기·고치기·이어 붙이기 공통)."""
        if r.status_code >= 400:
            raise _http_error(r)
        rid = (r.json() or {}).get("request_id")
        if not rid:
            raise VideoError("failed", "request_id 없음")
        while True:
            if time.monotonic() > deadline:
                raise VideoError("timeout", rid)
            await asyncio.sleep(POLL_XAI)
            r = await c.get(f"{XAI_BASE}/videos/{rid}", headers=auth)
            if r.status_code >= 400:
                if r.status_code >= 500:
                    continue
                raise _http_error(r)
            d = r.json() or {}
            status = d.get("status")
            if status == "done":
                break
            if status in ("failed", "expired"):
                err = d.get("error") or {}
                msg = redact(f"{err.get('code', status)} {err.get('message', '')}")
                raise VideoError("policy" if _is_policy(msg) else "failed", msg)
        video = d.get("video") or {}
        if video.get("respect_moderation") is False or not video.get("url"):
            raise VideoError("policy", "moderation")
        host = urlsplit(XAI_BASE).hostname
        return await _download(c, video["url"], auth, host)   # vidgen.x.ai 등 다른 호스트엔 키 안 보냄


# 🧭 영상 모델 라우터 (오너 결정 2026-10-03 '한 모델 말고 라우터로'): xAI 글·사진 → 영상은 기본 싼 lite($0.02/초),
# 화질을 콕 집어 말하면 기본 모델($0.05/초). 비싼 1.5($0.08)는 안 씀. 고치기·이어 붙이기는 영상 입력이 되는 기본 모델만.
# .env VIDEO_MODEL 을 직접 적으면 라우터 끔 (그 모델 고정).
XAI_CHEAP = "grok-imagine-video-1.5-lite"
_QUALITY = re.compile(r"고화질|화질|퀄리티|고퀄|영화(처럼|같이|급)|시네마|cinematic|디테일|정교|고급스럽|실사|사실적|리얼하게|"
                      r"\b(hd|4k|1080p?|high quality|realistic|detailed)\b|광고(용|처럼)|프로(급|처럼)", re.I)


def route_model(prov: Provider, request: str) -> tuple[Provider, str]:
    """(이번에 쓸 제공자, 이유). xAI·VIDEO_MODEL 안 정했을 때만 고름."""
    if prov.name != "xai" or os.getenv("VIDEO_MODEL", "").strip():
        return prov, "fixed"
    if _QUALITY.search(request or ""):
        return replace(prov, model=XAI_DEFAULT), "quality"
    return replace(prov, model=XAI_CHEAP), "cheap"


def active() -> Provider | None:
    """지금 쓸 제공자 (.env 를 부를 때마다 읽음 — 키를 넣고 재시작만 하면 켜짐). 키가 없으면 None = 기능 꺼짐."""
    g = os.getenv("GEMINI_API_KEY", "").strip()
    x = os.getenv("XAI_API_KEY", "").strip()
    pick = os.getenv("VIDEO_PROVIDER", "").strip().lower()
    model = os.getenv("VIDEO_MODEL", "").strip()
    order = ["xai", "gemini"] if pick in ("xai", "grok") else ["gemini", "xai"]
    for name in order:
        if name == "gemini" and g:
            m = model if model.startswith("veo-") else GEMINI_DEFAULT
            return Provider("gemini", g, m, (4, 6, 8), ("16:9", "9:16"))
        if name == "xai" and x:
            m = model if model.startswith("grok-") else XAI_DEFAULT
            return Provider("xai", x, m, tuple(range(1, 16)), ("16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3"))
    return None
