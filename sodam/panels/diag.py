"""🔌 원격 점검 (오너 메인): 켜기/끄기 · 토큰 바꾸기 · 오늘 조회 수. 창구 자체는 sodam/diag.py (별도 프로세스 sodam-diag)."""
from __future__ import annotations

from .. import diag, menu
from ..menu import OWNER, B, PanelCtx, Route, Screen

NOT_OWNER = Screen(None, toast="봇 오너만 쓸 수 있어요.", alert=True)


async def _is_owner(c: PanelCtx) -> bool:
    return c.uid in await c.svc.perms.owners()


async def s_diag(c: PanelCtx) -> Screen:
    if not await _is_owner(c):
        return NOT_OWNER
    db_path = c.svc.cfg.db_path
    on = await c.svc.db.get_state(0, diag.ENABLED_KEY) is not False
    tp = diag.token_path(db_path)
    installed = tp.exists()
    lines = ["🔌 <b>원격 점검 (클로드용)</b>",
             "클로드가 서버의 방 설정·대화·AI 기록·로그를 <b>읽기만</b> 할 수 있게 하는 창구예요. 쓰기·설정 변경은 못 해요.",
             "",
             f"상태: <b>{'켜짐 ✅' if on else '꺼짐 ⛔'}</b> · 창구 설치: {'됨' if installed else '아직 (다음 배포 때 자동)'}",
             f"오늘 조회: {diag.today_count(db_path)}번"]
    if installed:
        lines.append(f"토큰 지문: <code>{diag.fingerprint(tp.read_text().strip())}</code> (토큰 자체는 1:1 메시지로만)")
    kb = [[B("⛔ 끄기" if on else "✅ 켜기", "m:dgt")],
          [B("🔄 토큰 바꾸기 (예전 토큰 즉시 무효)", "m:dgr")] if installed else [],
          [B("⬅️ 처음으로", "m:home")]]
    return Screen("\n".join(lines), menu._kb([r for r in kb if r]))


async def r_toggle(c: PanelCtx) -> Screen:
    if not await _is_owner(c):
        return NOT_OWNER
    on = await c.svc.db.get_state(0, diag.ENABLED_KEY) is not False
    await c.svc.db.set_state(0, diag.ENABLED_KEY, None if not on else False)   # 끔 = False, 켬 = 줄 삭제(기본 켜짐)
    screen = await s_diag(c)
    screen.toast = "원격 점검을 껐어요." if on else "원격 점검을 켰어요."
    return screen


async def r_rotate(c: PanelCtx) -> Screen:
    if not await _is_owner(c):
        return NOT_OWNER
    tp = diag.token_path(c.svc.cfg.db_path)
    if not tp.exists():
        return Screen(None, toast="창구가 아직 설치 안 됐어요.", alert=True)
    diag.rotate_token(tp)
    await diag.notify_token(c.svc, c.bot, force=True)
    screen = await s_diag(c)
    screen.toast = "새 토큰을 1:1 로 보냈어요. 클로드 환경변수도 바꿔 주세요."
    return screen


menu.register_main(96, "dg", "🔌 원격 점검", OWNER)
for _code, _fn in (("dg", s_diag), ("dgt", r_toggle), ("dgr", r_rotate)):
    menu.register_route(_code, Route(_fn, OWNER, scoped=False))
