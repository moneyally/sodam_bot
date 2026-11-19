"""🧹 서비스 메시지 청소 화면 (그룹 허브). 동작은 sodam/cleanservice.py.

m:clsrv:<방>   끔 / 입장·퇴장만 / 전부 프리셋
입장 메시지는 옛 delete_join_message 설정으로도 켜지는데, 여기서 '입장·퇴장' 이상을 켜면 그것까지 같이 처리.
"""
from __future__ import annotations

from .. import cleanservice, menu  # noqa: F401  (cleanservice: 설정 등록 보장)
from ..menu import B, HubItem, PanelCtx, Screen

menu.register_preset("clean_service", [("off", "❌ 끔"), ("basic", "입장·퇴장"), ("all", "🧹 전부")], "clsrv")


async def s_cls(c: PanelCtx) -> Screen:
    s = await c.svc.db.get_settings(c.cid)
    mode = s.get("clean_service", "off")
    now = {"off": "❌ 꺼짐", "basic": "입장·퇴장 메시지 청소", "all": "🧹 전부 청소 (핀·영상채팅 등)"}.get(mode, mode)
    join_only = s["delete_join_message"] and mode == "off"
    lines = ["🧹 <b>서비스 메시지 청소</b>",
             "텔레그램이 자동으로 다는 <b>회색 알림</b>(○○님 입장·퇴장, 고정된 메시지, 영상채팅 시작 등)을 지워서 채팅을 깔끔하게 해요.", "",
             "• <b>입장·퇴장</b> — 입장/퇴장 알림만 지움",
             "• <b>전부</b> — 거기에 📌고정·제목/사진 변경·🎥영상채팅 알림까지", "",
             f"지금: <b>{now}</b>"]
    if join_only:
        lines.append("<i>(입장 메시지는 예전 설정으로 지워지고 있어요. 퇴장까지 지우려면 '입장·퇴장'을 누르세요.)</i>")
    lines.append("\n봇에게 <b>'메시지 삭제' 권한</b>이 있어야 지워져요. 지운 건 되돌릴 수 없어요(알림만 사라지고 고정·제목은 그대로).")
    rows = [menu._preset_row(s, c.cid, "clean_service"), menu._back(c.cid)]
    return Screen("\n".join(lines), menu._kb(rows))


menu.register_hub(HubItem(26, "clsrv", "🧹 서비스 메시지 청소"))
menu.register_screen("clsrv", s_cls)
