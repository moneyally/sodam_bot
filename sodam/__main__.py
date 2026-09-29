"""실행: python -m sodam"""
import asyncio
import logging
import subprocess
import time
from pathlib import Path

from telegram import Update
from telegram.error import NetworkError, TelegramError
from telegram.ext import Application, ApplicationBuilder, ContextTypes
from telegram.request import HTTPXRequest

from . import diag, handlers
from .announce import Announcer
from .backup import Backup
from .billing import Billing
from .captcha import Captcha
from .cas import Cas
from .config import Config, load_config
from .db import DB
from .games import GameManager
from .greet import Greeter
from .llm import LLM
from .moderation import Moderator
from .mtproto import MTProto
from .permissions import Permissions
from . import casino, namehist, persist
from .ratelimit import ChatRateLimiter
from .services import Services
from .sports import Sports
from .util import esc

# 텔레그램 전송 한도: 전체 초당 30·같은 방 분당 20 이 공식 한도 → 여유를 두고 25·18, RetryAfter 는 2번까지 기다렸다 재시도
RATE_LIMIT = dict(overall_max_rate=25, overall_time_period=1, group_max_rate=18, group_time_period=60, max_retries=2)
HEARTBEAT_SEC = 30          # data/heartbeat 파일 갱신 주기 (systemd 헬스체크가 mtime 을 봄, deploy/README.md)
POLL_STALE = 120           # getUpdates 는 30초마다 돌아옴 → 2분 넘게 안 돌아오면 받기(폴링)가 멈춘 것
ERROR_NOTIFY_GAP = 10 * 60  # 같은 종류 예외는 10분에 1번만 오너에게
ROOT = Path(__file__).resolve().parent.parent


def build_services(cfg: Config, db: DB) -> Services:
    perms = Permissions(cfg, db)
    svc = Services(cfg=cfg, db=db, perms=perms, mod=Moderator(cfg, db, perms),
                   llm=LLM(cfg, db), sports=Sports(cfg.sportsdb_key, db, cfg.tz),
                   cas=Cas(cfg.cas_api, lols_api=cfg.lols_api or None), backup=Backup(cfg, db), billing=Billing(cfg, db))
    svc.games = GameManager(svc)
    svc.greeter = Greeter(svc)
    svc.captcha = Captcha(svc)
    svc.announcer = Announcer(svc)
    if cfg.mtproto_api_id and cfg.mtproto_api_hash and cfg.bot_role != "dealer":
        svc.mtproto = MTProto(cfg, db)
    perms.on_admins = lambda bot, chat_id, admins: namehist.record_admins(svc, bot, chat_id, admins)
    return svc


BOT_SHORT_DESCRIPTION = "소통방 AI 비서 · 이름·아이디 변경 추적 · 사칭·도배 차단 (방 관리 무료)"
BOT_DESCRIPTION = ("🕵️ 멤버가 이름·@아이디를 바꾸면 알려주고, 누구든 변경 기록을 볼 수 있어요 (사칭·먹튀 확인).\n"
                   "🛡️ 입장 캡차, 도배·링크·사칭 차단, 경고·뮤트 — 무료\n"
                   "🤖 AI 비서, 게임, 예약공지, 채팅 통계\n\n"
                   "그룹에 추가하고 관리자로 지정하면 바로 시작돼요.")

DEALER_SHORT_DESCRIPTION = "소통방 포인트 게임 딜러 · 홀짝·슬롯·바카라·블랙잭·그래프·경마"
DEALER_DESCRIPTION = ("🃏 딜러 소담이 그룹에서 포인트 게임을 진행해요.\n"
                      "!가입 → !채굴 · !출석 으로 포인트 모으고 → !도움 에서 게임 고르기\n"
                      "P는 게임 포인트예요. 충전·환전·선물 기능은 없어요.")


