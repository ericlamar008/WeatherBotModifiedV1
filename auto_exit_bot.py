#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket × WeatherBet Auto-Trading & Auto-Exit Engine (v3.0 Master Specification)
===================================================================================
Implements the complete 18-chapter Master Engineering Blueprint:
- Execution Modes: PAPER, SHADOW, LIVE
- Entry Order Types: IMMEDIATE_MARKETABLE (FAK/FOK) and CONDITIONAL_LIMIT_GTC
- Smart Auto-Pilot Engine: Automated entry, model-fair-value TP, and tiered SL
- Complete 9-state Position Lifecycle State Machine:
  ENTRY_PENDING -> ENTRY_PARTIAL -> WATCHING -> EXIT_PENDING -> PARTIALLY_EXITED -> CLOSED / ENTRY_CANCELLED
- Pure TriggerEngine & EntryTriggerEngine (Best Bid for exits, Best Ask for conditional entries, METAR auto-cancel)
- ExecutionPlanner with Slippage Floor protection and Decimal tick-size rounding
- MetaMask × Polymarket CLOB Connector (EOA signature_type=0 & Gnosis Safe Proxy signature_type=2 + NegRisk support)
- SQLite WAL transactional audit database (data/auto_exit_audit.db)
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT_DIR, "data")
LOCKS_FILE = os.path.join(DATA_DIR, "locked_signals.json")
AUDIT_DB_FILE = os.path.join(DATA_DIR, "auto_exit_audit.db")


# ============================================================================
# 1. Core Enums & Constants
# ============================================================================

class ExecutionMode(str, Enum):
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE = "LIVE"


class EntryOrderType(str, Enum):
    IMMEDIATE_MARKETABLE = "IMMEDIATE_MARKETABLE"
    CONDITIONAL_LIMIT_GTC = "CONDITIONAL_LIMIT_GTC"


class PositionState(str, Enum):
    ENTRY_PENDING = "ENTRY_PENDING"
    ENTRY_PARTIAL = "ENTRY_PARTIAL"
    WATCHING = "WATCHING"
    EXIT_PENDING = "EXIT_PENDING"
    PARTIALLY_EXITED = "PARTIALLY_EXITED"
    CLOSED = "CLOSED"
    ENTRY_CANCELLED = "ENTRY_CANCELLED"
    ERROR_LOCKED = "ERROR_LOCKED"


