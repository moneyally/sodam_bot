#!/usr/bin/env python3
"""배너 spec(JSON) → 100×100 타일 PNG 들 + preview.png (tools/emoji_pack.py 재료).

사용: python tools/make_tiles.py SPEC.json OUTDIR
  SPEC = sodam/stickerforge/banner.py 의 spec. "image_files":[경로...] 로 로고·캐릭터 넣으면 image 레이어가 씀.
그다음: python tools/emoji_pack.py --dir OUTDIR --name 이름 --title 제목 --user 오너ID
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from sodam.stickerforge import banner as B


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    outdir = Path(sys.argv[2])
    outdir.mkdir(parents=True, exist_ok=True)
    spec["images"] = [Image.open(p).convert("RGBA") for p in spec.pop("image_files", [])]
    r = B.build_tiles(spec)
    for i, t in enumerate(r["tiles"]):
        t.save(outdir / f"{i:02d}.png")
    r["preview"].save(outdir / "preview.png")
    print(f"타일 {r['count']}개 ({r['cols']}×{r['rows']}) → {outdir}")
    print(f"재조립 오차: {r['qa_error']} (0 이어야 이음새 없음)")
    print(f"다음: python tools/emoji_pack.py --dir {outdir} --name <이름> --title <제목> --user <오너ID>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
