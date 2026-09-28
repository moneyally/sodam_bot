"""하네스용: 이름 변경 기록이 있는 멤버 (🕵️ 이름 기록 화면이 비어있지 않게)."""
import harness
from fakes import fake_user

from sodam import namehist


async def seed(svc):
    for first, uname in (("김대표", "kim_old"), ("<b>관리자</b> & 공지", "admin_fake"), ("오너", "owner_now")):
        await namehist.record(svc.db, fake_user(harness.OWNER, first, uname))


harness.SEEDERS.append(seed)
