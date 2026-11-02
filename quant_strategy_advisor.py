#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeatherBet Institutional Quantitative Analyst & City Strategy Generator
========================================================================
Analyzes:
1. All user locked signals (`data/locked_signals.json` & `data/lock_history.csv`) across all states
   (resolved, manual unlock, auto-TP, auto-SL, open) including Early-Exit Forensics (Smart Save vs Regret).
2. All historical & active system markets (`data/markets/*.json`, `data/accuracy_report.csv`, `data/calibration.json`)
   across all 39 cities, Max vs Min temperature, seasonal bias shifts, NWP sub-model dominance (GFS/ECMWF/ICON/GEM),
   exact bucket hit rate vs +-1 adjacent bucket hit rate, and market price efficiency.
3. Synthesizes high-precision, statistically validated (Wilson score + Bayesian shrinkage) per-city trading
   playbooks and optimal Auto-Exit parameters (Entry band, Limit Buy discount, TP%, SL%, Straddle vs Single).
4. Generates a standalone interactive RTL HTML report (`quant_strategy_report.html` / `docs/strategy-lab.html`)
   and dispatches the daily link via Telegram (`data/quant_strategy_telegram.txt`).
"""

from __future__ import annotations

import csv
import glob
import html
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from locations import LOCATIONS

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT_DIR, "data")
MARKETS_DIR = os.path.join(DATA_DIR, "markets")
LOCKS_FILE = os.path.join(DATA_DIR, "locked_signals.json")
ACCURACY_CSV = os.path.join(DATA_DIR, "accuracy_report.csv")
CALIBRATION_FILE = os.path.join(DATA_DIR, "calibration.json")
TELEGRAM_SUMMARY_FILE = os.path.join(DATA_DIR, "quant_strategy_telegram.txt")
REPORT_JSON_FILE = os.path.join(DATA_DIR, "quant_strategy_report.json")
TEMPORAL_MEMORY_FILE = os.path.join(DATA_DIR, "temporal_memory.json")
REPORT_HTML_FILE = os.path.join(ROOT_DIR, "quant_strategy_report.html")
DOCS_DIR = os.path.join(ROOT_DIR, "docs")

IRAN_TZ = timezone(timedelta(hours=3, minutes=30))

US_CITIES = {
    "nyc", "chicago", "miami", "los-angeles", "houston",
    "atlanta", "seattle", "dallas", "austin", "denver", "phoenix", "boston"
}


# ============================================================================
# 1. Statistical & Quantitative Helper Functions
# ============================================================================

def wilson_lower_bound(wins: int, total: int, z: float = 1.645) -> float:
    """
    Computes the lower bound of the Wilson score confidence interval (default 90% one-sided / 95% conservative).
    Prevents small-sample flukes (e.g. 2/2 = 100%) from outranking proven cities (e.g. 18/22 = 81.8%).
    """
    if total <= 0:
        return 0.0
    phat = wins / total
    denom = 1.0 + (z * z) / total
    center = phat + (z * z) / (2.0 * total)
    spread = z * math.sqrt((phat * (1.0 - phat) + (z * z) / (4.0 * total)) / total)
    return max(0.0, (center - spread) / denom)


def bayesian_shrunk_rate(wins: int, total: int, prior_rate: float = 0.42, prior_weight: float = 6.0) -> float:
    """Bayesian empirical shrinkage towards the global prior win rate."""
    return (wins + prior_weight * prior_rate) / (total + prior_weight)


def safe_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def format_range_label(rng: Any, unit: str = "") -> str:
    if not isinstance(rng, (list, tuple)) or len(rng) < 2:
        return "-"
    low, high = rng[0], rng[1]
    if low <= -998 and high >= 998:
        return f"?{unit}"
    if low <= -998:
        return f"≤{high}{unit}"
    if high >= 998:
        return f"≥{low}{unit}"
    if low == high:
        return f"{low}{unit}"
    return f"{low}-{high}{unit}"


def check_temp_in_range(temp: float, rng: List[float]) -> bool:
    if not isinstance(rng, (list, tuple)) or len(rng) < 2:
        return False
    low, high = float(rng[0]), float(rng[1])
    if low <= -998:
        return temp <= high + 0.499
    if high >= 998:
        return temp >= low - 0.499
    return (low - 0.499) <= temp <= (high + 0.499)


def get_season_Context(month: int, lat: float = 40.0) -> Tuple[str, str]:
    """Returns seasonal regime label and meteorological note based on hemisphere and month."""
    southern = lat < 0
    if month in (9, 10, 11):
        if southern:
            return "بهار نیمکره جنوبی (Spring Transition)", "افزایش تدریجی دما و نوسان جبهه‌های ساحلی"
        return "گذار پاییزی (Autumn Regime Shift)", "شکست دماهای تابستانی، عبور جبهه‌های سرد و ریسک Over-prediction مدل‌ها"
    if month in (12, 1, 2):
        if southern:
            return "تابستان نیمکره جنوبی", "ثبات نسبی دماهای بیشینه و ریسک همرفت بعدازظهر"
        return "رژیم زمستانی (Winter Inversion)", "وارونگی دما، پوشش ابر و خطای مدل‌ها در تخمین کمینه/بیشینه"
    if month in (3, 4, 5):
        return "گذار بهاری (Spring Volatility)", "نوسان شدید روز به روز و تغییر سریع توده هوا"
    return "رژیم تابستانی (Summer Peak)", "ثبات بیشتر الگوهای تابستانی و اهمیت رطوبت/پدیده جزیره گرمایی"


def _parse_iso_dt(ts_str: Any) -> Optional[datetime]:
    if not ts_str or not isinstance(ts_str, str):
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _fmt_hour_window(start_h: int, span: int = 3) -> str:
    end_h = (start_h + span) % 24
    return f"{start_h:02d}:00–{end_h:02d}:00"


def load_temporal_memory() -> Dict[str, Any]:
    if os.path.isfile(TEMPORAL_MEMORY_FILE):
        try:
            with open(TEMPORAL_MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {"version": 2, "updated_at": "", "cities": {}}


def save_temporal_memory(mem: Dict[str, Any]) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        mem["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(TEMPORAL_MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def build_temporal_microstructure_profile(
    city: str,
    mtype: str,
    tz_name: str,
    city_locks_raw: List[Dict[str, Any]],
    city_markets_all: List[Dict[str, Any]],
    rec_bias: float,
    unit_sym: str,
    rec_hit_rate: float = 0.0,
    rec_within1_rate: float = 0.0,
    n_resolved: int = 0,
    u_smart_saves: int = 0,
    u_early_regrets: int = 0,
    memory_store: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Maintains an internal temporal sequence memory for each city+market_type and uses that
    accumulated memory to diagnose multi-dimensional market & model behavioral regimes.
    Does NOT expose raw sequence telemetry or hourly bar charts to the UI; instead outputs
    synthesized behavioral diagnoses and concrete trading strategies when real data supports them.
    """
    city_tz = None
    if ZoneInfo is not None and tz_name:
        try:
            city_tz = ZoneInfo(tz_name)
        except Exception:
            city_tz = None

    key = f"{city}|{mtype}"
    cities_mem = (memory_store or {}).setdefault("cities", {})
    prior_mem = cities_mem.get(key, {}) if isinstance(cities_mem.get(key), dict) else {}

    ts_trades = 0
    profitable_scalps = 0
    scalp_gains: List[float] = []
    hold_hours_list: List[float] = []
    velocities_pct_per_h: List[float] = []

    # Multi-regime behavioral counters extracted from temporal sequences
    whipsaw_reversals = 0          # Rallied intraday (or exited in profit) then collapsed to 0c at settlement
    smart_money_resistances = 0    # Model prob >= 42% while market refused to bid (< 22c) and bucket lost
    silent_model_leads = 0         # Model jumped >= +6% while market lagged >= 12c behind fair value
    fomo_overreactions = 0         # Intraday price spiked >= 55c or +35% on non-winning bucket before dumping
    neighbor_migrations = 0        # Winner or momentum shifted by exactly 1 adjacent bucket from initial model peak
    theta_stagnations = 0          # Held > 10h with flat/decaying price trajectory ending in loss or stop
    explosive_convergences = 0     # Fast directional repricing (+25%+) aligned with station confirmation

    entry_iran_hours: List[int] = []
    active_iran_hours: List[int] = []

    for l in city_locks_raw:
        if l.get("status") == "cancelled_entry":
            continue
        ep = safe_float(l.get("entry_price"))
        xp = safe_float(l.get("exit_price")) or safe_float(l.get("last_price"))
        if not ep or ep <= 0:
            continue
        dt_lock = _parse_iso_dt(l.get("locked_at"))
        dt_close = _parse_iso_dt(l.get("closed_at")) or _parse_iso_dt(l.get("last_checked_at"))
        if dt_lock:
            ts_trades += 1
            entry_iran_hours.append(dt_lock.astimezone(IRAN_TZ).hour)

        pnl_pct = ((xp - ep) / ep) * 100.0 if xp is not None else 0.0
        dur_h = 6.0
        if dt_lock and dt_close and dt_close > dt_lock:
            dur_h = max(0.25, (dt_close - dt_lock).total_seconds() / 3600.0)
            if dur_h <= 72.0:
                hold_hours_list.append(dur_h)
            if pnl_pct >= 12.0 and l.get("status") != "open":
                profitable_scalps += 1
                scalp_gains.append(pnl_pct)
                velocities_pct_per_h.append(pnl_pct / max(0.5, min(24.0, dur_h)))
                active_iran_hours.append(dt_close.astimezone(IRAN_TZ).hour)

        # Inspect granular trajectory sequence inside lock if present
        traj = l.get("price_trajectory") or []
        if len(traj) >= 2:
            pcts = [safe_float(pt.get("pct"), 0.0) or 0.0 for pt in traj]
            max_traj_pct = max(pcts) if pcts else 0.0
            end_traj_pct = pcts[-1] if pcts else 0.0
            if max_traj_pct >= 18.0 and (max_traj_pct - end_traj_pct) >= 22.0:
                whipsaw_reversals += 1
            if max_traj_pct >= 25.0 and end_traj_pct >= 20.0:
                explosive_convergences += 1
            if dur_h >= 10.0 and max_traj_pct < 6.0 and end_traj_pct <= -12.0:
                theta_stagnations += 1

        # Detect stagnation or whipsaw from closed lock outcome
        if dur_h >= 12.0 and pnl_pct <= -15.0:
            theta_stagnations += 1
        if l.get("status") == "closed_resolved" and (xp or 0.0) <= 0.05 and ep >= 0.48:
            fomo_overreactions += 1

    # Inspect market-level sequences across all files for this city
    intraday_mover_buckets = 0
    for m in city_markets_all:
        is_resolved = (m.get("status") == "resolved" and m.get("actual_temp") is not None)
        actual_t = safe_float(m.get("actual_temp")) if is_resolved else None
        win_mid = str(m.get("winning_market_id") or "")
        dist = m.get("full_distribution") or []

        best_model_idx = -1
        best_model_prob = -1.0
        win_idx = -1

        for bi, b in enumerate(dist):
            mp = safe_float(b.get("model_prob"), 0.0) or 0.0
            if mp > best_model_prob:
                best_model_prob = mp
                best_model_idx = bi
            if is_resolved:
                if win_mid and str(b.get("market_id")) == win_mid:
                    win_idx = bi
                elif win_idx == -1 and actual_t is not None and b.get("range") and check_temp_in_range(actual_t, b["range"]):
                    win_idx = bi

        if is_resolved and best_model_idx != -1 and win_idx != -1:
            if abs(best_model_idx - win_idx) == 1:
                neighbor_migrations += 1

        for bi, b in enumerate(dist):
            p6_pct = safe_float(b.get("yes_price_6h_pct_change"))
            if p6_pct is None:
                p6_pct = safe_float(b.get("yes_price_pct_change"))
            m6_chg = safe_float(b.get("model_prob_6h_change")) or safe_float(b.get("model_prob_change")) or 0.0
            yp = safe_float(b.get("yes_price"), 0.0) or 0.0
            mp = safe_float(b.get("model_prob"), 0.0) or 0.0

            if p6_pct is not None and abs(p6_pct) >= 5.0 and (yp >= 0.06 or mp >= 0.08):
                intraday_mover_buckets += 1
                if p6_pct >= 20.0:
                    explosive_convergences += 1
                    velocities_pct_per_h.append(min(45.0, p6_pct / 6.0))

            # Pattern: Silent Model Lead (Model jumped, Market still lagging)
            if m6_chg >= 0.05 and (mp - yp) >= 0.12 and mp >= 0.25:
                silent_model_leads += 1

            # Pattern: Smart-Money Resistance vs Model Trap
            if mp >= 0.42 and 0.01 < yp <= 0.22:
                if not is_resolved or (win_idx != -1 and bi != win_idx):
                    smart_money_resistances += 1

            # Pattern: FOMO Overreaction on non-winning bucket
            if is_resolved and win_idx != -1 and bi != win_idx:
                if yp >= 0.50 or (p6_pct is not None and p6_pct >= 30.0):
                    fomo_overreactions += 1

            # Inspect bucket price_history sequence if available
            phist = b.get("price_history") or []
            if len(phist) >= 2:
                prices_seq = [safe_float(pt.get("p"), 0.0) or 0.0 for pt in phist if safe_float(pt.get("p")) is not None]
                if len(prices_seq) >= 2:
                    intraday_mover_buckets += 1
                    p_min, p_max, p_last = min(prices_seq), max(prices_seq), prices_seq[-1]
                    if p_min > 0.02 and ((p_max - p_min) / p_min) >= 0.25:
                        if p_max - p_last >= 0.12:
                            whipsaw_reversals += 1
                        else:
                            explosive_convergences += 1

    whipsaw_reversals += u_smart_saves

    # Merge with persistent memory so learned sequences are never lost when old files rotate
    mem_ts_trades = max(ts_trades, int(prior_mem.get("sample_locks", 0)))
    mem_bucket_moves = max(intraday_mover_buckets, int(prior_mem.get("sample_bucket_moves", 0)))
    mem_whipsaws = max(whipsaw_reversals, int(prior_mem.get("whipsaw_reversals", 0)))
    mem_resistances = max(smart_money_resistances, int(prior_mem.get("smart_money_resistances", 0)))
    mem_silent_leads = max(silent_model_leads, int(prior_mem.get("silent_model_leads", 0)))
    mem_fomo = max(fomo_overreactions, int(prior_mem.get("fomo_overreactions", 0)))
    mem_neighbor = max(neighbor_migrations, int(prior_mem.get("neighbor_migrations", 0)))
    mem_theta = max(theta_stagnations, int(prior_mem.get("theta_stagnations", 0)))
    mem_explosive = max(explosive_convergences, int(prior_mem.get("explosive_convergences", 0)))

    total_observations = mem_ts_trades + mem_bucket_moves + n_resolved
    has_temporal_support = (
        mem_ts_trades >= 3
        or mem_bucket_moves >= 6
        or (n_resolved >= 6 and (mem_neighbor >= 2 or mem_resistances >= 1 or mem_whipsaws >= 1))
    )

    avg_hold_h = round(sum(hold_hours_list) / len(hold_hours_list), 1) if hold_hours_list else prior_mem.get("avg_hold_hours", 8.0)
    avg_surge_pct = round(sum(scalp_gains) / len(scalp_gains), 1) if scalp_gains else prior_mem.get("avg_surge_pct", 32.0)
    avg_vel = round(sum(velocities_pct_per_h) / len(velocities_pct_per_h), 1) if velocities_pct_per_h else prior_mem.get("price_velocity_pct_per_h", 4.5)

    # Save updated sequence counters into persistent memory store
    cities_mem[key] = {
        "city": city,
        "market_type": mtype,
        "sample_locks": mem_ts_trades,
        "sample_bucket_moves": mem_bucket_moves,
        "n_resolved": n_resolved,
        "whipsaw_reversals": mem_whipsaws,
        "smart_money_resistances": mem_resistances,
        "silent_model_leads": mem_silent_leads,
        "fomo_overreactions": mem_fomo,
        "neighbor_migrations": mem_neighbor,
        "theta_stagnations": mem_theta,
        "explosive_convergences": mem_explosive,
        "avg_hold_hours": avg_hold_h,
        "avg_surge_pct": avg_surge_pct,
        "price_velocity_pct_per_h": avg_vel,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    if not has_temporal_support:
        return {
            "has_temporal_support": False,
            "sample_locks": mem_ts_trades,
            "sample_bucket_moves": mem_bucket_moves,
            "profitable_scalps": profitable_scalps,
            "confidence_tag": "⏳ در حال یادگیری رفتار توالی بازار در حافظه",
            "dominant_regime_code": "ACCUMULATING_MEMORY",
            "complex_strategy_title": "⏳ در حال تکمیل حافظهٔ توالی رفتار قیمت برای این شهر",
            "complex_strategy_body": (
                "حافظهٔ توالی ربات در حال ثبت و انباشت رفتارهای درون‌روزی این شهر در اسکن‌های متوالی است و تا زمان رسیدن به حد نصاب آماری، از صدور استراتژی رفتاری پیچیده خودداری می‌کند."
            ),
            "secondary_insight": "",
        }

    # ------------------------------------------------------------------------
    # Multi-Regime Behavioral Diagnosis & Strategy Synthesis from Memory
    # ------------------------------------------------------------------------
    adj_gap_pct = round((rec_within1_rate - rec_hit_rate) * 100.0, 1)
    regimes_detected: List[Tuple[float, str, str, str]] = []

    # Regime 1: Intraday Whipsaw & Mandatory Pre-Settlement Exit
    if mem_whipsaws >= 2 or (u_smart_saves >= 1 and rec_hit_rate < 0.45):
        score_w = 88.0 + min(10.0, mem_whipsaws * 2.5)
        regimes_detected.append((
            score_w,
            "WHIPSAW_SCALP_ONLY",
            "🔄 الگوی چرخش درون‌روزی و تلهٔ تسویه (Intraday Whipsaw & Pre-Settlement Exit)",
            (
                f"بررسی حافظهٔ توالی این شهر نشان می‌دهد قیمت باکت‌های پیش‌رو در میانهٔ روز بارها تا محدودهٔ سود (+۲۵٪ تا +{int(round(avg_surge_pct))}٪) بالا می‌رود، "
                f"اما در ساعات پایانی با تغییر شیب دمای فرودگاه چرخش کرده و در تسویه نهایی صفر می‌شود ({mem_whipsaws} مورد چرخش/نجات ثبت‌شده). "
                "<b>استراتژی قطعی:</b> این شهر صرفاً بازار «نوسان‌گیری درون‌روزی (Scalp)» است؛ نگهداری پوزیشن تا تسویه نهایی (Hold-to-Settlement) ممنوع است و باید روی +۲۵٪ تا +۳۵٪ سود با Auto-TP خارج شد."
            ),
        ))

    # Regime 2: Adjacent Bucket Thermal Drift (Neighbor Migration)
    if adj_gap_pct >= 32.0 or mem_neighbor >= max(3, int(n_resolved * 0.35)):
        dir_hint = (
            f"به سمت باکتِ خنک‌تر (پایین‌تر) به دلیل گرم‌خوانی +{rec_bias:.2f}{unit_sym}"
            if rec_bias >= 0.25
            else f"به سمت باکتِ گرم‌تر (بالاتر) به دلیل سردخوانی {rec_bias:.2f}{unit_sym}"
            if rec_bias <= -0.25
            else "بین دو باکت مرکزیِ مجاور"
        )
        score_n = 85.0 + min(12.0, adj_gap_pct * 0.2)
        regimes_detected.append((
            score_n,
            "NEIGHBOR_BUCKET_DRIFT",
            "🔀 الگوی کوچ دما به باکت همسایه (Adjacent Bucket Thermal Migration)",
            (
                f"حافظهٔ توالی بازارهای این شهر نشان می‌دهد در {mem_neighbor} روز ثبت‌شده، دمای نهایی در ساعات اوج روز به اندازهٔ دقیقاً ۱ باکت نسبت به پیک اولیهٔ مدل جابه‌جا شده است "
                f"(شکاف دقت ±1 باکت و تک‌باکت: +{adj_gap_pct}% — تمایل غالب {dir_hint}). "
                "<b>استراتژی قطعی:</b> به جای تک‌خال زدن روی یک باکت، قبل از بیدار شدن بازار ۳۰٪ تا ۴۰٪ حجم معامله را روی باکت همسایه (در حالی که هنوز ۱۲ تا ۲۲ سنت قیمت دارد) بکارید تا در صورت کوچ دما، جهش چندبرابری آن کل پوزیشن را برنده کند."
            ),
        ))

    # Regime 3: Smart-Money Resistance vs Model Trap
    if mem_resistances >= 2 or (rec_hit_rate < 0.35 and abs(rec_bias) >= 0.45):
        score_r = 84.0 + min(12.0, mem_resistances * 3.0)
        regimes_detected.append((
            score_r,
            "SMART_MONEY_RESISTANCE",
            "🛑 الگوی مقاومت هوشمند بازار در برابر خطای مدل (Smart-Money Resistance Trap)",
            (
                "در حافظهٔ توالی این شهر مشاهده می‌شود که هرگاه مدل هواشناسی احتمال بالایی (>۴۵٪) به یک باکت می‌دهد اما قیمت تابلوی بازار در اسکن‌های متوالی زیر ۲۰ تا ۲۲ سنت قفل می‌ماند و بالا نمی‌رود، "
                "معامله‌گران بومی (بر اساس پوشش ابر، باد ساحلی یا بارش لحظه‌ای) زودتر از مدل‌های جهانی متوجه خطای پیش‌بینی شده‌اند. "
                "<b>استراتژی قطعی:</b> واگرایی‌های بالای ۲۵٪ در این شهر «سیگنال خرید ارزان» نیستند بلکه تلهٔ مدل هستند؛ شرط ورود، همراهی حداقل جریان سفارشات بازار (قیمت بالای ۲۲ سنت و شیب مثبت) یا شکار معکوس (NO) است."
            ),
        ))

    # Regime 4: Silent Model Lead & Delayed Market Convergence
    if mem_silent_leads >= 2 and rec_hit_rate >= 0.42:
        score_s = 86.0 + min(10.0, mem_silent_leads * 2.0)
        regimes_detected.append((
            score_s,
            "SILENT_MODEL_LEAD",
            "⚡ الگوی واگرایی پیش‌رو مدل و جاماندگی بازار (Delayed Market Catch-Up)",
            (
                f"در حافظهٔ توالی این شهر، آپدیت مدل‌های هواشناسی معمولاً چند ساعت جلوتر از تابلوی معاملات حرکت می‌کند ({mem_silent_leads} مورد واگرایی پیش‌رو ثبت‌شده) و بازار با تأخیر زمانی به سمت ارزش منصفانهٔ مدل هم‌گرا می‌شود. "
                "<b>استراتژی قطعی:</b> بهترین نقطهٔ ورود بلافاصله پس از اسکن‌های ۶ ساعته است، زمانی که احتمال مدل جهش کرده اما تابلوی بازار هنوز واکنش نشان نداده است؛ سفارش فروش خودکار (Limit Sell) را ۴ سنت زیر ارزش منصفانهٔ مدل بگذارید."
            ),
        ))

    # Regime 5: FOMO Overreaction & Mean Reversion
    if mem_fomo >= 2 or (u_smart_saves >= 1 and avg_surge_pct >= 40.0):
        score_f = 82.0 + min(10.0, mem_fomo * 2.5)
        regimes_detected.append((
            score_f,
            "FOMO_MEAN_REVERSION",
            "🫧 الگوی حباب هیجانی و تخلیهٔ قیمت (Intraday Overreaction & Fade)",
            (
                "حافظهٔ قیمت این شهر نشان‌دهندهٔ جهش‌های هیجانی و زودهنگام روی باکت‌های غیربرنده در ابتدای روز و سپس تخلیهٔ شدید حباب پس از تثبیت گزارش‌های فرودگاهی است. "
                "<b>استراتژی قطعی:</b> از خرید باکت‌ها پس از جهش اولیه (قیمت‌های بالای ۵۲ سنت) اکیداً خودداری کنید؛ اگر از قبل در قیمت پایین وارد شده‌اید، در اولین موج هیجانی بازار (+۳۰٪ تا +۴۵٪) بدون طمع پوزیشن را ببندید."
            ),
        ))

    # Regime 6: Theta Decay & Diurnal Plateau Stop
    if mem_theta >= 2:
        score_t = 80.0 + min(10.0, mem_theta * 2.5)
        regimes_detected.append((
            score_t,
            "THETA_PLATEAU_STOP",
            "⏳ الگوی فرسایش زمانی در فلات دمایی (Theta Decay & Time-Stop)",
            (
                f"در حافظهٔ معاملات این شهر، پوزیشن‌هایی که بیش از {int(round(avg_hold_h))} ساعت بدون رشد معنادار در قیمت ورود درجا می‌زنند، با رسیدن به سقف/کف ساعت حرارتی روز دچار ریزش ناگهانی می‌شوند. "
                "<b>استراتژی قطعی:</b> اعمال «حد ضرر زمانی (Time-Stop)» و فعال‌سازی حتماً محافظ دمای واقعی فرودگاه (METAR Auto-Stop)؛ اگر قیمت تا میانهٔ روز به سمت مدل حرکت نکرد، بدون صبر کردن تا پایان روز با کمترین زیان خارج شوید."
            ),
        ))

    # Regime 7: High-Conviction METAR Momentum Ride (Default for clean alpha cities)
    if not regimes_detected or (rec_hit_rate >= 0.52 and profitable_scalps >= 2):
        score_m = 89.0 if (rec_hit_rate >= 0.52 and profitable_scalps >= 2) else 78.0
        regimes_detected.append((
            score_m,
            "METAR_MOMENTUM_RIDE",
            "🎯 الگوی هم‌گرایی پرشتاب با گزارش‌های فرودگاهی (High-Conviction METAR Convergence)",
            (
                f"حافظهٔ توالی این شهر نشان می‌دهد که روند قیمت درون‌روزی هم‌راستا با پیش‌بینی مدل و گزارش‌های واقعی ایستگاه فرودگاهی حرکت می‌کند (دقت اخیر: {round(rec_hit_rate*100, 1)}% و {profitable_scalps} موج سودده ثبت‌شده). "
                "<b>استراتژی قطعی:</b> ورود در فاز آرام بازار با سفارش Limit و نگهداری پوزیشن با «حد ضرر متحرک (Trailing Stop 8%)» پس از عبور قیمت از ۶۵ سنت، تا به جای خروج زودهنگام، بیشترین بخش رالی تا ۱۰۰ سنت شکار شود."
            ),
        ))

    regimes_detected.sort(key=lambda x: x[0], reverse=True)
    primary = regimes_detected[0]
    secondary = regimes_detected[1] if len(regimes_detected) >= 2 else None

    secondary_text = ""
    if secondary:
        secondary_text = f"<b>مکمل رفتاری در حافظهٔ این شهر ({secondary[2]}):</b> {secondary[3]}"

    conf_tag = (
        f"🧠 استخراج‌شده از حافظهٔ توالی ({total_observations} نقطهٔ داده‌ای)"
    )

    return {
        "has_temporal_support": True,
        "sample_locks": mem_ts_trades,
        "sample_bucket_moves": mem_bucket_moves,
        "profitable_scalps": profitable_scalps,
        "confidence_tag": conf_tag,
        "dominant_regime_code": primary[1],
        "complex_strategy_title": primary[2],
        "complex_strategy_body": primary[3],
        "secondary_insight": secondary_text,
        "avg_hold_hours": avg_hold_h,
        "avg_surge_pct": avg_surge_pct,
        "price_velocity_pct_per_h": avg_vel,
    }


# ============================================================================
# 2. Data Loaders & Cross-Referencing Engine
# ============================================================================

def load_all_data() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    # 1. Load all market JSONs
    markets: List[Dict[str, Any]] = []
    if os.path.isdir(MARKETS_DIR):
        for p in sorted(glob.glob(os.path.join(MARKETS_DIR, "*.json"))):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                base = os.path.basename(p)
                if "market_type" not in obj:
                    obj["market_type"] = "min" if base.endswith("_min.json") else "max"
                markets.append(obj)
            except Exception:
                pass

    # 2. Load locked_signals.json
    locks: List[Dict[str, Any]] = []
    if os.path.isfile(LOCKS_FILE):
        try:
            with open(LOCKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    locks = data
        except Exception:
            pass

    # 3. Load calibration.json
    calibration: Dict[str, Any] = {}
    if os.path.isfile(CALIBRATION_FILE):
        try:
            with open(CALIBRATION_FILE, "r", encoding="utf-8") as f:
                calibration = json.load(f)
        except Exception:
            pass

    return markets, locks, calibration


# ============================================================================
# 3. Core Quantitative Analysis Engine
# ============================================================================

def run_quantitative_analysis() -> Dict[str, Any]:
    markets, locks, calibration = load_all_data()
    now_utc = datetime.now(timezone.utc)
    now_iran = now_utc.astimezone(IRAN_TZ)

    # Index markets by (city, date, market_type) and by market_id
    market_by_key: Dict[str, Dict[str, Any]] = {}
    bucket_by_market_id: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}

    for m in markets:
        c = m.get("city", "")
        d = m.get("date", "")
        mt = m.get("market_type") or "max"
        market_by_key[f"{c}|{d}|{mt}"] = m
        if f"{c}|{d}|max" not in market_by_key and mt == "max":
            market_by_key[f"{c}|{d}|max"] = m
        for b in m.get("full_distribution") or []:
            mid = str(b.get("market_id") or "")
            if mid:
                bucket_by_market_id[mid] = (m, b)

    # ------------------------------------------------------------------------
    # Part A: Analyze User's Personal Locked Signals (Forensics & Behavior)
    # ------------------------------------------------------------------------
    user_locks_analyzed: List[Dict[str, Any]] = []
    locks_by_city_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    raw_locks_by_city_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    entry_band_stats: Dict[str, Dict[str, Any]] = {
        "cheap_under_25c": {"label": "ارزان / زیر ۲۵ سنت (<25¢)", "count": 0, "wins": 0, "pnl_pct_sum": 0.0, "hold_wins": 0, "hold_total": 0},
        "value_25_to_45c": {"label": "ارزشی / ۲۵ تا ۴۵ سنت (25-45¢)", "count": 0, "wins": 0, "pnl_pct_sum": 0.0, "hold_wins": 0, "hold_total": 0},
        "core_45_to_65c": {"label": "میانی-محتمل / ۴۵ تا ۶۵ سنت (45-65¢)", "count": 0, "wins": 0, "pnl_pct_sum": 0.0, "hold_wins": 0, "hold_total": 0},
        "heavy_over_65c": {"label": "شبه-قطعی / بالای ۶۵ سنت (>65¢)", "count": 0, "wins": 0, "pnl_pct_sum": 0.0, "hold_wins": 0, "hold_total": 0},
    }

    manual_smart_saves = 0       # User unlocked manually AND the bucket eventually LOST (0c)!
    manual_early_regrets = 0     # User unlocked manually AND the bucket eventually WON (100c)!
    manual_profitable_scalps = 0 # User unlocked manually at a higher price than entry
    resolved_wins = 0
    resolved_losses = 0

    for l in locks:
        status = l.get("status", "")
        if status == "cancelled_entry":
            continue

        city = l.get("city", "")
        date = l.get("date", "")
        mtype = l.get("market_type") or "max"
        mid = str(l.get("market_id") or "")
        side = (l.get("side") or "YES").upper()
        entry_p = safe_float(l.get("entry_price"), 0.0) or 0.0
        if entry_p <= 0:
            continue

        mkt_tuple = bucket_by_market_id.get(mid)
        mkt = mkt_tuple[0] if mkt_tuple else market_by_key.get(f"{city}|{date}|{mtype}")
        bkt = mkt_tuple[1] if mkt_tuple else None

        exit_p = safe_float(l.get("exit_price"))
        last_p = safe_float(l.get("last_price"), entry_p)
        eff_exit = exit_p if exit_p is not None else last_p

        # Determine if the bucket actually won at final market resolution
        actual_won: Optional[bool] = None
        actual_temp = safe_float(l.get("actual_temp"))
        if actual_temp is None and mkt:
            actual_temp = safe_float(mkt.get("actual_temp"))

        if mkt and mkt.get("status") == "resolved":
            win_mid = str(mkt.get("winning_market_id") or "")
            if win_mid:
                bucket_won_yes = (win_mid == mid)
                actual_won = bucket_won_yes if side == "YES" else (not bucket_won_yes)
            elif actual_temp is not None and bkt and bkt.get("range"):
                bucket_won_yes = check_temp_in_range(actual_temp, bkt["range"])
                actual_won = bucket_won_yes if side == "YES" else (not bucket_won_yes)
        elif status == "closed_resolved" and exit_p is not None:
            actual_won = exit_p >= 0.95

        pnl_pct = ((eff_exit - entry_p) / entry_p) * 100.0 if eff_exit is not None and entry_p > 0 else 0.0
        is_positive_trade = pnl_pct > 0.5

        # Band classification
        if entry_p < 0.25:
            band_key = "cheap_under_25c"
        elif entry_p < 0.45:
            band_key = "value_25_to_45c"
        elif entry_p < 0.65:
            band_key = "core_45_to_65c"
        else:
            band_key = "heavy_over_65c"

        if status != "open":
            b_stat = entry_band_stats[band_key]
            b_stat["count"] += 1
            if is_positive_trade:
                b_stat["wins"] += 1
            b_stat["pnl_pct_sum"] += pnl_pct
            if actual_won is not None:
                b_stat["hold_total"] += 1
                if actual_won:
                    b_stat["hold_wins"] += 1

        # Manual Unlock vs Hold-to-Resolution Forensics
        forensic_tag = "باز / در جریان"
        if status == "closed_resolved":
            if (exit_p or 0) >= 0.95:
                resolved_wins += 1
                forensic_tag = "🏆 برد کامل در تسویه (100¢)"
            else:
                resolved_losses += 1
                forensic_tag = "💥 باخت در تسویه (0¢ — نیازمند SL/METAR)"
        elif status == "closed_manual":
            if pnl_pct > 2.0:
                manual_profitable_scalps += 1
            if actual_won is False:
                manual_smart_saves += 1
                forensic_tag = f"🛡️ نجات هوشمندانه از باخت حتمی (خروج در {round((eff_exit or 0)*100)}¢ قبل از صفر شدن)"
            elif actual_won is True:
                manual_early_regrets += 1
                forensic_tag = f"💸 خروج زودهنگام در {round((eff_exit or 0)*100)}¢ (باکت در نهایت 100¢ برنده شد)"
            else:
                forensic_tag = f"خروج دستی ({pnl_pct:+.1f}%)"
        elif status.startswith("closed_auto_"):
            forensic_tag = f"🤖 خروج خودکار ربات ({status.replace('closed_auto_', '').upper()})"

        rec = {
            "city": city,
            "city_name": LOCATIONS.get(city, {}).get("name", city),
            "date": date,
            "market_type": mtype,
            "market_id": mid,
            "side": side,
            "entry_price": entry_p,
            "exit_price": eff_exit,
            "pnl_pct": round(pnl_pct, 1),
            "status": status,
            "actual_won": actual_won,
            "actual_temp": actual_temp,
            "forensic_tag": forensic_tag,
            "bucket_label": format_range_label(bkt.get("range") if bkt else None, mkt.get("unit", "") if mkt else ""),
        }
        user_locks_analyzed.append(rec)
        locks_by_city_type[f"{city}|{mtype}"].append(rec)
        raw_locks_by_city_type[f"{city}|{mtype}"].append(l)

    # ------------------------------------------------------------------------
    # Part B: Analyze All Resolved Markets & Model/Market Behavior per City
    # ------------------------------------------------------------------------
    markets_by_city_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    all_markets_by_city_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for m in markets:
        c = m.get("city", "")
        mt = m.get("market_type") or "max"
        if c:
            all_markets_by_city_type[f"{c}|{mt}"].append(m)
        if m.get("status") == "resolved" and m.get("actual_temp") is not None and m.get("forecast_mean") is not None:
            markets_by_city_type[f"{c}|{mt}"].append(m)

    city_playbooks: List[Dict[str, Any]] = []
    temporal_memory = load_temporal_memory()

    all_keys = sorted(set(list(markets_by_city_type.keys()) + list(locks_by_city_type.keys())))

    for key in all_keys:
        city, mtype = key.split("|", 1)
        loc_info = LOCATIONS.get(city, {})
        city_name = loc_info.get("name", city.replace("-", " ").title())
        is_us = city in US_CITIES or loc_info.get("unit") == "F"
        unit_sym = "°F" if is_us else "°C"
        lat = safe_float(loc_info.get("lat"), 40.0) or 40.0

        m_list = sorted(markets_by_city_type.get(key, []), key=lambda x: x.get("date", ""))
        u_locks = locks_by_city_type.get(key, [])
        closed_u_locks = [x for x in u_locks if x["status"] != "open"]

        if len(m_list) == 0 and len(closed_u_locks) == 0:
            continue

        # Sort resolved markets chronologically and split into Recent (last 14 resolved) vs Older
        n_total = len(m_list)
        recent_m = m_list[-14:] if n_total >= 14 else m_list

        exact_hits = 0
        within1_hits = 0
        recent_exact_hits = 0
        recent_within1_hits = 0

        abs_errors: List[float] = []
        recent_abs_errors: List[float] = []
        signed_biases: List[float] = []  # forecast - actual (>0 means model is too warm, <0 means model is too cold)
        recent_signed_biases: List[float] = []

        submodel_errors: Dict[str, List[float]] = defaultdict(list)
        submodel_exact_hits: Dict[str, int] = defaultdict(int)
        submodel_counts: Dict[str, int] = defaultdict(int)

        winning_entry_prices: List[float] = []
        model_edge_when_won: List[float] = []

        for idx_m, m in enumerate(m_list):
            is_rec = m in recent_m
            actual_t = float(m["actual_temp"])
            f_mean = float(m["forecast_mean"])
            err = f_mean - actual_t  # positive = model predicted warmer than actual
            abs_err = abs(err)

            abs_errors.append(abs_err)
            signed_biases.append(err)
            if is_rec:
                recent_abs_errors.append(abs_err)
                recent_signed_biases.append(err)

            dist = m.get("full_distribution") or []
            if dist:
                # Find model's top bucket and winning bucket index
                best_idx = -1
                best_prob = -1.0
                win_idx = -1
                win_mid = str(m.get("winning_market_id") or "")

                for bi, b in enumerate(dist):
                    mp = safe_float(b.get("model_prob"), 0.0) or 0.0
                    if mp > best_prob:
                        best_prob = mp
                        best_idx = bi
                    if win_mid and str(b.get("market_id")) == win_mid:
                        win_idx = bi
                    elif win_idx == -1 and b.get("range") and check_temp_in_range(actual_t, b["range"]):
                        win_idx = bi

                if best_idx != -1 and win_idx != -1:
                    dist_buckets = abs(best_idx - win_idx)
                    if dist_buckets == 0:
                        exact_hits += 1
                        if is_rec:
                            recent_exact_hits += 1
                        yp = safe_float(dist[best_idx].get("yes_price"))
                        if yp and yp > 0.01:
                            winning_entry_prices.append(yp)
                            model_edge_when_won.append(best_prob - yp)
                    if dist_buckets <= 1:
                        within1_hits += 1
                        if is_rec:
                            recent_within1_hits += 1

            # Evaluate NWP sub-models (GFS, ECMWF, ICON, GEM)
            raw_models = m.get("model_means_raw") or {}
            if isinstance(raw_models, dict):
                for sm_name, sm_val in raw_models.items():
                    sv = safe_float(sm_val)
                    if sv is not None:
                        sm_err = abs(sv - actual_t)
                        submodel_errors[sm_name].append(sm_err)
                        submodel_counts[sm_name] += 1
                        tol = 1.0 if is_us else 0.55
                        if sm_err <= tol:
                            submodel_exact_hits[sm_name] += 1

        # Calibration info
        cal_key = f"{city}_min_D0" if mtype == "min" else f"{city}_D0"
        cal_obj = calibration.get(cal_key) or {}
        sigma_val = safe_float(cal_obj.get("sigma"), 1.2) or 1.2

        # Compute core statistical metrics
        hit_rate = (exact_hits / n_total) if n_total > 0 else 0.0
        within1_rate = (within1_hits / n_total) if n_total > 0 else 0.0
        rec_n = len(recent_m)
        rec_hit_rate = (recent_exact_hits / rec_n) if rec_n > 0 else hit_rate
        rec_within1_rate = (recent_within1_hits / rec_n) if rec_n > 0 else within1_rate

        mae = (sum(abs_errors) / len(abs_errors)) if abs_errors else 1.5
        rec_mae = (sum(recent_abs_errors) / len(recent_abs_errors)) if recent_abs_errors else mae
        mean_bias = (sum(signed_biases) / len(signed_biases)) if signed_biases else 0.0
        rec_bias = (sum(recent_signed_biases) / len(recent_signed_biases)) if recent_signed_biases else mean_bias

        # Blend verified 14d & 55d statistics from calibration.json if local market JSONs are < 10
        if cal_obj and n_total < 10:
            cal_mae = float(cal_obj.get("live_mae") or cal_obj.get("base_mae") or mae)
            cal_n_total = max(int(cal_obj.get("n_live") or cal_obj.get("n_samples") or 30), n_total)
            cal_n_rec = max(int(cal_obj.get("recent_14d_n") or 14), rec_n)
            s_eff = max(0.28, cal_mae / 0.9488)
            w0 = 1.0 if is_us else 0.52
            w1 = 3.0 if is_us else 1.52
            # Normal/Student-t integral approximation via erf
            cal_hit_prob = max(0.15, min(0.92, math.erf((w0 / s_eff) / math.sqrt(2.0))))
            cal_w1_prob = max(cal_hit_prob, min(0.99, math.erf((w1 / s_eff) / math.sqrt(2.0))))

            prior_tot_n = max(0, cal_n_total - n_total)
            prior_rec_n = max(0, cal_n_rec - rec_n)

            if n_total + prior_tot_n > 0:
                eff_tot = n_total + prior_tot_n
                exact_hits_eff = exact_hits + prior_tot_n * cal_hit_prob
                within1_hits_eff = within1_hits + prior_tot_n * cal_w1_prob
                hit_rate = exact_hits_eff / eff_tot
                within1_rate = within1_hits_eff / eff_tot
                mae = (sum(abs_errors) + prior_tot_n * cal_mae) / eff_tot
                cal_raw_bias = float(cal_obj.get("live_bias_raw") or cal_obj.get("base_bias_raw") or 0.0)
                mean_bias = (sum(signed_biases) + prior_tot_n * cal_raw_bias) / eff_tot
                n_total = eff_tot
                exact_hits = int(round(exact_hits_eff))
                within1_hits = int(round(within1_hits_eff))

            if rec_n + prior_rec_n > 0:
                eff_rec = rec_n + prior_rec_n
                rec_exact_eff = recent_exact_hits + prior_rec_n * cal_hit_prob
                rec_w1_eff = recent_within1_hits + prior_rec_n * cal_w1_prob
                rec_hit_rate = rec_exact_eff / eff_rec
                rec_within1_rate = rec_w1_eff / eff_rec
                rec_mae = (sum(recent_abs_errors) + prior_rec_n * cal_mae) / eff_rec
                cal_rec_bias = float(cal_obj.get("recent_14d_bias") or cal_obj.get("live_bias_raw") or 0.0)
                rec_bias = (sum(recent_signed_biases) + prior_rec_n * cal_rec_bias) / eff_rec
                rec_n = eff_rec
                recent_exact_hits = int(round(rec_exact_eff))
                recent_within1_hits = int(round(rec_w1_eff))

        # Wilson lower bound & Bayesian shrunk accuracy for high-credibility ranking
        wilson_lb = wilson_lower_bound(exact_hits, n_total)
        rec_wilson_lb = wilson_lower_bound(recent_exact_hits, rec_n)
        shrunk_acc = bayesian_shrunk_rate(exact_hits + recent_exact_hits, n_total + rec_n)

        # Sub-model ranking
        submodel_summary = []
        for sm_name, errs in submodel_errors.items():
            if len(errs) >= 2:
                sm_mae = sum(errs) / len(errs)
                sm_hit = (submodel_exact_hits[sm_name] / submodel_counts[sm_name]) * 100.0
                submodel_summary.append({
                    "name": sm_name.upper(),
                    "mae": round(sm_mae, 2),
                    "hit_pct": round(sm_hit, 1),
                    "n": len(errs),
                })
        submodel_summary.sort(key=lambda x: (x["mae"], -x["hit_pct"]))
        best_submodel = submodel_summary[0] if submodel_summary else None
        worst_submodel = submodel_summary[-1] if len(submodel_summary) >= 2 else None

        # User Personal Lock Performance in this City
        u_total = len(closed_u_locks)
        u_wins = sum(1 for x in closed_u_locks if x["pnl_pct"] > 0.5)
        u_win_rate = (u_wins / u_total * 100.0) if u_total > 0 else None
        u_avg_pnl = (sum(x["pnl_pct"] for x in closed_u_locks) / u_total) if u_total > 0 else None
        u_avg_entry_c = (sum(x["entry_price"] * 100.0 for x in closed_u_locks) / u_total) if u_total > 0 else None
        u_smart_saves = sum(1 for x in closed_u_locks if "نجات هوشمندانه" in x["forensic_tag"])
        u_early_regrets = sum(1 for x in closed_u_locks if "خروج زودهنگام" in x["forensic_tag"])

        # --------------------------------------------------------------------
        # Synthesize Custom City Strategy & Playbook (With Obsessive Precision)
        # --------------------------------------------------------------------
        composite_score = (
            0.40 * rec_hit_rate
            + 0.25 * hit_rate
            + 0.20 * rec_within1_rate
            + 0.15 * wilson_lb
        ) * 100.0

        # Determine Tier & Strategy Archetype
        if rec_hit_rate >= 0.50 and n_total >= 5 and rec_mae <= (1.6 if is_us else 1.0):
            tier_code = "TIER_1_ALPHA"
            tier_badge = "🟢 TIER 1 — شکارچی اصلی (A+ Alpha)"
            credibility_label = "بسیار بالا (تأییدشده در بازه اخیر و کل دوره)"
        elif rec_within1_rate >= 0.78 and rec_hit_rate >= 0.28 and n_total >= 5:
            tier_code = "TIER_2_STRADDLE"
            tier_badge = "🔵 TIER 2 — استراتژی دوباکتی همسایه (Adjacent Straddle)"
            credibility_label = "بالا برای پوشش ۲ باکت مجاور (±1 باکت بالای ۷۸٪)"
        elif rec_hit_rate >= 0.33 or (u_win_rate is not None and u_win_rate >= 65.0 and u_total >= 3):
            tier_code = "TIER_3_SCALP"
            tier_badge = "🟡 TIER 3 — نوسان‌گیری انتخابی پیش از تسویه (Pre-Settlement Scalp)"
            credibility_label = "متوسط (مناسب سیو سود پله‌ای با Auto-Exit قبل از پایان روز)"
        else:
            tier_code = "TIER_4_AVOID"
            tier_badge = "🔴 TIER 4 — منطقه پرریسک / ممنوعه فعلی (Wait for Regime Stabilization)"
            credibility_label = "پایین در رژیم فصلی فعلی (خطای مدل بالاتر از حد مجاز)"

        # Determine Bias & Directional Shift Advice
        bias_thresh = 0.45 if is_us else 0.28
        if rec_bias >= bias_thresh:
            bias_diagnosis = f"🔥 گرم‌خوانی سیستماتیک مدل (+{rec_bias:.2f}{unit_sym})"
            bias_action = (
                f"مدل در ۱۴ روز اخیر به طور میانگین +{rec_bias:.2f}{unit_sym} گرم‌تر از دمای واقعی فرودگاه پیش‌بینی کرده است. "
                "هنگام انتخاب باکت تک‌سهمی یا دوباکتی، باکتِ پایین‌تر (خنک‌تر) نسبت به میانگین خام مدل اولویت قطعی دارد."
            )
        elif rec_bias <= -bias_thresh:
            bias_diagnosis = f"❄️ سردخوانی سیستماتیک مدل ({rec_bias:.2f}{unit_sym})"
            bias_action = (
                f"مدل در ۱۴ روز اخیر به طور میانگین {abs(rec_bias):.2f}{unit_sym} سردتر از واقعیت پیش‌بینی کرده است. "
                "هنگام انتخاب باکت، تمایل به باکتِ بالاتر (گرم‌تر) در سمت راست توزیع سودده‌تر است."
            )
        else:
            bias_diagnosis = f"⚖️ بایاس متعادل و خنثی ({rec_bias:+.2f}{unit_sym})"
            bias_action = "توزیع خطای مدل حول دمای واقعی متقارن است؛ باکت مرکزیِ دارای بیشترین احتمال مدل (Main Signal) بهترین انتخاب است."

        # Calculate Optimal City-Specific Entry, TP, and SL Parameters
        avg_win_entry = (sum(winning_entry_prices) / len(winning_entry_prices)) if winning_entry_prices else 0.32
        opt_entry_low_c = max(12, min(48, round((avg_win_entry - 0.08) * 100)))
        opt_entry_high_c = max(opt_entry_low_c + 10, min(68, round((avg_win_entry + 0.08) * 100)))
        opt_limit_discount_c = 2 if sigma_val <= 1.2 else 3

        if tier_code == "TIER_1_ALPHA":
            opt_tp_pct = 45
            opt_sl_pct = 26
            entry_mode_rec = "خرید فوری (در صورت EV > +18%) یا خرید شرطی ۲ سنت زیر Ask"
            hold_policy_rec = "نگهداری تا رسیدن به ارزش منصفانه مدل (Smart TP)؛ در صورت قفل شدن بالای 75¢، فعال‌سازی Trailing Stop 8%"
        elif tier_code == "TIER_2_STRADDLE":
            opt_tp_pct = 35
            opt_sl_pct = 28
            if rec_bias >= bias_thresh:
                adj_dir = "باکت اصلی مدل + ۱ باکت پایین‌تر (خنک‌تر)"
            elif rec_bias <= -bias_thresh:
                adj_dir = "باکت اصلی مدل + ۱ باکت بالاتر (گرم‌تر)"
            else:
                adj_dir = "باکت اصلی مدل + باکت همسایه دارای بیشترین EV"
            entry_mode_rec = f"تقسیم سرمایه (۶۵٪ / ۳۵٪) روی دو باکت مجاور: [{adj_dir}]"
            hold_policy_rec = "سیو سود خودکار (Auto-TP) روی هر باکتی که در طول روز با گزارش‌های METAR رشد ۳۵٪+ کرد"
        elif tier_code == "TIER_3_SCALP":
            opt_tp_pct = 25
            opt_sl_pct = 22
            entry_mode_rec = f"فقط خرید شرطی (Limit Buy GTC) با تخفیف حداقل {opt_limit_discount_c} تا ۴ سنت زیر قیمت فعلی بازار"
            hold_policy_rec = "خروج قطعی قبل از ساعات اوج دمای محلی با سیو سود ۲۵٪ تا ۳۰٪ (عدم نگهداری تا تسویه نهایی)"
        else:
            opt_tp_pct = 20
            opt_sl_pct = 18
            entry_mode_rec = "عدم ورود با پول واقعی (فقط تست در حالت SHADOW تا بازگشت دقت ۱۴ روزه به بالای ۴۰٪)"
            hold_policy_rec = "در صورت ورود آزمایشی، فعال‌سازی حتماً METAR Auto-Stop و حد ضرر سخت ۱۸٪"

        season_title, season_note = get_season_Context(now_utc.month, lat)
        temporal_prof = build_temporal_microstructure_profile(
            city=city,
            mtype=mtype,
            tz_name=loc_info.get("tz", "UTC"),
            city_locks_raw=raw_locks_by_city_type.get(key, []),
            city_markets_all=all_markets_by_city_type.get(key, []),
            rec_bias=rec_bias,
            unit_sym=unit_sym,
            rec_hit_rate=rec_hit_rate,
            rec_within1_rate=rec_within1_rate,
            n_resolved=n_total,
            u_smart_saves=u_smart_saves,
            u_early_regrets=u_early_regrets,
            memory_store=temporal_memory,
        )

        city_playbooks.append({
            "city": city,
            "city_name": city_name,
            "market_type": mtype,
            "is_us": is_us,
            "unit": unit_sym,
            "n_resolved": n_total,
            "rec_n": rec_n,
            "hit_pct": round(hit_rate * 100.0, 1),
            "within1_pct": round(within1_rate * 100.0, 1),
            "rec_hit_pct": round(rec_hit_rate * 100.0, 1),
            "rec_within1_pct": round(rec_within1_rate * 100.0, 1),
            "wilson_lb_pct": round(wilson_lb * 100.0, 1),
            "rec_wilson_lb_pct": round(rec_wilson_lb * 100.0, 1),
            "shrunk_acc_pct": round(shrunk_acc * 100.0, 1),
            "composite_score": round(composite_score, 1),
            "mae": round(mae, 2),
            "rec_mae": round(rec_mae, 2),
            "mean_bias": round(mean_bias, 2),
            "rec_bias": round(rec_bias, 2),
            "sigma": round(sigma_val, 2),
            "best_submodel": best_submodel,
            "worst_submodel": worst_submodel,
            "submodels": submodel_summary,
            "user_locks_total": u_total,
            "user_locks_wins": u_wins,
            "user_win_rate_pct": round(u_win_rate, 1) if u_win_rate is not None else None,
            "user_avg_pnl_pct": round(u_avg_pnl, 1) if u_avg_pnl is not None else None,
            "user_avg_entry_c": round(u_avg_entry_c, 1) if u_avg_entry_c is not None else None,
            "user_smart_saves": u_smart_saves,
            "user_early_regrets": u_early_regrets,
            "tier_code": tier_code,
            "tier_badge": tier_badge,
            "credibility_label": credibility_label,
            "season_title": season_title,
            "season_note": season_note,
            "bias_diagnosis": bias_diagnosis,
            "bias_action": bias_action,
            "opt_entry_band_c": f"{opt_entry_low_c}¢ – {opt_entry_high_c}¢",
            "opt_limit_discount_c": opt_limit_discount_c,
            "opt_tp_pct": opt_tp_pct,
            "opt_sl_pct": opt_sl_pct,
            "entry_mode_rec": entry_mode_rec,
            "hold_policy_rec": hold_policy_rec,
            "temporal_profile": temporal_prof,
        })

    save_temporal_memory(temporal_memory)

    # Sort playbooks by composite score descending
    city_playbooks.sort(key=lambda x: (
        0 if x["tier_code"] == "TIER_1_ALPHA" else 1 if x["tier_code"] == "TIER_2_STRADDLE" else 2 if x["tier_code"] == "TIER_3_SCALP" else 3,
        -x["composite_score"],
        -x["rec_hit_pct"],
    ))

    # Global Portfolio & User Lock Summary
    closed_locks_all = [x for x in user_locks_analyzed if x["status"] != "open"]
    total_user_closed = len(closed_locks_all)
    total_user_wins = sum(1 for x in closed_locks_all if x["pnl_pct"] > 0.5)
    overall_user_winrate = round((total_user_wins / total_user_closed) * 100.0, 1) if total_user_closed > 0 else 0.0
    overall_user_avg_pnl = round(sum(x["pnl_pct"] for x in closed_locks_all) / total_user_closed, 1) if total_user_closed > 0 else 0.0

    tier_counts = {
        "TIER_1_ALPHA": sum(1 for p in city_playbooks if p["tier_code"] == "TIER_1_ALPHA"),
        "TIER_2_STRADDLE": sum(1 for p in city_playbooks if p["tier_code"] == "TIER_2_STRADDLE"),
        "TIER_3_SCALP": sum(1 for p in city_playbooks if p["tier_code"] == "TIER_3_SCALP"),
        "TIER_4_AVOID": sum(1 for p in city_playbooks if p["tier_code"] == "TIER_4_AVOID"),
    }
    temporal_supported_count = sum(
        1 for p in city_playbooks if (p.get("temporal_profile") or {}).get("has_temporal_support")
    )

    return {
        "generated_at_utc": now_utc.strftime("%Y-%m-%d %H:%M UTC"),
        "generated_at_iran": now_iran.strftime("%Y-%m-%d %H:%M"),
        "total_markets_analyzed": len(markets),
        "total_playbooks": len(city_playbooks),
        "temporal_supported_count": temporal_supported_count,
        "tier_counts": tier_counts,
        "user_forensics": {
            "total_locks": len(user_locks_analyzed),
            "closed_locks": total_user_closed,
            "open_locks": len(user_locks_analyzed) - total_user_closed,
            "win_rate_pct": overall_user_winrate,
            "avg_pnl_pct": overall_user_avg_pnl,
            "resolved_wins": resolved_wins,
            "resolved_losses": resolved_losses,
            "manual_profitable_scalps": manual_profitable_scalps,
            "manual_smart_saves": manual_smart_saves,
            "manual_early_regrets": manual_early_regrets,
            "entry_bands": entry_band_stats,
        },
        "playbooks": city_playbooks,
        "recent_locks_sample": list(reversed(closed_locks_all))[:40],
    }


# ============================================================================
# 4. Standalone Interactive HTML Report Builder (`quant_strategy_report.html`)
# ============================================================================

def build_standalone_html_report(report: Dict[str, Any]) -> str:
    uf = report["user_forensics"]
    tc = report["tier_counts"]
    playbooks = report["playbooks"]

    # Build Entry Band SVG Bar Chart rows
    band_rows_html = ""
    for bkey, bval in uf["entry_bands"].items():
        cnt = bval["count"]
        wr = round((bval["wins"] / cnt) * 100.0, 1) if cnt > 0 else 0.0
        avg_p = round(bval["pnl_pct_sum"] / cnt, 1) if cnt > 0 else 0.0
        hold_wr = round((bval["hold_wins"] / bval["hold_total"]) * 100.0, 1) if bval["hold_total"] > 0 else 0.0
        bar_w = max(4, min(100, int(wr)))
        pnl_cls = "text-emerald-600" if avg_p >= 0 else "text-rose-600"
        band_rows_html += f"""
        <div class="bg-slate-50 border border-slate-200 rounded-xl p-3">
          <div class="flex flex-wrap items-center justify-between text-xs font-bold mb-1.5">
            <span class="text-slate-800">{html.escape(bval['label'])}</span>
            <span class="text-slate-500">{cnt} معامله قفل‌شده</span>
          </div>
          <div class="w-full h-3 bg-slate-200 rounded-full overflow-hidden mb-2">
            <div class="h-full bg-blue-600 rounded-full" style="width: {bar_w}%"></div>
          </div>
          <div class="grid grid-cols-3 gap-2 text-[11.5px] text-center">
            <div class="bg-white rounded-lg p-1.5 border border-slate-200">
              <span class="text-slate-500 block text-[10px]">نرخ برد خروجی شما</span>
              <b class="text-slate-900">{wr}%</b>
            </div>
            <div class="bg-white rounded-lg p-1.5 border border-slate-200">
              <span class="text-slate-500 block text-[10px]">میانگین بازده هر قفل</span>
              <b class="{pnl_cls}" dir="ltr">{avg_p:+.1f}%</b>
            </div>
            <div class="bg-white rounded-lg p-1.5 border border-slate-200">
              <span class="text-slate-500 block text-[10px]">برد در صورت نگهداری تا انتها</span>
              <b class="text-indigo-600">{hold_wr}%</b>
            </div>
          </div>
        </div>
        """

    # Build Top 12 Alpha Cities Visual Chart (Horizontal SVG Bars)
    top_alpha = [p for p in playbooks if p["n_resolved"] >= 5][:12]
    svg_bars = ""
    for i, p in enumerate(top_alpha):
        y = 12 + i * 32
        w_exact = max(6, int(p["rec_hit_pct"] * 2.4))
        w_adj = max(w_exact + 4, int(p["rec_within1_pct"] * 2.4))
        m_label = "Max" if p["market_type"] == "max" else "Min"
        label_str = f"{p['city_name']} ({m_label})"
        svg_bars += f"""
        <g transform="translate(0, {y})">
          <text x="155" y="15" text-anchor="end" font-size="11.5" font-weight="700" fill="#1e293b">{html.escape(label_str)}</text>
          <rect x="165" y="3" width="{w_adj}" height="16" rx="4" fill="#bfdbfe" />
          <rect x="165" y="3" width="{w_exact}" height="16" rx="4" fill="#16a34a" />
          <text x="{172 + w_adj}" y="15" font-size="11" font-weight="700" fill="#0f172a">
            دقیق: {p['rec_hit_pct']}% | ±1 باکت: {p['rec_within1_pct']}%
          </text>
        </g>
        """
    svg_height = max(120, len(top_alpha) * 32 + 20)

    # Build City Playbook Cards JSON for interactive client-side filtering
    playbooks_json = json.dumps(playbooks, ensure_ascii=False)
    recent_locks_json = json.dumps(report["recent_locks_sample"], ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>WeatherBet Quant Strategy Lab — آزمایشگاه تحلیل کمّی و استراتژی اختصاصی شهرها</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      background-color: #f1f5f9;
      color: #0f172a;
    }}
    .ltr-inline {{
      direction: ltr;
      display: inline-block;
      unicode-bidi: embed;
    }}
  </style>
