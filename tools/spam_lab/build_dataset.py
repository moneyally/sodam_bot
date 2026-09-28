"""데이터셋 만들기.

    python tools/spam_lab/build_dataset.py

1) synth.py(직접 쓴 가짜 스팸·헷갈리는 정상)에 가짜 주소·아이디·프로필·시각을 붙여 synthetic.jsonl (저장소 안)
2) 읽기 전용 스냅샷(lab.db)에서 실제 정상 멤버 단위 + 약한 스팸 라벨(mod_log)을 뽑아 synthetic 과 합친 units.json (저장소 밖)

단위(unit) = (방, 계정)의 처음 1~5개 메시지 + 프로필(아이디 유무·이름·user_id) + 신규 여부.
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import string

from common import SNAPSHOT, SYNTH, UNITS, split_of
from synth import HARDNEG, MULTI, SPAM

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
FIRST_N = 5
BOT_USERNAMES = {"sodam_ai_bot"}


def fake_values(rng: random.Random) -> dict:
    tg = "https://t.me/+" + "".join(rng.choice(string.ascii_letters + string.digits + "_-") for _ in range(16))
    if rng.random() < 0.3:
        tg = "t.me/" + rng.choice(["vip", "free", "signal", "otc", "event", "pump"]) + "_" + \
             "".join(rng.choice(string.ascii_lowercase) for _ in range(5)) + str(rng.randint(1, 99))
    handle = "@" + rng.choice(["vip", "otc", "cs", "help", "event", "coin", "usdt", "king", "lucky", "rich"]) + \
        rng.choice(["_", ""]) + "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(3, 6))) + \
        str(rng.randint(1, 999))
    site = rng.choice(["https://", "", ""]) + "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(4, 8))) + \
        rng.choice([".com", ".xyz", ".top", ".site", ".vip", ".net"])
    return {"TRX": "T" + "".join(rng.choice(B58) for _ in range(33)),
            "ETH": "0x" + "".join(rng.choice("0123456789abcdef") for _ in range(40)),
            "TG": tg, "ID": handle, "SITE": site}


def fill(text: str, vals: dict) -> str:
    return re.sub(r"\{(TRX|ETH|TG|ID|SITE)\}", lambda m: vals[m.group(1)], text)


# 가짜 프로필 (이름은 흔한 한국식 닉네임·영문·사칭형)
NAMES_PLAIN = ["김민지", "이수현", "박지훈", "Jenny", "Kevin Park", "하늘", "민수", "지은", "Alex", "소연", "준호", "유나",
               "코인러버", "성공투자", "행복한하루", "Daniel", "Lily", "서윤", "도윤", "Emma"]
NAMES_BIZ = ["💎VIP💎", "🔥OTC🔥", "USDT 매입", "💰테더💰", "🎰메이저🎰", "머니팩토리", "코인상담", "리딩마스터"]
NAMES_IMP = ["관리자", "운영팀", "Binance Support", "고객센터", "공식 상담원", "Admin", "보안팀", "방장 비서"]


def spam_profile(rng: random.Random, cat: str) -> dict:
    """LLM 봇 계정 가정: 새 계정 60%·오래된(구입한) 계정 40%, 아이디 있음 70%."""
    new = rng.random() < 0.6
    uid = rng.randint(8_700_000_000, 8_999_999_999) if new else rng.randint(900_000_000, 7_500_000_000)
    if cat == "impersonation" and rng.random() < 0.7:
        name = rng.choice(NAMES_IMP)
    elif cat in ("classic_ad", "recruitment") and rng.random() < 0.5:
        name = rng.choice(NAMES_BIZ)
    else:
        name = rng.choice(NAMES_PLAIN)
    uname = None
    if rng.random() < 0.7:
        uname = "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(5, 9))) + \
            (str(rng.randint(1, 9999)) if rng.random() < 0.6 else "")
    return {"user_id": uid, "username": uname, "name": name}


def build_synth(rng: random.Random) -> list[dict]:
    out: list[dict] = []
    uid_seq = 0
    for i, (cat, fam, msgs) in enumerate(SPAM):
        vals = fake_values(rng)
        prof = spam_profile(rng, cat)
        room = rng.randrange(7)
        t = rng.random()  # 창 안의 상대 위치 (0~1)
        gaps = [0] + sorted(rng.randint(20, 240) for _ in msgs[1:])
        out.append({"id": f"s{i:03d}", "label": "spam", "source": "synthetic", "category": cat, "family": fam,
                    "msgs": [fill(m, vals) for m in msgs], "gaps": gaps, "room_idx": room, "t_rel": t,
                    "profile": prof, "newcomer": True, "context": None})
    for j, (fam, same, texts) in enumerate(MULTI):
        vals = fake_values(rng)
        rooms = rng.sample(range(7), 3)
        t = rng.random()
        prof0 = spam_profile(rng, "multi_room")
        for k, text in enumerate(texts):
            prof = prof0 if same else spam_profile(rng, "multi_room")
            out.append({"id": f"m{j:02d}{k}", "label": "spam", "source": "synthetic", "category": "multi_room",
                        "family": fam, "msgs": [fill(text, vals)], "gaps": [0], "room_idx": rooms[k],
                        "t_rel": t, "t_offset_s": k * rng.randint(60, 300), "profile": prof, "newcomer": True,
                        "context": None, "campaign_same_account": same})
    for i, (kind, msgs, ctx) in enumerate(HARDNEG):
        vals = fake_values(rng)
        new = rng.random() < 0.46  # 실제 정상 계정의 46% 가 '기준 데이터보다 새 ID'
        uid = rng.randint(8_700_000_000, 8_999_999_999) if new else rng.randint(300_000_000, 7_500_000_000)
        prof = {"user_id": uid, "username": None if rng.random() < 0.07 else "user" + str(rng.randint(100, 99999)),
                "name": rng.choice(NAMES_PLAIN + NAMES_BIZ[:3])}
        out.append({"id": f"h{i:03d}", "label": "legit", "source": "synthetic_hardneg", "category": "hn_" + kind,
                    "family": f"hn{i}", "msgs": [fill(m, vals) for m in msgs],
                    "gaps": [0] + [rng.randint(20, 200) for _ in msgs[1:]], "room_idx": rng.randrange(7),
                    "t_rel": rng.random(), "profile": prof, "newcomer": True, "context": ctx})
        uid_seq += 1
    for u in out:
        u["split"] = split_of(u["family"])
    return out


def build_real() -> tuple[list[dict], list[int], dict]:
    c = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rooms = [r["chat_id"] for r in c.execute(
        "SELECT chat_id, COUNT(*) n FROM messages WHERE chat_id<0 GROUP BY chat_id ORDER BY n DESC")]
    admins = {(r["chat_id"], r["user_id"]) for r in c.execute("SELECT chat_id, user_id FROM chat_admins")}
    admins |= {(r["chat_id"], r["user_id"]) for r in c.execute("SELECT chat_id, user_id FROM free_members")}
    # 약한 스팸 라벨: 실제로 제재된 사람 (CAS 밴 · 사칭 뮤트 · 도배 뮤트/반복 경고는 따로 셈)
    weak: dict[tuple[int, int], str] = {}
    stats = {}
    for r in c.execute("SELECT chat_id, target_id, action, detail FROM mod_log "
                       "WHERE action IN ('ban','mute','warn','kick') AND target_id IS NOT NULL"):
        d = r["detail"] or ""
        lab = ("cas_ban" if "스팸 명단" in d else "impersonation_mute" if "사칭" in d else
               "flood_mute" if "도배" in d else "repeat_warn" if "반복" in d else
               "captcha_kick" if "캡차" in d else "admin_" + r["action"])
        stats[lab] = stats.get(lab, 0) + 1
        weak.setdefault((r["chat_id"], r["target_id"]), lab)
    weak_user = {uid: lab for (_, uid), lab in weak.items() if lab in ("cas_ban", "impersonation_mute")}
    users = {r["user_id"]: r for r in c.execute("SELECT * FROM users")}
    joined = {(r["chat_id"], r["user_id"]): r["joined_at"] for r in c.execute("SELECT * FROM members")}
    t0, t1 = c.execute("SELECT MIN(ts), MAX(ts) FROM messages").fetchone()
    units: list[dict] = []
    all_msgs: list[dict] = []
    for r in c.execute("SELECT chat_id, user_id, text, ts, reply_to_user FROM messages "
                       "WHERE is_bot=0 AND chat_id<0 AND flagged=0 ORDER BY ts, id"):
        all_msgs.append({"chat_id": r["chat_id"], "user_id": r["user_id"], "text": r["text"] or "", "ts": r["ts"],
                         "reply": r["reply_to_user"] is not None})
    by_unit: dict[tuple[int, int], list[dict]] = {}
    for m in all_msgs:
        by_unit.setdefault((m["chat_id"], m["user_id"]), []).append(m)
    for (chat, uid), msgs in by_unit.items():
        if (chat, uid) in admins:
            continue
        u = users.get(uid)
        if u is not None and u["is_bot"]:
            continue
        first = [m for m in msgs if m["text"].strip()][:FIRST_N]
        if not first:
            continue
        lab = weak.get((chat, uid))
        if lab is None and uid in weak_user:  # 다른 방에서 CAS·사칭으로 제재된 계정 → 이 방 글도 약한 스팸
            lab = weak_user[uid]
        name = " ".join(x for x in [(u["first_name"] if u else ""), (u["last_name"] if u else "")] if x).strip()
        j = joined.get((chat, uid))
        units.append({
            "id": f"r{chat % 100000:05d}_{uid}", "label": "spam" if lab in ("cas_ban", "impersonation_mute") else "legit",
            "source": "real_weakspam" if lab in ("cas_ban", "impersonation_mute") else "real",
            "category": ("weak_" + lab) if lab else "real_member", "weak_label": lab,
            "family": f"u{uid}", "msgs": [m["text"] for m in first],
            "ts": [m["ts"] for m in first], "chat_id": chat,
            "reply": [m["reply"] for m in first],
            "profile": {"user_id": uid, "username": (u["username"] if u else None) or None, "name": name},
            "newcomer": bool(j), "joined_at": j, "n_total": len(msgs), "context": None})
    for u in units:
        u["split"] = split_of(u["family"])
    meta = {"t0": t0, "t1": t1, "rooms": rooms, "weak_label_counts": stats,
            "n_real_msgs": len(all_msgs)}
    return units, all_msgs, meta


def main() -> None:
    rng = random.Random(20260927)
    synth = build_synth(rng)
    with SYNTH.open("w") as f:
        for u in synth:
            f.write(json.dumps(u, ensure_ascii=False) + "\n")
    real, all_msgs, meta = build_real()
    rooms, t0, t1 = meta["rooms"][:7], meta["t0"], meta["t1"]
    # 가짜 단위를 실제 방·시간축에 끼워 넣기
    for u in synth:
        base = t0 + int(u["t_rel"] * (t1 - t0 - 3600)) + u.get("t_offset_s", 0)
        acc, ts = base, []
        for g in u["gaps"]:
            acc += g
            ts.append(acc)
        u["ts"], u["chat_id"] = ts, rooms[u["room_idx"]]
        u["reply"] = [u["context"] == "reply"] * len(u["msgs"])
    units = real + synth
    UNITS.write_text(json.dumps({"meta": meta, "units": units, "real_msgs": all_msgs}, ensure_ascii=False))
    cnt: dict[str, int] = {}
    for u in units:
        k = f"{u['label']}/{u['source']}"
        cnt[k] = cnt.get(k, 0) + 1
    print("units:", cnt)
    print("synthetic spam messages:", sum(len(u["msgs"]) for u in synth if u["label"] == "spam"))
    print("real legit units newcomer(joined_at known):", sum(u["newcomer"] for u in real if u["label"] == "legit"))
    print("weak label counts (mod_log):", meta["weak_label_counts"])
    print("real msgs:", meta["n_real_msgs"], "rooms:", len(meta["rooms"]))


if __name__ == "__main__":
    main()
