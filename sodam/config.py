import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .settings import parse_hhmm
from .tron import valid_tron_address


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)


@dataclass(frozen=True)
class Config:
    telegram_token: str
    openai_api_key: str
    owner_ids: frozenset[int]
    bot_name: str
    call_names: tuple[str, ...]
    model: str
    guard_model: str
    reasoning_effort: str
    daily_token_budget: int
    db_path: str
    tz: ZoneInfo
    log_chat_id: int | None
    sportsdb_key: str
    backup_dir: str = "data/backups"
    backup_keep: int = 14
    backup_time: str = "05:00"
    backup_send_to_log: bool = False
    cas_api: str = "https://api.cas.chat/check"
    lols_api: str = "https://api.lols.bot/account"   # 빈 값이면 lols 조회 안 함
    cache_retention: str = ""  # '', 'in_memory', '24h'
    image_model: str = "gpt-image-2.5-flare"         # 새 이미지 (빠름)
    image_edit_model: str = "gpt-image-2.5-sunburst"  # 사진 고치기 (원본 유지가 정확)
    image_quality: str = "medium"                    # low·medium·high (비쌀수록 오래 걸림)
    # 구독 결제 (PAY_ADDRESS 가 비어 있으면 결제 기능 꺼짐 = 모든 방 무료)
    pay_address: str = ""
    sub_price_usdt: str = "30"
    sub_days: int = 30
    trial_days: int = 3
    free_ai_per_day: int = 10
    invoice_minutes: int = 60
    trongrid_api_key: str = ""
    # all: 한 봇이 전부 / main: 포인트 게임(!) 빼고 전부 / dealer: 포인트 게임만 (게임 전용 딜러 봇)
    # 같은 DB_PATH 를 쓰면 포인트·방 설정·구독을 두 봇이 같이 본다
    bot_role: str = "all"


def load_config() -> Config:
    # 봇을 두 개(메인·딜러) 켤 때: SODAM_ENV=.env.dealer python -m sodam
    load_dotenv(os.getenv("SODAM_ENV", ".env"))
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not token:
        raise SystemExit(".env 에 TELEGRAM_BOT_TOKEN 을 넣어주세요.")
    # OPENAI_API_KEY 가 없으면 AI 기능만 꺼진 채로 실행 (관리·캡차·예약공지 등은 동작)

    bot_name = os.getenv("BOT_NAME", "").strip() or "소담"
    raw_names = os.getenv("BOT_CALL_NAMES", "").strip() or f"{bot_name}아,{bot_name}이,{bot_name}"
    names = [n.strip() for n in raw_names.split(",") if n.strip()]
    log_chat = os.getenv("LOG_CHAT_ID", "").strip()
    return Config(
        telegram_token=token,
        openai_api_key=key,
        owner_ids=_ids(os.getenv("OWNER_IDS", "")),
        bot_name=bot_name,
        # 긴 이름부터 비교해야 "소담아"가 "소담"보다 먼저 잡힘
        call_names=tuple(sorted(names, key=len, reverse=True)),
        model=os.getenv("OPENAI_MODEL", "gpt-5.4").strip(),
        guard_model=os.getenv("OPENAI_GUARD_MODEL", "gpt-5.4-mini").strip(),
        reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "").strip(),
        daily_token_budget=int(os.getenv("DAILY_TOKEN_BUDGET", "2000000")),
        db_path=os.getenv("DB_PATH", "data/sodam.db"),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Asia/Seoul")),
        log_chat_id=int(log_chat) if log_chat else None,
        sportsdb_key=os.getenv("SPORTSDB_KEY", "123").strip(),
        backup_dir=os.getenv("BACKUP_DIR", "").strip() or "data/backups",
        backup_keep=max(1, int(os.getenv("BACKUP_KEEP", "14"))),
        backup_time=parse_hhmm(os.getenv("BACKUP_TIME", "").strip() or "05:00"),
        image_model=os.getenv("OPENAI_IMAGE_MODEL", "").strip() or "gpt-image-2.5-flare",
        image_edit_model=os.getenv("OPENAI_IMAGE_EDIT_MODEL", "").strip() or "gpt-image-2.5-sunburst",
        image_quality=os.getenv("OPENAI_IMAGE_QUALITY", "").strip() or "medium",
        backup_send_to_log=os.getenv("BACKUP_SEND_TO_LOG", "").strip().lower() in ("1", "true", "yes", "on"),
        cas_api=os.getenv("CAS_API", "").strip() or "https://api.cas.chat/check",
        lols_api=os.getenv("LOLS_API", "https://api.lols.bot/account").strip(),
        cache_retention=_retention(os.getenv("OPENAI_CACHE_RETENTION", "")),
        pay_address=_pay_address(os.getenv("PAY_ADDRESS", "")),
        sub_price_usdt=_price(os.getenv("SUB_PRICE_USDT", "30")),
        sub_days=max(1, int(os.getenv("SUB_DAYS", "30"))),
        trial_days=max(0, int(os.getenv("TRIAL_DAYS", "3"))),
        free_ai_per_day=max(0, int(os.getenv("FREE_AI_PER_DAY", "10"))),
        invoice_minutes=min(180, max(10, int(os.getenv("INVOICE_MINUTES", "60")))),
        trongrid_api_key=os.getenv("TRONGRID_API_KEY", "").strip(),
        bot_role=_role(os.getenv("BOT_ROLE", "")),
    )


def _role(raw: str) -> str:
    role = raw.strip().lower() or "all"
    if role not in ("all", "main", "dealer"):
        raise SystemExit("BOT_ROLE 은 all / main / dealer 중 하나로 적어주세요.")
    return role


def _pay_address(raw: str) -> str:
    addr = raw.strip()
    if addr and not valid_tron_address(addr):
        raise SystemExit("PAY_ADDRESS 가 올바른 트론(TRC20) 주소가 아니에요. 아임토큰에서 TRX/USDT 받기 주소를 그대로 복사해주세요.")
    return addr


def _price(raw: str) -> str:
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        raise SystemExit("SUB_PRICE_USDT 는 숫자여야 해요 (예: 30)") from None
    if not Decimal("1") <= value <= Decimal("100000") or value != value.quantize(Decimal("0.01")):
        raise SystemExit("SUB_PRICE_USDT 는 1 ~ 100000 사이, 소수점 둘째 자리까지만 쓸 수 있어요")
    return str(value)


def _retention(raw: str) -> str:
    """기본 24h: 띄엄띄엄 대화하는 소통방은 캐시가 5~10분이면 사라져서 매번 전액. gpt-5.4 등은 24h 추가 요금 없음
    (OpenAI 프롬프트 캐싱 문서). 지원 안 하는 모델이면 .env 에 in_memory."""
    raw = raw.strip().lower() or "24h"
    if raw in ("in_memory", "24h"):
        return raw
    raise SystemExit("OPENAI_CACHE_RETENTION 은 비우거나 in_memory / 24h 중 하나여야 해요.")