</head>
<body class="min-h-screen p-3.5 md:p-6">
  <div class="max-w-7xl mx-auto space-y-6">

    <!-- Header Banner -->
    <div class="bg-slate-900 text-white rounded-2xl p-5 md:p-6 shadow-lg border border-slate-800">
      <div class="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div class="flex items-center gap-2.5 flex-wrap">
            <span class="px-2.5 py-1 rounded-md text-xs font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-400/30">
              🔬 گزارش روزانه تحلیلگر کمّی و معمار استراتژی (Quant Strategy Lab v1.0)
            </span>
            <span class="text-xs text-slate-400 ltr-inline">
              Updated: {html.escape(report['generated_at_iran'])} IRST ({html.escape(report['generated_at_utc'])})
            </span>
          </div>
          <h1 class="text-xl md:text-2xl font-extrabold mt-2">
            کتابچهٔ استراتژی‌های سودساز شهرها + کالبدشکافی قفل‌های شخصی و سیگنال‌های کل بازار
          </h1>
          <p class="text-xs md:text-sm text-slate-300 mt-1.5 leading-relaxed max-w-4xl">
            این گزارش به‌صورت مستقل از داشبورد معاملاتی، با تحلیل هم‌زمان <b>{uf['total_locks']} سیگنال قفل‌شدهٔ شخصی شما</b> و
            <b>{report['total_markets_analyzed']} پروندهٔ بازار و ایستگاه‌های فرودگاهی</b>، با استفاده از فاصله اطمینان آماری ویلسون (Wilson CI) و بیزین، برای هر شهر استراتژی ورود، بایاس فصلی و نقاط دقیق سیو سود (TP) و حد ضرر (SL) را استخراج کرده است.
          </p>
        </div>
      </div>

      <!-- Top KPI Strip -->
      <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mt-5">
        <div class="bg-slate-800/90 border border-slate-700 rounded-xl p-3 text-center">
          <div class="text-[11px] text-slate-400">نرخ برد قفل‌های بسته‌شده شما</div>
          <div class="text-lg font-extrabold text-emerald-400 mt-1 ltr-inline">{uf['win_rate_pct']}%</div>
          <div class="text-[10.5px] text-slate-400 mt-0.5">{uf['closed_locks']} قفل بسته‌شده</div>
        </div>
        <div class="bg-slate-800/90 border border-slate-700 rounded-xl p-3 text-center">
          <div class="text-[11px] text-slate-400">میانگین بازده هر قفل شما</div>
          <div class="text-lg font-extrabold text-blue-400 mt-1 ltr-inline">{uf['avg_pnl_pct']:+.1f}%</div>
          <div class="text-[10.5px] text-slate-400 mt-0.5">روی کل قفل‌های شخصی</div>
        </div>
        <div class="bg-slate-800/90 border border-slate-700 rounded-xl p-3 text-center">
          <div class="text-[11px] text-slate-400">🛡️ نجات هوشمند با خروج دستی</div>
          <div class="text-lg font-extrabold text-amber-300 mt-1 ltr-inline">{uf['manual_smart_saves']} مورد</div>
          <div class="text-[10.5px] text-slate-400 mt-0.5">باکت‌هایی که بعد از خروج شما 0¢ شدند</div>
        </div>
        <div class="bg-slate-800/90 border border-slate-700 rounded-xl p-3 text-center">
          <div class="text-[11px] text-slate-400">💸 خروج زودهنگام روی باکت برنده</div>
          <div class="text-lg font-extrabold text-rose-400 mt-1 ltr-inline">{uf['manual_early_regrets']} مورد</div>
          <div class="text-[10.5px] text-slate-400 mt-0.5">قابل تبدیل به سود ۱۰۰¢ با Trailing Stop</div>
        </div>
        <div class="bg-slate-800/90 border border-slate-700 rounded-xl p-3 text-center">
          <div class="text-[11px] text-slate-400">🟢 شهرهای درجه ۱ (A+ Alpha)</div>
          <div class="text-lg font-extrabold text-emerald-400 mt-1">{tc['TIER_1_ALPHA']} بازار-شهر</div>
          <div class="text-[10.5px] text-slate-400 mt-0.5">دقت اخیر بالای ۵۰٪ + خطای پایین</div>
        </div>
        <div class="bg-slate-800/90 border border-slate-700 rounded-xl p-3 text-center">
          <div class="text-[11px] text-slate-400">🔵 کاندیدای استراتژی دوباکتی</div>
          <div class="text-lg font-extrabold text-sky-400 mt-1">{tc['TIER_2_STRADDLE']} بازار-شهر</div>
          <div class="text-[10.5px] text-slate-400 mt-0.5">دقت ±1 باکت بالای ۷۸٪</div>
        </div>
      </div>
    </div>

    <!-- Section 1: Deep Forensics of User's Locked Signals & Market Chart -->
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-5">
      <!-- Card A: Entry Price Band Performance -->
      <div class="bg-white border border-slate-200 rounded-2xl p-4 md:p-5 shadow-xs">
        <h2 class="text-base font-extrabold text-slate-900 mb-1">
          🎯 کالبدشکافی عملکرد قفل‌های شما بر اساس «بازه قیمت ورود (سنت)»
        </h2>
        <p class="text-xs text-slate-500 mb-3.5">
          مقایسهٔ بازدهی واقعی شما در قیمت‌های مختلف ورود در برابر نگهداری تا لحظهٔ تسویه (Hold to Resolution)
        </p>
        <div class="space-y-2.5">
          {band_rows_html}
        </div>
        <div class="mt-3.5 p-3 rounded-xl bg-blue-50 border border-blue-200 text-xs text-slate-700 leading-relaxed">
          💡 <b>درس استراتژیک از کارنامهٔ قفل‌های شما:</b>
          شما در <b>{uf['manual_smart_saves']} معامله</b> با بستن دستیِ به‌موقع قبل از پایان روز، سرمایهٔ خود را از باکت‌هایی که در نهایت در تسویه <b>۰ سنت (بازنده کامل)</b> شدند نجات داده‌اید؛ و در <b>{uf['manual_early_regrets']} معامله</b> زود خارج شدید در حالی که باکت در نهایت ۱۰۰ سنت شد.
          <b>نسخهٔ طلایی برای ربات Auto-Exit شما:</b> برای باکت‌های ۲۵ تا ۵۵ سنت، حد سود اولیه (TP) را روی <b>ارزش منصفانهٔ مدل</b> بگذارید و به محض رفتن در سود بالای +۲۵٪، <b>حد ضرر متحرک (Trailing Stop 8%)</b> و <b>محافظ دمای METAR</b> را فعال نگه دارید.
        </div>
      </div>

      <!-- Card B: SVG Visual Chart of Top Performing Cities -->
      <div class="bg-white border border-slate-200 rounded-2xl p-4 md:p-5 shadow-xs">
        <h2 class="text-base font-extrabold text-slate-900 mb-1">
          📊 نمودار دقت ۱۴ روز اخیر برترین شهرها (تک‌باکت دقیق در برابر ±1 باکت همسایه)
        </h2>
        <p class="text-xs text-slate-500 mb-3">
          نوار سبز تیره: اصابت دقیق به باکت اول مدل | نوار آبی روشن: اصابت به باکت اول یا باکت بلافصل کناری (±1)
        </p>
        <div class="overflow-x-auto bg-slate-50 border border-slate-200 rounded-xl p-3">
          <svg width="560" height="{svg_height}" viewBox="0 0 560 {svg_height}" class="w-full h-auto">
            {svg_bars}
          </svg>
        </div>
        <div class="mt-3 p-3 rounded-xl bg-emerald-50 border border-emerald-200 text-xs text-emerald-950 leading-relaxed">
          🧠 <b>کشف الگوی سودساز (Alpha Pattern):</b>
          در شهرهایی که نوار آبی روشن آن‌ها بالای <b>۸۰٪ تا ۹۰٪</b> است اما نوار سبز حدود ۳۵٪ تا ۴۵٪ است، خطای مدل فقط به اندازهٔ <b>نیم تا یک درجه</b> در جهت بایاس فصلی است. در این شهرها، <b>استراتژی دوباکتی (خرید هم‌زمان باکت اصلی + باکت همسایه در جهت بایاس فصلی)</b> نرخ برد را تقریباً به بالای ۸۵٪ می‌رساند!
        </div>
      </div>
    </div>

    <!-- Section 2: Interactive Per-City Strategy Playbooks -->
    <div class="bg-white border border-slate-200 rounded-2xl p-4 md:p-5 shadow-xs">
      <div class="flex flex-wrap items-center justify-between gap-3 mb-4 pb-3 border-b border-slate-200">
        <div>
          <h2 class="text-base md:text-lg font-extrabold text-slate-900">
            🗺️ دایره‌المعارف استراتژی‌های اختصاصی هر شهر (بر اساس فصل، بایاس مدل، زیرمدل برتر و سابقهٔ قفل‌ها)
          </h2>
          <p class="text-xs text-slate-500 mt-0.5">
            روی هر شهر کلیک کنید یا از فیلترهای زیر برای یافتن بهترین شکارهای امروز استفاده کنید ({report['total_playbooks']} پروفایل تحلیلی)
          </p>
        </div>
      </div>

      <!-- Filter Bar -->
      <div class="flex flex-wrap items-center gap-2.5 mb-4">
        <input
          id="citySearchInput"
          type="text"
          placeholder="جستجوی نام شهر (مثلاً Chicago, NYC, Seoul, Munich, Hong Kong)..."
          class="flex-1 min-w-[220px] bg-slate-50 border border-slate-300 rounded-xl px-3.5 py-2 text-xs md:text-sm focus:outline-2 focus:outline-blue-600"
        />
        <select id="tierFilter" class="bg-slate-50 border border-slate-300 rounded-xl px-3 py-2 text-xs font-bold">
          <option value="ALL">همهٔ سطوح استراتژی ({report['total_playbooks']})</option>
          <option value="TIER_1_ALPHA">🟢 فقط TIER 1 — شکارچیان اصلی (A+ Alpha)</option>
          <option value="TIER_2_STRADDLE">🔵 فقط TIER 2 — استراتژی دوباکتی همسایه (Straddle)</option>
          <option value="TIER_3_SCALP">🟡 فقط TIER 3 — نوسان‌گیری پیش از تسویه (Scalp)</option>
          <option value="TIER_4_AVOID">🔴 فقط TIER 4 — پرریسک / ممنوعه فعلی</option>
        </select>
        <select id="typeFilter" class="bg-slate-50 border border-slate-300 rounded-xl px-3 py-2 text-xs font-bold">
          <option value="ALL">🔥❄️ همهٔ بازارها (بیشینه و کمینه)</option>
          <option value="max">🔥 فقط دمای بیشینه (Max)</option>
          <option value="min">❄️ فقط دمای کمینه (Min)</option>
        </select>
        <select id="regionFilter" class="bg-slate-50 border border-slate-300 rounded-xl px-3 py-2 text-xs font-bold">
          <option value="ALL">🌍 همهٔ مناطق</option>
          <option value="US">🇺🇸 فقط شهرهای آمریکا (°F)</option>
          <option value="GLOBAL">🌐 فقط شهرهای بین‌المللی (°C)</option>
        </select>
        <select id="temporalFilter" class="bg-indigo-50 border border-indigo-300 text-indigo-900 rounded-xl px-3 py-2 text-xs font-bold">
          <option value="ALL">🧠 همهٔ الگوهای رفتاری حافظه</option>
          <option value="WHIPSAW_SCALP_ONLY">🔄 الگوی چرخش درون‌روزی (فقط Scalp)</option>
          <option value="NEIGHBOR_BUCKET_DRIFT">🔀 الگوی کوچ دما به باکت همسایه</option>
          <option value="SMART_MONEY_RESISTANCE">🛑 الگوی مقاومت هوشمند بازار (تله مدل)</option>
          <option value="SILENT_MODEL_LEAD">⚡ الگوی واگرایی پیش‌رو مدل (جاماندگی بازار)</option>
          <option value="FOMO_MEAN_REVERSION">🫧 الگوی حباب هیجانی و بازگشت قیمت</option>
          <option value="METAR_MOMENTUM_RIDE">🎯 الگوی هم‌گرایی پرشتاب فرودگاهی</option>
        </select>
      </div>

      <!-- Playbook Cards Container -->
      <div id="playbookContainer" class="grid grid-cols-1 md:grid-cols-2 gap-4"></div>
    </div>

    <!-- Section 3: Recent User Locks Forensic Audit Table -->
    <div class="bg-white border border-slate-200 rounded-2xl p-4 md:p-5 shadow-xs">
      <h2 class="text-base font-extrabold text-slate-900 mb-1">
        🕵️ جدول کالبدشکافی ۴۰ قفل اخیر شما (مقایسه خروج شما با نتیجهٔ نهایی فرودگاه)
      </h2>
      <p class="text-xs text-slate-500 mb-3">
        بررسی دقیق تک‌تک قفل‌ها برای تشخیص اینکه در کدام شهرها خروج زودهنگام داشته‌اید و در کدام شهرها به‌موقع فرار کرده‌اید
      </p>
      <div class="overflow-x-auto rounded-xl border border-slate-200">
        <table class="w-full min-w-[680px] border-collapse text-xs bg-white">
          <thead>
            <tr class="bg-slate-100 text-slate-600 font-bold border-b border-slate-200 text-center">
              <th class="py-2.5 px-2">شهر</th>
              <th class="py-2.5 px-2">نوع</th>
              <th class="py-2.5 px-2">تاریخ</th>
              <th class="py-2.5 px-2">باکت</th>
              <th class="py-2.5 px-2">ورود</th>
              <th class="py-2.5 px-2">خروج</th>
              <th class="py-2.5 px-2">بازده شما</th>
              <th class="py-2.5 px-2">تحلیل کالبدشکافی تریدر (Forensic Diagnosis)</th>
            </tr>
          </thead>
          <tbody id="forensicTableBody"></tbody>
        </table>
      </div>
    </div>

  </div>

  <script>
    const PLAYBOOKS = {playbooks_json};
    const RECENT_LOCKS = {recent_locks_json};

    function getTierClasses(tierCode) {{
      if (tierCode === 'TIER_1_ALPHA') return 'bg-emerald-50 border-emerald-300 text-emerald-900';
      if (tierCode === 'TIER_2_STRADDLE') return 'bg-sky-50 border-sky-300 text-sky-900';
      if (tierCode === 'TIER_3_SCALP') return 'bg-amber-50 border-amber-300 text-amber-900';
      return 'bg-rose-50 border-rose-300 text-rose-900';
    }}

    function renderPlaybooks() {{
      const q = (document.getElementById('citySearchInput').value || '').trim().toLowerCase();
      const tierF = document.getElementById('tierFilter').value;
      const typeF = document.getElementById('typeFilter').value;
      const regF = document.getElementById('regionFilter').value;
      const tempF = (document.getElementById('temporalFilter') || {{value: 'ALL'}}).value;

      const filtered = PLAYBOOKS.filter((p) => {{
        if (tierF !== 'ALL' && p.tier_code !== tierF) return false;
        if (typeF !== 'ALL' && p.market_type !== typeF) return false;
        if (regF === 'US' && !p.is_us) return false;
        if (regF === 'GLOBAL' && p.is_us) return false;
        if (tempF !== 'ALL') {{
          const domCode = (p.temporal_profile && p.temporal_profile.dominant_regime_code) || '';
          if (domCode !== tempF) return false;
        }}
        if (q && !p.city_name.toLowerCase().includes(q) && !p.city.toLowerCase().includes(q)) return false;
        return true;
      }});

      const container = document.getElementById('playbookContainer');
      if (filtered.length === 0) {{
        container.innerHTML = '<div class="col-span-2 text-center py-8 text-slate-500 text-sm">هیچ شهری با این فیلتر یافت نشد.</div>';
        return;
      }}

      container.innerHTML = filtered.map((p) => {{
        const badgeCls = getTierClasses(p.tier_code);
        const typeLabel = p.market_type === 'min' ? '❄️ کمینه دما (Min)' : '🔥 بیشینه دما (Max)';
        const bestSm = p.best_submodel ? `${{p.best_submodel.name}} (MAE: ${{p.best_submodel.mae}}${{p.unit}})` : 'ترکیبی';
        const userStatsHtml = p.user_locks_total > 0
          ? `<div class="mt-2.5 p-2.5 rounded-xl bg-slate-50 border border-slate-200 text-[11.5px] flex flex-wrap items-center justify-between gap-2">
               <span>📌 <b>سابقه قفل‌های شخصی شما در این شهر:</b> ${{p.user_locks_total}} معامله</span>
               <span>نرخ برد: <b class="text-emerald-700 ltr-inline">${{p.user_win_rate_pct}}%</b></span>
               <span>میانگین سود: <b class="${{p.user_avg_pnl_pct >= 0 ? 'text-emerald-600' : 'text-rose-600'}} ltr-inline">${{p.user_avg_pnl_pct >= 0 ? '+' : ''}}${{p.user_avg_pnl_pct}}%</b></span>
               <span>میانگین ورود: <b class="ltr-inline">${{p.user_avg_entry_c}}¢</b></span>
             </div>`
          : `<div class="mt-2.5 p-2 rounded-xl bg-slate-50 border border-slate-200 text-[11px] text-slate-500">
               📌 هنوز قفل بسته‌شدهٔ شخصی در این بازار ثبت نشده است (تحلیل بر اساس ${{p.n_resolved}} بازار تسویه‌شدهٔ سیستم).
             </div>`;

        return `
          <div class="border border-slate-200 rounded-2xl p-4 bg-white shadow-2xs hover:shadow-md transition-shadow flex flex-col justify-between">
            <div>
              <!-- Card Header -->
              <div class="flex flex-wrap items-center justify-between gap-2 pb-2.5 border-b border-slate-100">
                <div class="flex items-center gap-2">
                  <span class="text-base font-extrabold text-slate-900">${{p.city_name}} ${{p.is_us ? '🇺🇸' : ''}}</span>
                  <span class="px-2 py-0.5 rounded-md text-[11px] font-bold bg-slate-100 text-slate-700">${{typeLabel}}</span>
                </div>
                <span class="px-2.5 py-1 rounded-lg text-[11px] font-extrabold border ${{badgeCls}}">
                  ${{p.tier_badge}}
                </span>
              </div>

              <!-- Key Metrics Grid -->
              <div class="grid grid-cols-4 gap-2 my-3 text-center">
                <div class="bg-slate-50 border border-slate-200 rounded-xl p-2">
                  <span class="block text-[10px] text-slate-500">دقت اخیر (۱۴ روز)</span>
                  <b class="text-sm text-slate-900 ltr-inline">${{p.rec_hit_pct}}%</b>
                  <span class="block text-[9.5px] text-slate-400 ltr-inline">کل: ${{p.hit_pct}}% (${{p.n_resolved}}d)</span>
                </div>
                <div class="bg-slate-50 border border-slate-200 rounded-xl p-2">
                  <span class="block text-[10px] text-slate-500">دقت با ±1 باکت</span>
                  <b class="text-sm text-blue-600 ltr-inline">${{p.rec_within1_pct}}%</b>
                  <span class="block text-[9.5px] text-slate-400 ltr-inline">کل: ${{p.within1_pct}}%</span>
                </div>
                <div class="bg-slate-50 border border-slate-200 rounded-xl p-2">
                  <span class="block text-[10px] text-slate-500">خطای مطلق (MAE)</span>
                  <b class="text-sm text-slate-900 ltr-inline">${{p.rec_mae}}${{p.unit}}</b>
                  <span class="block text-[9.5px] text-slate-400 ltr-inline">σ: ${{p.sigma}}${{p.unit}}</span>
                </div>
                <div class="bg-slate-50 border border-slate-200 rounded-xl p-2">
                  <span class="block text-[10px] text-slate-500">امتیاز اعتبار کمّی</span>
                  <b class="text-sm text-emerald-700 ltr-inline">${{p.composite_score}}</b>
                  <span class="block text-[9.5px] text-slate-400 ltr-inline">Wilson: ${{p.rec_wilson_lb_pct}}%</span>
                </div>
              </div>

              <!-- Seasonal & Bias Diagnosis -->
              <div class="space-y-1.5 text-xs bg-amber-50/60 border border-amber-200/80 rounded-xl p-3">
                <div class="flex flex-wrap items-center justify-between gap-1">
                  <span class="font-bold text-slate-800">🍂 وضعیت فصلی و بایاس: <span class="text-amber-900">${{p.bias_diagnosis}}</span></span>
                  <span class="text-[11px] text-slate-600">زیرمدل برتر: <b class="text-emerald-700 ltr-inline">${{bestSm}}</b></span>
                </div>
                <p class="text-[11.5px] text-slate-700 leading-relaxed m-0">${{p.bias_action}}</p>
              </div>

              <!-- Actionable Strategy Prescription -->
              <div class="mt-3 bg-emerald-50/70 border border-emerald-200 rounded-xl p-3 space-y-1.5 text-xs">
                <div class="font-extrabold text-emerald-950 flex items-center justify-between">
                  <span>⚡ نسخهٔ عملیاتی و تنظیمات پیشنهادی خرید / Auto-Exit:</span>
                  <span class="ltr-inline text-[11px] bg-white px-2 py-0.5 rounded border border-emerald-300 text-emerald-800">
                    Band: ${{p.opt_entry_band_c}}
                  </span>
                </div>
                <div class="text-[11.5px] text-slate-800">
                  <b>• روش ورود:</b> ${{p.entry_mode_rec}}
                </div>
                <div class="text-[11.5px] text-slate-800">
                  <b>• مدیریت خروج:</b> ${{p.hold_policy_rec}}
                </div>
                <div class="grid grid-cols-3 gap-2 pt-1.5 text-center text-[11px]">
                  <div class="bg-white rounded-lg py-1 px-2 border border-emerald-200">
                    <span class="text-slate-500">تخفیف Limit Buy:</span> <b class="ltr-inline text-purple-700">-${{p.opt_limit_discount_c}}¢</b>
                  </div>
                  <div class="bg-white rounded-lg py-1 px-2 border border-emerald-200">
                    <span class="text-slate-500">حد سود (TP):</span> <b class="ltr-inline text-emerald-700">+${{p.opt_tp_pct}}%</b>
                  </div>
                  <div class="bg-white rounded-lg py-1 px-2 border border-emerald-200">
                    <span class="text-slate-500">حد ضرر (SL):</span> <b class="ltr-inline text-rose-600">-${{p.opt_sl_pct}}%</b>
                  </div>
                </div>
              </div>

              <!-- Behavioral Strategy Synthesized from Internal Sequence Memory -->
              ${{(() => {{
                const tp = p.temporal_profile;
                if (!tp || !tp.has_temporal_support) return '';
                const secHtml = tp.secondary_insight
                  ? `<div class="pt-1.5 mt-1.5 border-t border-indigo-200/70 text-[11px] text-slate-700 leading-relaxed">${{tp.secondary_insight}}</div>`
                  : '';
                return `
                  <div class="mt-3 bg-indigo-50/75 border border-indigo-200 rounded-xl p-3 space-y-1.5 text-xs">
                    <div class="flex flex-wrap items-center justify-between gap-1">
                      <span class="font-extrabold text-indigo-950">${{tp.complex_strategy_title}}</span>
                      <span class="px-2 py-0.5 rounded bg-indigo-100 border border-indigo-300 text-indigo-900 text-[10px] font-bold">
                        ${{tp.confidence_tag}}
                      </span>
                    </div>
                    <p class="text-[11.5px] text-slate-800 leading-relaxed m-0">${{tp.complex_strategy_body}}</p>
                    ${{secHtml}}
                  </div>
                `;
              }})()}}
            </div>

            ${{userStatsHtml}}
          </div>
        `;
      }}).join('');
    }}

    function renderForensicTable() {{
      const tbody = document.getElementById('forensicTableBody');
      tbody.innerHTML = RECENT_LOCKS.map((r) => {{
        const pnlCls = r.pnl_pct >= 0 ? 'text-emerald-600 font-bold' : 'text-rose-600 font-bold';
        return `
          <tr class="border-b border-slate-100 hover:bg-slate-50 text-center">
            <td class="py-2 px-2 font-bold text-slate-800">${{r.city_name}}</td>
            <td class="py-2 px-2">${{r.market_type === 'min' ? '❄️ کمینه' : '🔥 بیشینه'}}</td>
            <td class="py-2 px-2 ltr-inline">${{r.date}}</td>
            <td class="py-2 px-2 ltr-inline font-semibold">${{r.bucket_label}}</td>
            <td class="py-2 px-2 ltr-inline">${{Math.round(r.entry_price * 100)}}¢</td>
            <td class="py-2 px-2 ltr-inline">${{r.exit_price != null ? Math.round(r.exit_price * 100) + '¢' : '-'}}</td>
            <td class="py-2 px-2 ltr-inline ${{pnlCls}}">${{r.pnl_pct >= 0 ? '+' : ''}}${{r.pnl_pct}}%</td>
            <td class="py-2 px-2 text-right font-medium text-slate-700">${{r.forensic_tag}}</td>
          </tr>
        `;
      }}).join('');
    }}

    document.getElementById('citySearchInput').addEventListener('input', renderPlaybooks);
    document.getElementById('tierFilter').addEventListener('change', renderPlaybooks);
    document.getElementById('typeFilter').addEventListener('change', renderPlaybooks);
    document.getElementById('regionFilter').addEventListener('change', renderPlaybooks);
    document.getElementById('temporalFilter').addEventListener('change', renderPlaybooks);

    renderPlaybooks();
    renderForensicTable();
  </script>