async def set_profile(bot, dealer: bool) -> None:
    """명령 메뉴·소개 등록. 네트워크 오류로 봇 시작이 멈추지 않게 실패는 로그만."""
    try:
        await bot.set_my_commands([] if dealer else handlers.BOT_MENU)
        # 봇 프로필·검색 결과에 보이는 소개
        await bot.set_my_short_description(DEALER_SHORT_DESCRIPTION if dealer else BOT_SHORT_DESCRIPTION)
        await bot.set_my_description(DEALER_DESCRIPTION if dealer else BOT_DESCRIPTION)
    except TelegramError as e:
        logging.info("bot commands/description not updated: %s", e)


def _log_in_local_time(tz) -> None:
    """서버가 UTC 여도 로그 시각은 .env 의 TIMEZONE(기본 한국시간)으로."""
    from datetime import datetime
    logging.Formatter.converter = lambda *_: datetime.now(tz).timetuple()


def git_version(root: Path = ROOT) -> str | None:
    """git 커밋 짧은 해시. 없으면(서버에 .git 없음) VERSION 파일, 그것도 없으면 None."""
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return (root / "VERSION").read_text().strip() or None
    except OSError:
        return None


def heartbeat_path(cfg: Config) -> Path:
    # 딜러 봇은 같은 DB 폴더를 같이 써서 따로 (한쪽이 멈춰도 다른 쪽 하트비트에 가려지지 않게, deploy/healthcheck.sh)
    return Path(cfg.db_path).resolve().parent / ("heartbeat-dealer" if cfg.bot_role == "dealer" else "heartbeat")


def write_heartbeat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(int(time.time())))
    tmp.replace(path)  # 원자적 교체 → 헬스체크가 반쯤 쓴 파일을 읽지 않게


class PollRequest(HTTPXRequest):
    """getUpdates 전용 연결. 응답이 올 때마다 시각 기록 → 받기만 멈춘 경우도 하트비트가 멈춤
    (실제 2026-09-28: RemoteProtocolError 뒤 폴링만 멈췄는데 get_webhook_info 는 성공해서 재시작이 안 됐음)."""
    last_ok = time.monotonic()

    async def do_request(self, *a, **kw):
        out = await super().do_request(*a, **kw)
        PollRequest.last_ok = time.monotonic()
        return out


async def job_heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    """텔레그램에 실제로 닿고 + 받기(폴링)도 돌 때만 기록 → 멈춰 있으면 하트비트가 멈춰 감시(supervise.sh·systemd)가 재시작."""
    idle = time.monotonic() - PollRequest.last_ok
    if idle > POLL_STALE:
        logging.warning("heartbeat: 업데이트 받기가 %d초째 멈춤", idle)
        return
    try:
        await asyncio.wait_for(context.bot.get_webhook_info(), timeout=20)   # 가벼운 호출 (시작 때 get_me 는 안 부름)
    except Exception as e:  # 연결 오류·시간 초과: 이번엔 기록 안 함 (3분 넘게 이어지면 재시작됨)
        logging.warning("heartbeat: 텔레그램 확인 실패 (%s)", e)
        return
    try:
        await asyncio.to_thread(write_heartbeat, context.job.data)
    except OSError as e:
        logging.warning("heartbeat 기록 실패: %s", e)


