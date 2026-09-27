"""🛡️ 스팸 방패 (AI) 화면 (방 관리자 1:1). 검사 자체는 sodam/spamshield.py.

m:spm:<방ID>                        모드 프리셋(끔·기록만·알림) · 신규 기준(일) · 검사 메시지 수 · 최근 30일 숫자
m:spl:<방ID>                        👁️ 걸렸을 글 최근 10개 (글은 이스케이프 + 링크는 눌리지 않게)
m:spv:<방ID>:<판정ID>               상세 + 처리 버튼 + 👍 맞음 / 👎 틀림 (오탐률 측정)
m:spx:<방ID>:<판정ID>:d|m|b|t[:p]   🗑 지우기 · 🔇 뮤트 1일 · 🚫 밴 · ✅ 괜찮음 (관리자 1:1 알림과 목록 공용, p = 목록에서 누름)
                                    누를 때마다 지금 권한 확인(fresh, 지우기 = '메시지 삭제', 나머지 = '사용자 차단'),
                                    결정은 한 문장 UPDATE 로 한 번만 (두 관리자가 같이 눌러도 한 번), 상태는 전부 DB (재시작 뒤에도 됨)
m:spf:<방ID>:<판정ID>:c|w           👍 판정 맞음(스팸) / 👎 틀림(정상) 표시 (목표값 — 두 번 눌러도 같음)
"""
from __future__ import annotations

from telegram.error import BadRequest, TelegramError

from .. import menu, spamshield
from ..menu import ADMIN, B, HubItem, PanelCtx, Route, Screen
from ..permissions import may, no_right_text
from ..subscription import chat_title
from ..util import esc, fmt_time, to_int
from . import log as log_panel

menu.register_preset("spamshield_mode", [("off", "❌ 끔"), ("shadow", "👁️ 기록만"), ("alert", "🔔 알림")], "spm")
menu.register_preset("spamshield_days", [(v, f"🆕 {v}일") for v in ("1", "3", "7")], "spm")
menu.register_preset("spamshield_first_n", [(v, f"📨 {v}개") for v in ("3", "5", "10")], "spm")
log_panel.ACTIONS.setdefault("spamshield_alert", "🛡️ 스팸 방패 알림")
log_panel.ACTIONS.setdefault("spamshield_delete", "🛡️ 스팸 방패 지우기")
log_panel.ACTIONS.setdefault("spamshield_ok", "🛡️ 스팸 방패 괜찮음")

ACTS = {"d": "delete", "m": "restrict", "b": "restrict", "t": "restrict"}
LIST_EXCERPT = 60


async def s_spm(c: PanelCtx) -> Screen:
    svc, cid = c.svc, c.cid
    s = await svc.db.get_settings(cid)
    mode = s["spamshield_mode"]
    st = await spamshield.stats(svc.db, cid)
    lines = ["🛡️ <b>스팸 방패 (AI)</b>",
             "새로 들어온 사람의 처음 몇 개 메시지만 봐요. 평범한 대화는 절대 막지 않고, 자동 제재도 하지 않아요.",
             "보는 것: 지갑주소·초대 링크·사칭 문구 같은 규칙 · 다른 방에서 <b>다른 계정</b>이 같은 링크·글을 올렸는지 · "
             "처음 글을 고쳐서 링크를 넣었는지 · AI 사기 점수. 광고 점수만으론 알리지 않아요 (업자방 광고는 정상).",
             "",
             f"지금: <b>{esc(spamshield.MODES.get(mode, mode))}</b>"]
    if mode == "shadow":
        lines.append("👁️ 걸렸을 글을 아래 목록에만 남겨요 (알림 없음). 판정이 맞았는지 표시해 보고 알림으로 바꾸세요.")
    elif mode == "alert":
        lines.append("🔔 걸리면 '사용자 차단' 권한이 있는 관리자님께 1:1 로 알려드려요 "
                     "([🗑 지우기] [🔇 뮤트 1일] [🚫 밴] [✅ 괜찮음]).")
    if mode != "off" and not await svc.paid_features(cid):
        lines.append("⚠️ 이용 기간(구독·체험) 중인 방에서만 동작해요. 지금은 쉬고 있어요.")
    lines += [f"🆕 대상: 들어온 지 {s['spamshield_days']}일 안 된 사람의 처음 {s['spamshield_first_n']}개 메시지 "
              "(관리자·자유 멤버·봇 제외, 입장 기록이 없는 사람은 대상 아님)",
              f"🤖 AI: 대상 메시지만 mini AI 로 (방 AI 한도에서 차감, 다 쓰면 AI 없이 기록만). "
              f"오늘 {await spamshield.ai_used_today(svc, cid)}/{spamshield.DAILY_AI_CAP}회",
              f"⚖️ 기준: 사기 점수 {spamshield.SCAM_ALONE:g} 이상, 또는 강한 신호가 있으면 {spamshield.SCAM_WITH_SIGNAL:g} 이상",
              "",
              f"📈 최근 30일: 검사 {st['checked']} · 걸림 {st['would']} · 알림 보냄 {st['alerted']} · "
              f"관리자 확인(스팸) {st['spam']} · 괜찮음/틀림 {st['ok']}"
              + (f" · AI 건너뜀 {st['no_ai']}" if st["no_ai"] else "")]
    trusted = await spamshield.trusted_count(svc.db, cid)
    if trusted:
        lines.append(f"✅ 괜찮음으로 표시한 사람: {trusted}명 (이 방에선 검사 안 해요)")
    rows = [menu._preset_row(s, cid, "spamshield_mode"), menu._preset_row(s, cid, "spamshield_days"),
            menu._preset_row(s, cid, "spamshield_first_n"),
            [B(f"👁️ 걸렸을 글 목록 ({st['would']})", f"m:spl:{cid}")],
            menu._back(cid)]
    return Screen("\n".join(lines), menu._kb(rows))


