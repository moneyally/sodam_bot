"""🔘 모든 메뉴 버튼: 버튼이 보내는 칸 수 vs 화면 함수가 읽는 칸 번호 — python tests/run_all.py button_args

2026-11-17 프레스티지 짝궁 '채팅 순위 추가 기간 버튼이 안 눌려요': 버튼은 m:crkxt:<방>:<기간> (방 번호는 menu 가 떼고 넘겨
handler 엔 칸 1개) 인데 handler 가 c.arg(1) 을 읽어 늘 빈칸 → 화면만 다시 그리고 저장 안 됨. 하네스는 '예외 없음'만 봐서 못 잡았음.
→ 코드 전체의 'm:<라우트>:…' 버튼 모양(파이썬 문법으로 f-string 칸 수)을 모아, 그 라우트 handler 가 읽는 c.arg(N) 최대 N 이
버튼이 주는 칸 수보다 크거나 같으면 FAIL. 새 버튼·화면이 생겨도 자동으로 검사됨.
"""
import ast
import importlib
import inspect
import pathlib
import pkgutil
import re

from fakes import runner

import sodam.handlers  # noqa: F401  (모든 화면 등록)
import sodam.panels as P
from sodam import menu

test, run_all = runner()
# 확인한 예외 (없어도 되는 칸이거나, 버튼 글자를 이어 붙여 만들어 검사가 못 셈) — 새로 넣을 땐 이유와 함께
OPTIONAL = {
    "ckb": "쪽 번호 — menu 쪽 나눔 도우미가 ':쪽' 을 붙임, 없으면 0쪽",
    "ib": "쪽 번호 (선택)", "ibr": "쪽 번호 (선택)", "kb": "쪽 번호 (선택)",
    "spx": "f'…:{id}:' + 동작 글자 로 이어 붙임, ':p' 는 목록에서 누를 때만",
    "cp": "composer 버튼은 도우미가 'm:cp:<초안>:<동작>…' 을 만듦",
}
ROOT = pathlib.Path(__file__).resolve().parent.parent / "sodam"


def _load_panels():
    for m in pkgutil.iter_modules(P.__path__):
        importlib.import_module("sodam.panels." + m.name)


def _literal(node) -> str | None:
    """문자열 상수·f-string 을 '{}' 자리표시로 펼침."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            out.append(v.value if isinstance(v, ast.Constant) else "{}")
        return "".join(out)
    return None


def button_shapes() -> dict[str, set[int]]:
    """라우트 코드 → 버튼이 code 뒤에 붙이는 칸 수들."""
    shapes: dict[str, set[int]] = {}
    for f in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            s = _literal(node)
            if not s or not s.startswith("m:"):
                continue
            m = re.match(r"m:([a-z0-9_]+)(.*)$", s, re.S)
            if not m or m.group(1) == "k":          # m:k:<토큰> 은 토큰 동작이라 따로
                continue
            rest = m.group(2)
            if rest and not rest.startswith(":"):
                continue
            shapes.setdefault(m.group(1), set()).add(rest.count(":"))
    return shapes


def max_arg(fn) -> int:
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return -1
    ns = [int(x) for x in re.findall(r"c\.arg\((\d+)\)", src)] + [int(x) for x in re.findall(r"c\.args\[(\d+)\]", src)]
    return max(ns) if ns else -1


@test
async def every_button_gives_the_args_its_screen_reads():
    _load_panels()
    shapes = button_shapes()
    bad = []
    for code, route in sorted(menu.ROUTES.items()):
        used = max_arg(route.handler)
        got = shapes.get(code)
        if used < 0 or not got or code in OPTIONAL:
            continue
        give = max(n - 1 if route.scoped else n for n in got)   # scoped: 첫 칸 = 방 번호(menu 가 뗌)
        if used >= give:
            bad.append(f"{code}: 화면이 c.arg({used}) 를 읽는데 버튼은 칸 {give}개뿐 {sorted(got)} — {route.handler.__qualname__}")
    assert not bad, "\n".join(bad)


@test
async def the_audit_catches_the_crkxt_bug():
    """검사 자체가 진짜 버그 꼴을 잡는지 (가짜 라우트로)."""
    from sodam.menu import Route

    async def broken(c):
        return c.arg(1)
    shapes = {"zzt": {2}}             # m:zzt:{cid}:{p}
    used, give = max_arg(broken), max(n - 1 for n in shapes["zzt"])
    assert used >= give and Route(broken, menu.ADMIN).scoped


if __name__ == "__main__":
    import asyncio
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