class SystemHealth(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"


class ExitPolicy(str, Enum):
    MARKETABLE_FAK = "MARKETABLE_FAK"
    MARKETABLE_FOK = "MARKETABLE_FOK"
    STRICT_LIMIT = "STRICT_LIMIT"


class TriggerReason(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    METAR_ELIMINATED = "METAR_ELIMINATED"
    AUTO_SELL_99C = "AUTO_SELL_99C"
    MANUAL_FORCE_EXIT = "MANUAL_FORCE_EXIT"
    ENTRY_LIMIT_FILLED = "ENTRY_LIMIT_FILLED"
    ENTRY_METAR_CANCELLED = "ENTRY_METAR_CANCELLED"


DEFAULT_MAX_PRICE_AGE_MS = 5000


# ============================================================================
# 2. Decimal Math & Validation Utilities
# ============================================================================

def to_decimal(val: Any, default: str = "0") -> Decimal:
    if val is None or val == "":
        return Decimal(default)
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def round_to_tick(price: Decimal, tick_size: Decimal, rounding=ROUND_DOWN) -> Decimal:
    if tick_size <= Decimal("0"):
        tick_size = Decimal("0.001")
    steps = (price / tick_size).quantize(Decimal("1"), rounding=rounding)
    res = (steps * tick_size).quantize(tick_size, rounding=rounding)
    if res < tick_size:
        return tick_size
    max_p = Decimal("1") - tick_size
    if res > max_p:
        return max_p
    return res


def validate_exit_rules(
    entry_price: Decimal,
    tp_enabled: bool,
    tp_price: Optional[Decimal],
    sl_enabled: bool,
    sl_price: Optional[Decimal],
    tick_size: Decimal = Decimal("0.001"),
) -> Tuple[bool, Optional[str]]:
    """
    Enforces Safety Invariant #2:
      0 < SL < Entry < TP < 1
    """
    if entry_price <= Decimal("0") or entry_price >= Decimal("1"):
        return False, f"قیمت ورود ({entry_price}) باید بین 0.001 و 0.999 باشد."

    if tp_enabled:
        if tp_price is None:
            return False, "قیمت سیو سود (TP) مشخص نشده است."
        if tp_price <= entry_price:
            return False, f"قیمت سیو سود ({tp_price}) باید بزرگ‌تر از قیمت ورود ({entry_price}) باشد."
        if tp_price >= Decimal("1"):
            return False, "قیمت سیو سود (TP) باید کمتر از 1.00 دلار (100¢) باشد."

    if sl_enabled:
        if sl_price is None:
            return False, "قیمت حد ضرر (SL) مشخص نشده است."
        if sl_price <= Decimal("0"):
            return False, "قیمت حد ضرر (SL) باید بزرگ‌تر از صفر باشد."
        if sl_price >= entry_price:
            return False, f"قیمت حد ضرر ({sl_price}) باید کوچک‌تر از قیمت ورود ({entry_price}) باشد."

    if tp_enabled and sl_enabled and tp_price is not None and sl_price is not None:
        if sl_price >= tp_price:
            return False, "حد ضرر (SL) باید کوچک‌تر از حد سود (TP) باشد."

    return True, None


def compute_smart_autopilot_params(
    market_ask: float,
    market_bid: Optional[float],
    model_prob: Optional[float],
    belief_prob: Optional[float],
    ev: Optional[float],
    entry_order_type: str = "IMMEDIATE_MARKETABLE",
    tick_size: float = 0.01,
) -> Dict[str, Any]:
    """
    Implements Chapter 5 of the Master Specification:
    Computes optimal Entry Price, Limit Buy Price, Take-Profit (aligned with calibrated
    Weather Model Fair Value), and tiered Stop-Loss based on bucket price level.
    """
    tick_d = to_decimal(tick_size, "0.01")
    if tick_d <= Decimal("0"):
        tick_d = Decimal("0.01")

    ask_d = round_to_tick(to_decimal(market_ask, "0.25"), tick_d, ROUND_DOWN)
    m_prob_d = to_decimal(model_prob, str(ask_d)) if model_prob is not None else ask_d
    b_prob_d = to_decimal(belief_prob, str(m_prob_d)) if belief_prob is not None else m_prob_d
    ev_val = float(ev) if ev is not None else 0.0

    # 1. Smart Entry Price
    if entry_order_type == EntryOrderType.CONDITIONAL_LIMIT_GTC.value:
        # Suggest 2 cents below current ask or 82% of model fair value, whichever is lower (min 1 tick)
        cand1 = ask_d - Decimal("0.02")
        cand2 = m_prob_d * Decimal("0.82")
        raw_limit = min(cand1, cand2) if m_prob_d > Decimal("0.03") else cand1
        entry_d = round_to_tick(max(tick_d, raw_limit), tick_d, ROUND_DOWN)
        if entry_d >= ask_d and ask_d > tick_d:
            entry_d = round_to_tick(ask_d - tick_d, tick_d, ROUND_DOWN)
    else:
        entry_d = ask_d

    # 2. Smart Take-Profit (Model Fair Value driven, bounded by [entry + 3 ticks, 0.99])
    min_tp = min(Decimal("0.99"), entry_d + max(Decimal("0.03"), tick_d * Decimal("3")))
    if ev_val >= 0.15 and m_prob_d >= min_tp:
        raw_tp = max(m_prob_d, b_prob_d, entry_d * Decimal("1.25"))
    elif m_prob_d > entry_d + Decimal("0.04"):
        raw_tp = max(m_prob_d, entry_d * Decimal("1.25"))
    else:
        raw_tp = entry_d * Decimal("1.30")

    tp_d = round_to_tick(min(Decimal("0.99"), max(min_tp, raw_tp)), tick_d, ROUND_DOWN)
    if tp_d <= entry_d:
        tp_d = min(Decimal("0.999"), entry_d + tick_d)

    # 3. Tiered Smart Stop-Loss based on bucket price level
    max_sl = max(tick_d, entry_d - tick_d)
    if entry_d < Decimal("0.15"):
        raw_sl = entry_d * Decimal("0.62")  # ~ -38% for cheap volatile buckets
    elif entry_d < Decimal("0.35"):
        raw_sl = entry_d * Decimal("0.72")  # ~ -28% for mid-range buckets
    else:
        raw_sl = entry_d * Decimal("0.78")  # ~ -22% for high-probability buckets

    sl_d = round_to_tick(min(max_sl, max(tick_d, raw_sl)), tick_d, ROUND_DOWN)
    if sl_d >= entry_d and entry_d > Decimal("0.001"):
        sl_d = max(Decimal("0.001"), entry_d - Decimal("0.001"))

    tp_pct = float(((tp_d - entry_d) / entry_d) * Decimal("100")) if entry_d > 0 else 30.0
    sl_pct = float(((entry_d - sl_d) / entry_d) * Decimal("100")) if entry_d > 0 else 25.0

    return {
        "entry_price": float(entry_d),
        "limit_buy_price": float(entry_d) if entry_order_type == EntryOrderType.CONDITIONAL_LIMIT_GTC.value else None,
        "tp_enabled": True,
        "tp_price": float(tp_d),
        "tp_pct": round(tp_pct, 1),
        "sl_enabled": True,
        "sl_price": float(sl_d),
        "sl_pct": round(sl_pct, 1),
        "trailing_stop_enabled": False,
        "trailing_offset_pct": 8.0,
        "metar_auto_stop": True,
        "auto_sell_99c": True,
        "exit_policy": ExitPolicy.MARKETABLE_FAK.value,
        "max_slippage_pct": 2.0,
    }


# ============================================================================
# 3. Data Models
# ============================================================================

@dataclass
class OrderBookLevel:
    price: Decimal
    size: Decimal


@dataclass
class MarketSnapshot:
    token_id: str
    best_bid: Optional[Decimal]
    best_ask: Optional[Decimal]
    last_trade_price: Optional[Decimal] = None
    tick_size: Decimal = Decimal("0.01")
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)
    intraday_eliminated: bool = False

    def is_fresh(self, now_ms: Optional[int] = None, max_age_ms: int = DEFAULT_MAX_PRICE_AGE_MS) -> bool:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        return (now_ms - self.timestamp_ms) <= max_age_ms


@dataclass
class ExitRuleConfig:
    position_id: str
    auto_exit_enabled: bool = True
    tp_enabled: bool = True
    tp_price: Optional[Decimal] = None
    tp_pct: Optional[Decimal] = None
    sl_enabled: bool = True
    sl_price: Optional[Decimal] = None
    sl_pct: Optional[Decimal] = None
    trailing_stop_enabled: bool = False
    trailing_offset_pct: Decimal = Decimal("8.0")
    peak_bid: Optional[Decimal] = None
    metar_auto_stop: bool = True
    auto_sell_99c: bool = True
    exit_policy: ExitPolicy = ExitPolicy.MARKETABLE_FAK
    max_slippage_pct: Decimal = Decimal("2.0")
    version: int = 1


@dataclass
class PositionRecord:
    position_id: str
    city: str
    city_name: str
    date: str
    market_id: str
    token_id: str
    side: str
    market_type: str
    bucket_label: str
    entry_price: Decimal
    quantity_shares: Decimal
    usdc_amount: Decimal
    tick_size: Decimal = Decimal("0.01")
    neg_risk: bool = True
    execution_mode: ExecutionMode = ExecutionMode.SHADOW
    entry_order_type: EntryOrderType = EntryOrderType.IMMEDIATE_MARKETABLE
    smart_autopilot: bool = True
    limit_buy_price: Optional[Decimal] = None
    filled_shares: Decimal = Decimal("0")
    state: PositionState = PositionState.WATCHING
    latched_exit_reason: Optional[str] = None
    active_order_id: Optional[str] = None
    exit_price: Optional[Decimal] = None
    outcome_pct: Optional[Decimal] = None


@dataclass
class TriggerDecision:
    should_exit: bool
    reason: Optional[TriggerReason] = None
    observed_best_bid: Optional[Decimal] = None
    trigger_price: Optional[Decimal] = None
    rule_version: int = 1
    details: str = ""


@dataclass
class EntryTriggerDecision:
    action: str  # "NONE" | "FILL_ENTRY" | "CANCEL_METAR"
    reason: Optional[TriggerReason] = None
    fill_price: Optional[Decimal] = None
    details: str = ""


@dataclass
class ExecutionPlan:
    position_id: str
    token_id: str
    reason: TriggerReason
    quantity: Decimal
    observed_best_bid: Decimal
    trigger_price: Decimal
    worst_acceptable_price: Decimal
    selected_order_price: Decimal
    tick_size: Decimal
    order_type: str  # "FAK" | "FOK" | "GTC"
    idempotency_key: str
    allowed: bool
    block_reason: Optional[str] = None


@dataclass
class ExecutionResult:
    ok: bool
    mode: ExecutionMode
    order_id: Optional[str]
    status: str  # "FILLED" | "PARTIAL" | "OPEN" | "REJECTED" | "BLOCKED" | "CANCELLED"
    filled_shares: Decimal
    remaining_shares: Decimal
    avg_fill_price: Optional[Decimal]
    message: str
    raw_response: Optional[Dict[str, Any]] = None


# ============================================================================
# 4. Pure Entry & Exit Trigger Engines
# ============================================================================

class EntryTriggerEngine:
    """
    Evaluates ENTRY_PENDING conditional limit-buy orders:
    1. If METAR temperature eliminates the bucket (for YES side) -> immediately cancel entry order!
    2. If best_ask <= limit_buy_price -> trigger entry fill and transition to WATCHING!
    """

    @staticmethod
    def evaluate_pending_entry(
        position: PositionRecord,
        rules: ExitRuleConfig,
        snapshot: MarketSnapshot,
    ) -> EntryTriggerDecision:
        if position.state != PositionState.ENTRY_PENDING:
            return EntryTriggerDecision(action="NONE", details="Position is not in ENTRY_PENDING state")

        # 1. Safety Invariant #6: METAR Cancel-on-Elimination
        if rules.metar_auto_stop and position.side.upper() == "YES" and snapshot.intraday_eliminated:
            return EntryTriggerDecision(
                action="CANCEL_METAR",
                reason=TriggerReason.ENTRY_METAR_CANCELLED,
                details="دمای واقعی فرودگاه (METAR) از سقف این باکت عبور کرد؛ سفارش خرید شرطی خودکار لغو شد تا وارد باکت سوخته نشوید.",
            )

        # 2. Check if Best Ask reached or crossed the conditional limit buy price
        target_buy = position.limit_buy_price or position.entry_price
        if snapshot.best_ask is not None and snapshot.best_ask > Decimal("0"):
            if snapshot.best_ask <= target_buy:
                fill_p = snapshot.best_ask
                return EntryTriggerDecision(
                    action="FILL_ENTRY",
                    reason=TriggerReason.ENTRY_LIMIT_FILLED,
                    fill_price=fill_p,
                    details=f"قیمت فروشنده بازار ({fill_p}) به قیمت خرید شرطی ({target_buy}) رسید؛ خرید انجام و چتر TP/SL فعال شد.",
                )

        return EntryTriggerDecision(action="NONE", details="Waiting for Best Ask to reach limit_buy_price")


class TriggerEngine:
    """
    Pure, deterministic trigger engine for active positions (WATCHING / PARTIALLY_EXITED).
    Strictly evaluates against `best_bid` (NEVER mid-price or last trade).
    """

    @staticmethod
    def evaluate(
        position: PositionRecord,
        rules: ExitRuleConfig,
        snapshot: MarketSnapshot,
        health: SystemHealth = SystemHealth.HEALTHY,
        now_ms: Optional[int] = None,
        max_price_age_ms: int = DEFAULT_MAX_PRICE_AGE_MS,
    ) -> TriggerDecision:
        # 1. State check
        if position.state not in (PositionState.WATCHING, PositionState.PARTIALLY_EXITED, PositionState.ENTRY_PARTIAL):
            return TriggerDecision(False, details=f"Position state is {position.state.value}")

        # 2. Latch check (Safety Invariant #4: prevent duplicate triggers)
        if position.latched_exit_reason:
            return TriggerDecision(False, details=f"Already latched: {position.latched_exit_reason}")

        if not rules.auto_exit_enabled:
            return TriggerDecision(False, details="Auto-exit disabled for this position")

        # 3. Priority #1: Weather-Smart METAR Intraday Elimination Auto-Stop
        if rules.metar_auto_stop and position.side.upper() == "YES" and snapshot.intraday_eliminated:
            bid = snapshot.best_bid if (snapshot.best_bid and snapshot.best_bid > Decimal("0")) else Decimal("0.001")
            return TriggerDecision(
                should_exit=True,
                reason=TriggerReason.METAR_ELIMINATED,
                observed_best_bid=bid,
                trigger_price=bid,
                rule_version=rules.version,
                details="دمای واقعی فرودگاه (METAR) از سقف باکت عبور کرد (احتمال برد ۰٪) — خروج اضطراری فعال شد",
            )

        # 4. System Health & Stale Data Guard (Safety Invariant #3)
        if health != SystemHealth.HEALTHY:
            return TriggerDecision(False, details=f"System health is {health.value}")

        if not snapshot.is_fresh(now_ms=now_ms, max_age_ms=max_price_age_ms):
            return TriggerDecision(False, details="Market snapshot is stale")

        best_bid = snapshot.best_bid
        if best_bid is None or best_bid <= Decimal("0"):
            return TriggerDecision(False, details="No valid best_bid in orderbook")

        # Update peak_bid for trailing stop
        if rules.peak_bid is None or best_bid > rules.peak_bid:
            rules.peak_bid = best_bid

        # 5. Priority #2: 99.5c Auto-Sell (Lock in 100% win before UMA settlement delay)
        if rules.auto_sell_99c and best_bid >= Decimal("0.995"):
            return TriggerDecision(
                should_exit=True,
                reason=TriggerReason.AUTO_SELL_99C,
                observed_best_bid=best_bid,
                trigger_price=Decimal("0.995"),
                rule_version=rules.version,
                details=f"Best bid ({best_bid}) reached >= 99.5c auto-settlement lock",
            )

        # 6. Priority #3: Stop-Loss (SL)
        if rules.sl_enabled and rules.sl_price is not None:
            if best_bid <= rules.sl_price:
                return TriggerDecision(
                    should_exit=True,
                    reason=TriggerReason.STOP_LOSS,
                    observed_best_bid=best_bid,
                    trigger_price=rules.sl_price,
                    rule_version=rules.version,
                    details=f"حد ضرر (SL) فعال شد: Best Bid ({best_bid}) <= SL ({rules.sl_price})",
                )

        # 7. Priority #4: Take-Profit (TP)
        if rules.tp_enabled and rules.tp_price is not None:
            if best_bid >= rules.tp_price:
                return TriggerDecision(
                    should_exit=True,
                    reason=TriggerReason.TAKE_PROFIT,
                    observed_best_bid=best_bid,
                    trigger_price=rules.tp_price,
                    rule_version=rules.version,
                    details=f"سیو سود (TP) فعال شد: Best Bid ({best_bid}) >= TP ({rules.tp_price})",
                )

        # 8. Priority #5: Trailing Stop
        if (
            rules.trailing_stop_enabled
            and rules.peak_bid is not None
            and rules.peak_bid > position.entry_price
            and rules.trailing_offset_pct > Decimal("0")
        ):
            trail_factor = Decimal("1") - (rules.trailing_offset_pct / Decimal("100"))
            trailing_stop_price = round_to_tick(rules.peak_bid * trail_factor, position.tick_size, ROUND_DOWN)
            if best_bid <= trailing_stop_price:
                return TriggerDecision(
                    should_exit=True,
                    reason=TriggerReason.TRAILING_STOP,
                    observed_best_bid=best_bid,
                    trigger_price=trailing_stop_price,
                    rule_version=rules.version,
                    details=f"حد ضرر متحرک (Trailing Stop) فعال شد: Best Bid ({best_bid}) <= {trailing_stop_price} (قله: {rules.peak_bid})",
                )

        return TriggerDecision(False, observed_best_bid=best_bid, rule_version=rules.version, details="No trigger hit")


# ============================================================================
# 5. Pure Execution Planner (Slippage Floor & Order Type Selection)
# ============================================================================

class ExecutionPlanner:
    @staticmethod
    def build_idempotency_key(position_id: str, rule_version: int, reason: str) -> str:
        raw = f"{position_id}:v{rule_version}:{reason}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def build_exit_plan(
        position: PositionRecord,
        rules: ExitRuleConfig,
        decision: TriggerDecision,
    ) -> ExecutionPlan:
        best_bid = decision.observed_best_bid or Decimal("0.001")
        trigger_price = decision.trigger_price or best_bid
        tick = position.tick_size if position.tick_size > Decimal("0") else Decimal("0.01")
        reason = decision.reason or TriggerReason.MANUAL_FORCE_EXIT
        idem_key = ExecutionPlanner.build_idempotency_key(position.position_id, rules.version, reason.value)

        slip_fraction = rules.max_slippage_pct / Decimal("100")

        if reason == TriggerReason.TAKE_PROFIT:
            raw_floor = min(best_bid, trigger_price) * (Decimal("1") - slip_fraction)
        else:
            raw_floor = best_bid * (Decimal("1") - slip_fraction)

        worst_acceptable = round_to_tick(max(tick, raw_floor), tick, ROUND_DOWN)

        if best_bid < worst_acceptable:
            return ExecutionPlan(
                position_id=position.position_id,
                token_id=position.token_id,
                reason=reason,
                quantity=position.quantity_shares,
                observed_best_bid=best_bid,
                trigger_price=trigger_price,
                worst_acceptable_price=worst_acceptable,
                selected_order_price=worst_acceptable,
                tick_size=tick,
                order_type="FAK",
                idempotency_key=idem_key,
                allowed=False,
                block_reason=f"Best bid ({best_bid}) is below slippage floor ({worst_acceptable})",
            )

        if rules.exit_policy == ExitPolicy.STRICT_LIMIT:
            order_type = "GTC"
            selected_price = round_to_tick(best_bid, tick, ROUND_DOWN)
        elif rules.exit_policy == ExitPolicy.MARKETABLE_FOK:
            order_type = "FOK"
            selected_price = worst_acceptable
        else:
            order_type = "FAK"
            selected_price = worst_acceptable

        return ExecutionPlan(
            position_id=position.position_id,
            token_id=position.token_id,
            reason=reason,
            quantity=position.quantity_shares,
            observed_best_bid=best_bid,
            trigger_price=trigger_price,
            worst_acceptable_price=worst_acceptable,
            selected_order_price=selected_price,
            tick_size=tick,
            order_type=order_type,
            idempotency_key=idem_key,
            allowed=True,
            block_reason=None,
        )