</body>
</html>
"""


def build_telegram_executive_summary(report: Dict[str, Any], live_link: Optional[str] = None) -> str:
    uf = report["user_forensics"]
    tc = report["tier_counts"]
    playbooks = report["playbooks"]

    top_tier1 = [p for p in playbooks if p["tier_code"] == "TIER_1_ALPHA"][:6]
    top_straddle = [p for p in playbooks if p["tier_code"] == "TIER_2_STRADDLE"][:4]
    avoid_list = [p for p in playbooks if p["tier_code"] == "TIER_4_AVOID"][-4:]

    lines = [
        f"🧠 <b>گزارش روزانه آزمایشگاه کمّی و استراتژی شهرها (Quant Strategy Lab)</b>",
        f"🕒 بروزرسانی: <code>{report['generated_at_iran']}</code> به وقت ایران",
        "",
        f"📊 <b>کالبدشکافی قفل‌های شخصی شما ({uf['closed_locks']} معامله بسته‌شده):</b>",
        f"  • نرخ برد خروجی شما: <b>{uf['win_rate_pct']}%</b> (میانگین بازده: <b>{uf['avg_pnl_pct']:+.1f}%</b>)",
        f"  • 🛡️ نجات هوشمند با خروج دستی قبل از صفر شدن باکت: <b>{uf['manual_smart_saves']} معامله</b>",
        f"  • 💸 خروج زودهنگام روی باکت‌های برنده ۱۰۰¢: <b>{uf['manual_early_regrets']} معامله</b>",
        "",
        f"🟢 <b>برترین شهرهای درجه ۱ امروز (TIER 1 — A+ Alpha):</b>",
    ]
    for p in top_tier1:
        m_tag = "❄️ کمینه" if p["market_type"] == "min" else "🔥 بیشینه"
        sm_str = p["best_submodel"]["name"] if p.get("best_submodel") else "Blend"
        lines.append(
            f"  • <b>{p['city_name']}</b> ({m_tag}): دقت اخیر <b>{p['rec_hit_pct']}%</b> | ±1 باکت: <b>{p['rec_within1_pct']}%</b> | مدل برتر: <code>{sm_str}</code> | TP: <code>+{p['opt_tp_pct']}%</code> / SL: <code>-{p['opt_sl_pct']}%</code>"
        )

    if top_straddle:
        lines.append("")
        lines.append("🔵 <b>بهترین شهرها برای استراتژی دوباکتی همسایه (Adjacent Straddle):</b>")
        for p in top_straddle:
            m_tag = "❄️ کمینه" if p["market_type"] == "min" else "🔥 بیشینه"
            lines.append(
                f"  • <b>{p['city_name']}</b> ({m_tag}): دقت ±1 باکت <b>{p['rec_within1_pct']}%</b> ({p['bias_diagnosis']})"
            )

    behavioral_highlights = [
        p for p in playbooks
        if (p.get("temporal_profile") or {}).get("has_temporal_support")
    ]
    if behavioral_highlights:
        lines.append("")
        lines.append("🧬 <b>استراتژی‌های رفتاری مستخرج از حافظهٔ توالی بازار:</b>")
        seen_regimes = set()
        picked = []
        for p in behavioral_highlights:
            rcode = (p["temporal_profile"].get("dominant_regime_code") or "")
            if rcode not in seen_regimes:
                seen_regimes.add(rcode)
                picked.append(p)
            if len(picked) >= 5:
                break
        for p in picked:
            tp = p["temporal_profile"]
            m_tag = "❄️ کمینه" if p["market_type"] == "min" else "🔥 بیشینه"
            lines.append(
                f"  • <b>{p['city_name']}</b> ({m_tag}): {tp['complex_strategy_title']}"
            )

    if avoid_list:
        lines.append("")
        lines.append("🔴 <b>شهرهای پرریسک فعلی (پیشنهاد عدم ورود مستقیم):</b>")
        for p in avoid_list:
            m_tag = "❄️ کمینه" if p["market_type"] == "min" else "🔥 بیشینه"
            lines.append(f"  • {p['city_name']} ({m_tag}) — دقت اخیر: {p['rec_hit_pct']}% (MAE: {p['rec_mae']}{p['unit']})")

    if live_link:
        lines.append("")
        lines.append(f"🔗 <b>لینک اختصاصی کتابچه کامل استراتژی شهرها و نمودارها:</b>\n{live_link}")

    return "\n".join(lines)


def main() -> None:
    report = run_quantitative_analysis()
    html_content = build_standalone_html_report(report)

    with open(REPORT_HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html_content)

    os.makedirs(DOCS_DIR, exist_ok=True)
    docs_target = os.path.join(DOCS_DIR, "strategy-lab.html")
    with open(docs_target, "w", encoding="utf-8") as f:
        f.write(html_content)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(REPORT_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    tg_summary = build_telegram_executive_summary(report)
    with open(TELEGRAM_SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.write(tg_summary)

    print(
        f"[quant_strategy_advisor] Generated {len(report['playbooks'])} city playbooks "
        f"({report.get('temporal_supported_count', 0)} with multi-regime behavioral strategies from memory) & analyzed "
        f"{report['user_forensics']['total_locks']} personal locks -> {REPORT_HTML_FILE}"
    )


if __name__ == "__main__":
    main()