def _state(row) -> str:
    if row["status"] == "t" or row["feedback"] == "w":
        return "👎 정상"
    if row["status"] in ("m", "b") or row["deleted"] or row["feedback"] == "c":
        return "👍 스팸"
    return "확인 전"


async def s_spl(c: PanelCtx) -> Screen:
    rows_db = await spamshield.recent_flagged(c.svc.db, c.cid)
    lines = ["👁️ <b>걸렸을 글</b> (최근 10개)",
             "🔍 를 눌러 자세히 보고, 판정이 맞았는지 👍/👎 로 표시해 주세요. 링크는 눌리지 않게 바꿔 보여드려요."]
    if not rows_db:
        lines.append("\n(아직 없어요)")
    for i, r in enumerate(rows_db, 1):
        ex = (r["excerpt"] or "")[:LIST_EXCERPT]
        score = f"사기 {r['scam']:.2f}" if r["scam"] is not None else "AI 없음"
        lines.append(f"\n{i}. <code>{fmt_time(r['ts'], c.svc.cfg.tz)}</code> {esc(r['name'] or str(r['user_id']))} · "
                     f"{score} · {_state(r)}" + (" · 알림 보냄" if r["sent"] else "")
                     + f"\n   {esc(ex)}{'…' if len(r['excerpt'] or '') > LIST_EXCERPT else ''}")
    btns = [B(f"🔍 {i}", f"m:spv:{c.cid}:{r['id']}") for i, r in enumerate(rows_db, 1)]
    rows = menu._chunks(btns, 5) + [[B("⬅️ 스팸 방패", f"m:spm:{c.cid}")]]
    return Screen("\n".join(lines), menu._kb(rows))


async def _detail(c: PanelCtx, row, origin: str, toast: str | None = None) -> Screen:
    text = spamshield.detail_text(await chat_title(c.svc, c.cid), row, c.svc.cfg.tz)
    rows = spamshield.action_rows(c.cid, row, origin)
    if origin == "p":
        cb = f"m:spf:{c.cid}:{row['id']}:"
        rows.append([B(("● " if row["feedback"] == "c" else "") + "👍 맞음(스팸)", cb + "c"),
                     B(("● " if row["feedback"] == "w" else "") + "👎 틀림(정상)", cb + "w")])
        rows.append([B("⬅️ 목록", f"m:spl:{c.cid}")])
    return Screen(text, menu._kb(rows) if rows else None, toast=toast)


async def _row(c: PanelCtx):
    vid = to_int(c.arg(0))
    row = await spamshield.get_verdict(c.svc.db, c.cid, vid) if vid else None
    return row if row and row["would_alert"] else None


