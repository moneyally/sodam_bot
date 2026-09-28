"""🔧 헬퍼: MTProto 도우미 상태 (오너 1:1 전용, sodam/mtproto.py).

m:mt    상태 — ① 봇 세션(참가자) · ② 사용자 세션(채널 조회수): 설정됨? 연결됨? 마지막 오류·FloodWait + 켜는 법
m:mtr   🔄 다시 연결 (뒤에서 끊고 다시 로그인, 30초에 1번)
라우트(OWNER) + _owner_only 로 누를 때마다 오너인지 다시 확인. 참가자 기반 데이터는 채널 관리 화면이 보여 줌 (여기 아님).
"""
from __future__ import annotations

from functools import wraps

from .. import menu
from ..menu import OWNER, B, PanelCtx, Route, Screen
from ..util import esc, fmt_time

NOT_OWNER = Screen(None, toast="봇 오너만 쓸 수 있어요.", alert=True)

HOW_BOT = ("<b>① 켜는 법</b>\n"
           "1. my.telegram.org 에 로그인 → <b>API development tools</b> → 앱 만들기\n"
           "2. 나온 <code>api_id</code>·<code>api_hash</code> 를 서버 <code>.env</code> 에\n"
           "<code>MTPROTO_API_ID=…</code>\n<code>MTPROTO_API_HASH=…</code>\n"
           "3. 봇 재시작 → 봇 토큰으로 자동 로그인 (봇 폴링과 안 겹쳐요)")
HOW_USER = ("<b>② 켜는 법 (선택, 채널 조회수)</b>\n"
            "봇 전용으로 만든 텔레그램 계정으로 서버 터미널에서\n<code>python tools/mtproto_login.py</code>\n"
            "→ 전화번호·코드·2단계 비밀번호를 <b>터미널에만</b> 입력 → 봇 재시작.\n"
            "⚠️ 로그인 코드를 텔레그램 채팅(이 봇 포함)으로 보내면 텔레그램이 코드를 바로 무효화해요.\n"
            "그 계정은 조회수를 볼 채널에 들어가 있어야 해요. 끄려면 <code>data/mtproto_user.session</code> 삭제 후 재시작.")


def _owner_only(fn):
    @wraps(fn)
    async def wrapped(c: PanelCtx) -> Screen:
        if c.uid not in await c.svc.perms.owners():   # 라우터 확인과 이중으로 (누를 때마다)
            return NOT_OWNER
        return await fn(c)
    return wrapped


def _block(title: str, s: dict, tz) -> list[str]:
    lines = [f"<b>{title}</b>",
             f"설정: {'✅' if s['configured'] else '❌ 안 됨'} · 연결: {'✅ ' + esc(s['me']) if s['connected'] else '❌'}"]
    if s["last_error"]:
        lines.append(f"마지막 오류 ({fmt_time(s['last_error_ts'], tz)}): <code>{esc(s['last_error'])}</code>")
    if s["last_flood"]:
        ts, secs = s["last_flood"]
        lines.append(f"마지막 FloodWait ({fmt_time(ts, tz)}): {secs}초"
                     + (f" · {fmt_time(int(s['flood_until']), tz)} 까지 쉼" if s["flood_until"] else ""))
    return lines


@_owner_only
async def s_status(c: PanelCtx) -> Screen:
    mt, tz = c.svc.mtproto, c.svc.cfg.tz
    lines = ["🔧 <b>MTProto 헬퍼</b>",
             "Bot API 로는 못 얻는 데이터(채널·그룹 참가자 목록, 채널 글 조회수)를 텔레그램 MTProto 로 가져와요. "
             "선택 기능이라 꺼져 있어도 봇은 그대로 돌아요.", ""]
    kb = []
    if mt is None:
        lines += ["지금 <b>꺼져 있어요</b> (.env 에 MTPROTO_API_ID/HASH 없음).", "", HOW_BOT, "", HOW_USER]
    else:
        st = mt.status()
        if st["starting"]:
            lines += ["⏳ 연결하는 중… 잠시 뒤 새로고침", ""]
        lines += _block("① 봇 세션 · 참가자 목록", st["bot"], tz) + [""]
        lines += _block("② 사용자 세션 · 채널 조회수 (선택)", st["user"], tz) + [""]
        if not st["bot"]["connected"]:
            lines += [HOW_BOT, ""]
        if not st["user"]["configured"]:
            lines += [HOW_USER]
        kb.append([B("🔄 다시 연결", "m:mtr")])
    kb.append([B("🔃 새로고침", "m:mt"), B("⬅️ 처음으로", "m:home")])
    return Screen("\n".join(lines).rstrip(), menu._kb(kb))


@_owner_only
async def r_restart(c: PanelCtx) -> Screen:
    mt = c.svc.mtproto
    if mt is None:
        return Screen(None, toast="설정이 없어요 (.env 에 MTPROTO_API_ID/HASH).", alert=True)
    if not mt.restart():
        return Screen(None, toast="방금 연결을 시작했어요. 30초 뒤 다시 눌러주세요.", alert=True)
    screen = await s_status(c)
    screen.toast = "다시 연결 중… 잠시 뒤 🔃 새로고침"
    return screen


menu.register_main(95, "mt", "🔧 헬퍼", OWNER)
menu.register_route("mt", Route(s_status, OWNER, scoped=False))
menu.register_route("mtr", Route(r_restart, OWNER, scoped=False))
