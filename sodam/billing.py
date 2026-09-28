"""방당 월정액 구독 (USDT TRC20).

보안 원칙
- 서버에는 '받는 주소'만 둔다. 개인키·시드는 절대 두지 않는다 (아임토큰 등 지갑 앱이 보관).
- 입금 확인은 사용자 말이 아니라 트론 블록체인(TronGrid)을 직접 조회해서 한다.
- 확인 조건: 공식 USDT 컨트랙트 / 받는 주소 / 정확한 금액 / 확정(solidified) 거래 / 청구서 유효시간 / 거래 1회만 사용
- 청구서마다 금액 끝자리를 다르게 해서(예: 30.0137) 누가 낸 돈인지 구분한다.
- 결제 정보(금액·주소)는 방에 노출하지 않고 관리자 1:1 채팅에서만 보여준다.

참고: https://developers.tron.network/reference/get-trc20-transaction-info-by-account-address
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

import httpx

from .config import Config
from .db import DB
from .tron import USDT_CONTRACT

log = logging.getLogger(__name__)

UNIT = 1_000_000
TRONGRID = "https://api.trongrid.io"
LATE_GRACE = 30 * 60      # 유효시간 안에 보냈는데 확인이 늦은 경우를 위해 만료 후에도 30분 더 찾아봄
IDLE_SCAN = 10 * 60       # 대기 청구서가 없어도 이 간격으로 입금 조회 (만료 후 늦은 입금·청구서 없는 입금 탐지)
CURSOR_KEY = "billing_cursor_ms"   # chat_state(chat_id=0): 마지막으로 본 입금의 block_timestamp(ms)
CURSOR_OVERLAP = 10 * 60  # 확정이 늦게 된 거래를 놓치지 않게 커서보다 10분 앞부터 다시 조회 (중복은 tx UNIQUE 로 걸러짐)
FIRST_LOOKBACK = LATE_GRACE  # 커서도 청구서도 없을 때(첫 실행) 되돌아볼 기간
REUSE_LEFT = 5 * 60       # 청구서 다시 누름: 남은 시간이 이보다 짧으면 새로 (곧 끝날 청구서로 보내 '늦은 입금'이 되지 않게)
STREAK_KEY = "billing_fail_streak"
FAIL_ALERT = 3            # TronGrid 조회가 연속 이만큼 실패하면 오너에게 한 번 알림


def fmt_usdt(units: int) -> str:
    return str((Decimal(units) / UNIT).quantize(Decimal("0.0001"), rounding=ROUND_DOWN))


@dataclass
class Status:
    state: str          # free(결제 기능 꺼짐) / trial / paid / expired
    until: int | None   # 만료 시각 (초)

    @property
    def active(self) -> bool:
        return self.state in ("free", "trial", "paid")


class Billing:
    def __init__(self, cfg: Config, db: DB, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.db = db
        self.http = http or httpx.AsyncClient(timeout=15)
        self.price_units = int(Decimal(cfg.sub_price_usdt) * UNIT)
        self._lock = asyncio.Lock()
        self._inv_lock = asyncio.Lock()  # 청구서 만들기: 연타·동시 생성이 같은 금액·청구서 두 장을 만들지 않게
        self._last_scan = 0.0      # 마지막 성공 조회 (monotonic)
        self.fail_streak = 0       # TronGrid 연속 실패 횟수 (DB chat_state 0/STREAK_KEY 에도 → 장애 중 재시작해도 복구 알림)
        self._streak_loaded = False
        self._alert: str | None = None  # "down"/"up" — run_check 가 한 번 꺼내서 보고

    async def _save_streak(self) -> None:
        try:
            await self.db.set_state(0, STREAK_KEY, self.fail_streak or None)
        except Exception as e:   # 기록 실패는 알림만 못 이어갈 뿐
            log.warning("billing streak not saved: %s", e)

    def take_alert(self) -> str | None:
        """TronGrid 장애('down', 연속 FAIL_ALERT 번 실패 시 1번)·복구('up') 알림을 한 번만 꺼낸다."""
        a, self._alert = self._alert, None
        return a

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.pay_address)

    async def close(self) -> None:
        await self.http.aclose()

    # ── 구독 상태 ─────────────────────────────────────────
    async def ensure_trial(self, chat_id: int, added_by: int | None = None) -> None:
        """방에 처음 들어갔을 때 무료 체험 시작 (이미 있으면 그대로)."""
        if chat_id >= 0:
            return
        await self.db.start_subscription(chat_id, int(time.time()) + self.cfg.trial_days * 86400, added_by)

    async def status(self, chat_id: int) -> Status:
        if not self.enabled or chat_id >= 0:  # 1:1 채팅은 구독 대상이 아님 (AI 는 별도 무료 한도로 관리)
            return Status("free", None)
        row = await self.db.get_subscription(chat_id)
        if not row:
            await self.ensure_trial(chat_id)
            row = await self.db.get_subscription(chat_id)
        now = int(time.time())
        if row["paid_until"] and row["paid_until"] > now:
            return Status("paid", row["paid_until"])
        if row["trial_until"] and row["trial_until"] > now:
            return Status("trial", row["trial_until"])
        return Status("expired", max(row["paid_until"] or 0, row["trial_until"] or 0))

    async def active(self, chat_id: int) -> bool:
        return (await self.status(chat_id)).active

    async def extend(self, chat_id: int, days: int) -> int:
        """남은 기간(유료·체험 중 늦은 쪽) 뒤로 이어서 연장. SQL 한 번으로 처리해 동시 연장이 서로 덮어쓰지 않게."""
        return await self.db.extend_paid(chat_id, days * 86400, int(time.time()))

    # ── 청구서 ────────────────────────────────────────────
    async def create_invoice(self, chat_id: int, user_id: int):
        """같은 방에 아직 유효한 청구서가 있으면 그걸 다시 보여준다 (버튼 연타 대비)."""
        async with self._inv_lock:
            return await self._create_invoice_locked(chat_id, user_id)

    async def _create_invoice_locked(self, chat_id: int, user_id: int):
        now = int(time.time())
        # 관리자마다 자기 청구서 (다른 관리자 청구서를 보여주면 그 사람은 확인 버튼을 쓸 수 없음)
        existing = await self.db.open_invoice(chat_id, now + REUSE_LEFT, user_id)
        if existing:
            return existing
        taken = await self.db.pending_amounts(now - LATE_GRACE)
        for _ in range(50):
            amount = self.price_units + secrets.randbelow(999) * 100 + 100  # +0.0001 ~ +0.0999
            if amount not in taken:
                break
        else:
            raise RuntimeError("대기 중인 청구서가 너무 많아요. 잠시 후 다시 시도해주세요.")
        await self.db.add_invoice(chat_id, user_id, amount, now, now + self.cfg.invoice_minutes * 60)
        return await self.db.open_invoice(chat_id, now, user_id)

    # ── 블록체인 조회 ─────────────────────────────────────
    async def fetch_transfers(self, since_ms: int) -> list[dict]:
        headers = {"TRON-PRO-API-KEY": self.cfg.trongrid_api_key} if self.cfg.trongrid_api_key else {}
        params = {"only_confirmed": "true", "only_to": "true", "contract_address": USDT_CONTRACT,
                  "min_timestamp": since_ms, "limit": 200, "order_by": "block_timestamp,asc"}
        url = f"{TRONGRID}/v1/accounts/{self.cfg.pay_address}/transactions/trc20"
        out: list[dict] = []
        for _ in range(5):  # 최대 1000건
            r = await self.http.get(url, params=params, headers=headers)
            r.raise_for_status()
            body = r.json()
            out.extend(body.get("data") or [])
            fingerprint = (body.get("meta") or {}).get("fingerprint")
            if not fingerprint:
                break
            params["fingerprint"] = fingerprint
        return out

    def _valid_transfer(self, t: dict) -> int | None:
        """우리 주소로 온 진짜 USDT 전송이면 금액(단위), 아니면 None."""
        token = t.get("token_info") or {}
        try:
            value = int(t.get("value", ""))
        except (TypeError, ValueError):
            return None
        if (token.get("address") != USDT_CONTRACT or int(token.get("decimals", -1)) != 6
                or t.get("to") != self.cfg.pay_address or t.get("type") != "Transfer" or value <= 0):
            return None
        return value

    async def check_pending(self) -> tuple[list[dict], list[dict]]:
        """대기 청구서와 블록체인 입금을 대조. (결제된 청구서들, 청구서와 안 맞는 입금들).
        버튼과 30초 주기 작업이 동시에 돌아도 한 번에 하나만 대조하도록 잠근다."""
        if not self.enabled:
            return [], []
        async with self._lock:
            return await self._check_pending_locked()

    async def _check_pending_locked(self) -> tuple[list[dict], list[dict]]:
        now = int(time.time())
        pending = await self.db.pending_invoices(now - LATE_GRACE)
        # 대기 청구서가 없어도 IDLE_SCAN 마다 조회 → 만료 후 늦게 온 입금·청구서 없는 입금도 오너에게 보고
        if not pending and self._last_scan and time.monotonic() - self._last_scan < IDLE_SCAN:
            return [], []
        cursor_ms = await self.db.get_state(0, CURSOR_KEY)
        starts = [(min(p["created"] for p in pending) - 120) * 1000] if pending else []
        if cursor_ms:
            starts.append(int(cursor_ms) - CURSOR_OVERLAP * 1000)
        elif not pending:  # 첫 실행(커서 없음)·청구서 없음 → 최근 FIRST_LOOKBACK 만
            starts.append((now - FIRST_LOOKBACK) * 1000)
        since_ms = min(starts)
        if not self._streak_loaded:   # 재시작 전 연속 실패 수 ('장애' 알림 뒤 재시작돼도 '복구' 알림이 가게)
            self._streak_loaded = True
            self.fail_streak = max(self.fail_streak, int(await self.db.get_state(0, STREAK_KEY, 0) or 0))
        try:
            transfers = await self.fetch_transfers(since_ms)
        except Exception:
            self.fail_streak += 1
            if self.fail_streak == FAIL_ALERT:
                self._alert = "down"
            await self._save_streak()
            raise
        if self.fail_streak >= FAIL_ALERT:
            self._alert = "up"
        if self.fail_streak:
            self.fail_streak = 0
            await self._save_streak()
        self._last_scan = time.monotonic()

        paid, unmatched = [], []
        # 입금 기록 직후(연장 전) 죽은 경우: tx 는 이미 기록돼 아래에선 '본 거래'로 건너뛰므로 여기서 연장.
        # 오래 멈춰 청구서가 그 사이 만료 처리됐어도 (pay_invoice 가 청구서당 1번만)
        for r in await self.db.unapplied_payments():
            until = await self.db.pay_invoice(r["id"], r["pay_tx"], r["chat_id"], self.cfg.sub_days * 86400,
                                              int(time.time()))
            if until is not None:
                pending = [p for p in pending if p["id"] != r["id"]]
                paid.append({**dict(r), "tx_id": r["pay_tx"], "until": until, "from": r["from_addr"] or ""})
        new_cursor = int(cursor_ms or 0)
        for t in transfers:
            new_cursor = max(new_cursor, int(t.get("block_timestamp", 0) or 0))
            value = self._valid_transfer(t)
            tx_id = str(t.get("transaction_id", ""))
            if value is None or not tx_id:
                continue
            ts = int(t.get("block_timestamp", 0)) // 1000
            inv = next((p for p in pending if p["amount_units"] == value
                        and p["created"] - 120 <= ts <= p["expires"]), None)
            # 거래 ID 는 한 번만 기록된다 (UNIQUE). 이미 본 거래면 건너뜀 → 이중 적용 불가 (기록 뒤 죽은 건 위에서 처리)
            if not await self.db.record_payment(tx_id, inv["id"] if inv else None,
                                                inv["chat_id"] if inv else None, value, t.get("from", ""), ts):
                continue
            until = await self.db.pay_invoice(inv["id"], tx_id, inv["chat_id"], self.cfg.sub_days * 86400,
                                              int(time.time())) if inv else None
            if until is not None:
                pending = [p for p in pending if p["id"] != inv["id"]]
                paid.append({**dict(inv), "tx_id": tx_id, "until": until, "from": t.get("from", "")})
            else:
                # 청구서가 없거나(만료 후 입금 포함), 방금 취소·결제 처리돼서 반영 못 한 입금 → 반드시 오너에게 보고
                unmatched.append({"tx_id": tx_id, "amount_units": value, "from": t.get("from", ""), "ts": ts})
        if new_cursor > int(cursor_ms or 0):
            await self.db.set_state(0, CURSOR_KEY, new_cursor)
        await self.db.expire_invoices(now - LATE_GRACE)
        return paid, unmatched
