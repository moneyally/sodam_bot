"""하네스 데이터: 🚀 빠른 설정 — 봇 초대 때 대표님 1:1 버튼을 시작점으로, 진행 중 초안(이어서 하기)도 넣어 둔다."""
import json
import time

import harness
from fakes import FakeBot

from sodam import subscription


async def seed_onboard(svc):
    cid = harness.CHAT
    for uid in (harness.TG, harness.OWNER):
        await svc.db._write("INSERT OR REPLACE INTO onboard_state(chat_id, user_id, kind, answers, ver, expires) "
                            "VALUES(?,?,?,?,?,?)", (cid, uid, "trade", json.dumps({"scam_guard": False}), 3,
                                                    int(time.time()) + 1800))
    bot = FakeBot()
    await subscription.send_panel_dm(svc, bot, cid, harness.TG)   # 봇 초대 때 가는 1:1 (결제 + 🚀 버튼)
    harness.add_buttons(bot.named("send_message")[-1][3]["reply_markup"])


harness.SEEDERS.append(seed_onboard)