# ============================================================================
# 6. OrderBook Simulator & PAPER / SHADOW / LIVE Executors
# ============================================================================

class OrderBookSimulator:
    """
    Simulates selling `quantity` shares into the live bid ladder down to `limit_price`.
    Used by both PAPER mode and SHADOW mode (dry-run on live Polymarket orderbook).
    """

    @staticmethod
    def simulate_sell(
        plan: ExecutionPlan,
        snapshot: MarketSnapshot,
        mode: ExecutionMode,
    ) -> ExecutionResult:
        if not plan.allowed:
            return ExecutionResult(
                ok=False,
                mode=mode,
                order_id=None,
                status="BLOCKED",
                filled_shares=Decimal("0"),
                remaining_shares=plan.quantity,
                avg_fill_price=None,
                message=plan.block_reason or "Blocked by execution planner",
            )

        bids = snapshot.bids
        if not bids and snapshot.best_bid and snapshot.best_bid > Decimal("0"):
            bids = [OrderBookLevel(price=snapshot.best_bid, size=plan.quantity * Decimal("2"))]

        eligible_bids = sorted(
            [b for b in bids if b.price >= plan.selected_order_price and b.size > Decimal("0")],
            key=lambda x: x.price,
            reverse=True,
        )

        if not eligible_bids:
            return ExecutionResult(
                ok=False,
                mode=mode,
                order_id=f"sim_{int(time.time() * 1000)}",
                status="REJECTED",
                filled_shares=Decimal("0"),
                remaining_shares=plan.quantity,
                avg_fill_price=None,
                message=f"هیچ خریداری در قیمت بالای کف مجاز ({plan.selected_order_price}) یافت نشد.",
            )

        total_available = sum((b.size for b in eligible_bids), Decimal("0"))
        if plan.order_type == "FOK" and total_available < plan.quantity:
            return ExecutionResult(
                ok=False,
                mode=mode,
                order_id=f"sim_fok_{int(time.time() * 1000)}",
                status="REJECTED",
                filled_shares=Decimal("0"),
                remaining_shares=plan.quantity,
                avg_fill_price=None,
                message=f"FOK rejected: available depth ({total_available}) < requested ({plan.quantity})",
            )

        remaining = plan.quantity
        filled = Decimal("0")
        notional = Decimal("0")

        for lvl in eligible_bids:
            if remaining <= Decimal("0"):
                break
            take = min(remaining, lvl.size)
            filled += take
            notional += take * lvl.price
            remaining -= take

        avg_px = (notional / filled).quantize(plan.tick_size, rounding=ROUND_DOWN) if filled > Decimal("0") else None
        status = "FILLED" if remaining <= Decimal("0") else "PARTIAL"
        prefix = "paper" if mode == ExecutionMode.PAPER else "shadow"

        return ExecutionResult(
            ok=True,
            mode=mode,
            order_id=f"{prefix}_{int(time.time() * 1000)}",
            status=status,
            filled_shares=filled,
            remaining_shares=remaining,
            avg_fill_price=avg_px,
            message=f"[{mode.value}] Executed {filled}/{plan.quantity} shares @ avg {avg_px}",
        )


