---
name: telegram-sticker-forge
description: Turn any image plus a free-form request into a verified Telegram video sticker (512x512 VP9 WebM, transparent, under 3 s and 256 KB), a 100x100 pack icon, or a premium emoji. Handles both transparent cut-outs (mascots, illustrations, dark glow art) and full-frame photos, token-streaming Korean/English captions, and a catalog of stackable motions and effects (glitch, sparkle, recoil, slam, sweep, shockwave...). Use this whenever someone asks for a Telegram sticker, sticker pack, animated/video sticker, .webm sticker, 스티커, 움직이는 스티커, 텔레그램 스티커, 팩 아이콘, 프리미엄 이모지, wants a picture "animated" for Telegram, or reports that @Stickers rejected a file (too big, wrong size, no transparency, "title is unacceptable"). Also use it to add motion, glitch, text or effects to an existing sticker image.
---

# Telegram Sticker Forge

> **소담 봇에 붙인 위치 (2026-09-28)** — 엔진은 `sodam/stickerforge/` (받은 stickerlib 그대로 + 아래만 바꿈), 글꼴
> `sodam/data_files/fonts/BlackHanSans.ttf`(OFL). 이 문서의 `scripts/forge.py` 대신 **`python tools/sticker_forge.py IMAGE 'SPEC-JSON' out.webm --preview p.png [--icon i.webm]`**.
> - ffmpeg = imageio-ffmpeg 정적 바이너리(libvpx-vp9 포함), ffprobe 대신 `ffmpeg -i` 로 검사. 설치: `pip install -r requirements.txt` (numpy·scipy·imageio-ffmpeg) — apt 필요 없음.
> - 인코딩 `-deadline good -cpu-used 1` (best/0 과 PSNR 같음 27.47 vs 27.48, 44초 → 4초). 한 장 총 ~10초(그리기 6초).
> - 소담 AI 도구 `make_sticker(spec, icon)` (`sodam/panels/sticker.py`): AI 가 이 문서의 매핑대로 spec 을 디자인 → `stickerforge.sanitize`
>   (이름은 목록만, 값은 그 함수 인자 이름 + 숫자·참거짓·숫자 목록만, 범위는 아래 '하얗게 날아가지 않는' 한도로 자름, **파일 경로를 받는
>   rain/rise·font 는 막음**) → 검사표 전부 PASS 여야 보냄, 실패면 효과 하나 덜고 한 번 더 → 스티커 + 파일(@Stickers 등록용) 전송.
>   원본은 붙은·답장한 사진, 없으면 요청자 프사. 사람마다 하루 5개. 테스트 `python tests/run_all.py sticker`.

