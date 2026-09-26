"""실행: python -m sodam"""
import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder

from . import handlers
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
from .permissions import Permissions
from . import casino, namehist
from .services import Services
from .sports import Sports


def build_services(cfg: Config, db: DB) -> Services:
    perms = Permissions(cfg, db)
    svc = Services(cfg=cfg, db=db, perms=perms, mod=Moderator(cfg, db, perms),
                   llm=LLM(cfg, db), sports=Sports(cfg.sportsdb_key, db, cfg.tz),
                   cas=Cas(cfg.cas_api), backup=Backup(cfg, db), billing=Billing(cfg, db))
    svc.games = GameManager(svc)
    svc.greeter = Greeter(svc)
    svc.captcha = Captcha(svc)
    svc.announcer = Announcer(svc)
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


def _log_in_local_time(tz) -> None:
    """서버가 UTC 여도 로그 시각은 .env 의 TIMEZONE(기본 한국시간)으로."""
    from datetime import datetime
    logging.Formatter.converter = lambda *_: datetime.now(tz).timetuple()


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # 30초마다 도는 작업 로그 소음 제거
    cfg = load_config()
    _log_in_local_time(cfg.tz)
    db = DB(cfg.db_path)

    async def post_init(app: Application) -> None:
        await db.open()
        app.bot_data.update(svc=build_services(cfg, db), limiter=handlers.RateLimiter(),
                            chats=set(), joins={}, cas_seen=set(), tasks=set())
        dealer = cfg.bot_role == "dealer"
        await app.bot.set_my_commands([] if dealer else handlers.BOT_MENU)
        try:  # 봇 프로필·검색 결과에 보이는 소개
            await app.bot.set_my_short_description(DEALER_SHORT_DESCRIPTION if dealer else BOT_SHORT_DESCRIPTION)
            await app.bot.set_my_description(DEALER_DESCRIPTION if dealer else BOT_DESCRIPTION)
        except TelegramError as e:
            logging.info("bot description not updated: %s", e)
        svc: Services = app.bot_data["svc"]
        code = await svc.perms.prepare_claim_code()
        if code:
            # 서버 화면(터미널)을 볼 수 있는 사람 = 서버 주인만 알 수 있는 1회용 코드
            logging.warning("=" * 60)
            logging.warning("🔑 오너가 아직 없어요. 봇과 1:1 채팅에서 이렇게 보내세요:  /owner %s", code)
            logging.warning("   (재시작하면 코드가 바뀌어요. 등록 후엔 이 안내가 안 나와요)")
            logging.warning("=" * 60)
        me = await app.bot.get_me()
        logging.info("%s (@%s) 시작! 모델=%s", cfg.bot_name, me.username,
                     cfg.model if cfg.openai_api_key else "없음 (OPENAI_API_KEY 미설정 → AI 기능 꺼짐)")
        if cfg.pay_address:
            logging.info("구독 결제 켜짐: 월 %s USDT(TRC20) / %d일, 체험 %d일, 받는 주소 %s…%s",
                         cfg.sub_price_usdt, cfg.sub_days, cfg.trial_days, cfg.pay_address[:5], cfg.pay_address[-4:])
            if not cfg.trongrid_api_key:
                logging.warning("TRONGRID_API_KEY 가 없어요. 입금 확인이 느리거나 막힐 수 있어요 (trongrid.io 무료 발급)")
        else:
            logging.info("구독 결제 꺼짐 (PAY_ADDRESS 없음) → 모든 방 무료")

    async def post_shutdown(app: Application) -> None:
        svc: Services | None = app.bot_data.get("svc")
        if svc:
            await casino.shutdown(svc)  # 진행 중인 블랙잭·하이로우·그래프 판 환불 (DB 닫기 전)
            await svc.sports.close()
            await svc.cas.close()
            await svc.billing.close()
        await db.close()

    app = (ApplicationBuilder()
           .token(cfg.telegram_token)
           .concurrent_updates(True)  # AI 응답을 기다리는 동안에도 도배 검사 등은 계속 돌게
           # 기본 5초는 서버 네트워크가 잠깐 느려지면 메시지 처리가 끊김 → 넉넉하게
           .connect_timeout(10).read_timeout(20).write_timeout(20).pool_timeout(10)
           .get_updates_read_timeout(30)
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .build())
    handlers.register(app, cfg.tz, cfg.backup_time, cfg.bot_role)
    # chat_member 는 명시적으로 받아야 오는 업데이트 (입장 메시지를 숨긴 방에서도 입장 감지)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except KeyboardInterrupt:
        # Ctrl+C 를 두 번 눌러 정리 도중 끊긴 경우. 데이터는 매번 커밋되므로 안전하다
        pass
    finally:
        db.stop_sync()  # 정상 종료면 이미 닫혀 있어서 아무 일도 안 함. 끊겼으면 여기서 DB 스레드 정리
    logging.info("%s 종료", cfg.bot_name)


if __name__ == "__main__":
    main()

