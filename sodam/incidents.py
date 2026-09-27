"""🚨 사건 묶기: 같은 방·같은 종류·같은 대상의 자동 알림을 1:1 메시지 하나로 (실측: 자동 관리 알림의 39% 가 같은 방에서
10분 안에 또 옴 → 사람마다 알림이 줄줄이 쌓였고, 스팸 방패 + 사기 의심 검사를 같이 켜면 같은 사람에 두 통).

open_or_bump(svc, bot, chat_id, kind, key, text, kb, recipients):
  - 첫 사건 → 받는 사람마다 1:1 한 통 (DB incidents 에 메시지 ID 저장 → 재시작해도 이어서 고침)
  - WINDOW(10분, 미끄러지는 창: 마지막 사건 기준) 안의 같은 (방, kind, key) → 그 메시지들을 고친다:
    머리 🚨 감지 → 🔴 지속(3건↑ 또는 5분↑) · '+N건 · 마지막 HH:MM' · 이전 사건 한 줄씩
  - WINDOW 동안 조용하면 틱(hooks.add_tick_hook)이 🟢 잠잠해짐 으로 고치고 닫음 → 그 뒤 사건은 새 메시지
  - 버튼: 대상(sub, 보통 사람 ID)별로 최신 버튼을 모아 둠 — 다른 사람 버튼은 안 사라짐 (여러 명이면 이름표)
  - 고치기 실패(메시지 없음 등) → 새로 보내고 저장된 ID 교체. 1:1 막힘(Forbidden) → 그 사람은 건너뜀
차지(열기/더하기)는 db.atomic 한 번 → 동시에 온 사건도 새 메시지는 한 번만. 여는 쪽이 보내는 중(sending)이면
더하는 쪽은 기록만 하고, 여는 쪽이 다 보낸 뒤 최신 상태로 한 번 더 고친다.
svc 는 db·cfg 만 쓴다 (moderation.Moderator 도 그대로 넘길 수 있게).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest, Forbidden, TelegramError

from . import hooks
from .db import register_schema
from .util import esc, send_retry

log = logging.getLogger(__name__)

WINDOW = 600            # 마지막 사건 뒤 이 시간 안이면 같은 사건 (미끄러지는 창)
ONGOING_COUNT = 3       # 이만큼 쌓이면 🔴 지속
ONGOING_AFTER = 300     # 처음 뒤 이 시간이 지나면 🔴 지속
SENDING_STALE = 60      # 여는 쪽이 이 시간 넘게 '보내는 중'이면 죽은 걸로 봄 (재시작)
HISTORY = 6             # 이전 사건 한 줄 보관 수
MAX_SUBS = 8            # 버튼 묶음(대상) 최대 수 — 넘으면 가장 오래된 것부터 뺌
KEEP_DAYS = 7
MAX_LEN = 3900          # 텔레그램 4096 자 한도 안
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

STATES = {"new": "🚨 감지", "ongoing": "🔴 지속", "calm": "🟢 잠잠해짐"}
KINDS = {"captcha": "캡차 실패", "flood": "도배 뮤트", "imperson": "사칭 의심", "autoban": "경고 누적 밴",
         "cas": "스팸 명단 차단", "raid": "대량 입장", "anomaly": "방 이상징후", "scam": "사기·스팸 의심",
         "fedban": "공동 차단 명단"}

register_schema("""
CREATE TABLE IF NOT EXISTS incidents (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    key       TEXT NOT NULL,
    state     TEXT NOT NULL DEFAULT 'new',   -- new 🚨 / ongoing 🔴 / calm 🟢(닫힘)
    opened    INTEGER NOT NULL,
    last      INTEGER NOT NULL,
    count     INTEGER NOT NULL DEFAULT 1,
    msgs_json TEXT NOT NULL DEFAULT '{}',    -- {받는 사람 ID: 메시지 ID}
    body      TEXT NOT NULL DEFAULT '',      -- 가장 최근 사건 글 (HTML)
    hist      TEXT NOT NULL DEFAULT '[]',    -- 사건 한 줄씩 (오래된 → 최근)
    kb        TEXT NOT NULL DEFAULT '[]',    -- [[sub, 이름표, [[[글, 데이터], ...], ...]], ...]
    recips    TEXT NOT NULL DEFAULT '[]',    -- 요청된 받는 사람 전체 (여는 쪽이 따라잡을 때)
    sending   INTEGER NOT NULL DEFAULT 0     -- 여는 쪽이 첫 메시지를 보내는 중인 시각 (0 = 아님)
);
CREATE INDEX IF NOT EXISTS incidents_key ON incidents(chat_id, kind, key, last);
CREATE INDEX IF NOT EXISTS incidents_open ON incidents(state, last);
""", migrate={"incidents": "plain"})


def scam_incident(user_id: int) -> tuple[str, str]:
    """🛡️ 스팸 방패·🕵️ 사기 의심 검사가 같이 쓰는 사건 (kind, key): 같은 방·같은 사람 = 알림 하나."""
    return "scam", str(user_id)


def _now() -> float:
    return time.time()


@dataclass
class Result:
    id: int
    opened: bool      # True = 새 사건(새 메시지), False = 기존 사건에 더함(고침)
    delivered: int    # 보냈거나 고친 받는 사람 수


def _rid(r) -> int:
    return r if isinstance(r, int) else r.id


def _line(text: str) -> str:
    """이전 사건 한 줄: 첫 줄에서 태그만 뺌 (&lt; 같은 엔티티는 그대로라 HTML 안전)."""
    first = (text or "").strip().split("\n", 1)[0]
    first = re.sub(r"<[^>]*>", "", first).removeprefix("📣 ").strip()
    return first[:100] + ("…" if len(first) > 100 else "")


def _rows(kb: InlineKeyboardMarkup | None) -> list:
    if not kb:
        return []
    return [[[b.text, b.callback_data] for b in row if b.callback_data] for row in kb.inline_keyboard]


def merge_kb(groups: list, sub: str, label: str, rows: list) -> list:
    """대상별 최신 버튼. 같은 대상의 새 버튼이 옛 버튼을 대신하고, 다른 대상 버튼은 그대로 (처리 안 된 사람 버튼 유지).
    버튼 없는 사건은 기존 버튼을 건드리지 않는다."""
    if not rows:
        return groups
    out = [g for g in groups if g[0] != sub] + [[sub, label, rows]]
    return out[-MAX_SUBS:]


def build_kb(groups: list) -> InlineKeyboardMarkup | None:
    named = sum(1 for g in groups if g[1]) > 1   # 여러 사람이면 누구 버튼인지 이름표
    out = []
    for _sub, label, rows in groups:
        for i, row in enumerate(rows):
            btns = []
            for j, (text, data) in enumerate(row):
                if named and label and i == 0 and j == 0:
                    text = f"{label[:12]} · {text}"
                btns.append(InlineKeyboardButton(text, callback_data=data))
            if btns:
                out.append(btns)
    return InlineKeyboardMarkup(out) if out else None


def _hhmm(svc, ts: float) -> str:
    return datetime.fromtimestamp(ts, svc.cfg.tz).strftime("%H:%M")


def render(svc, row) -> tuple[str, InlineKeyboardMarkup | None]:
    head = f"{STATES.get(row['state'], '🚨 감지')} · <b>{esc(KINDS.get(row['kind'], row['kind']))}</b>"
    if row["count"] > 1:
        head += f" · +{row['count'] - 1}건 · 마지막 {_hhmm(svc, row['last'])}"
    if row["state"] == "calm":
        head += f"\n({WINDOW // 60}분 동안 새 일이 없었어요)"
    prev = json.loads(row["hist"] or "[]")[:-1]
    tail = ("\n\n이전:\n" + "\n".join(f"• {p}" for p in prev[-(HISTORY - 1):])) if prev else ""
    body = row["body"]
    room = MAX_LEN - len(head) - len(tail) - 2
    if len(body) > room:        # 잘라도 태그가 깨지지 않게 태그를 뺀 글로
        body = esc(re.sub(r"<[^>]*>", "", body))[:max(room - 1, 0)] + "…"
    return f"{head}\n{body}{tail}", build_kb(json.loads(row["kb"] or "[]"))


# ── DB ────────────────────────────────────────────────────
async def _claim(svc, chat_id: int, kind: str, key: str, text: str, kb_rows: list, sub: str, label: str,
                 recipients: list[int], now: float) -> tuple[int, bool, bool]:
    """(사건 ID, 새로 열었는지, 여는 쪽이 아직 보내는 중인지). 한 번의 atomic — 동시에 와도 여는 건 하나."""
    line, ts = _line(text), int(now)

    def run(c):
        row = c.execute("SELECT id, opened, count, hist, kb, recips, sending FROM incidents "
                        "WHERE chat_id=? AND kind=? AND key=? AND state!='calm' AND last>=? ORDER BY id DESC LIMIT 1",
                        (chat_id, kind, key, ts - WINDOW)).fetchone()
        if row is None:
            iid = c.execute("INSERT INTO incidents(chat_id, kind, key, state, opened, last, count, body, hist, kb, "
                            "recips, sending) VALUES(?,?,?,?,?,?,1,?,?,?,?,?)",
                            (chat_id, kind, key, "new", ts, ts, text, json.dumps([line], ensure_ascii=False),
                             json.dumps(merge_kb([], sub, label, kb_rows), ensure_ascii=False),
                             json.dumps(recipients), ts)).lastrowid
            return iid, True, False
        iid, opened, count, hist, kbj, recj, sending = tuple(row)
        count += 1
        state = "ongoing" if count >= ONGOING_COUNT or ts - opened > ONGOING_AFTER else "new"
        hist = (json.loads(hist) + [line])[-HISTORY:]
        recips = list(dict.fromkeys(json.loads(recj) + recipients))
        c.execute("UPDATE incidents SET state=?, last=?, count=?, body=?, hist=?, kb=?, recips=? WHERE id=?",
                  (state, ts, count, text, json.dumps(hist, ensure_ascii=False),
                   json.dumps(merge_kb(json.loads(kbj), sub, label, kb_rows), ensure_ascii=False),
                   json.dumps(recips), iid))
        return iid, False, bool(sending) and ts - sending < SENDING_STALE
    return await svc.db.atomic(run)


async def get(db, iid: int):
    return await db._one("SELECT * FROM incidents WHERE id=?", (iid,))


async def _save_msgs(svc, iid: int, changes: dict[str, int | None], *, done_sending: bool = False) -> int:
    """받는 사람별 메시지 ID 를 바꾼 것만 합침 (동시에 고치는 쪽 것을 덮어쓰지 않게). 합친 뒤 사건 수를 돌려줌."""
    def run(c):
        row = c.execute("SELECT msgs_json, count FROM incidents WHERE id=?", (iid,)).fetchone()
        if row is None:
            return 0
        msgs = json.loads(row[0] or "{}")
        for k, v in changes.items():
            if v is None:
                msgs.pop(k, None)
            else:
                msgs[k] = v
        c.execute("UPDATE incidents SET msgs_json=?" + (", sending=0" if done_sending else "") + " WHERE id=?",
                  (json.dumps(msgs), iid))
        return row[1]
    return await svc.db.atomic(run)


# ── 텔레그램 ──────────────────────────────────────────────
async def _send(bot, rid: int, text: str, kb) -> int | None:
    try:   # 1:1·로그방이라 중복돼도 괜찮음 → 응답만 끊긴 경우도 한 번 더
        m = await send_retry(lambda: bot.send_message(rid, text, parse_mode="HTML", reply_markup=kb,
                                                      link_preview_options=NO_PREVIEW), dup_ok=True)
        return m.message_id
    except (Forbidden, BadRequest) as e:   # 봇과 1:1 을 시작 안 했거나 봇을 차단함
        log.info("사건 알림 전송 실패 (%s): %s", rid, e)
    except TelegramError as e:
        log.warning("사건 알림 전송 실패 (%s, 네트워크): %s", rid, e)
    return None


async def _deliver(bot, rid: int, mid: int | None, text: str, kb, resend: bool = True) -> tuple[bool, int | None]:
    """(바뀌었는지, 메시지 ID). 있으면 고치고, 고칠 수 없으면(지워짐·오래됨) 새로 보냄 (resend=False 면 안 보냄)."""
    if mid:
        try:
            await bot.edit_message_text(text, chat_id=rid, message_id=mid, parse_mode="HTML", reply_markup=kb,
                                        link_preview_options=NO_PREVIEW)
            return False, mid
        except Forbidden:
            return True, None                     # 봇 차단 → 이 사람은 빼 둠 (다음 사건에 다시 시도)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return False, mid
            log.info("사건 알림 고치기 실패 (%s/%s): %s → 새로 보냄", rid, mid, e)
        except TelegramError as e:                # 네트워크: 고쳐졌을 수도 있으니 새로 보내지 않음 (다음 사건에 다시)
            log.warning("사건 알림 고치기 실패 (%s, 네트워크): %s", rid, e)
            return False, mid
        if not resend:
            return True, None
    elif not resend:
        return False, None
    new = await _send(bot, rid, text, kb)
    return new != mid, new


async def _push(svc, bot, iid: int, recipients: list[int], *, done_sending: bool = False,
                resend: bool = True) -> tuple[int, bool]:
    """지금 DB 상태로 그려서 받는 사람들에게 보내거나 고침. (전한 수, 그리는 사이 사건이 더해졌는지)."""
    row = await get(svc.db, iid)
    if row is None:
        return 0, False
    text, kb = render(svc, row)
    msgs = json.loads(row["msgs_json"] or "{}")
    changes: dict[str, int | None] = {}
    delivered = 0
    for rid in recipients:
        changed, mid = await _deliver(bot, rid, msgs.get(str(rid)), text, kb, resend)
        delivered += mid is not None
        if changed:
            changes[str(rid)] = mid
    if changes or done_sending:
        after = await _save_msgs(svc, iid, changes, done_sending=done_sending)
    else:
        after = (await svc.db._one("SELECT count FROM incidents WHERE id=?", (iid,)) or {"count": 0})["count"]
    return delivered, after != row["count"]


async def open_or_bump(svc, bot, chat_id: int, kind: str, key, text: str, kb: InlineKeyboardMarkup | None,
                       recipients, *, sub=None, label: str = "") -> Result:
    """사건 하나를 알림. recipients = 받는 사람 ID(또는 .id 가 있는 사람) 목록, 순서대로·중복 없이.
    sub = 버튼 묶음 대상(보통 사람 ID, 없으면 사건 전체 하나), label = 여러 대상일 때 버튼 앞 이름표."""
    rids = list(dict.fromkeys(_rid(r) for r in recipients))
    iid, opened, busy = await _claim(svc, chat_id, kind, str(key), text, _rows(kb),
                                     "" if sub is None else str(sub), label, rids, _now())
    if not opened and busy:
        return Result(iid, False, 0)   # 여는 쪽이 보내는 중 → 다 보낸 뒤 이 사건까지 반영해 고침
    delivered, stale = await _push(svc, bot, iid, rids, done_sending=opened)
    for _ in range(3):                 # 그리는 사이 다른 사건이 더해졌으면 최신으로 다시 (여는 쪽은 늦게 온 받는 사람까지)
        if not stale:
            break
        row = await get(svc.db, iid)
        targets = list(dict.fromkeys(rids + (json.loads(row["recips"]) if opened else [])))
        delivered, stale = await _push(svc, bot, iid, targets)
    return Result(iid, opened, delivered)


async def tick(svc, bot) -> int:
    """30초마다 (hooks.add_tick_hook): WINDOW 동안 조용한 사건을 🟢 잠잠해짐 으로 고치고 닫음. 닫은 수."""
    now = int(_now())
    n = 0
    for row in await svc.db._all("SELECT id FROM incidents WHERE state!='calm' AND last<?", (now - WINDOW,)):
        def run(c, iid=row["id"]):   # 한 문장 차지 → 두 프로세스·겹친 틱이어도 한 번만 고침
            return c.execute("UPDATE incidents SET state='calm' WHERE id=? AND state!='calm' AND last<?",
                             (iid, now - WINDOW)).rowcount
        if not await svc.db.atomic(run):
            continue
        n += 1
        full = await get(svc.db, row["id"])
        await _push(svc, bot, row["id"], [int(k) for k in json.loads(full["msgs_json"] or "{}")], resend=False)
    if n or now % 3600 < 30:
        await svc.db._write("DELETE FROM incidents WHERE state='calm' AND last<?", (now - KEEP_DAYS * 86400,))
    return n


hooks.add_tick_hook(tick)
