"""🔌 원격 점검 창구 조회 (클로드 작업 환경 → 서버, 읽기 전용). 서버 쪽은 sodam/diag.py.

    python tools/diag.py health
    python tools/diag.py user q=@SAKE_LLL   (숫자 ID·@아이디·예전 @·이름 → 이름 기록·들어가 있는 방)
    python tools/diag.py rooms q=벳블리
    python tools/diag.py settings chat=벳블리
    python tools/diag.py messages chat=벳블리 hours=6 [q=소담] [user=123]
    python tools/diag.py agent_runs chat=벳블리 limit=20
    python tools/diag.py voice chat=벳블리 · modlog chat=… hours=24 · counters [chat=…] [day=YYYY-MM-DD]
    python tools/diag.py tables · logs unit=sodam lines=200 [grep=오류] [since='30 min ago']
    python tools/diag.py cleanup [chat=세컨드]   (🧹 멤버 정리 마지막 스캔 — 분류별·접속 상태 분포 숫자만)

토큰: 환경변수 SODAM_DIAG_TOKEN (오너가 봇 1:1 로 받아 클로드 환경 설정에 넣음 — 출력·저장 안 함).
주소: SODAM_DIAG_URL (기본 https://178-104-55-232.sslip.io).
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

URL = os.getenv("SODAM_DIAG_URL", "https://178-104-55-232.sslip.io").rstrip("/")


def _ctx() -> ssl.SSLContext:
    for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if os.getenv(k) and os.path.exists(os.environ[k]):
            return ssl.create_default_context(cafile=os.environ[k])
    if os.path.exists("/root/.ccr/ca-bundle.crt"):      # 클로드 작업 환경의 프록시 인증서
        return ssl.create_default_context(cafile="/root/.ccr/ca-bundle.crt")
    return ssl.create_default_context()


def get(route: str, **params) -> tuple[int, dict]:
    tok = os.getenv("SODAM_DIAG_TOKEN", "").strip()
    if not tok:
        sys.exit("SODAM_DIAG_TOKEN 없음 — 오너가 봇 1:1 로 받은 토큰을 클로드 환경변수에 넣어야 해요")
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(f"{URL}/v1/{route}" + (f"?{q}" if q else ""), headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ctx()) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {"error": e.reason}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    params = dict(a.split("=", 1) for a in argv[1:] if "=" in a)
    status, body = get(argv[0], **params)
    print(json.dumps(body, ensure_ascii=False, indent=1))
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
