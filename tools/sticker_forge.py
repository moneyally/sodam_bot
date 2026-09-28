"""스티커 공방 명령줄 (운영자·Claude 가 직접 만들어 보고 눈으로 확인할 때). AI 호출 없음.

python tools/sticker_forge.py IMAGE '{"motion":["idle"],"fx":["sparkle"],"caption":"안녕"}' out.webm [--preview p.png] [--icon icon.webm]
spec 은 소담 AI 도구와 같은 sanitize 를 거침 → 검사표(PASS/FAIL)를 찍고, 실패면 종료 코드 1.
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sodam import stickerforge as SF  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("spec", help="JSON 문자열 또는 .json 파일")
    ap.add_argument("out")
    ap.add_argument("--preview")
    ap.add_argument("--icon")
    a = ap.parse_args()
    raw = json.load(open(a.spec, encoding="utf-8")) if a.spec.endswith(".json") else json.loads(a.spec)
    spec, err = SF.sanitize(raw)
    if err:
        print("spec 오류:", err)
        return 1
    print("spec:", json.dumps(spec, ensure_ascii=False))
    res = asyncio.run(SF.forge(open(a.image, "rb").read(), spec, icon=bool(a.icon)))
    for name, val, ok in res.rows:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<12} {val}")
    print("  ->", "READY" if res.ok else f"NOT READY {res.error}", f"(배경 {res.keying})")
    if res.webm:
        open(a.out, "wb").write(res.webm)
    if a.preview and res.preview:
        open(a.preview, "wb").write(res.preview)
    if a.icon and res.icon:
        open(a.icon, "wb").write(res.icon)
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
