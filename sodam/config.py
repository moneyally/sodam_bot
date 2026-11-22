import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .settings import parse_hhmm
from .tron import valid_tron_address


def default_call_names(name: str) -> list[str]:
    """봇 이름 → 부르는 말. 받침 있으면 '소담아·소담이', 없으면 '프레스티지야' (한국어 호격).
    예전엔 늘 '이름아·이름이' 라서 받침 없는 클론 이름은 '프레스티지야' 를 못 알아들음 (2026-11-17)."""
    last = name[-1:] if name else ""
    if "가" <= last <= "힣":
        has_batchim = (ord(last) - 0xAC00) % 28 != 0
        return [name + "아", name + "이", name] if has_batchim else [name + "야", name]
    return [name + "아", name + "야", name]          # 한글이 아니면 둘 다


def name_variants(bot_name: str) -> list[str]:
    """봇 이름에서 사람들이 실제로 부르는 말들: 그대로 · 띄어쓰기 뺀 것 · 끝의 '봇/bot' 뗀 것.
    '뉴월드 봇' → ['뉴월드 봇', '뉴월드봇', '뉴월드'] — 손님은 '뉴월드야' 라고 부름 (2026-11-17 오너 비상: 뉴월드 클론이 안 답함).
    한 글자로 줄어드는 건 안 씀 (아무 말에나 걸리게 됨)."""
    import re
    name = " ".join((bot_name or "").split())
    out: list[str] = []
    for v in (name, name.replace(" ", ""), re.sub(r"\s*(?:봇|bot|BOT|Bot)$", "", name).strip()):
        if len(v) >= 2 and v not in out:
            out.append(v)
        if " " in v and len(v.replace(" ", "")) >= 2 and v.replace(" ", "") not in out:
            out.append(v.replace(" ", ""))
    return out or [bot_name]


def call_names_for(bot_name: str) -> list[str]:
    """이 봇을 부르는 말 기본값. 화이트라벨 클론은 제 이름 + '소담아·소담이·소담' 도 받음 —
    소담 쓰던 방을 넘겨받으면 멤버들이 하던 대로 '소담아' 불러도 답하게 (2026-11-17 오너 '둘 다 먹히게').
    같은 방에 본체 소담이 아직 있으면 둘 다 답하니, 넘길 땐 본체를 빼는 게 순서."""
    names: list[str] = []
    for base in name_variants(bot_name):
        names += [n for n in default_call_names(base) if n not in names]
    if bot_name != "소담":
        names += [n for n in default_call_names("소담") if n not in names]
    return names


def resolve_call_names(raw: str, bot_name: str) -> list[str]:
    """BOT_CALL_NAMES 설정(쉼표)을 각 이름의 **호격형까지 펼친다** — '목사,소담' → 목사야·목사·소담아·소담이·소담.
    예전엔 적은 글자 그대로만 호출어라, '소담' 만 넣으면 '소담아·소담이' 가 안 먹혔음 (2026-11-21 수리남 목사 봇 '목사야·소담아 호출 안 됨').
    비우면 봇 이름 기준 기본값(call_names_for)."""
    raw = (raw or "").strip()
    if not raw:
        return call_names_for(bot_name)
    out: list[str] = []
    for n in raw.split(","):
        n = n.strip()
        if n:
            out += [v for v in default_call_names(n) if v not in out]   # 호격(~야/~아/~이) 자동 + 원형
    return out


def default_backup_dir(db_path: str) -> str:
    """백업 폴더 기본값 = DB 가 있는 폴더 안 backups. 본체(data/sodam.db)는 예전과 같은 data/backups,
    클론(data/clones/<slug>/sodam.db)은 제 폴더 안 — 예전엔 둘 다 'data/backups' 라 05:00 에 같은 이름으로 동시에 쓰다
    서로 파일을 지워 본체 백업이 실패하고 그날 백업이 다 날아감, 오래된 것 정리도 서로의 백업을 지울 수 있었음 (2026-11-17)."""
    return str(Path(db_path).parent / "backups")


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
    sub_price_usdt: str = "50"
    sub_days: int = 30
    trial_days: int = 3
    free_ai_per_day: int = 10
    invoice_minutes: int = 60
    trongrid_api_key: str = ""
    # all: 한 봇이 전부 / main: 포인트 게임(!) 빼고 전부 / dealer: 포인트 게임만 (게임 전용 딜러 봇)
    # 같은 DB_PATH 를 쓰면 포인트·방 설정·구독을 두 봇이 같이 본다
    bot_role: str = "all"
    # 생각하는 에이전트 (agent.wants_thinking): off / auto(기본: 관리자·오너 요청 + 여러 단계·분석 요청) / always.
    # 그 요청은 Responses API 로 추론+도구를 같이 씀 (chat.completions 는 도구와 추론을 같이 못 씀). 멤버 잡담은 예전 방식(싸게)
    agent_think: str = "auto"
    agent_think_effort: str = "low"
    # 🧭 하이브리드 라우팅 (sodam/route.py): 잡담·조회는 이 작은 모델, 일은 model. 빈 값이면 전부 model (기능 끔).
    # 기본값은 .env 읽을 때(AGENT_LIGHT_MODEL, 기본 gpt-5.4-mini) — Config 를 직접 만드는 곳(테스트)은 끔
    light_model: str = ""
    # MTProto 도우미 (sodam/mtproto.py, 선택): my.telegram.org 에서 받은 API ID/HASH. 비우면 꺼짐
    mtproto_api_id: int = 0
    mtproto_api_hash: str = ""
    # 로마자/영어 호출어 (외국인 번역기 — 'Sodam help me …'), 대소문자 무시. 비우면 영어 호출 꺼짐
    call_names_en: tuple[str, ...] = ("sodam",)
    # 문의처 (봇 초대 인사·새 관리자 안내에 표시). 비우면 안 보임. .env BOT_SUPPORT_CONTACT
    support_contact: str = ""

    @property
    def is_clone(self) -> bool:
        """화이트라벨 클론봇인지 — DB 가 data/clones/<slug>/ 안이면 클론 (본체는 data/sodam.db).
        클론은 📓 일기를 등록 채널에 안 올림 (2026-11-22 오너 '클론들이 자꾸 채널에 일기 올림')."""
        return "clones" in Path(self.db_path).parts