class ErrorNotifier:
    """예상 못 한 예외를 오너(관리자 보고)에게. 같은 예외 종류는 ERROR_NOTIFY_GAP 에 1번. 네트워크 오류는 제외.
    handlers.on_error(로그) 와 별도로 붙는 두 번째 error handler (PTB 는 등록된 error handler 를 모두 호출)."""

    def __init__(self, gap: float = ERROR_NOTIFY_GAP, clock=time.monotonic):
        self.gap = gap
        self.clock = clock
        self.last: dict[str, float] = {}

    async def __call__(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if err is None or isinstance(err, NetworkError):  # TimedOut 은 NetworkError 의 하위 클래스
            return
        kind = type(err).__name__
        now = self.clock()
        if kind in self.last and now - self.last[kind] < self.gap:
            return
        self.last[kind] = now
        svc: Services | None = context.application.bot_data.get("svc")
        if not svc:
            return
        where = f"job {context.job.name}" if context.job else (type(update).__name__ if update else "-")
        try:
            await svc.mod.report(context.bot, f"[오류] <code>{esc(kind)}</code>: {esc(str(err)[:300])}\n"
                                              f"위치: {esc(where)} (같은 종류는 {int(self.gap // 60)}분간 다시 안 알림)")
        except Exception:
            logging.exception("오류 알림 전송 실패")


async def notify_owner(app: Application, text: str) -> None:
    svc: Services | None = app.bot_data.get("svc")
    if not svc:
        return
    try:
        await asyncio.wait_for(svc.mod.report(app.bot, text), timeout=15)
    except Exception as e:  # 알림 실패가 시작·종료를 막지 않게
        logging.info("오너 알림 실패: %s", e)


def build_app(cfg: Config, db: DB) -> Application:
    version = git_version()
    vtag = f" (버전 {version})" if version else ""

    async def post_init(app: Application) -> None:
        await db.open()
        app.bot_data.update(svc=build_services(cfg, db), limiter=handlers.RateLimiter(),
                            chats=set(), joins={}, cas_seen=set(), tasks=set())
        dealer = cfg.bot_role == "dealer"
        await set_profile(app.bot, dealer)
        svc: Services = app.bot_data["svc"]
        if cfg.bot_role != "main":          # ! 게임을 맡는 프로세스만 (메인·딜러 분리 때 메인이 딜러의 판을 환불하지 않게)
            await casino.startup(svc)       # kill -9·컨테이너 회수로 정산 못 한 베팅 환불 (폴링 시작 전)
        # 재시작 전 상태 복구: 메뉴 입력·예약공지 마법사·기억 정리 예약, 끝내지 못한 게임 방에 안내 (sodam/persist.py)
        await persist.restore(svc, app.bot)
        code = await svc.perms.prepare_claim_code()
        if code:
            # 서버 화면(터미널)을 볼 수 있는 사람 = 서버 주인만 알 수 있는 1회용 코드
            logging.warning("=" * 60)
            logging.warning("🔑 오너가 아직 없어요. 봇과 1:1 채팅에서 이렇게 보내세요:  /owner %s", code)
            logging.warning("   (재시작하면 코드가 바뀌어요. 등록 후엔 이 안내가 안 나와요)")
            logging.warning("=" * 60)
        # 봇 정보는 app.initialize() 가 이미 받아 둠 → 여기서 다시 조회하면 네트워크 오류로 시작이 멈출 수 있음
        logging.info("%s (@%s) 시작!%s 모델=%s", cfg.bot_name, app.bot.username, vtag,
                     cfg.model if cfg.openai_api_key else "없음 (OPENAI_API_KEY 미설정 → AI 기능 꺼짐)")
        if cfg.pay_address:
            logging.info("구독 결제 켜짐: 월 %s USDT(TRC20) / %d일, 체험 %d일, 받는 주소 %s…%s",
                         cfg.sub_price_usdt, cfg.sub_days, cfg.trial_days, cfg.pay_address[:5], cfg.pay_address[-4:])
            if not cfg.trongrid_api_key:
                logging.warning("TRONGRID_API_KEY 가 없어요. 입금 확인이 느리거나 막힐 수 있어요 (trongrid.io 무료 발급)")
        else:
            logging.info("구독 결제 꺼짐 (PAY_ADDRESS 없음) → 모든 방 무료")
        if svc.mtproto:
            svc.mtproto.start_background()   # 로그인은 뒤에서, 실패해도 봇은 계속 (오너 🔧 헬퍼 화면에 오류)
        jq = app.job_queue
        hb = heartbeat_path(cfg)
        write_heartbeat(hb)
        PollRequest.last_ok = time.monotonic()   # 받기 시계는 봇 시작부터
        jq.run_repeating(job_heartbeat, interval=HEARTBEAT_SEC, first=HEARTBEAT_SEC, data=hb, name="heartbeat")
        # 재시작으로 타이머가 사라진 임시 안내 지우기 등 (메인·딜러 봇 모두 — 자기가 보낸 글만)
        jq.run_repeating(persist.job_sweep, interval=30, first=15, name="persist_sweep")

        # 시작 알림은 폴링 시작 뒤 (네트워크가 느려도 시작이 멈추지 않게)
        async def _started(_ctx) -> None:
            await notify_owner(app, f"▶️ {esc(cfg.bot_name)} 시작{esc(vtag)}")
        jq.run_once(_started, 1, name="start_notice")

        async def _diag_token(_ctx) -> None:   # 🔌 원격 점검 창구가 토큰을 만들었으면(설치됨) 오너 1:1 로 한 번 (sodam/diag.py)
            try:
                await diag.notify_token(svc, app.bot)
            except Exception as e:
                logging.info("점검 토큰 알림 실패: %s", e)
        if cfg.bot_role != "dealer":
            jq.run_repeating(_diag_token, interval=300, first=20, name="diag_token")

    async def post_stop(app: Application) -> None:
        # post_shutdown 때는 봇 연결이 이미 닫혀 있어서 여기서 보냄
        svc: Services | None = app.bot_data.get("svc")
        if svc:   # 몇 초 모아 보내던 입장·퇴장 인사 (배포 재시작으로 사라지지 않게)
            await persist.flush_on_stop(svc, app.bot, app.bot_data)
        await notify_owner(app, f"⏹ {esc(cfg.bot_name)} 정상 종료{esc(vtag)}")

    async def post_shutdown(app: Application) -> None:
        svc: Services | None = app.bot_data.get("svc")
        if svc:
            await casino.shutdown(svc)  # 진행 중인 블랙잭·하이로우·그래프 판 환불 (DB 닫기 전)
            if svc.mtproto:
                await svc.mtproto.stop()
            await svc.sports.close()
            await svc.cas.close()
            await svc.billing.close()
        await db.close()

    app = (ApplicationBuilder()
           .token(cfg.telegram_token)
           .concurrent_updates(True)  # AI 응답을 기다리는 동안에도 도배 검사 등은 계속 돌게
           # 기본 5초는 서버 네트워크가 잠깐 느려지면 메시지 처리가 끊김 → 넉넉하게
           .connect_timeout(10).read_timeout(20).write_timeout(20).pool_timeout(10)
           .get_updates_request(PollRequest(read_timeout=30, write_timeout=20, connect_timeout=10, pool_timeout=10))
           .rate_limiter(ChatRateLimiter(**RATE_LIMIT))   # 429 는 그 방만 멈춤 (sodam/ratelimit.py)
           .post_init(post_init)
           .post_stop(post_stop)
           .post_shutdown(post_shutdown)
           .build())
    handlers.register(app, cfg.tz, cfg.backup_time, cfg.bot_role)
    app.add_error_handler(ErrorNotifier())  # handlers.on_error(로그) 다음에 호출됨
    return app


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # 30초마다 도는 작업 로그 소음 제거
    cfg = load_config()
    _log_in_local_time(cfg.tz)
    db = DB(cfg.db_path)
    app = build_app(cfg, db)
    # chat_member 는 명시적으로 받아야 오는 업데이트 (입장 메시지를 숨긴 방에서도 입장 감지)
    # 재시작 동안 쌓인 업데이트도 받는다 (버리면 그 사이 입장한 사람을 영영 모름). 오래된 메시지엔 AI 가 답하지 않음 (util.is_stale)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    except KeyboardInterrupt:
        # Ctrl+C 를 두 번 눌러 정리 도중 끊긴 경우. 데이터는 매번 커밋되므로 안전하다
        pass
    finally:
        db.stop_sync()  # 정상 종료면 이미 닫혀 있어서 아무 일도 안 함. 끊겼으면 여기서 DB 스레드 정리
    logging.info("%s 종료", cfg.bot_name)


if __name__ == "__main__":
    main()
