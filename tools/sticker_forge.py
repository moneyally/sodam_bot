"""스티커 공방 명령줄 (운영자·Claude 가 직접 만들어 보고 눈으로 확인할 때). AI 호출 없음.

python tools/sticker_forge.py IMAGE '{"motion":["idle"],"fx":["sparkle"],"caption":"안녕"}' out.webm [--preview p.png] [--icon icon.webm]
python tools/sticker_forge.py IMAGE '{"recipe":"출근_번개잽","seed":3,"caption":"출근완료"}' out.webm
python tools/sticker_forge.py IMAGE '{"mode":"photo","motion":[{"type":"punch","hits":2}],"fx":["sweep","sparkle","glitch"]}' ump.mp4 --mp4
python tools/sticker_forge.py --catalog '출근완료 강렬하게' [--kind glow]
spec 은 소담 AI 도구와 같은 sanitize 를 거침 → 검사표(PASS/FAIL) + qc 경고를 찍고, 실패면 종료 코드 1.
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
    ap.add_argument("image", nargs="?")
    ap.add_argument("spec", nargs="?", help="JSON 문자열 또는 .json 파일")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--preview")
    ap.add_argument("--icon")
    ap.add_argument("--mp4", action="store_true", help="움프(640 H.264 6초) 로")
    ap.add_argument("--catalog", metavar="QUERY", help="레시피 후보·부품 목록만 보고 끝")
    ap.add_argument("--kind", choices=["cutout", "photo", "glow", "mono"])
    a = ap.parse_args()
    if a.catalog:
        from sodam.panels.sticker import catalog_text
        print(catalog_text(a.catalog, a.kind))
        return 0
    if not (a.image and a.spec and a.out):
        ap.error("IMAGE SPEC OUT 이 필요 (또는 --catalog)")
    raw = json.load(open(a.spec, encoding="utf-8")) if a.spec.endswith(".json") else json.loads(a.spec)
    if a.mp4:
        raw.setdefault("mode", "photo"); raw.setdefault("radius", 0)
    spec, err = SF.sanitize(raw)
    if err:
        print("spec 오류:", err)
        return 1
    print("spec:", json.dumps(spec, ensure_ascii=False))
    image = open(a.image, "rb").read()
    res = asyncio.run(SF.forge_video(image, spec) if a.mp4 else SF.forge(image, spec, icon=bool(a.icon)))
    for name, val, ok in res.rows:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<12} {val}")
    print("  ->", "READY" if res.ok else f"NOT READY {res.error}", f"(배경 {res.keying})")
    print("  지표:", res.metrics)
    for w in res.warnings:
        print("  ⚠", w)
    data = res.mp4 if a.mp4 else res.webm
    if data:
        open(a.out, "wb").write(data)
    if a.preview and res.preview:
        open(a.preview, "wb").write(res.preview)
    if a.icon and res.icon:
        open(a.icon, "wb").write(res.icon)
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