def load_config() -> Config:
    # 봇을 두 개(메인·딜러) 켤 때: SODAM_ENV=.env.dealer python -m sodam
    load_dotenv(os.getenv("SODAM_ENV", ".env"))
    # 오너가 1:1 로 넣은 API 키 (sodam/apikeys.py — data/keys.env, 같은 이름이면 이쪽이 이김)
    load_dotenv(Path(os.getenv("DB_PATH", "data/sodam.db")).resolve().parent / "keys.env", override=True)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not token:
        raise SystemExit(".env 에 TELEGRAM_BOT_TOKEN 을 넣어주세요.")
    # OPENAI_API_KEY 가 없으면 AI 기능만 꺼진 채로 실행 (관리·캡차·예약공지 등은 동작)

    bot_name = os.getenv("BOT_NAME", "").strip() or "소담"
    names = resolve_call_names(os.getenv("BOT_CALL_NAMES", ""), bot_name)   # 설정 이름도 호격 자동 (목사 → 목사야·목사)
    # 외국인(번역기)이 영어로 'Sodam help me …' 처럼 부르면 호출로 인식 (대소문자 무시).
    # 클론봇도 소담 방에서 넘어온 멤버가 'Sodam' 으로 부를 수 있어 같이 받음 (2026-11-17 오너)
    raw_en = os.getenv("BOT_CALL_NAMES_EN", "").strip() or "sodam"
    en_names = [n.strip().lower() for n in raw_en.split(",") if n.strip()]
    log_chat = os.getenv("LOG_CHAT_ID", "").strip()
    return Config(
        telegram_token=token,
        openai_api_key=key,
        owner_ids=_ids(os.getenv("OWNER_IDS", "")),
        bot_name=bot_name,
        # 긴 이름부터 비교해야 "소담아"가 "소담"보다 먼저 잡힘
        call_names=tuple(sorted(names, key=len, reverse=True)),
        call_names_en=tuple(sorted(en_names, key=len, reverse=True)),
        model=os.getenv("OPENAI_MODEL", "gpt-5.4").strip(),
        guard_model=os.getenv("OPENAI_GUARD_MODEL", "gpt-5.4-mini").strip(),
        reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "").strip(),
        daily_token_budget=int(os.getenv("DAILY_TOKEN_BUDGET", "2000000")),
        db_path=os.getenv("DB_PATH", "data/sodam.db"),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Asia/Seoul")),
        log_chat_id=int(log_chat) if log_chat else None,
        sportsdb_key=os.getenv("SPORTSDB_KEY", "123").strip(),
        backup_dir=os.getenv("BACKUP_DIR", "").strip() or default_backup_dir(os.getenv("DB_PATH", "data/sodam.db")),
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
        sub_price_usdt=_price(os.getenv("SUB_PRICE_USDT", "50")),
        sub_days=max(1, int(os.getenv("SUB_DAYS", "30"))),
        trial_days=max(0, int(os.getenv("TRIAL_DAYS", "3"))),
        free_ai_per_day=max(0, int(os.getenv("FREE_AI_PER_DAY", "10"))),
        invoice_minutes=min(180, max(10, int(os.getenv("INVOICE_MINUTES", "60")))),
        trongrid_api_key=os.getenv("TRONGRID_API_KEY", "").strip(),
        bot_role=_role(os.getenv("BOT_ROLE", "")),
        agent_think=_choice("AGENT_THINK", "auto", ("off", "auto", "always")),
        agent_think_effort=_choice("AGENT_THINK_EFFORT", "low", ("low", "medium", "high")),
        light_model=os.getenv("AGENT_LIGHT_MODEL", "gpt-5.4-mini").strip(),
        mtproto_api_id=_int0(os.getenv("MTPROTO_API_ID", "")),
        mtproto_api_hash=os.getenv("MTPROTO_API_HASH", "").strip(),
        support_contact=os.getenv("BOT_SUPPORT_CONTACT", "@ban_1599").strip(),
    )


def _int0(raw: str) -> int:
    """선택 기능의 숫자 설정: 잘못 적어도 봇은 켜지고 그 기능만 꺼짐."""
    raw = raw.strip()
    return int(raw) if raw.isdecimal() else 0


def _choice(key: str, default: str, allowed: tuple[str, ...]) -> str:
    value = os.getenv(key, "").strip().lower() or default
    if value not in allowed:
        raise SystemExit(f"{key} 는 비우거나 {' / '.join(allowed)} 중 하나여야 해요.")
    return value


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
