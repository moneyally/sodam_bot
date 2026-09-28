# Telegram rules and the @Stickers flow

## Format limits (the bot rejects anything outside these)

| | Video sticker | Pack icon | Premium emoji |
|---|---|---|---|
| Container / codec | WebM / VP9 | WebM / VP9 | WebM / VP9 (or static PNG/WEBP) |
| Size | 512×512 (one side exactly 512) | 100×100 | 100×100 |
| Length | ≤ 3 s (we use 2.9667 s = 89 f @ 30 fps) | ≤ 3 s, loops | ≤ 3 s |
| FPS | ≤ 30 | ≤ 30 | ≤ 30 |
| File size | ≤ 256 KB | ≤ 32 KB | ≤ 64 KB |
| Pixel format | `yuva420p` + `alpha_mode=1` | same | same |
| Audio | none | none | none |

## Registering a pack (@Stickers)

1. `/newvideo` (not `/newpack`, that is for static stickers)
2. Reply with the **pack name as plain text** (≤ 64 chars, no links, no `@`, no `t.me`)
3. Send each `.webm` **as a file / document** (📎 → File), then reply with an emoji; repeat
4. `/publish`
5. Send a 100×100 icon `.webm` or `/skip`
6. Reply with the short name: letters, digits, `_` only → `t.me/addstickers/<short_name>`

Edit later with `/editsticker`, `/setpackicon`, `/delsticker`. Premium emoji
packs use `/newemojipack` with the same flow.

## Error messages and what they really mean

| Message | Real cause |
|---|---|
| `Sorry, this title is unacceptable` | A file was sent where the bot asked for the pack *name*; or the name contains a link/@ |
| `File is too big` / sticker arrives with a black background | Sent as a video, not as a file; Telegram re-encoded it |
| `Wrong file type` / rejected silently | Not VP9, or dimensions not 512, or has an audio track |
| Sticker shows with black instead of transparent | Encoded without `-metadata:s:v:0 alpha_mode=1` |
| `Too long` | Duration rounded to > 3.000 s (use 2.9667 s) |

## Sending stickers from a bot

`sendSticker` with the `.webm` as an `InputFile` works for individual stickers;
for pack creation use `createNewStickerSet` / `addStickerToSet` with
`sticker_format="video"`. Either way the bytes must be the verified file
unchanged — never let a client re-encode it.
