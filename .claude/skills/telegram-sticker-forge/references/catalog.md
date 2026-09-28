# Motion / effect / caption catalog

All motions return `(angle°, sx, sy, dx, dy)` in 1024-px master units; stacking
adds angles and offsets and multiplies scales. Periodic terms are whole cycles
per loop (2.9667 s), impulse terms decay before the loop ends.

## Motions (`motion: [{"type": ...}]`)

| type | params (default) | feel | notes |
|---|---|---|---|
| `idle` | amp_rot 2, amp_bob 10, breathe 0.012, phase 0, cycles 1 | breathing sway | the safe default |
| `breathe` | idle with amp_rot 0.7, amp_bob 4 | barely moving | sleep / night / "잔잔하게" |
| `float` | amp_rot 2.2, amp_bob 16, sway 6 | zero-g drift | space, dreamy, balloons |
| `punch` | hits 3, amount 0.06 | zoom beats | pair with `scan`/`sparkle`; hits 2 for calmer |
| `shake` | amp 6, freq 6 | vibration | excitement, cold, nervous |
| `wobble` | amp 9, cycles 2 | jelly tilt with overshoot | cute characters |
| `hop` | jumps 2, height 110, squash 0.16 | crouch → jump → squash | needs margin ≥ 0.14 |
| `pop` | hold 0.55 | pops in, breathes, sucked out | entrance / disappearing; seamless |
| `slam` | drops 1, height 420 | falls in, squashes 28 %, rebounds | re-entry loop; margin ≥ 0.14 |
| `recoil` | times [0.10,0.62,1.14,1.90,2.42], kick 26, direction -1, tilt -1.6, shake_hz 22 | gun kickback | direction -1 = muzzle on right |
| `jab` | times [0.22,1.12,2.02], lunge 0.11 | crouch → strike toward viewer → 9 Hz rebound | thumbs-up, punch, "찌르기" |
| `zoom` | amount 0.06 | photo breathing zoom | photo mode default |
| `pan` | amount 40 | slow horizontal drift | photo mode |
| `still` | – | none | icons / logos |

Hit times (`recoil.times`, `jab.times`, slam impact) are collected into
`ctx.hits`; every hit-synced effect reads them.

## Effects (`fx: [{"type": ...}]`, applied in order)

### Backdrop (put first)
| type | params | look |
|---|---|---|
| `rays` | spokes 12, strength 0.22, color, center [0.5,0.42] | rotating sunburst behind the silhouette |
| `outline` | color white, width 5, pulse 0 | sticker border around the alpha |
| `glow` | color, radius 10, strength 0.6, pulse true | soft bloom behind |

### Colour / light (inside the silhouette)
| type | params | look |
|---|---|---|
| `sweep` | strength 0.5, width 0.09, color | diagonal highlight, one pass per loop |
| `scan` | height 70, alpha 0.45 | horizontal light bar, top → bottom |
| `aura` | flicker 0.06, flare 0.32, tau 0.12 | brightness flicker of semi-transparent (glow) pixels; flares on hits. For `glow`-keyed art |
| `flashbang` | amount 55, tau 0.10 | whole-silhouette brightness pop on hits |

### Overlays
| type | params | look |
|---|---|---|
| `sparkle` | subset (indices 0–9), color | twinkling stars; use `subset` to thin |
| `hearts` | color | hearts rising |
| `rain` / `rise` | image (PNG with alpha) | your artwork falling / rising (coins, tokens, logos) |
| `meteors` | paths [(t0,(x0,y0),(x1,y1))] | shooting stars |
| `zzz` | origin [330,120], color | three Z's floating up |
| `flash` | at [x,y], size 56 | muzzle flash star at a point on hits |
| `shockwave` | center, color, r0 55, r1 315, life 0.38 | expanding ring on hits |
| `bolts` | center, color, count 6, life 0.17 | lightning forks on hits (seeded) |

### Distortion (put last)
| type | params | look |
|---|---|---|
| `glitch` | amp 7, bursts 3, k 11, on_hits false | R/B channel split in bursts or on hits |
| `slice_glitch` | bands 6, shift 26, split 3, life 0.13, bursts | torn horizontal strips; safe on monochrome art |
| `ghost` | – | motion trail from the previous two frames |

## Caption (`caption: {...}`)

| key | default | meaning |
|---|---|---|
| text | – | 2–7 characters ideal |
| palette | gold | gold silver ice fire pink neon white |
| anims | bounce, punch, shine | + wave, shake, glow |
| position | bottom | bottom / top |
| typing | true | letters every `step` s from `delay`; false = all at once |
| size_max / stroke / depth / tracking | 76 / 9 / 6 / 3 | px |
| top / mid / bottom / extrude | palette | RGB overrides |

## Combos that customers approved

| Sticker intent | motion | fx |
|---|---|---|
| greeting (안녕하세요) | idle 1.5/8 | glitch amp 6 bursts 2 + sparkle {1,4,6,8} |
| greeting, strong version | wobble 8/2 | glitch + sparkle |
| sent / done (전송완료) | punch 3/0.07 | scan + sparkle {0,2,5,9} |
| sent, strong | slam | scan |
| good night (잘자요) | breathe | meteors + zzz + sparkle {1,3,7} |
| eat well (식사챙기세요) | shake 5/4 or hop 2/100 | rays + sparkle |
| thumbs-up, no text | punch 3/0.09 or pop | outline white 5 + sparkle |
| at work (출근완료), colourful mascot | punch 3/0.06 | outline 4 + glitch amp 6 + sparkle |
| at work, dark glow art | jab | aura + shockwave + bolts + slice_glitch + sparkle |
| scam caught (gun scene) | recoil ×5 | flash at muzzle + flashbang + glitch on_hits + sparkle |
| photo + caption, CEO thanks | zoom | sweep 0.42 + sparkle; caption gold bounce+punch+shine |
| love (사랑합니다), heart burst | punch 2/0.08 | hearts + glow pink + sparkle |
| space / astronaut | float + punch 2/0.04 | sweep + sparkle |

## Margins

| motion | margin |
|---|---|
| idle, breathe, float, punch, shake, zoom | 0.06–0.10 |
| wobble, pop, jab, recoil | 0.10–0.12 |
| hop, slam | 0.14–0.16 |
