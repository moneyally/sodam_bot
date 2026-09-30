# Spec schema

> **소담 판 추가 (2026-09-30)**: 아래 원본 형식에 더해 `motion` 에 `{"type":"keyframes","pivot":[x,y],"keys":[{t,scale,sx,sy,rotate,x,y,opacity,ease}]}`,
> 최상위 `layers:[{type: particles|grade|flash|lightning|transition|<fx 이름>, …, start, end}]`, 움프용 `loop`(false = 6초 한 번)·`cover`.
> 값의 형식·범위는 코드가 기준: `sodam/stickerforge/__init__.py` 의 `layer_params()`·`KEY_RANGES`, 예시는 `sodam/stickerforge/examples.py`.
> 예: `{"loop":false,"layers":[{"type":"transition","kind":"burn","start":0.12,"end":0.8,"embers":50},
> {"type":"particles","shape":"smoke","spawn":"bottom","angle":-90,"grow":2.2,"blur":4}]}` = 사진이 타서 사라지며 연기.

`forge.py spec.json` reads one JSON object. Unknown keys are ignored; every key
but `image` and `out` has a default.

```jsonc
{
  "image": "in.png",               // source image (any format Pillow reads)
  "out": "01_hello.webm",
  "mode": "cutout",                // cutout | photo
  "keying": {                      // cutout only
    "mode": "auto",                // auto | white | black | color | glow | none
    "tol": 20,                     // candidate range for flat keying (raise if halo remains)
    "fill_holes": true             // fill small enclosed transparent pockets (keying mistakes)
  },
  "margin": 0.08,                  // free space around the subject (room for motion). 0.05 calm .. 0.16 slam/hop
  "radius": 56,                    // photo only: corner radius px (0 = square)
  "overscan": 1.06,                // photo only: extra scale so pans never show the edge
  "seed": 1,                       // seeds random fx (bolts, slice_glitch) so loops repeat identically
  "limit_kb": 250,                 // target size (Telegram max 256)
  "font": "assets/fonts/BlackHanSans.ttf",

  "motion": [                      // list; angles/offsets add, scales multiply
    {"type": "idle", "amp_rot": 2, "amp_bob": 10, "breathe": 0.012},
    {"type": "punch", "hits": 2, "amount": 0.05}
  ],
  "fx": [                          // list; applied in order (draw order)
    {"type": "sweep", "strength": 0.5},
    {"type": "sparkle", "subset": [0, 2, 4, 6, 9]},
    {"type": "glitch", "amp": 6, "bursts": 2}
  ],
  "caption": {                     // optional
    "text": "안녕하세요",
    "palette": "gold",             // gold silver ice fire pink neon white
    "anims": ["bounce", "punch", "shine"],   // + wave shake glow
    "position": "bottom",          // bottom | top
    "typing": true,                // false = whole line at once
    "size_max": 76, "stroke": 9, "depth": 6, "tracking": 3, "step": 0.135, "delay": 0.12,
    "top": [255,250,205], "mid": [255,205,60], "bottom": [238,146,8], "extrude": [92,46,0]  // override palette
  }
}
```

CLI shorthand for the same thing: `--motion "idle:amp_rot=2,punch:hits=2:amount=0.05" --fx "sweep,sparkle:subset=[0,2,4]"`.
Values after `=` are parsed as JSON when possible (numbers, lists, true/false).

## Complete examples

### Mascot on white, greeting, calm + sparkle

```json
{"image": "hello.png", "out": "01_hello.webm",
 "motion": [{"type": "idle", "amp_rot": 1.5, "amp_bob": 8}],
 "fx": [{"type": "glitch", "amp": 6, "bursts": 2}, {"type": "sparkle", "subset": [1, 4, 6, 8]}],
 "margin": 0.10}
```

### Strong-motion alternative for the same mascot (offer as "v2")

```json
{"image": "hello.png", "out": "01_hello_v2.webm",
 "motion": [{"type": "wobble", "amp": 8, "cycles": 2}],
 "fx": [{"type": "outline", "width": 4}, {"type": "sparkle", "subset": [0, 3, 7]}],
 "margin": 0.12}
```

### Dark glow art, thumbs-up jab with lightning

```json
{"image": "dark_work.png", "out": "work_done.webm",
 "keying": {"mode": "glow"}, "margin": 0.07,
 "motion": [{"type": "jab", "times": [0.22, 1.12, 2.02]}],
 "fx": [{"type": "aura"}, {"type": "shockwave", "center": [256, 236]},
        {"type": "bolts", "center": [256, 236], "color": [150, 120, 255]},
        {"type": "slice_glitch"}, {"type": "sparkle", "subset": [0, 3, 5, 8]}]}
```

### Gunshot scene (illustration that should keep its rectangle)

```json
{"image": "scam.png", "out": "07_scam.webm",
 "mode": "photo", "radius": 56,
 "motion": [{"type": "recoil", "times": [0.10, 0.62, 1.14, 1.90, 2.42], "kick": 26, "direction": -1}],
 "fx": [{"type": "flash", "at": [374, 213]}, {"type": "flashbang", "amount": 55},
        {"type": "glitch", "amp": 9, "on_hits": true}, {"type": "sparkle", "subset": [2, 5, 9]}]}
```

`at` is the muzzle position in 512-px coordinates: measure it on the image
(x / width * 512, y / height * 512).

### Photo + typed caption

```json
{"image": "ceo.jpg", "out": "03_thanks.webm", "mode": "photo",
 "motion": [{"type": "zoom", "amount": 0.06}],
 "fx": [{"type": "sweep", "strength": 0.42}, {"type": "sparkle"}],
 "caption": {"text": "감사합니다", "palette": "gold", "anims": ["bounce", "punch", "shine"]}}
```

### Sleep sticker

```json
{"image": "night.png", "out": "03_night.webm",
 "motion": [{"type": "breathe"}],
 "fx": [{"type": "meteors"}, {"type": "zzz", "origin": [300, 118]}, {"type": "sparkle", "subset": [1, 3, 7]}]}
```

### Pack icon and premium emoji

Same spec, add flags: `forge.py spec.json --icon icon.webm --emoji emoji.webm`.
Both are the 512 render downscaled to 100 px with an unsharp pass and their own
bitrate ladders (≤32 KB at 24 fps for the icon, ≤64 KB for emoji). For a logo
icon prefer a spec with `motion: [{"type": "still"}]` and only `sweep` — at 100 px
motion costs more bits than it is worth. If a text logo must be readable at
100 px, a static PNG emoji beats any video.