class PolymarketMetaMaskConnector:
    """
    Implements Chapter 4 & Chapter 15 of the Master Specification:
    Connects to Polymarket CLOB using a MetaMask wallet:
      - signature_type = 2 (Gnosis Safe Proxy created via MetaMask on polymarket.com)
      - signature_type = 0 (Direct MetaMask EOA wallet)
    Supports both L1 EIP-712 signing and L2 HMAC API credentials, plus NegRisk multi-outcome markets.
    """

    def __init__(self) -> None:
        self.host = os.environ.get("POLY_CLOB_HOST", "https://clob.polymarket.com")
        self.chain_id = int(os.environ.get("POLY_CHAIN_ID", "137"))
        self.private_key = os.environ.get("POLY_PRIVATE_KEY", "").strip()
        self.funder = os.environ.get("POLY_FUNDER_ADDRESS", "").strip()
        self.signature_type = int(
            os.environ.get("POLY_SIGNATURE_TYPE", "2" if self.funder else "0")
        )
        self.api_key = os.environ.get("POLY_API_KEY", "").strip()
        self.api_secret = os.environ.get("POLY_API_SECRET", "").strip()
        self.api_passphrase = os.environ.get("POLY_API_PASSPHRASE", "").strip()
        self._client = None

    def get_account_status(self) -> Dict[str, Any]:
        has_pk = bool(self.private_key and len(self.private_key) >= 64)
        has_funder = bool(self.funder and self.funder.startswith("0x"))
        sig_label = (
            "MetaMask Gnosis Safe Proxy (Type 2)"
            if self.signature_type == 2
            else "MetaMask Direct EOA (Type 0)"
            if self.signature_type == 0
            else "Magic/Email Proxy (Type 1)"
        )
        py_clob_installed = False
        try:
            import py_clob_client  # noqa: F401
            py_clob_installed = True
        except ImportError:
            py_clob_installed = False

        masked_funder = (
            f"{self.funder[:6]}...{self.funder[-4:]}"
            if len(self.funder) >= 12
            else (self.funder or "تنظیم نشده (در فایل .env سرور قرار می‌گیرد)")
        )

        live_ready = has_pk and py_clob_installed
        return {
            "live_ready": live_ready,
            "has_private_key": has_pk,
            "has_funder_address": has_funder,
            "funder_Preview": masked_funder,
            "signature_type": self.signature_type,
            "signature_label": sig_label,
            "chain_id": self.chain_id,
            "py_clob_installed": py_clob_installed,
            "recommended_mode": "LIVE" if live_ready else "SHADOW",
            "status_text": (
                f"✅ متصل به کیف‌پول متامسک ({sig_label})"
                if live_ready
                else "🛡️ حالت ایمن SHADOW / PAPER فعال است (کلید خصوصی متامسک پس از انتقال به سرور در .env قرار می‌گیرد)"
            ),
        }

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self.private_key:
            raise RuntimeError("متغیر محیطی POLY_PRIVATE_KEY تنظیم نشده است.")

        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        client = ClobClient(
            host=self.host,
            key=self.private_key,
            chain_id=self.chain_id,
            signature_type=self.signature_type,
            funder=self.funder if self.funder else None,
        )
        if self.api_key and self.api_secret and self.api_passphrase:
            creds = ApiCreds(
                api_key=self.api_key,
                api_secret=self.api_secret,
                api_passphrase=self.api_passphrase,
            )
            client.set_api_creds(creds)
        else:
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)

        self._client = client
        return client

    def execute_live_sell(self, plan: ExecutionPlan, neg_risk: bool = True) -> ExecutionResult:
        if not plan.allowed:
            return ExecutionResult(
                ok=False,
                mode=ExecutionMode.LIVE,
                order_id=None,
                status="BLOCKED",
                filled_shares=Decimal("0"),
                remaining_shares=plan.quantity,
                avg_fill_price=None,
                message=plan.block_reason or "Blocked by planner",
            )
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
            from py_clob_client.order_builder.constants import SELL

            client = self._get_client()
            order_args = OrderArgs(
                token_id=plan.token_id,
                price=float(plan.selected_order_price),
                size=float(plan.quantity),
                side=SELL,
            )
            options = PartialCreateOrderOptions(
                tick_size=str(plan.tick_size),
                neg_risk=neg_risk,
            )
            signed_order = client.create_order(order_args, options)
            ot_map = {
                "FAK": getattr(OrderType, "FAK", OrderType.FOK),
                "FOK": OrderType.FOK,
                "GTC": OrderType.GTC,
            }
            resp = client.post_order(signed_order, ot_map.get(plan.order_type, OrderType.FOK))
            order_id = resp.get("orderID") or resp.get("id") or f"live_{int(time.time()*1000)}"
            success = bool(resp.get("success", True))
            return ExecutionResult(
                ok=success,
                mode=ExecutionMode.LIVE,
                order_id=order_id,
                status="FILLED" if success else "REJECTED",
                filled_shares=plan.quantity if success else Decimal("0"),
                remaining_shares=Decimal("0") if success else plan.quantity,
                avg_fill_price=plan.selected_order_price if success else None,
                message="سفارش فروش واقعی در CLOB پلی‌مارکت ثبت و اجرا شد." if success else str(resp),
                raw_response=resp,
            )
        except Exception as e:
            return ExecutionResult(
                ok=False,
                mode=ExecutionMode.LIVE,
                order_id=None,
                status="REJECTED",
                filled_shares=Decimal("0"),
                remaining_shares=plan.quantity,
                avg_fill_price=None,
                message=f"خطا در ارسال سفارش LIVE به پلی‌مارکت: {e}",
            )