You are the sticker maker. A customer sends an image and says what they want in
plain language ("배경 빼고 글리치 넣어서 출근완료 스티커", "make it shake when the
gun fires", "잘자요 느낌으로 잔잔하게"). Your job is to translate that into a spec,
render it with `scripts/forge.py`, look at the result, and hand back a file that
passes every Telegram rule the first time.

Nothing here is hard-coded to one mascot or one phrase. Every motion, effect,
caption and keying choice is a parameter; your judgement picks the values.

## Setup (once per machine)

```bash
pip install pillow numpy scipy
apt-get install -y ffmpeg            # needs libvpx-vp9: ffmpeg -encoders | grep vp9
```

Korean caption font is bundled (`assets/fonts/BlackHanSans.ttf`, OFL). Pass
`--font` or `"font"` in the spec for another face — captions need a heavy
display weight; regular UI fonts vanish at sticker size.

## Workflow

1. **Read the image before deciding anything.** Look at it. Answer: is the
   subject a cut-out (mascot, logo, illustration with a plain background) or a
   photo that should fill the frame? Is the background white / black / a flat
   colour / already transparent? Is it *dark art with glowing effects on black*
   (needs `glow` keying, auto can't tell)? Where is the subject's face, the
   muzzle of a gun, the thing that should flash? Does the image already contain
   text (then don't add a caption on top of it)?

2. **Translate the request into a spec.** Use the mapping tables below and
   `references/catalog.md`. Prefer a combination of 1 motion + 2–3 effects that
   *fits the picture*, not the loudest thing available. When a customer asks for
   several stickers, give each a distinct motion/effect combo so the pack reads
   as a set. When they ask for "something different from last time", change the
   motion family, not just a parameter.

3. **Render.** Quick path for simple asks, JSON spec for anything with
   parameters (see `references/spec-schema.md`):

   ```bash
   python3 scripts/forge.py IMAGE -o out.webm --motion "idle,punch:hits=2" \
           --fx "sparkle,sweep" --caption "안녕하세요" --preview preview.png
   python3 scripts/forge.py spec.json --preview preview.png [--icon icon.webm] [--emoji emoji.webm]
   ```

4. **Look at `preview.png`** (8 frames on grey). Check: no part of the body
   became transparent (dark clothes keyed away), the caption never touches the
   subject's face or the frame edge, impact frames aren't washed out, the last
   frame matches the first (seamless loop) unless the motion is a drop-in.
   Fix and re-render rather than shipping something you haven't seen.

5. **Read the check table.** `forge.py` decodes the output and prints PASS/FAIL
   per rule (dimensions, codec, fps, duration, size, alpha_mode, alpha_range,
   audio, holes, glyphs). Exit code 1 means not shippable. `holes` counts
   transparent pockets enclosed by opaque pixels; for `glow`-keyed art it is
   skipped because the haze legitimately has gaps.

6. **Deliver.** Send the `.webm` as a *file/document*, never as a video (Telegram
   re-encodes videos and the result fails). For a pack, also offer a 100x100 icon
   (`--icon`). Tell the customer the registration steps only if they ask
   (`references/telegram.md`).

## Request → spec mapping

### Source type

| Customer says / image looks like | mode | keying |
|---|---|---|
| mascot, character, logo, sticker art with plain white/black/green background | `cutout` | `auto` (detects white/black/colour) |
| dark character with light aura, chains, sparks on black | `cutout` | `glow` |
| PNG already transparent | `cutout` | `auto` → none |
| photograph, screenshot, meme, anything that should fill the square | `photo` | – (rounded corners via `radius`) |

If auto keying eats part of the subject (white hair on white, black coat on
black) raise/lower `keying.tol` or switch mode; the preview shows it.

### Motion (pick one family, optionally stack a subtle second)

| Feeling asked for | motion |
|---|---|
| calm, idle, breathing, sleeping, night, 잔잔하게 | `breathe` or `idle` (+ `zzz`/`meteors` fx) |
| floating, space, dreamy, 둥둥 | `float` |
| emphasis, beat, 강조, 펀치, "pop on the beat" | `punch` (2–3 hits) |
| excited, nervous, 덜덜, vibrating | `shake` |
| cute jelly, 흔들흔들, cartoon | `wobble` |
| jumping, happy, 신나게 | `hop` |
| appears/disappears, 짠!, entrance | `pop` |
| impact, arrival, 쿵, dramatic entrance | `slam` |
| gun, shooting, kickback, 총 반동 | `recoil` + `flash` at the muzzle + `flashbang` + `glitch on_hits` |
| thumbs up, punch forward, 잽, 찌르기, "strike" | `jab` + `shockwave` + `bolts`/`slice_glitch` |
| photo, no character | `zoom` (default), `punch`, `shake`, `pan` |

Impulse motions (`recoil`, `jab`, `slam`) expose hit times; hit-synced effects
(`flash`, `flashbang`, `shockwave`, `bolts`, `aura` flare, `glitch on_hits`,
`slice_glitch`) fire on them automatically.

### Effects (stack 1–3; order = draw order)

| Asked for | fx |
|---|---|
| glitch, 글리치, digital, broken | `glitch` (colourful art) / `slice_glitch` (monochrome or high-contrast art — full-frame channel split turns those into rainbow fog) |
| sparkle, 반짝, stars, 별 | `sparkle` (thin with `subset`) |
| light passing, shine, 광채, 빛 스윕 | `sweep` |
| sending, uploading, scanning, 전송 | `scan` |
| holy, entrance, sunburst, 빛살 | `rays` |
| sticker border, cut-out look, 흰 테두리 | `outline` |
| neon, soft glow, 은은한 빛 | `glow` |
| lightning aura, 번개, electric | `aura` (glow-keyed art) + `bolts` |
| love, 하트 | `hearts` |
| coins/tokens/logos falling or rising | `rain` / `rise` with `image=` |
| shooting stars, night sky | `meteors` |
| sleeping, zzz | `zzz` (set `origin` near the head) |

### Caption (token-streaming text)

Letters type in one at a time (0.135 s apart) with outline + 3D + gradient.
`anims`: `bounce` (letters pop in), `punch` (line scales once when typed),
`shine` (sweep), `wave`, `shake`, `glow`. Palettes: `gold` (default), `silver`,
`ice`, `fire`, `pink`, `neon`, `white`, or explicit `top/mid/bottom/extrude`.
Keep captions 2–7 characters; longer text shrinks and loses punch. In cut-out
mode the subject is automatically seated above the caption band so they never
overlap; in photo mode the caption sits over the bottom of the picture, so
prefer photos with a plain lower quarter. If the artwork already has text
baked in, don't add another caption.

## Judgement calls that matter

- **Never cut the character into parts** to animate a hand or arm. Cut edges tear
  fur and outlines and customers notice instantly. Animate the whole body and
  let effects carry the energy.
- **Less motion = more quality** under a tight byte budget. If a render only
  fits at the bottom of the bitrate ladder (crf 40+), thin the sparkles,
  drop one effect, or reduce motion amplitude before accepting mush.
- **Loop hygiene.** Everything periodic is a whole number of cycles per loop.
  Impulse motions must finish decaying ≥0.6 s before the end. `slam`/`pop` are
  intentionally "re-entry" loops — say so when delivering.
- **Impact frames** should never white out the whole picture: keep `aura`
  flare ≤0.35, `flashbang` amount ≤60, glitch `amp` ≤7.
- **Dark art on black** (`glow` keying): the body stays opaque, the haze becomes
  semi-transparent, so it works on light chat themes too. Explain that the
  `holes` check is skipped for it and you verified the body by eye.
- **When the customer says "별로" (meh)**, they usually want a different style,
  not a tweaked parameter. Offer two alternatives with different motion
  families (e.g. one full-frame-effects set, one strong-motion set).
- **Real people + brands.** A photo of a real person appearing to endorse a
  coin/product can be reported when published publicly; fine for private use,
  mention it once if the pack is going public.

## Output naming

`NN_slug.webm` per sticker (`01_hello.webm`), `icon.webm` for the pack icon.
When delivering several, zip `stickers/`, `preview.png`, and a short
`README.txt` listing each sticker's phrase and motion/effect combo.

## References

- `references/catalog.md` — every motion/effect/caption parameter with ranges and combos that worked
- `references/spec-schema.md` — JSON spec format and complete examples (mascot, glow art, gunshot, photo + caption, icon)
- `references/telegram.md` — format rules, @Stickers registration flow, error messages and their real causes
- `references/pitfalls.md` — the encoding and keying traps that cost days; read before changing `engine.py`
- `scripts/check.py` — verify any existing .webm; `scripts/rust-checker/` — dependency-free Rust verifier for CI
