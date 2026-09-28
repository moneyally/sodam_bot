# Pitfalls (each of these cost real time; the engine already handles them)

## Encoding

- **`-pix_fmt yuva420p` alone does not give you transparency.** libvpx writes
  the alpha plane but players only read it when the stream carries
  `-metadata:s:v:0 alpha_mode=1`.
- **ffprobe reports alpha files as `yuv420p`.** The default VP9 decoder ignores
  Matroska BlockAdditional. Verify by decoding: `ffmpeg -c:v libvpx-vp9 -i x.webm -vf alphaextract -frames:v 1 a.png` and check the extrema are 0–255. `verify.spec` does this.
- **Exactly 3.000 s rounds to 3.033 s** in the container. Use 89 frames @ 30 fps.
- **`-auto-alt-ref 0` is mandatory** with alpha; alt-ref frames corrupt the alpha stream.
- **The alpha plane is not counted by `-b:v`**, so transparent stickers come out
  larger than the bitrate suggests. That is why the cut-out ladder starts at 460k
  while photo stickers start at 660k.
- **Under a tight budget, remove motion before adding bits.** A static alpha
  plane and background let the encoder spend everything on detail; this is how
  32 KB icons stay readable.
- **2-pass is worth it**; single-pass overshoots the size unpredictably.

## Rendering

- Keep the master **premultiplied** through the affine warp. Straight-alpha
  resampling drags transparent black into every edge and produces dark halos.
- **One resample per frame.** Rotate + scale + translate + 1024→512 in a single
  `Image.transform`; chaining resizes blurs the character.
- `Image.fromarray(...)` returns a read-only image: `ImageDraw.floodfill` on it
  silently does nothing. Call `.copy()` first (the keying module uses scipy
  labels instead, which avoids the trap).
- Blends in ffmpeg must run in `gbrp`; a screen blend in YUV pushes chroma from
  128 to ~191 and tints the picture. The engine avoids ffmpeg filters entirely.
- **Full-frame R/B channel split on monochrome high-contrast art** turns the
  whole picture into rainbow fog. Use `slice_glitch` (split inside torn strips
  only) for that kind of art.
- `aura` flare above ~0.35 or `flashbang` above ~60 whites out the frame.

## Keying

- Flood-fill from the border so enclosed regions (black eyes, dark coat interiors)
  survive. Pure "dark = transparent" hollows characters out.
- White-background sticker art usually has a faint grey rim just outside its
  white outline; the flood fill stops there as long as the threshold stays
  above it (default `min(RGB) >= 235`).
- Dark clothes on a green/black background need a chroma-aware candidate
  (`max(RGB) < 30 and chroma < 12`) and a hole fill afterwards; the `holes`
  check exists because a customer found the jacket missing.
- Glow-on-black art: body opaque, haze soft — see `keying.key_glow`. Its `holes`
  count is legitimately large; the check is skipped for it.

## Captions

- Draw all outlines first, then all fills. Flattening each glyph first lets the
  next glyph's outline eat the previous one (하 → 히). `glyph_check` measures
  this on a motion-free render, because with `wave`/`punch` the glyphs are not
  where the reference expects.
- `drawtext` in ffmpeg does not touch alpha, so text over a transparent region
  is invisible. The caption is an RGBA layer composited in Python instead.

## Product

- **Do not cut body parts** (hand, arm) to animate them separately; fur and
  outlines tear along the seam. Whole-body motion + effects instead.
- Customers evaluating a pack look at it on both dark and light chat themes;
  a preview on mid-grey approximates both.
- "별로" (meh) means "different style", not "same thing tweaked".