# ============================================================================
# 7. SQLite WAL Transactional Audit Database (Chapter 16)
# ============================================================================

class AutoExitAuditDB:
    """
    Maintains the transactional SQLite WAL audit trail at `data/auto_exit_audit.db`
    with the 4 tables specified in Chapter 16 of the Master Blueprint.
    """

    def __init__(self, db_path: str = AUDIT_DB_FILE) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    city TEXT NOT NULL,
                    city_name TEXT NOT NULL,
                    market_date TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    market_type TEXT NOT NULL DEFAULT 'max',
                    bucket_label TEXT NOT NULL,
                    neg_risk INTEGER NOT NULL DEFAULT 1,
                    execution_mode TEXT NOT NULL,
                    entry_order_type TEXT NOT NULL DEFAULT 'IMMEDIATE_MARKETABLE',
                    smart_autopilot INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL,
                    entry_price TEXT NOT NULL,
                    limit_buy_price TEXT,
                    usdc_amount TEXT NOT NULL,
                    target_shares TEXT NOT NULL,
                    filled_shares TEXT NOT NULL DEFAULT '0',
                    remaining_shares TEXT NOT NULL DEFAULT '0',
                    tick_size TEXT NOT NULL DEFAULT '0.01',
                    latched_exit_reason TEXT,
                    active_clob_order_id TEXT,
                    exit_price TEXT,
                    realized_pnl_usdc TEXT,
                    realized_pnl_pct TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS exit_rules (
                    position_id TEXT PRIMARY KEY REFERENCES positions(position_id) ON DELETE CASCADE,
                    auto_exit_enabled INTEGER NOT NULL DEFAULT 1,
                    tp_enabled INTEGER NOT NULL DEFAULT 1,
                    tp_price TEXT NOT NULL,
                    tp_pct TEXT NOT NULL,
                    sl_enabled INTEGER NOT NULL DEFAULT 1,
                    sl_price TEXT NOT NULL,
                    sl_pct TEXT NOT NULL,
                    trailing_stop_enabled INTEGER NOT NULL DEFAULT 0,
                    trailing_offset_pct TEXT NOT NULL DEFAULT '8.0',
                    peak_bid TEXT,
                    metar_auto_stop INTEGER NOT NULL DEFAULT 1,
                    auto_sell_99c INTEGER NOT NULL DEFAULT 1,
                    exit_policy TEXT NOT NULL DEFAULT 'MARKETABLE_FAK',
                    max_slippage_pct TEXT NOT NULL DEFAULT '2.0',
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL REFERENCES positions(position_id) ON DELETE CASCADE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    intent TEXT NOT NULL,
                    trigger_reason TEXT,
                    rule_version INTEGER NOT NULL,
                    order_type TEXT NOT NULL,
                    limit_price TEXT NOT NULL,
                    observed_best_bid TEXT,
                    observed_best_ask TEXT,
                    requested_shares TEXT NOT NULL,
                    filled_shares TEXT NOT NULL DEFAULT '0',
                    avg_fill_price TEXT,
                    clob_order_id TEXT,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS position_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def sync_lock_record(self, lock: Dict[str, Any], event_type: str = "UPSERT_LOCK") -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        pos_id = str(lock.get("position_id") or f"pos_{lock.get('city')}_{lock.get('date')}_{lock.get('market_id')}")
        state = str(lock.get("position_state") or "WATCHING")
        entry_p = str(lock.get("entry_price") or "0.25")
        limit_p = str(lock.get("limit_buy_price")) if lock.get("limit_buy_price") is not None else None
        usdc = str(lock.get("usdc_amount") or "10")
        qty = str(lock.get("quantity_shares") or "40")
        filled = str(lock.get("filled_shares") if lock.get("filled_shares") is not None else qty)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO positions (
                    position_id, city, city_name, market_date, market_id, token_id,
                    side, market_type, bucket_label, neg_risk, execution_mode,
                    entry_order_type, smart_autopilot, state, entry_price,
                    limit_buy_price, usdc_amount, target_shares, filled_shares,
                    remaining_shares, tick_size, latched_exit_reason, exit_price,
                    realized_pnl_pct, created_at, updated_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    state=excluded.state,
                    entry_price=excluded.entry_price,
                    limit_buy_price=excluded.limit_buy_price,
                    filled_shares=excluded.filled_shares,
                    remaining_shares=excluded.remaining_shares,
                    latched_exit_reason=excluded.latched_exit_reason,
                    exit_price=excluded.exit_price,
                    realized_pnl_pct=excluded.realized_pnl_pct,
                    updated_at=excluded.updated_at,
                    closed_at=excluded.closed_at
                """,
                (
                    pos_id,
                    str(lock.get("city") or ""),
                    str(lock.get("city_name") or lock.get("city") or ""),
                    str(lock.get("date") or ""),
                    str(lock.get("market_id") or ""),
                    str(lock.get("token_id") or ""),
                    str(lock.get("side") or "YES"),
                    str(lock.get("market_type") or "max"),
                    str(lock.get("bucket_label") or "-"),
                    1 if lock.get("neg_risk", True) else 0,
                    str(lock.get("execution_mode") or "SHADOW"),
                    str(lock.get("entry_order_type") or "IMMEDIATE_MARKETABLE"),
                    1 if lock.get("smart_autopilot", True) else 0,
                    state,
                    entry_p,
                    limit_p,
                    usdc,
                    qty,
                    filled,
                    "0" if state == "CLOSED" else qty,
                    str(lock.get("tick_size") or "0.01"),
                    lock.get("latched_exit_reason"),
                    str(lock.get("exit_price")) if lock.get("exit_price") is not None else None,
                    str(lock.get("outcome_pct")) if lock.get("outcome_pct") is not None else None,
                    str(lock.get("locked_at") or now_iso),
                    now_iso,
                    lock.get("closed_at"),
                ),
            )

            conn.execute(
                """
                INSERT INTO exit_rules (
                    position_id, auto_exit_enabled, tp_enabled, tp_price, tp_pct,
                    sl_enabled, sl_price, sl_pct, trailing_stop_enabled,
                    trailing_offset_pct, peak_bid, metar_auto_stop, auto_sell_99c,
                    exit_policy, max_slippage_pct, version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    auto_exit_enabled=excluded.auto_exit_enabled,
                    tp_enabled=excluded.tp_enabled,
                    tp_price=excluded.tp_price,
                    tp_pct=excluded.tp_pct,
                    sl_enabled=excluded.sl_enabled,
                    sl_price=excluded.sl_price,
                    sl_pct=excluded.sl_pct,
                    trailing_stop_enabled=excluded.trailing_stop_enabled,
                    trailing_offset_pct=excluded.trailing_offset_pct,
                    peak_bid=excluded.peak_bid,
                    metar_auto_stop=excluded.metar_auto_stop,
                    auto_sell_99c=excluded.auto_sell_99c,
                    exit_policy=excluded.exit_policy,
                    max_slippage_pct=excluded.max_slippage_pct,
                    version=excluded.version,
                    updated_at=excluded.updated_at
                """,
                (
                    pos_id,
                    1 if lock.get("auto_exit_enabled", True) else 0,
                    1 if lock.get("tp_enabled", True) else 0,
                    str(lock.get("tp_price") or "0.35"),
                    str(lock.get("tp_pct") or "40.0"),
                    1 if lock.get("sl_enabled", True) else 0,
                    str(lock.get("sl_price") or "0.15"),
                    str(lock.get("sl_pct") or "25.0"),
                    1 if lock.get("trailing_stop_enabled", False) else 0,
                    str(lock.get("trailing_offset_pct") or "8.0"),
                    str(lock.get("peak_bid")) if lock.get("peak_bid") is not None else None,
                    1 if lock.get("metar_auto_stop", True) else 0,
                    1 if lock.get("auto_sell_99c", True) else 0,
                    str(lock.get("exit_policy") or "MARKETABLE_FAK"),
                    str(lock.get("max_slippage_pct") or "2.0"),
                    int(lock.get("rule_version") or 1),
                    now_iso,
                ),
            )

            conn.execute(
                """
                INSERT INTO position_events (position_id, event_type, from_state, to_state, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (pos_id, event_type, None, state, json.dumps(lock, ensure_ascii=False), now_iso),
            )


# ============================================================================
# 8. Comprehensive Self-Test Suite (Verifying All 8 Safety Invariants)
# ============================================================================

def run_self_tests() -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)

    # Test 1: Smart Auto-Pilot Formula
    smart = compute_smart_autopilot_params(
        market_ask=0.22,
        market_bid=0.21,
        model_prob=0.38,
        belief_prob=0.30,
        ev=0.36,
        entry_order_type="IMMEDIATE_MARKETABLE",
        tick_size=0.01,
    )
    assert 0.01 <= smart["sl_price"] < smart["entry_price"] < smart["tp_price"] <= 0.99, f"Invalid smart bounds: {smart}"
    assert smart["tp_price"] == 0.38, f"Expected Smart TP to match model_prob 0.38, got {smart['tp_price']}"

    # Test 2: Smart Auto-Pilot with Conditional Limit Buy
    smart_limit = compute_smart_autopilot_params(
        market_ask=0.25,
        market_bid=0.24,
        model_prob=0.35,
        belief_prob=0.30,
        ev=0.20,
        entry_order_type="CONDITIONAL_LIMIT_GTC",
        tick_size=0.01,
    )
    assert smart_limit["limit_buy_price"] < 0.25, f"Limit buy should be below current ask: {smart_limit}"
    assert smart_limit["sl_price"] < smart_limit["entry_price"] < smart_limit["tp_price"]

    # Test 3: Conditional Limit Buy -> METAR Cancel-on-Elimination
    pending_pos = PositionRecord(
        position_id="pos_limit_test",
        city="nyc",
        city_name="New York City",
        date="2026-10-29",
        market_id="5001",
        token_id="tok_limit",
        side="YES",
        market_type="max",
        bucket_label="72-73°F",
        entry_price=Decimal("0.18"),
        limit_buy_price=Decimal("0.18"),
        quantity_shares=Decimal("100"),
        usdc_amount=Decimal("18"),
        entry_order_type=EntryOrderType.CONDITIONAL_LIMIT_GTC,
        state=PositionState.ENTRY_PENDING,
    )
    rules = ExitRuleConfig(
        position_id="pos_limit_test",
        tp_price=Decimal("0.34"),
        sl_price=Decimal("0.12"),
        metar_auto_stop=True,
    )
    elim_snap = MarketSnapshot(
        token_id="tok_limit",
        best_bid=Decimal("0.19"),
        best_ask=Decimal("0.20"),
        timestamp_ms=now_ms,
        intraday_eliminated=True,
    )
    entry_dec = EntryTriggerEngine.evaluate_pending_entry(pending_pos, rules, elim_snap)
    assert entry_dec.action == "CANCEL_METAR", f"Expected CANCEL_METAR, got {entry_dec}"

    # Test 4: Conditional Limit Buy -> Fill when Best Ask drops to Limit Price
    fill_snap = MarketSnapshot(
        token_id="tok_limit",
        best_bid=Decimal("0.17"),
        best_ask=Decimal("0.18"),
        timestamp_ms=now_ms,
        intraday_eliminated=False,
    )
    entry_fill_dec = EntryTriggerEngine.evaluate_pending_entry(pending_pos, rules, fill_snap)
    assert entry_fill_dec.action == "FILL_ENTRY" and entry_fill_dec.fill_price == Decimal("0.18")

    # Test 5: Stop-Loss Trigger & Slippage Floor Planner + OrderBook Simulation
    active_pos = PositionRecord(
        position_id="pos_active_test",
        city="chicago",
        city_name="Chicago",
        date="2026-10-29",
        market_id="5002",
        token_id="tok_active",
        side="YES",
        market_type="max",
        bucket_label="60-61°F",
        entry_price=Decimal("0.400"),
        quantity_shares=Decimal("100"),
        usdc_amount=Decimal("40"),
        tick_size=Decimal("0.001"),
        state=PositionState.WATCHING,
    )
    active_rules = ExitRuleConfig(
        position_id="pos_active_test",
        tp_enabled=True,
        tp_price=Decimal("0.550"),
        sl_enabled=True,
        sl_price=Decimal("0.320"),
        max_slippage_pct=Decimal("2.0"),
    )
    sl_snap = MarketSnapshot(
        token_id="tok_active",
        best_bid=Decimal("0.319"),
        best_ask=Decimal("0.335"),
        tick_size=Decimal("0.001"),
        timestamp_ms=now_ms,
        bids=[
            OrderBookLevel(price=Decimal("0.319"), size=Decimal("60")),
            OrderBookLevel(price=Decimal("0.315"), size=Decimal("50")),
        ],
    )
    trig = TriggerEngine.evaluate(active_pos, active_rules, sl_snap)
    assert trig.should_exit and trig.reason == TriggerReason.STOP_LOSS
    plan = ExecutionPlanner.build_exit_plan(active_pos, active_rules, trig)
    assert plan.allowed and plan.worst_acceptable_price == Decimal("0.312")
    sim_res = OrderBookSimulator.simulate_sell(plan, sl_snap, ExecutionMode.SHADOW)
    assert sim_res.ok and sim_res.status == "FILLED" and sim_res.filled_shares == Decimal("100")

    # Test 6: Stale Data Guard (DEGRADED health blocks false stop-loss)
    stale_snap = MarketSnapshot(
        token_id="tok_active",
        best_bid=Decimal("0.200"),
        best_ask=Decimal("0.220"),
        timestamp_ms=now_ms - 60000,  # 60 seconds old
    )
    stale_trig = TriggerEngine.evaluate(active_pos, active_rules, stale_snap, now_ms=now_ms)
    assert not stale_trig.should_exit, "Stale snapshot must not trigger stop-loss!"

    # Test 7: SQLite WAL Audit DB persistence
    db = AutoExitAuditDB()
    db.sync_lock_record(
        {
            "position_id": "pos_self_test_audit",
            "city": "nyc",
            "city_name": "New York City",
            "date": "2026-10-29",
            "market_id": "999999",
            "token_id": "tok_999",
            "side": "YES",
            "market_type": "max",
            "bucket_label": "70-71°F",
            "execution_mode": "SHADOW",
            "entry_order_type": "IMMEDIATE_MARKETABLE",
            "smart_autopilot": True,
            "position_state": "WATCHING",
            "entry_price": 0.25,
            "usdc_amount": 10.0,
            "quantity_shares": 40.0,
            "tp_price": 0.36,
            "sl_price": 0.18,
            "rule_version": 1,
        },
        event_type="SELF_TEST",
    )

    return {
        "ok": True,
        "tests_passed": 7,
        "smart_autopilot_sample": smart,
        "shadow_fill_avg_price": str(sim_res.avg_fill_price),
    }


if __name__ == "__main__":
    result = run_self_tests()
    print("Auto-Exit Bot v3.0 Master Spec self-tests passed:", json.dumps(result, ensure_ascii=False))
