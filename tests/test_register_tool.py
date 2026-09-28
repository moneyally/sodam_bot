"""다른 모듈이 AI 도구를 더하기: python tests/run_all.py register_tool"""
from fakes import runner

from sodam import tools
from sodam.permissions import Role

test, run_all = runner()


@test
def register_tool_adds_once_and_read_only():
    async def fn(ctx, a):
        return "ok"
    t = tools.Tool("zz_probe", "시험용", {}, [], fn, Role.ADMIN)
    tools.register_tool(t, read_only=True)
    tools.register_tool(t, read_only=True)                     # 같은 도구를 또 불러도 한 번만
    try:
        assert [x.name for x in tools.TOOLS].count("zz_probe") == 1 and "zz_probe" in tools.READ_ONLY
        assert t in tools.available(Role.ADMIN, {}, False) and t not in tools.available(Role.MEMBER, {}, False)
        other = tools.Tool("zz_probe", "다른 것", {}, [], fn, Role.ADMIN)
        try:
            tools.register_tool(other)
            raise AssertionError("이름 중복이 통과됨")
        except ValueError:
            pass
    finally:
        tools.TOOLS.remove(t)
        tools._BY_NAME.pop("zz_probe", None)
        tools.READ_ONLY.discard("zz_probe")


if __name__ == "__main__":
    run_all()
