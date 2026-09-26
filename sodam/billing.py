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
        now = int(time.time())
        # 관리자마다 자기 청구서 (다른 관리자 청구서를 보여주면 그 사람은 확인 버튼을 쓸 수 없음)
        existing = await self.db.open_invoice(chat_id, now, user_id)
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
        if not pending:
            return [], []
        since_ms = (min(p["created"] for p in pending) - 120) * 1000
        transfers = await self.fetch_transfers(since_ms)

        paid, unmatched = [], []
        for t in transfers:
            value = self._valid_transfer(t)
            tx_id = str(t.get("transaction_id", ""))
            if value is None or not tx_id:
                continue
            ts = int(t.get("block_timestamp", 0)) // 1000
            inv = next((p for p in pending if p["amount_units"] == value
                        and p["created"] - 120 <= ts <= p["expires"]), None)
            # 거래 ID 는 한 번만 기록된다 (UNIQUE). 이미 본 거래면 건너뜀 → 이중 적용 불가.
            # 단, 기록 직후 죽어서 그 청구서가 아직 대기 중이면 연장을 다시 시도 (청구서는 pay_invoice 가 1번만 처리)
            new = await self.db.record_payment(tx_id, inv["id"] if inv else None,
                                               inv["chat_id"] if inv else None, value, t.get("from", ""), ts)
            if not new and not (inv and await self.db.payment_invoice_id(tx_id) == inv["id"]):
                continue
            until = await self.db.pay_invoice(inv["id"], tx_id, inv["chat_id"], self.cfg.sub_days * 86400,
                                              int(time.time())) if inv else None
            if until is not None:
                pending = [p for p in pending if p["id"] != inv["id"]]
                paid.append({**dict(inv), "tx_id": tx_id, "until": until, "from": t.get("from", "")})
            elif new:
                # 청구서가 없거나, 방금 취소·결제 처리돼서 반영 못 한 입금 → 돈은 들어왔으니 반드시 오너에게 보고
                unmatched.append({"tx_id": tx_id, "amount_units": value, "from": t.get("from", ""), "ts": ts})
        await self.db.expire_invoices(now - LATE_GRACE)
        return paid, unmatched