async def s_spv(c: PanelCtx) -> Screen:
    row = await _row(c)
    if not row:
        return Screen(None, toast="없는 기록이에요. 목록을 다시 열어주세요.", alert=True)
    return await _detail(c, row, "p")


async def r_action(c: PanelCtx) -> Screen:
    row, act, origin = await _row(c), c.arg(1), ("p" if c.arg(2) == "p" else "")
    if not row or act not in ACTS:
        return Screen(None, toast="만료된 알림이에요.", alert=True)
    right = ACTS[act]
    if not await spamshield_may(c, right):   # 누를 때마다 지금 권한으로 (권한을 잃은 관리자는 막힘)
        return Screen(None, toast=no_right_text(right), alert=True)
    svc, bot, vid, uid = c.svc, c.bot, row["id"], row["user_id"]
    if act == "d":
        if not await spamshield.claim_delete(svc.db, vid, c.uid):
            why = "이미 지운 메시지예요." if row["deleted"] else "이미 괜찮음으로 처리한 알림이에요."
            return Screen(None, toast=why, alert=True)
        toast = "🗑 지웠어요."
        try:
            await bot.delete_message(c.cid, row["msg_id"])
        except BadRequest as e:
            if "not found" not in str(e).lower():   # 이미 지워진 메시지면 지운 것으로 둔다
                await spamshield.release_delete(svc.db, vid, c.uid)
                return Screen(None, toast=f"실패했어요: {str(e)[:100]} (봇에게 '메시지 삭제' 권한이 있는지 확인해주세요)",
                              alert=True)
            toast = "이미 지워진 메시지예요."
        except TelegramError as e:
            await spamshield.release_delete(svc.db, vid, c.uid)
            return Screen(None, toast=f"실패했어요: {str(e)[:100]}", alert=True)
        await svc.db.audit(c.cid, c.uid, uid, "spamshield_delete", f"판정 #{vid}")
    else:
        if not await spamshield.claim_status(svc.db, vid, act, c.uid):
            cur = await spamshield.get_verdict(svc.db, c.cid, vid)
            done = spamshield.STATUS.get(cur["status"], "") if cur else ""
            return Screen(None, toast="이미 처리된 알림이에요" + (f" ({done.split(' —')[0]})." if done else "."), alert=True)
        reason = "스팸 방패: 신규 입장자 스팸 의심"
        try:
            if act == "b":
                await svc.mod.ban(bot, c.cid, uid, c.uid, reason)
            elif act == "m":
                await svc.mod.mute(bot, c.cid, uid, spamshield.MUTE_MINUTES, c.uid, reason)
            else:
                await spamshield.trust(svc.db, c.cid, uid, c.uid)
                await svc.db.audit(c.cid, c.uid, uid, "spamshield_ok", f"판정 #{vid}")
        except TelegramError as e:
            await spamshield.release_status(svc.db, vid, act, c.uid)
            return Screen(None, toast=f"실패했어요: {str(e)[:100]} (봇에게 '사용자 차단' 권한이 있는지 확인해주세요)",
                          alert=True)
        toast = spamshield.STATUS[act].split(" —")[0]
    row = await spamshield.get_verdict(svc.db, c.cid, vid)
    return await _detail(c, row, origin, toast=toast)


async def spamshield_may(c: PanelCtx, right: str) -> bool:
    return await may(c.svc.perms, c.bot, c.cid, c.uid, right)


async def r_feedback(c: PanelCtx) -> Screen:
    row, value = await _row(c), c.arg(1)
    if not row or value not in spamshield.FEEDBACK:
        return Screen(None, toast="없는 기록이에요. 목록을 다시 열어주세요.", alert=True)
    if row["feedback"] != value:
        await spamshield.set_feedback(c.svc.db, row["id"], value, c.uid)
        row = await spamshield.get_verdict(c.svc.db, c.cid, row["id"])
    return await _detail(c, row, "p", toast=spamshield.FEEDBACK[value] + "했어요")


menu.register_hub(HubItem(37, "spm", "🛡️ 스팸 방패 (AI)"))
menu.register_screen("spm", s_spm)
menu.register_screen("spl", s_spl)
menu.register_route("spv", Route(s_spv, ADMIN))
menu.register_route("spx", Route(r_action, ADMIN, fresh=True))
menu.register_route("spf", Route(r_feedback, ADMIN))
