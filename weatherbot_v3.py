#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weatherbot_v3.py -- Main orchestrator for WeatherBet v3
=====================================================================================
V5 CHANGES (per explicit user directives): global shared-capital-pool removed
(each city/market sized independently); STRATEGY_VERSION stamping added.

--- WEATHERBET INTERACTIVE-LOCK FEATURE ---------
get_clob_book_bid() lives in clob_utils.py, shared with price_monitor.py.

--- PHASE 3 FIX LOG (WEATHERBOT_ROADMAP.md, یافته F3) -------------------------
`_local_dates_for_city(now, loc, count)` converts shared UTC `now` into each
city's own local calendar via its `tz` field before building the date list.

--- ACCURACY REPORT AUTO-EXPORT ----------------------------------------------
run_once() calls export_accuracy_report.build_accuracy_report() after the
dashboard is built.

--- روادراه: فاز ۳ -- اسکن سبک (heartbeat) + فاز ۳-الف (price_refresh) -------
discover_new_signals/refresh_open_market_info/refresh_all_open_market_prices/
run_lite_scan/run_price_refresh -- طبق طراحی قبلی، بدون تغییر رفتار.

=====================================================================================
نقشه‌راه بهبود (۱۶ سپتامبر ۲۰۲۶) -- خلاصهٔ افزودنی‌های این نسخه
=====================================================================================
فاز ۳+۴: در discover_new_signals و refresh_open_market_info، فراخوانی
fc.build_combined_distribution قدیمی با fc.build_combined_distribution_full
جایگزین شد (همان دو فراخوانی ECMWF+GFS، بدون افزایش API؛ فقط علاوه بر
members ترکیبی قدیمی -- که forecast_mean/sigma زنده از آن دقیقاً همان
مقدار قبلی را می‌گیرد -- دیکشنری اعضای هر مدل هم برمی‌گرداند). سه فیلد
جدید (forecast_mean_shadow, model_means_raw) در هر مارکت ذخیره می‌شود --
فقط برای تحلیل آینده، بدون اثر روی سیگنال زنده. fc.update_model_skill_
from_live در scan_and_update/run_lite_scan اضافه شد.

فاز ۶ (بازار دمای کمینه، کاملاً مستقل): market_path_min/load_market_min/
save_market_min/load_all_min_markets، get_polymarket_event_min،
discover_new_min_signals (فقط در اسکن سنگین -- طبق تصمیم صریح کاربر که
کشف سیگنال جدید باید فقط در اسکن سنگین باشد، نه سبک)، و
resolve_expired_min_markets (در هر دو مسیر سنگین و سبک، تا هیچ بازاری دیر
resolve نشود) به scan_and_update/run_lite_scan متصل شدند.
=====================================================================================
Usage:
python weatherbot_v3.py backfill      # one-time: calibrate sigma+bias from history
python weatherbot_v3.py serve         # runs the scan loop AND serves dashboard.html
python weatherbot_v3.py once          # single scan cycle, then exit (no server)
python weatherbot_v3.py lite_scan     # light refresh: prices/forecasts + resolve, NO sizing
python weatherbot_v3.py price_refresh # lightest refresh: ONLY market prices, no weather calls
python weatherbot_v3.py run           # main loop, no server
=====================================================================================
"""

import os
import re
import sys
import json
import time
import hashlib
import threading
import functools
import http.server
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from locations import LOCATIONS, MONTHS
from market_time import local_day_status
import forecasting as fc
import strategy as strat
import resolution as res
from clob_utils import (
    get_clob_book_bid, get_gamma_event_prices, get_gamma_event_prices_min,
    get_gamma_event_quotes, get_gamma_event_quotes_min,
)
import export_accuracy_report as accuracy_report

# =============================================================================
# CONFIG
# =============================================================================

with open("config.json", encoding="utf-8") as f:
    CFG = json.load(f)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
MARKETS_DIR = DATA_DIR / "markets"
MARKETS_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
ENTRIES_FILE = DATA_DIR / "my_entries.txt"
LOCK_REQUESTS_FILE = DATA_DIR / "lock_requests.txt"
LOCK_PROCESSED_FILE = DATA_DIR / "lock_requests_processed.json"

def _compute_strategy_version():
    """Derive market-refresh version automatically from strategy.py content."""
    try:
        strategy_path = Path(__file__).with_name("strategy.py")
        return "auto-" + hashlib.sha256(strategy_path.read_bytes()).hexdigest()[:16]
    except Exception:
        return "fallback-v10"

STRATEGY_VERSION = _compute_strategy_version()

MAX_RETRIES = 3
RETRY_DELAY_S = 3
SERVE_PORT = 8765
EVENT_FETCH_WORKERS = CFG.get("event_fetch_workers", 8)

ENTRIES_FILE_HEADER = (
    "# ============================================================================\n"
    "# my_entries.txt -- WeatherBet v3 manual entry log\n"
    "# ============================================================================\n"
    "#\n"
    "# Every time the bot locks in a new signal, it adds a line like:\n"
    "# nyc_2026-07-10_1=no\n"
    "# If you ACTUALLY placed that trade for real, change \"no\" to \"yes\".\n"
    "# ============================================================================\n\n"
)

LOCK_REQUESTS_HEADER = (
    "# ============================================================================\n"
    "# lock_requests.txt -- WeatherBet v3 manual COMMIT log\n"
    "# ============================================================================\n"
    "# Written automatically by the dashboard's \"Lock this moment\" button.\n"
    "# ============================================================================\n\n"
)

STRATEGY_PARAMS = {
    "balance_units": CFG.get("balance_units", 100),
    "main_signal_min_units": CFG.get("main_signal_min_units", 5),
    "ladder_max_buckets": CFG.get("ladder_max_buckets", 3),
    "kelly_fraction": CFG.get("kelly_fraction", 0.25),
    "hedge_pool_size": CFG.get("hedge_pool_size", 12),
    "tail_df": CFG.get("tail_df", 5.0),
    "scenario_step": CFG.get("scenario_step", 0.5),
    "scenario_n_sigma": CFG.get("scenario_n_sigma", 4.0),
    "hedge_min_plausibility": CFG.get("hedge_min_plausibility", 0.08),
    "worst_case_prob_mass": CFG.get("worst_case_prob_mass", 0.90),
    "worst_case_max_loss_frac": CFG.get("worst_case_max_loss_frac", 0.10),
    "scenario_market_weight": CFG.get("scenario_market_weight", 0.5),
    "min_distinction_ratio": CFG.get("min_distinction_ratio", 1.5),
    "main_signal_max_units": CFG.get("main_signal_max_units", 0.30),
    "belief_prob_full_confidence": CFG.get("belief_prob_full_confidence", 0.4),
}

VC_KEY = CFG.get("vc_key", "")
MIN_VOLUME = CFG.get("min_volume", 500)
MIN_HOURS = CFG.get("min_hours", 2.0)
MAX_HOURS = CFG.get("max_hours", 72.0)
SCAN_INTERVAL = CFG.get("scan_interval", 3600)

# =============================================================================
# STATE / MARKET PERSISTENCE
# =============================================================================

def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.setdefault("my_balance_units", STRATEGY_PARAMS["balance_units"])
        state.setdefault("my_total_pnl_units", 0.0)
        state.setdefault("my_wins", 0)
        state.setdefault("my_losses", 0)
        return state
    return {
        "balance_units": STRATEGY_PARAMS["balance_units"], "total_pnl_units": 0.0,
        "wins": 0, "losses": 0, "my_balance_units": STRATEGY_PARAMS["balance_units"],
        "my_total_pnl_units": 0.0, "my_wins": 0, "my_losses": 0,
    }

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

def market_path(city_slug, date_str):
    return MARKETS_DIR / f"{city_slug}_{date_str}.json"

def load_market(city_slug, date_str):
    p = market_path(city_slug, date_str)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None

def save_market(m):
    market_path(m["city"], m["date"]).write_text(
        json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8"
    )

def load_all_markets():
    out = []
    for f in MARKETS_DIR.glob("*.json"):
        if f.name.endswith("_min.json"):
            continue
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out

def market_key(city_slug, date_str):
    return f"{city_slug}_{date_str}"

# --- فاز ۶: پایداری فایل بازار دمای کمینه، کاملاً مستقل -----------------------

def market_path_min(city_slug, date_str):
    return MARKETS_DIR / f"{city_slug}_{date_str}_min.json"

def load_market_min(city_slug, date_str):
    p = market_path_min(city_slug, date_str)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None

def save_market_min(m):
    market_path_min(m["city"], m["date"]).write_text(
        json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8"
    )

def load_all_min_markets():
    out = []
    for f in MARKETS_DIR.glob("*_min.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out

# =============================================================================
# MANUAL ENTRY LOG
# =============================================================================

def load_entries():
    if not ENTRIES_FILE.exists():
        ENTRIES_FILE.write_text(ENTRIES_FILE_HEADER, encoding="utf-8")
        return {}
    entries = {}
    for line in ENTRIES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        entries[key.strip()] = val.strip().lower() == "yes"
    return entries

def append_new_entry_lines(new_keys):
    if not new_keys:
        return
    if not ENTRIES_FILE.exists():
        ENTRIES_FILE.write_text(ENTRIES_FILE_HEADER, encoding="utf-8")
    existing = ENTRIES_FILE.read_text(encoding="utf-8")
    lines_to_add = [f"{k}=no" for k in new_keys if f"{k}=" not in existing]
    if lines_to_add:
        with ENTRIES_FILE.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines_to_add) + "\n")

# =============================================================================
# MANUAL LOCK/COMMIT LOG
# =============================================================================

def load_lock_requests():
    if not LOCK_REQUESTS_FILE.exists():
        LOCK_REQUESTS_FILE.write_text(LOCK_REQUESTS_HEADER, encoding="utf-8")
        return {}
    requests_map = {}
    for line in LOCK_REQUESTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, ts = line.split("=", 1)
        requests_map[key.strip()] = ts.strip()
    return requests_map

def load_processed_lock_requests():
    if LOCK_PROCESSED_FILE.exists():
        try:
            return set(json.loads(LOCK_PROCESSED_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()

def save_processed_lock_requests(processed_set):
    LOCK_PROCESSED_FILE.write_text(
        json.dumps(sorted(processed_set), ensure_ascii=False), encoding="utf-8"
    )

def apply_lock_requests(now):
    requests_map = load_lock_requests()
    if not requests_map:
        return 0
    processed = load_processed_lock_requests()
    new_keys = [k for k in requests_map if k not in processed]
    if not new_keys:
        return 0
    committed_count = 0
    for key in new_keys:
        try:
            city_slug, date_str = key.rsplit("_", 1)
        except ValueError:
            processed.add(key)
            continue
        mkt = load_market(city_slug, date_str)
        if not mkt or mkt.get("status") == "resolved":
            processed.add(key)
            continue
        live_alloc = mkt.get("live_allocation")
        if not live_alloc:
            processed.add(key)
            continue
        committed = []
        for idx, a in enumerate(live_alloc, start=1):
            a2 = dict(a)
            a2["idx"] = idx
            a2["entry_key"] = f"{city_slug}_{date_str}_{idx}"
            committed.append(a2)
        mkt["committed_allocation"] = committed
        mkt["committed_at"] = now.isoformat()
        mkt["committed_full_distribution"] = mkt.get("full_distribution")
        mkt["committed_worst_case_loss_frac"] = mkt.get("worst_case_loss_frac")
        mkt["committed_best_case_pnl_units"] = mkt.get("best_case_pnl_units")
        mkt["committed_worst_case_pnl_units"] = mkt.get("worst_case_pnl_units")
        mkt["committed_success_probability"] = mkt.get("success_probability")
        mkt["committed_confidence"] = mkt.get("confidence")
        mkt["committed_balance_units"] = mkt.get("balance_units", STRATEGY_PARAMS["balance_units"])
        save_market(mkt)
        append_new_entry_lines([a["entry_key"] for a in committed])
        processed.add(key)
        committed_count += 1
        print(f"  [COMMITTED @ live snapshot] {mkt['city_name']} {date_str} x{len(committed)} positions")
    save_processed_lock_requests(processed)
    return committed_count

# =============================================================================
# POLYMARKET HELPERS
# =============================================================================

def get_polymarket_event(city_slug, month, day, year):
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=(5, 8))
            data = r.json()
            if data and isinstance(data, list) and len(data) > 0:
                return data[0]
            return None
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)
    return None

def get_polymarket_event_for_market_date(city_slug, date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return get_polymarket_event(city_slug, MONTHS[dt.month - 1], dt.day, dt.year)

def get_polymarket_event_min(city_slug, month, day, year):
    """(فاز ۶) معادل get_polymarket_event ولی با اسلاگ lowest- به‌جای
    highest- (تأییدشده از URL واقعی بازارهای کمینهٔ زندهٔ Polymarket)."""
    slug = f"lowest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=(5, 8))
            data = r.json()
            if data and isinstance(data, list) and len(data) > 0:
                return data[0]
            return None
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)
    return None

def get_polymarket_event_for_market_date_min(city_slug, date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return get_polymarket_event_min(city_slug, MONTHS[dt.month - 1], dt.day, dt.year)

# NOTE: get_clob_book_bid() lives in clob_utils.py so that price_monitor.py
# can reuse the exact same implementation instead of duplicating it.

def parse_temp_range(question):
    if not question:
        return None
    q = question.replace("\u2013", "-").replace("\u2014", "-")
    num = r"(-?\d+(?:\.\d+)?)"
    if re.search(r"or below|or less|below " + num, q, re.IGNORECASE):
        m = re.search(num + r"\s*[\u00b0]?\s*[FC]\s*(?:or below|or less)", q, re.IGNORECASE)
        if m:
            return (-999.0, float(m.group(1)))
        m = re.search(r"below\s*" + num + r"\s*[\u00b0]?\s*[FC]", q, re.IGNORECASE)
        if m:
            return (-999.0, float(m.group(1)))
    if re.search(r"or higher|or more|above " + num, q, re.IGNORECASE):
        m = re.search(num + r"\s*[\u00b0]?\s*[FC]\s*(?:or higher|or more)", q, re.IGNORECASE)
        if m:
            return (float(m.group(1)), 999.0)
        m = re.search(r"above\s*" + num + r"\s*[\u00b0]?\s*[FC]", q, re.IGNORECASE)
        if m:
            return (float(m.group(1)), 999.0)
    m = re.search(r"between\s*" + num + r"\s*-\s*" + num + r"\s*[\u00b0]?\s*[FC]", q, re.IGNORECASE)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(num + r"\s*(?:-|to)\s*" + num + r"\s*[\u00b0]?\s*[FC]", q, re.IGNORECASE)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r"be\s*" + num + r"\s*[\u00b0]?\s*[FC]\s*on", q, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v, v)
    m = re.search(num + r"\s*[\u00b0]\s*[FC]\b", q, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None

def hours_to_resolution(end_date_str):
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 3600)
    except Exception:
        return 999.0

def _refresh_local_day_timing(mkt, loc, now, event_end_date=None):
    """Persists countdown data derived from the city's local target day."""
    timing = local_day_status(mkt["date"], loc, now)
    mkt["hours_left"] = round(timing["remaining_seconds"] / 3600.0, 1)
    mkt["time_status"] = timing["kind"]
    mkt["local_day_end_utc"] = timing["local_day_end_utc"].isoformat()
    if event_end_date:
        mkt["event_end_date"] = event_end_date
    return timing

def fetch_outcomes(event):
    """Fast market extraction including Gamma orderbook bestBid/bestAsk (zero extra HTTP calls)."""
    outcomes = []
    for market in event.get("markets", []):
        question = market.get("question", "")
        mid = str(market.get("id", ""))
        volume = float(market.get("volume", 0))
        rng = parse_temp_range(question)
        if not rng:
            continue
        try:
            prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
            yes_price = float(prices[0])
            no_price = float(prices[1]) if len(prices) > 1 else round(1.0 - yes_price, 4)
        except Exception:
            continue

        try:
            token_ids = json.loads(market.get("clobTokenIds", "[]"))
            yes_token_id = token_ids[0] if len(token_ids) >= 1 else None
            no_token_id = token_ids[1] if len(token_ids) >= 2 else None
        except Exception:
            yes_token_id, no_token_id = None, None

        best_bid = None
        best_ask = None
        for k, attr in (("bestBid", "bid"), ("bestAsk", "ask")):
            raw_v = market.get(k)
            if raw_v is not None:
                try:
                    fv = float(raw_v)
                    if 0.0 <= fv <= 1.0:
                        if attr == "bid":
                            best_bid = round(fv, 4)
                        else:
                            best_ask = round(fv, 4)
                except Exception:
                    pass

        if best_bid is not None and best_ask is not None and best_ask >= best_bid:
            real_spread = round(best_ask - best_bid, 4)
        else:
            real_spread = round(abs((yes_price + no_price) - 1.0), 4)

        outcomes.append({
            "question": question, "market_id": mid, "range": rng,
            "yes_price": round(yes_price, 4),
            "no_price": round(no_price, 4),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_ask_spread": real_spread,
            "volume": round(volume, 0),
            "spread": real_spread,
            "yes_token_id": yes_token_id,
            "no_token_id": no_token_id,
        })
    outcomes.sort(key=lambda x: x["range"][0])
    return outcomes

def _fetch_sell_values_for_allocation(allocation, tradable_dist, pool=None):
    """Fetch bid prices only for actual allocation legs, in parallel."""
    by_market_id = {d["market_id"]: d for d in tradable_dist}
    jobs = []
    for index, leg in enumerate(allocation):
        bucket = by_market_id.get(leg["market_id"])
        if not bucket:
            continue
        token_id = bucket.get("yes_token_id") if leg["side"] == "YES" else bucket.get("no_token_id")
        if token_id:
            jobs.append((index, token_id))

    if not jobs:
        for leg in allocation:
            leg["sell_value"] = None
        return

    def collect(pool_obj):
        futures = {pool_obj.submit(get_clob_book_bid, token_id): index for index, token_id in jobs}
        values = {}
        for future in as_completed(futures):
            index = futures[future]
            try:
                values[index] = future.result()
            except Exception:
                values[index] = None
        return values

    if pool is None:
        with ThreadPoolExecutor(max_workers=EVENT_FETCH_WORKERS) as local_pool:
            values = collect(local_pool)
    else:
        values = collect(pool)

    for index, leg in enumerate(allocation):
        leg["sell_value"] = values.get(index)

# =============================================================================
# PHASE 1 -- DISCOVERY + LIVE REFRESH (حداکثر دما)
# V5: single-pass, no shared-pool competition.
# =============================================================================

_tz_warned = set()

def _local_dates_for_city(now, loc, count=4):
    """Returns `count` date strings (YYYY-MM-DD) starting from "today" in
    THIS CITY'S OWN local timezone (loc["tz"]), not global UTC."""
    if ZoneInfo is not None:
        try:
            local_now = now.astimezone(ZoneInfo(loc["tz"]))
            return [(local_now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(count)]
        except Exception:
            if loc["tz"] not in _tz_warned:
                print(f"  [TZ-WARN] could not resolve timezone {loc['tz']} for {loc['name']} "
                      f"(run: pip install tzdata) -- falling back to UTC date for now")
                _tz_warned.add(loc["tz"])
    return [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(count)]

def _attach_distribution_changes(old_dist, new_dist, update_model=True, update_price=True, scan_mode="6h"):
    """محاسبه و حفظ دقیق تغییرات احتمال مدل، احتمال بازار و قیمت YES در تمام
    اسکن‌ها (اسکن ۶ساعته، اسکن روزانه، و اسکن سبک ۱۰ دقیقه‌ای قیمت):
      - در هر اسکن ۶ ساعته (scan_mode="6h")، هم تغییرات نسبت به اسکن سبک ۱۰ دقیقه‌ای
        قبلی (yes_price_10m_change) و هم تغییرات نسبت به اسکن ۶ ساعتهٔ قبلی
        (model_prob_6h_change و yes_price_6h_change) به صورت مجزا و دقیق ثبت می‌شوند.
      - در اسکن‌های سبک ۱۰ دقیقه‌ای (scan_mode="10m")، مبنای اسکن ۶ ساعته حفظ شده و
        تغییرات ۱۰ دقیقه‌ای و ۶ ساعته هم‌زمان به‌روزرسانی می‌شوند."""
    old_by_id = {
        str(b.get("market_id")): b
        for b in (old_dist or [])
        if b.get("market_id") is not None
    }
    is_6h = (scan_mode == "6h") or update_model

    for c in (new_dist or []):
        mid = str(c.get("market_id", ""))
        old = old_by_id.get(mid)
        new_model = c.get("model_prob")
        new_price = c.get("yes_price")

        if old is None:
            if new_model is not None:
                m_val = round(float(new_model), 4)
                c["scan6h_model_prob"] = m_val
                c["prev_scan6h_model_prob"] = m_val
                c["prev_model_prob"] = m_val
                c["model_prob_6h_change"] = 0.0
                c["model_prob_change"] = 0.0
                c["model_prob_pct_change"] = 0.0
            if new_price is not None:
                p_val = round(float(new_price), 4)
                c["scan6h_yes_price"] = p_val
                c["prev_scan6h_yes_price"] = p_val
                c["prev_yes_price"] = p_val
                c["yes_price_10m_change"] = 0.0
                c["yes_price_10m_pct_change"] = 0.0
                c["yes_price_6h_change"] = 0.0
                c["yes_price_6h_pct_change"] = 0.0
                c["yes_price_change"] = 0.0
                c["yes_price_pct_change"] = 0.0
            continue

        # ۱) تغییرات احتمال مدل (model_prob) نسبت به اسکن ۶ ساعتهٔ قبلی
        ref_model_6h = old.get("scan6h_model_prob")
        if ref_model_6h is None:
            ref_model_6h = old.get("model_prob")

        if is_6h and update_model and ref_model_6h is not None and new_model is not None:
            diff_m = round(float(new_model) - float(ref_model_6h), 4)
            if abs(diff_m) >= 0.0005:
                prev_m = round(float(ref_model_6h), 4)
                pct_m = (
                    round((float(new_model) - float(ref_model_6h)) / float(ref_model_6h) * 100.0, 1)
                    if float(ref_model_6h) > 0.001 else None
                )
                c["prev_scan6h_model_prob"] = prev_m
                c["model_prob_6h_change"] = diff_m
                c["model_prob_6h_pct_change"] = pct_m
            else:
                c["prev_scan6h_model_prob"] = old.get("prev_scan6h_model_prob", old.get("prev_model_prob", round(float(ref_model_6h), 4)))
                c["model_prob_6h_change"] = old.get("model_prob_6h_change", old.get("model_prob_change", 0.0))
                c["model_prob_6h_pct_change"] = old.get("model_prob_6h_pct_change", old.get("model_prob_pct_change", 0.0))

            c["scan6h_model_prob"] = round(float(new_model), 4)
            c["prev_model_prob"] = c["prev_scan6h_model_prob"]
            c["model_prob_change"] = c["model_prob_6h_change"]
            c["model_prob_pct_change"] = c["model_prob_6h_pct_change"]
        else:
            for k in (
                "scan6h_model_prob", "prev_scan6h_model_prob",
                "model_prob_6h_change", "model_prob_6h_pct_change",
                "prev_model_prob", "model_prob_change", "model_prob_pct_change",
            ):
                if k in old:
                    c[k] = old[k]
            if "scan6h_model_prob" not in c and ref_model_6h is not None:
                c["scan6h_model_prob"] = round(float(ref_model_6h), 4)

        # ۲) تغییرات قیمت و احتمال بازار (yes_price) -- هم نسبت به اسکن سبک ۱۰ دقیقه‌ای قبلی و هم نسبت به اسکن ۶ ساعتهٔ قبلی
        old_price_10m = old.get("yes_price")
        ref_price_6h = old.get("scan6h_yes_price")
        if ref_price_6h is None:
            ref_price_6h = old.get("ask") if old.get("ask") is not None else old_price_10m

        # الف) تغییر نسبت به اسکن سبک ۱۰ دقیقه‌ای قبلی
        if update_price and old_price_10m is not None and new_price is not None:
            diff_10m = round(float(new_price) - float(old_price_10m), 4)
            if abs(diff_10m) >= 0.0005:
                c["prev_10m_yes_price"] = round(float(old_price_10m), 4)
                c["yes_price_10m_change"] = diff_10m
                c["yes_price_10m_pct_change"] = (
                    round(diff_10m / float(old_price_10m) * 100.0, 1)
                    if float(old_price_10m) > 0.001 else round(diff_10m * 100.0, 1)
                )
            else:
                c["prev_10m_yes_price"] = old.get("prev_10m_yes_price", round(float(old_price_10m), 4))
                c["yes_price_10m_change"] = old.get("yes_price_10m_change", 0.0)
                c["yes_price_10m_pct_change"] = old.get("yes_price_10m_pct_change", 0.0)

        # ب) تغییر نسبت به اسکن ۶ ساعتهٔ قبلی
        if is_6h and update_price and ref_price_6h is not None and new_price is not None:
            diff_p_6h = round(float(new_price) - float(ref_price_6h), 4)
            if abs(diff_p_6h) >= 0.0005:
                prev_p6 = round(float(ref_price_6h), 4)
                pct_p6 = (
                    round((float(new_price) - float(ref_price_6h)) / float(ref_price_6h) * 100.0, 1)
                    if float(ref_price_6h) > 0.001 else round(diff_p_6h * 100.0, 1)
                )
                c["prev_scan6h_yes_price"] = prev_p6
                c["yes_price_6h_change"] = diff_p_6h
                c["yes_price_6h_pct_change"] = pct_p6
            else:
                prev_p6 = old.get("prev_scan6h_yes_price", old.get("prev_yes_price"))
                if prev_p6 is not None and abs(float(new_price) - float(prev_p6)) >= 0.0005:
                    diff_prev = round(float(new_price) - float(prev_p6), 4)
                    pct_prev = (
                        round(diff_prev / float(prev_p6) * 100.0, 1)
                        if float(prev_p6) > 0.001 else round(diff_prev * 100.0, 1)
                    )
                    c["prev_scan6h_yes_price"] = round(float(prev_p6), 4)
                    c["yes_price_6h_change"] = diff_prev
                    c["yes_price_6h_pct_change"] = pct_prev
                else:
                    c["prev_scan6h_yes_price"] = round(float(ref_price_6h), 4)
                    c["yes_price_6h_change"] = old.get("yes_price_6h_change", old.get("yes_price_change", 0.0))
                    c["yes_price_6h_pct_change"] = old.get("yes_price_6h_pct_change", old.get("yes_price_pct_change", 0.0))

            c["scan6h_yes_price"] = round(float(new_price), 4)
            c["prev_yes_price"] = c["prev_scan6h_yes_price"]
            c["yes_price_change"] = c["yes_price_6h_change"]
            c["yes_price_pct_change"] = c["yes_price_6h_pct_change"]

        elif update_price and new_price is not None:
            # حالت اسکن سبک ۱۰ دقیقه‌ای (scan_mode="10m"):
            # مبنای ۶ ساعته (scan6h_yes_price) را حفظ می‌کنیم تا اسکن ۶ ساعتهٔ بعدی با آن مقایسه شود
            if ref_price_6h is not None:
                c["scan6h_yes_price"] = round(float(ref_price_6h), 4)
            if "prev_scan6h_yes_price" in old:
                c["prev_scan6h_yes_price"] = old["prev_scan6h_yes_price"]

            base_p = ref_price_6h if ref_price_6h is not None else old_price_10m
            if base_p is not None and abs(float(new_price) - float(base_p)) >= 0.0005:
                diff_p6 = round(float(new_price) - float(base_p), 4)
                pct_p6 = (
                    round(diff_p6 / float(base_p) * 100.0, 1)
                    if float(base_p) > 0.001 else round(diff_p6 * 100.0, 1)
                )
                c["yes_price_6h_change"] = diff_p6
                c["yes_price_6h_pct_change"] = pct_p6
            else:
                c["yes_price_6h_change"] = old.get("yes_price_6h_change", 0.0)
                c["yes_price_6h_pct_change"] = old.get("yes_price_6h_pct_change", 0.0)

            if abs(float(c.get("yes_price_10m_change") or 0.0)) >= 0.0005:
                c["prev_yes_price"] = c.get("prev_10m_yes_price", round(float(old_price_10m or new_price), 4))
                c["yes_price_change"] = c["yes_price_10m_change"]
                c["yes_price_pct_change"] = c["yes_price_10m_pct_change"]
            else:
                c["prev_yes_price"] = round(float(base_p or new_price), 4)
                c["yes_price_change"] = c.get("yes_price_6h_change", old.get("yes_price_change", 0.0))
                c["yes_price_pct_change"] = c.get("yes_price_6h_pct_change", old.get("yes_price_pct_change", 0.0))
        else:
            for k in (
                "scan6h_yes_price", "prev_scan6h_yes_price",
                "yes_price_6h_change", "yes_price_6h_pct_change",
                "prev_10m_yes_price", "yes_price_10m_change", "yes_price_10m_pct_change",
                "prev_yes_price", "yes_price_change", "yes_price_pct_change",
            ):
                if k in old:
                    c[k] = old[k]


def discover_new_signals(now, state):
    new_positions = 0

    fetch_jobs = []
    for city_slug in LOCATIONS:
        loc = LOCATIONS[city_slug]
        dates = _local_dates_for_city(now, loc, 4)
        for i, date in enumerate(dates):
            dt = datetime.strptime(date, "%Y-%m-%d")
            fetch_jobs.append((city_slug, date, i, dt))

    events_by_job = {}
    with ThreadPoolExecutor(max_workers=EVENT_FETCH_WORKERS) as pool:
        future_to_job = {
            pool.submit(get_polymarket_event, city_slug, MONTHS[dt.month - 1], dt.day, dt.year):
                (city_slug, date, i, dt)
            for (city_slug, date, i, dt) in fetch_jobs
        }
        for future in as_completed(future_to_job):
            job = future_to_job[future]
            try:
                events_by_job[job] = future.result()
            except Exception:
                events_by_job[job] = None

    for city_slug, loc in LOCATIONS.items():
        print(f"  -> {loc['name']}...", end=" ", flush=True)
        dates = _local_dates_for_city(now, loc, 4)

        for i, date in enumerate(dates):
            dt = datetime.strptime(date, "%Y-%m-%d")
            event = events_by_job.get((city_slug, date, i, dt))
            if not event:
                continue

            end_date = event.get("endDate", "")

            mkt = load_market(city_slug, date) or {
                "city": city_slug, "city_name": loc["name"], "date": date,
                "unit": loc["unit"], "status": "open",
                "created_at": now.isoformat(), "committed_allocation": None,
            }

            if mkt["status"] == "resolved":
                continue

            timing = _refresh_local_day_timing(mkt, loc, now, end_date)
            hours = timing["remaining_seconds"] / 3600.0
            mkt["last_scan"] = now.isoformat()

            needs_version_refresh = mkt.get("strategy_version") != STRATEGY_VERSION

            if (hours < MIN_HOURS or hours > MAX_HOURS) and not needs_version_refresh:
                save_market(mkt)
                continue

            outcomes = fetch_outcomes(event)
            if not outcomes:
                save_market(mkt)
                continue

            event_volume = float(event.get("volume", 0) or 0)
            if event_volume < MIN_VOLUME:
                mkt["skipped_reason"] = f"event volume {event_volume:.0f} < min_volume {MIN_VOLUME}"
                save_market(mkt)
                continue

            dist_precheck = [d for d in outcomes if d["volume"] > 0]
            if not dist_precheck:
                save_market(mkt)
                continue

            # (فاز ۳+۴ + BMA + GMM + METAR Intraday)
            members, model_members = fc.build_combined_distribution_full(city_slug, loc, date)
            mean, sigma = fc.build_calibrated_distribution(
                members, city_slug, i, loc["unit"], model_members=model_members
            )
            shadow_mean, model_means_raw = fc.build_shadow_mean(model_members, city_slug, i, loc["unit"])
            if fc.get_gate_status(city_slug, i) == "revert_to_shadow" and shadow_mean is not None:
                mean = round(float(shadow_mean), 2)

            if mean is None:
                mkt["skipped_reason"] = "ensemble forecast unavailable (mean is None)"
                save_market(mkt)
                continue

            observed_bound = fc.get_intraday_observed_bound(loc, date, is_min=False) if i == 0 else None
            model_weights = fc.get_model_weights(city_slug, list((model_means_raw or {}).keys()))
            model_spread = fc.compute_model_spread(model_means_raw, model_weights)

            dist = fc.full_bucket_distribution(
                mean, sigma, outcomes,
                model_means_raw=model_means_raw,
                city_slug=city_slug,
                horizon_days=i,
                unit=loc["unit"],
                is_min=False,
                observed_bound=observed_bound,
            )

            tradable_dist = [d for d in dist if d["volume"] > 0]
            if not tradable_dist:
                save_market(mkt)
                continue

            portfolio = strat.build_portfolio(
                city_slug, tradable_dist, STRATEGY_PARAMS, mean=mean, sigma=sigma
            )

            if portfolio["allocation"]:
                _fetch_sell_values_for_allocation(portfolio["allocation"], tradable_dist)

            _attach_distribution_changes(
                mkt.get("full_distribution"),
                portfolio["full_distribution"],
                update_model=True,
                update_price=True,
            )

            was_new_signal = bool(portfolio["allocation"]) and not mkt.get("live_allocation")

            mkt.update({
                "horizon_days": i, "forecast_mean": mean, "sigma": sigma,
                "forecast_mean_shadow": shadow_mean, "model_means_raw": model_means_raw,
                "model_spread": model_spread,
                "observed_intraday_temp": observed_bound,
                "full_distribution": portfolio["full_distribution"],
                "live_allocation": portfolio["allocation"],
                "total_allocated_units": portfolio.get("total_allocated_units"),
                "portfolio_score": portfolio.get("portfolio_score"),
                "worst_case_loss_frac": portfolio.get("worst_case_loss_frac"),
                "best_case_pnl_units": portfolio.get("best_case_pnl_units"),
                "worst_case_pnl_units": portfolio.get("worst_case_pnl_units"),
                "success_probability": portfolio.get("success_probability"),
                "confidence": portfolio.get("confidence"),
                "balance_units": STRATEGY_PARAMS["balance_units"],
                "strategy_version": STRATEGY_VERSION,
            })
            mkt.pop("skipped_reason", None)

            if was_new_signal:
                new_positions += 1
                print(f" [NEW SIGNAL x{len(portfolio['allocation'])}] {mkt['city_name']} {date}")

            save_market(mkt)

        print("ok")

    return new_positions

# --- فاز ۶: کشف بازار دمای کمینه (فقط در اسکن سنگین، طبق تصمیم کاربر) --------

def discover_new_min_signals(now, state):
    """معادل کامل discover_new_signals ولی برای بازار دمای کمینه --
    fetch_outcomes/parse_temp_range/strat.build_portfolio عیناً REUSE
    می‌شوند. تنها تفاوت‌ها: اسلاگ Polymarket (lowest-)، مسیر فایل جدا
    (market_path_min)، فراخوانی fc.build_combined_distribution_min/
    build_calibrated_distribution_min، و فیلد "market_type": "min"."""
    new_positions = 0

    fetch_jobs = []
    for city_slug in LOCATIONS:
        loc = LOCATIONS[city_slug]
        dates = _local_dates_for_city(now, loc, 4)
        for i, date in enumerate(dates):
            dt = datetime.strptime(date, "%Y-%m-%d")
            fetch_jobs.append((city_slug, date, i, dt))

    events_by_job = {}
    with ThreadPoolExecutor(max_workers=EVENT_FETCH_WORKERS) as pool:
        future_to_job = {
            pool.submit(get_polymarket_event_min, city_slug, MONTHS[dt.month - 1], dt.day, dt.year):
                (city_slug, date, i, dt)
            for (city_slug, date, i, dt) in fetch_jobs
        }
        for future in as_completed(future_to_job):
            job = future_to_job[future]
            try:
                events_by_job[job] = future.result()
            except Exception:
                events_by_job[job] = None

    for city_slug, loc in LOCATIONS.items():
        dates = _local_dates_for_city(now, loc, 4)
        for i, date in enumerate(dates):
            dt = datetime.strptime(date, "%Y-%m-%d")
            event = events_by_job.get((city_slug, date, i, dt))
            if not event:
                continue

            end_date = event.get("endDate", "")
            mkt = load_market_min(city_slug, date) or {
                "city": city_slug, "city_name": loc["name"], "date": date,
                "unit": loc["unit"], "status": "open", "market_type": "min",
                "created_at": now.isoformat(), "committed_allocation": None,
            }

            if mkt["status"] == "resolved":
                continue

            timing = _refresh_local_day_timing(mkt, loc, now, end_date)
            hours = timing["remaining_seconds"] / 3600.0
            mkt["last_scan"] = now.isoformat()

            needs_version_refresh = mkt.get("strategy_version") != STRATEGY_VERSION
            if (hours < MIN_HOURS or hours > MAX_HOURS) and not needs_version_refresh:
                save_market_min(mkt)
                continue

            outcomes = fetch_outcomes(event)
            if not outcomes:
                save_market_min(mkt)
                continue

            event_volume = float(event.get("volume", 0) or 0)
            if event_volume < MIN_VOLUME:
                mkt["skipped_reason"] = f"event volume {event_volume:.0f} < min_volume {MIN_VOLUME}"
                save_market_min(mkt)
                continue

            dist_precheck = [d for d in outcomes if d["volume"] > 0]
            if not dist_precheck:
                save_market_min(mkt)
                continue

            members = fc.build_combined_distribution_min(city_slug, loc, date)
            mean, sigma = fc.build_calibrated_distribution_min(members, city_slug, i, loc["unit"])

            if mean is None:
                mkt["skipped_reason"] = "ensemble forecast unavailable (mean is None)"
                save_market_min(mkt)
                continue

            observed_bound = fc.get_intraday_observed_bound(loc, date, is_min=True) if i == 0 else None

            dist = fc.full_bucket_distribution(
                mean, sigma, outcomes,
                city_slug=city_slug,
                horizon_days=i,
                unit=loc["unit"],
                is_min=True,
                observed_bound=observed_bound,
            )
            tradable_dist = [d for d in dist if d["volume"] > 0]
            if not tradable_dist:
                save_market_min(mkt)
                continue

            portfolio = strat.build_portfolio(
                city_slug, tradable_dist, STRATEGY_PARAMS, mean=mean, sigma=sigma
            )
            if portfolio["allocation"]:
                _fetch_sell_values_for_allocation(portfolio["allocation"], tradable_dist)

            _attach_distribution_changes(
                mkt.get("full_distribution"),
                portfolio["full_distribution"],
                update_model=True,
                update_price=True,
            )

            was_new_signal = bool(portfolio["allocation"]) and not mkt.get("live_allocation")

            mkt.update({
                "horizon_days": i, "forecast_mean": mean, "sigma": sigma,
                "observed_intraday_temp": observed_bound,
                "full_distribution": portfolio["full_distribution"],
                "live_allocation": portfolio["allocation"],
                "total_allocated_units": portfolio.get("total_allocated_units"),
                "portfolio_score": portfolio.get("portfolio_score"),
                "worst_case_loss_frac": portfolio.get("worst_case_loss_frac"),
                "best_case_pnl_units": portfolio.get("best_case_pnl_units"),
                "worst_case_pnl_units": portfolio.get("worst_case_pnl_units"),
                "success_probability": portfolio.get("success_probability"),
                "confidence": portfolio.get("confidence"),
                "balance_units": STRATEGY_PARAMS["balance_units"],
                "strategy_version": STRATEGY_VERSION,
            })
            mkt.pop("skipped_reason", None)

            if was_new_signal:
                new_positions += 1

            save_market_min(mkt)

    return new_positions

# =============================================================================
# PHASE 2 -- COUNTDOWN REFRESH
# =============================================================================

def refresh_all_locked_markets(now):
    """Refreshes raw Gamma metadata but derives all operational time from the
    city-local target day, including when Gamma is temporarily unavailable."""
    open_markets = [m for m in load_all_markets() if m.get("status") != "resolved"]
    if not open_markets:
        return
    with ThreadPoolExecutor(max_workers=EVENT_FETCH_WORKERS) as pool:
        future_to_mkt = {
            pool.submit(get_polymarket_event_for_market_date, m["city"], m["date"]): m
            for m in open_markets
        }
        events_by_mkt_key = {}
        for future in as_completed(future_to_mkt):
            mkt = future_to_mkt[future]
            key = (mkt["city"], mkt["date"])
            try:
                events_by_mkt_key[key] = future.result()
            except Exception:
                events_by_mkt_key[key] = None
    for mkt in open_markets:
        loc = LOCATIONS.get(mkt.get("city"))
        if not loc:
            continue
        event = events_by_mkt_key.get((mkt["city"], mkt["date"]))
        end_date = event.get("endDate", "") if event else None
        _refresh_local_day_timing(mkt, loc, now, end_date)
        mkt["last_scan"] = now.isoformat()
        save_market(mkt)

def refresh_all_locked_markets_min(now):
    """معادل کامل refresh_all_locked_markets ولی برای بازار کمینه -- تنها
    فرقش استفاده از load_all_min_markets/get_polymarket_event_for_market_date_min/
    save_market_min است. بدون این تابع، hours_left بازارهای کمینه فقط یک‌بار
    در لحظهٔ discover_new_min_signals محاسبه و برای همیشه یخ‌زده می‌ماند."""
    open_markets = [m for m in load_all_min_markets() if m.get("status") != "resolved"]
    if not open_markets:
        return
    with ThreadPoolExecutor(max_workers=EVENT_FETCH_WORKERS) as pool:
        future_to_mkt = {
            pool.submit(get_polymarket_event_for_market_date_min, m["city"], m["date"]): m
            for m in open_markets
        }
        events_by_mkt_key = {}
        for future in as_completed(future_to_mkt):
            mkt = future_to_mkt[future]
            key = (mkt["city"], mkt["date"])
            try:
                events_by_mkt_key[key] = future.result()
            except Exception:
                events_by_mkt_key[key] = None
    for mkt in open_markets:
        loc = LOCATIONS.get(mkt.get("city"))
        if not loc:
            continue
        event = events_by_mkt_key.get((mkt["city"], mkt["date"]))
        end_date = event.get("endDate", "") if event else None
        _refresh_local_day_timing(mkt, loc, now, end_date)
        mkt["last_scan"] = now.isoformat()
        save_market_min(mkt)

def refresh_open_market_info(now):
    """Phase 3 (heartbeat سبک): بازخوانی قیمت/پیش‌بینی بازارهای باز از صفر
    -- بدون strat.build_portfolio، بدون سایزینگ.

    (فاز ۳+۴) fc.build_combined_distribution_full جایگزین نقطهٔ فراخوانی
    قدیمی شد؛ forecast_mean/sigma زنده کاملاً همان مقدار قبلی می‌گیرند.
    forecast_mean_shadow/model_means_raw هم اینجا (در اسکن سبک، طبق تصمیم
    صریح کاربر که این‌ها هم در هر دو مسیر باشند) به‌روزرسانی می‌شوند.
    """
    open_markets = [m for m in load_all_markets() if m.get("status") == "open"]
    if not open_markets:
        return 0

    merged_params = strat.get_merged_params(STRATEGY_PARAMS)

    with ThreadPoolExecutor(max_workers=EVENT_FETCH_WORKERS) as pool:
        future_to_mkt = {
            pool.submit(get_polymarket_event_for_market_date, m["city"], m["date"]): m
            for m in open_markets
        }
        events_by_key = {}
        for future in as_completed(future_to_mkt):
            mkt = future_to_mkt[future]
            key = (mkt["city"], mkt["date"])
            try:
                events_by_key[key] = future.result()
            except Exception:
                events_by_key[key] = None

    refreshed = 0
    for mkt in open_markets:
        loc = LOCATIONS.get(mkt["city"])
        if not loc:
            continue

        event = events_by_key.get((mkt["city"], mkt["date"]))
        end_date = event.get("endDate", "") if event else None
        _refresh_local_day_timing(mkt, loc, now, end_date)
        mkt["last_lite_refresh"] = now.isoformat()
        save_market(mkt)

        local_dates = _local_dates_for_city(now, loc, 4)
        try:
            horizon_days = local_dates.index(mkt["date"])
        except ValueError:
            continue

        if not event:
            continue

        outcomes = fetch_outcomes(event)
        if not outcomes:
            continue

        tradable_outcomes = [d for d in outcomes if d["volume"] > 0]
        if not tradable_outcomes:
            continue

        try:
            members, model_members = fc.build_combined_distribution_full(mkt["city"], loc, mkt["date"])
            mean, sigma = fc.build_calibrated_distribution(
                members, mkt["city"], horizon_days, loc["unit"], model_members=model_members
            )
            shadow_mean, model_means_raw = fc.build_shadow_mean(model_members, mkt["city"], horizon_days, loc["unit"])
            if fc.get_gate_status(mkt["city"], horizon_days) == "revert_to_shadow" and shadow_mean is not None:
                mean = round(float(shadow_mean), 2)
        except Exception:
            continue

        if mean is None:
            continue

        observed_bound = fc.get_intraday_observed_bound(loc, mkt["date"], is_min=False) if horizon_days == 0 else None
        model_weights = fc.get_model_weights(mkt["city"], list((model_means_raw or {}).keys()))
        model_spread = fc.compute_model_spread(model_means_raw, model_weights)

        dist = fc.full_bucket_distribution(
            mean, sigma, tradable_outcomes,
            model_means_raw=model_means_raw,
            city_slug=mkt["city"],
            horizon_days=horizon_days,
            unit=loc["unit"],
            is_min=False,
            observed_bound=observed_bound,
        )
        tradable_dist = [d for d in dist if d["volume"] > 0]
        if not tradable_dist:
            continue

        candidates = strat.build_candidate_set(tradable_dist, merged_params)
        full_distribution = sorted(
            [c for c in candidates if c["side"] == "YES"], key=lambda x: x["range"][0]
        )
        if not full_distribution:
            continue

        _attach_distribution_changes(
            mkt.get("full_distribution"),
            full_distribution,
            update_model=True,
            update_price=True,
        )

        mkt["forecast_mean"] = mean
        mkt["sigma"] = sigma
        mkt["forecast_mean_shadow"] = shadow_mean
        mkt["model_means_raw"] = model_means_raw
        mkt["model_spread"] = model_spread
        mkt["observed_intraday_temp"] = observed_bound
        mkt["full_distribution"] = full_distribution
        mkt["last_lite_refresh"] = now.isoformat()
        save_market(mkt)
        refreshed += 1

    return refreshed

def refresh_open_market_info_min(now):
    """معادل کامل refresh_open_market_info ولی برای بازار کمینه."""
    open_markets = [m for m in load_all_min_markets() if m.get("status") == "open"]
    if not open_markets:
        return 0

    merged_params = strat.get_merged_params(STRATEGY_PARAMS)

    with ThreadPoolExecutor(max_workers=EVENT_FETCH_WORKERS) as pool:
        future_to_mkt = {
            pool.submit(get_polymarket_event_for_market_date_min, m["city"], m["date"]): m
            for m in open_markets
        }
        events_by_key = {}
        for future in as_completed(future_to_mkt):
            mkt = future_to_mkt[future]
            key = (mkt["city"], mkt["date"])
            try:
                events_by_key[key] = future.result()
            except Exception:
                events_by_key[key] = None

    refreshed = 0
    for mkt in open_markets:
        loc = LOCATIONS.get(mkt["city"])
        if not loc:
            continue

        event = events_by_key.get((mkt["city"], mkt["date"]))
        end_date = event.get("endDate", "") if event else None
        _refresh_local_day_timing(mkt, loc, now, end_date)
        mkt["last_lite_refresh"] = now.isoformat()
        save_market_min(mkt)

        local_dates = _local_dates_for_city(now, loc, 4)
        try:
            horizon_days = local_dates.index(mkt["date"])
        except ValueError:
            continue

        if not event:
            continue

        outcomes = fetch_outcomes(event)
        if not outcomes:
            continue

        tradable_outcomes = [d for d in outcomes if d["volume"] > 0]
        if not tradable_outcomes:
            continue

        try:
            members = fc.build_combined_distribution_min(mkt["city"], loc, mkt["date"])
            mean, sigma = fc.build_calibrated_distribution_min(members, mkt["city"], horizon_days, loc["unit"])
        except Exception:
            continue

        if mean is None:
            continue

        observed_bound = fc.get_intraday_observed_bound(loc, mkt["date"], is_min=True) if horizon_days == 0 else None

        dist = fc.full_bucket_distribution(
            mean, sigma, tradable_outcomes,
            city_slug=mkt["city"],
            horizon_days=horizon_days,
            unit=loc["unit"],
            is_min=True,
            observed_bound=observed_bound,
        )
        tradable_dist = [d for d in dist if d["volume"] > 0]
        if not tradable_dist:
            continue

        candidates = strat.build_candidate_set(tradable_dist, merged_params)
        full_distribution = sorted(
            [c for c in candidates if c["side"] == "YES"], key=lambda x: x["range"][0]
        )
        if not full_distribution:
            continue

        _attach_distribution_changes(
            mkt.get("full_distribution"),
            full_distribution,
            update_model=True,
            update_price=True,
        )

        mkt["forecast_mean"] = mean
        mkt["sigma"] = sigma
        mkt["observed_intraday_temp"] = observed_bound
        mkt["full_distribution"] = full_distribution
        mkt["last_lite_refresh"] = now.isoformat()
        save_market_min(mkt)
        refreshed += 1

    return refreshed

def refresh_all_open_market_prices(now):
    """فاز ۳ نقشه‌راه، زیرگام ۳-الف: فقط قیمت بازار (yes_price) و اسپرد سفارشات
    (bestBid/bestAsk) همهٔ باکت‌های موجود در full_distribution همهٔ مارکت‌های open
    را از Gamma API تازه می‌کند -- و هم‌زمان تغییرات قیمت، پتانسیل رشد نوسان‌گیری
    (swing_edge / swing_upside_pct)، باور نهایی (belief_prob) و EV را دقیق
    به‌روزرسانی می‌کند."""
    open_markets = [m for m in load_all_markets() if m.get("status") == "open"]
    if not open_markets:
        return 0

    merged_params = strat.get_merged_params(STRATEGY_PARAMS)

    event_keys = {(m["city"], m["date"]) for m in open_markets}

    def _fetch_quotes(key):
        city, date = key
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            return get_gamma_event_quotes(city, MONTHS[dt.month - 1], dt.day, dt.year)
        except Exception:
            return {}

    quotes_by_event = {}
    with ThreadPoolExecutor(max_workers=EVENT_FETCH_WORKERS) as pool:
        future_to_key = {pool.submit(_fetch_quotes, key): key for key in event_keys}
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                quotes_by_event[key] = future.result()
            except Exception:
                quotes_by_event[key] = {}

    refreshed = 0
    for mkt in open_markets:
        key = (mkt["city"], mkt["date"])
        quotes = quotes_by_event.get(key)
        if not quotes:
            continue

        full_dist = mkt.get("full_distribution")
        if not full_dist:
            continue

        old_snapshot = [dict(b) for b in full_dist]
        changed = False
        for bucket in full_dist:
            mid = str(bucket.get("market_id", ""))
            q_info = quotes.get(mid)
            if not q_info or q_info.get("price") is None:
                continue
            new_p = round(float(q_info["price"]), 4)
            best_bid = q_info.get("bid")
            best_ask = q_info.get("ask")
            eff_ask, spread = strat._effective_entry_price(new_p, best_bid, best_ask)
            bucket["yes_price"] = new_p
            bucket["ask"] = new_p
            bucket["effective_ask"] = eff_ask
            bucket["best_bid"] = round(float(best_bid), 4) if best_bid is not None else bucket.get("best_bid")
            bucket["best_ask"] = round(float(best_ask), 4) if best_ask is not None else bucket.get("best_ask")
            bucket["bid_ask_spread"] = spread
            bucket["market_prob"] = new_p
            model_prob = bucket.get("model_prob")
            if model_prob is not None:
                swing_edge = round(float(model_prob) - new_p, 4)
                bucket["swing_edge"] = swing_edge
                bucket["swing_upside_pct"] = round((swing_edge / new_p) * 100.0, 1) if new_p >= 0.01 else 0.0
            belief_prob = strat.fuse_belief(model_prob, new_p, merged_params)
            bucket["belief_prob"] = belief_prob
            if eff_ask > 0.001 and belief_prob is not None:
                bucket["ev"] = round((belief_prob - eff_ask) / eff_ask, 4)
            changed = True

        if changed:
            _attach_distribution_changes(
                old_snapshot,
                full_dist,
                update_model=False,
                update_price=True,
                scan_mode="10m",
            )
            mkt["full_distribution"] = full_dist
            mkt["last_price_refresh"] = now.isoformat()
            save_market(mkt)
            refreshed += 1

    return refreshed


def refresh_all_open_market_prices_min(now):
    """معادل کامل refresh_all_open_market_prices ولی برای بازار کمینه."""
    open_markets = [m for m in load_all_min_markets() if m.get("status") == "open"]
    if not open_markets:
        return 0

    merged_params = strat.get_merged_params(STRATEGY_PARAMS)

    event_keys = {(m["city"], m["date"]) for m in open_markets}

    def _fetch_quotes_min(key):
        city, date = key
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            return get_gamma_event_quotes_min(city, MONTHS[dt.month - 1], dt.day, dt.year)
        except Exception:
            return {}

    quotes_by_event = {}
    with ThreadPoolExecutor(max_workers=EVENT_FETCH_WORKERS) as pool:
        future_to_key = {pool.submit(_fetch_quotes_min, key): key for key in event_keys}
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                quotes_by_event[key] = future.result()
            except Exception:
                quotes_by_event[key] = {}

    refreshed = 0
    for mkt in open_markets:
        key = (mkt["city"], mkt["date"])
        quotes = quotes_by_event.get(key)
        if not quotes:
            continue

        full_dist = mkt.get("full_distribution")
        if not full_dist:
            continue

        old_snapshot = [dict(b) for b in full_dist]
        changed = False
        for bucket in full_dist:
            mid = str(bucket.get("market_id", ""))
            q_info = quotes.get(mid)
            if not q_info or q_info.get("price") is None:
                continue
            new_p = round(float(q_info["price"]), 4)
            best_bid = q_info.get("bid")
            best_ask = q_info.get("ask")
            eff_ask, spread = strat._effective_entry_price(new_p, best_bid, best_ask)
            bucket["yes_price"] = new_p
            bucket["ask"] = new_p
            bucket["effective_ask"] = eff_ask
            bucket["best_bid"] = round(float(best_bid), 4) if best_bid is not None else bucket.get("best_bid")
            bucket["best_ask"] = round(float(best_ask), 4) if best_ask is not None else bucket.get("best_ask")
            bucket["bid_ask_spread"] = spread
            bucket["market_prob"] = new_p
            model_prob = bucket.get("model_prob")
            if model_prob is not None:
                swing_edge = round(float(model_prob) - new_p, 4)
                bucket["swing_edge"] = swing_edge
                bucket["swing_upside_pct"] = round((swing_edge / new_p) * 100.0, 1) if new_p >= 0.01 else 0.0
            belief_prob = strat.fuse_belief(model_prob, new_p, merged_params)
            bucket["belief_prob"] = belief_prob
            if eff_ask > 0.001 and belief_prob is not None:
                bucket["ev"] = round((belief_prob - eff_ask) / eff_ask, 4)
            changed = True

        if changed:
            _attach_distribution_changes(
                old_snapshot,
                full_dist,
                update_model=False,
                update_price=True,
                scan_mode="10m",
            )
            mkt["full_distribution"] = full_dist
            mkt["last_price_refresh"] = now.isoformat()
            save_market_min(mkt)
            refreshed += 1

    return refreshed


LAST_SCAN_FILE = DATA_DIR / "last_scan.json"

def _record_scan_timestamp(kind):
    """ثبت زمان آخرین اسکن سنگین/سبک برای نمایش «آخرین به‌روزرسانی» در داشبورد."""
    data = {}
    if LAST_SCAN_FILE.exists():
        try:
            data = json.loads(LAST_SCAN_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[kind] = datetime.now(timezone.utc).isoformat()
    LAST_SCAN_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

# =============================================================================
# PHASE 3 -- RESOLUTION (حداکثر)
# =============================================================================

def resolve_expired_markets(state):
    resolved_count = 0
    entries = load_entries()
    for mkt in load_all_markets():
        if mkt.get("status") in ("resolved", "resolved_no_signal"):
            continue
        grading_allocation = mkt.get("committed_allocation") or mkt.get("live_allocation")
        hours_left = mkt.get("hours_left", 999)
        if not grading_allocation:
            if hours_left > 0.5:
                continue
            loc = LOCATIONS[mkt["city"]]
            dt = datetime.strptime(mkt["date"], "%Y-%m-%d")
            settlement = res.get_polymarket_settlement(mkt["city"], MONTHS[dt.month - 1], dt.day, dt.year)
            if not settlement:
                mkt["status"] = "expired_no_signal"
                save_market(mkt)
                continue
            settlement_temp = res.get_actual_temp_from_settlement(mkt, settlement)
            mkt["actual_temp"] = settlement_temp if settlement_temp is not None else res.get_display_temp(loc, mkt["date"], VC_KEY)
            mkt["status"] = "resolved_no_signal"
            mkt["pnl_units"] = None
            mkt["my_pnl_units"] = None
            save_market(mkt)
            continue
        if hours_left > 0.5:
            continue
        loc = LOCATIONS[mkt["city"]]
        dt = datetime.strptime(mkt["date"], "%Y-%m-%d")
        settlement = res.get_polymarket_settlement(mkt["city"], MONTHS[dt.month - 1], dt.day, dt.year)
        if settlement is None:
            continue
        all_settled = all(a["market_id"] in settlement for a in grading_allocation)
        if not all_settled:
            continue
        settlement_temp = res.get_actual_temp_from_settlement(mkt, settlement)
        mkt["actual_temp"] = settlement_temp if settlement_temp is not None else res.get_display_temp(loc, mkt["date"], VC_KEY)
        raw_pnl_units = 0.0
        raw_my_pnl_units = 0.0
        any_user_entry = False
        for a in grading_allocation:
            yes_won = settlement[a["market_id"]]
            won = yes_won if a["side"] == "YES" else (not yes_won)
            trade_pnl = (a["units"] * (1.0 / a["price"] - 1.0)) if won else -a["units"]
            a["won"] = won
            a["pnl_units"] = round(trade_pnl, 2)
            raw_pnl_units += trade_pnl
            user_entered = entries.get(a.get("entry_key", ""), False)
            a["user_entered"] = user_entered
            if user_entered:
                any_user_entry = True
                raw_my_pnl_units += trade_pnl
        pnl_units = round(raw_pnl_units, 2)
        my_pnl_units = round(raw_my_pnl_units, 2)
        mkt["status"] = "resolved"
        mkt["graded_against"] = "committed" if mkt.get("committed_allocation") else "live_fallback"
        mkt["resolved_outcome"] = "win" if pnl_units >= 0 else "loss"
        mkt["pnl_units"] = pnl_units
        mkt["my_pnl_units"] = my_pnl_units if any_user_entry else None
        mkt["graded_allocation"] = grading_allocation
        state["balance_units"] = round(state["balance_units"] + pnl_units, 2)
        state["total_pnl_units"] = round(state["total_pnl_units"] + pnl_units, 2)
        if pnl_units >= 0:
            state["wins"] += 1
        else:
            state["losses"] += 1
        if any_user_entry:
            state["my_balance_units"] = round(state["my_balance_units"] + my_pnl_units, 2)
            state["my_total_pnl_units"] = round(state["my_total_pnl_units"] + my_pnl_units, 2)
            if my_pnl_units >= 0:
                state["my_wins"] += 1
            else:
                state["my_losses"] += 1
        save_market(mkt)
        resolved_count += 1
    return resolved_count

def resolve_expired_min_markets(state):
    """(فاز ۶) معادل کامل resolve_expired_markets ولی برای بازار کمینه --
    همان state مشترک (balance_units/wins/losses) با بازار حداکثر
    استفاده می‌شود."""
    resolved_count = 0
    entries = load_entries()
    for mkt in load_all_min_markets():
        if mkt.get("status") in ("resolved", "resolved_no_signal"):
            continue
        grading_allocation = mkt.get("committed_allocation") or mkt.get("live_allocation")
        hours_left = mkt.get("hours_left", 999)

        if not grading_allocation:
            if hours_left > 0.5:
                continue
            loc = LOCATIONS[mkt["city"]]
            dt = datetime.strptime(mkt["date"], "%Y-%m-%d")
            settlement = res.get_polymarket_settlement_min(mkt["city"], MONTHS[dt.month - 1], dt.day, dt.year)
            if not settlement:
                mkt["status"] = "expired_no_signal"
                save_market_min(mkt)
                continue
            settlement_temp = res.get_actual_temp_from_settlement(mkt, settlement)
            mkt["actual_temp"] = settlement_temp if settlement_temp is not None else res.get_display_temp_min(loc, mkt["date"], VC_KEY)
            mkt["status"] = "resolved_no_signal"
            mkt["pnl_units"] = None
            mkt["my_pnl_units"] = None
            save_market_min(mkt)
            continue

        if hours_left > 0.5:
            continue
        loc = LOCATIONS[mkt["city"]]
        dt = datetime.strptime(mkt["date"], "%Y-%m-%d")
        settlement = res.get_polymarket_settlement_min(mkt["city"], MONTHS[dt.month - 1], dt.day, dt.year)
        if settlement is None:
            continue
        all_settled = all(a["market_id"] in settlement for a in grading_allocation)
        if not all_settled:
            continue

        settlement_temp = res.get_actual_temp_from_settlement(mkt, settlement)
        mkt["actual_temp"] = settlement_temp if settlement_temp is not None else res.get_display_temp_min(loc, mkt["date"], VC_KEY)

        raw_pnl_units = 0.0
        raw_my_pnl_units = 0.0
        any_user_entry = False
        for a in grading_allocation:
            yes_won = settlement[a["market_id"]]
            won = yes_won if a["side"] == "YES" else (not yes_won)
            trade_pnl = (a["units"] * (1.0 / a["price"] - 1.0)) if won else -a["units"]
            a["won"] = won
            a["pnl_units"] = round(trade_pnl, 2)
            raw_pnl_units += trade_pnl
            user_entered = entries.get(a.get("entry_key", ""), False)
            a["user_entered"] = user_entered
            if user_entered:
                any_user_entry = True
                raw_my_pnl_units += trade_pnl

        pnl_units = round(raw_pnl_units, 2)
        my_pnl_units = round(raw_my_pnl_units, 2)
        mkt["status"] = "resolved"
        mkt["graded_against"] = "committed" if mkt.get("committed_allocation") else "live_fallback"
        mkt["resolved_outcome"] = "win" if pnl_units >= 0 else "loss"
        mkt["pnl_units"] = pnl_units
        mkt["my_pnl_units"] = my_pnl_units if any_user_entry else None
        mkt["graded_allocation"] = grading_allocation

        state["balance_units"] = round(state["balance_units"] + pnl_units, 2)
        state["total_pnl_units"] = round(state["total_pnl_units"] + pnl_units, 2)
        if pnl_units >= 0:
            state["wins"] += 1
        else:
            state["losses"] += 1
        if any_user_entry:
            state["my_balance_units"] = round(state["my_balance_units"] + my_pnl_units, 2)
            state["my_total_pnl_units"] = round(state["my_total_pnl_units"] + my_pnl_units, 2)
            if my_pnl_units >= 0:
                state["my_wins"] += 1
            else:
                state["my_losses"] += 1

        save_market_min(mkt)
        resolved_count += 1
    return resolved_count

# (مرحلهٔ ۲) پیش‌گرفتن موازی پاسخ‌های Open-Meteo -- نگاه کنید به توضیح بالای
# prefetch_ensembles در forecasting.py. با weather_prefetch=false در config.json
# رفتار دقیقاً مثل قبل می‌شود.
WEATHER_PREFETCH = bool(CFG.get("weather_prefetch", True))
WEATHER_PREFETCH_WORKERS = int(CFG.get("weather_prefetch_workers", 4))

def _prepare_weather_cache(max_cities=None, min_cities=None):
    fc.clear_ensemble_cache()
    fc.set_ensemble_cache_enabled(WEATHER_PREFETCH)
    if not WEATHER_PREFETCH:
        print("  [prefetch] غیرفعال است (weather_prefetch=false) -- رفتار قبلی")
        return
    try:
        stats = fc.prefetch_ensembles(LOCATIONS, max_cities, min_cities, WEATHER_PREFETCH_WORKERS)
        print(f"  [prefetch] {stats['cached']} از {stats['tasks']} آدرس هواشناسی در {stats['seconds']} ثانیه "
              f"گرفته شد ({WEATHER_PREFETCH_WORKERS} کار همزمان)")
        if os.environ.get("WEATHERBOT_VERIFY_PREFETCH") == "1":
            same, checked = fc.verify_ensemble_cache(5)
            print(f"  [prefetch-verify] {same} از {checked} نمونه با دریافت مستقیم برابر بود")
    except Exception as e:
        print(f"  [prefetch] هشدار: پیش‌گرفتن ناموفق بود ({e}) -- اسکن بدون آن ادامه می‌یابد")

def scan_and_update():
    state = load_state()
    now = datetime.now(timezone.utc)
    _prepare_weather_cache()
    new_positions = discover_new_signals(now, state)
    new_min_positions = discover_new_min_signals(now, state)
    refresh_all_locked_markets(now)
    refresh_all_locked_markets_min(now)
    committed = apply_lock_requests(now)
    resolved_count = resolve_expired_markets(state)
    resolved_min_count = resolve_expired_min_markets(state)
    save_state(state)
    fc.update_calibration_from_live(load_all_markets(), LOCATIONS)
    fc.update_calibration_from_live_min(load_all_min_markets(), LOCATIONS)
    try:
        fc.update_model_skill_from_live(load_all_markets(), LOCATIONS)
    except Exception as e:
        print(f"  هشدار: به‌روزرسانی مهارت مدل‌ها ناموفق بود: {e}")
    return new_positions + new_min_positions, resolved_count + resolved_min_count, committed

def _start_local_server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(Path.cwd()))
    httpd = http.server.ThreadingHTTPServer(("localhost", SERVE_PORT), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd

def run_once():
    t_start = time.perf_counter()
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] scanning {len(LOCATIONS)} cities...")
    new_pos, resolved, committed = scan_and_update()
    # (مرحلهٔ ۱) ساخت داشبورد اصلی (dashboard.html) غیرفعال شد -- فقط داشبورد
    # تعاملی (simple.html) توسط dashboard_simple.py ساخته می‌شود.
    try:
        n_rows = accuracy_report.build_accuracy_report()
        print(f"  [accuracy-report] {n_rows} رکورد در data/accuracy_report.csv به‌روزرسانی شد")
    except Exception as e:
        print(f"  [accuracy-report] هشدار: گزارش دقت ساخته نشد ({e}) -- بات ادامه می‌دهد")
    _record_scan_timestamp("full")
    print(f"  new signals: {new_pos} | committed: {committed} | resolved: {resolved}")
    print(f"  mark your real trades in: {ENTRIES_FILE}")

def run_lite_scan():
    """اسکن سبک (heartbeat) -- فقط تازه‌سازی اطلاعات نمایشی بازارهای باز،
    resolve بازارهای منقضی، و به‌روزرسانی کالیبراسیون زنده. هیچ سیگنال
    جدیدی ساخته نمی‌شود (طبق تصمیم صریح کاربر، discover_new_min_signals
    اینجا فراخوانی نمی‌شود -- فقط در اسکن سنگین)."""
    t_start = time.perf_counter()
    now = datetime.now(timezone.utc)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] اسکن سبک -- تازه‌سازی اطلاعات {len(LOCATIONS)} شهر...")

    try:
        open_max_cities = {m.get("city") for m in load_all_markets() if m.get("status") == "open"}
        open_min_cities = {m.get("city") for m in load_all_min_markets() if m.get("status") == "open"}
        _prepare_weather_cache(open_max_cities, open_min_cities)
    except Exception as e:
        fc.clear_ensemble_cache()
        fc.set_ensemble_cache_enabled(False)
        print(f"  [prefetch] هشدار: آماده‌سازی ناموفق بود ({e}) -- رفتار قبلی")

    refreshed = refresh_open_market_info(now)
    refresh_open_market_info_min(now)
    state = load_state()
    resolved_count = resolve_expired_markets(state)
    resolved_min_count = resolve_expired_min_markets(state)
    save_state(state)

    try:
        fc.update_calibration_from_live(load_all_markets(), LOCATIONS)
    except Exception as e:
        print(f"  هشدار: به‌روزرسانی کالیبراسیون زنده ناموفق بود: {e}")
    try:
        fc.update_calibration_from_live_min(load_all_min_markets(), LOCATIONS)
    except Exception as e:
        print(f"  هشدار: به‌روزرسانی کالیبراسیون زندهٔ کمینه ناموفق بود: {e}")
    try:
        fc.update_model_skill_from_live(load_all_markets(), LOCATIONS)
    except Exception as e:
        print(f"  هشدار: به‌روزرسانی مهارت مدل‌ها ناموفق بود: {e}")

    _record_scan_timestamp("lite")
    print(f"  بازارهای تازه‌سازی‌شده: {refreshed} | resolve‌شده: {resolved_count + resolved_min_count}")
    print(f"  توجه: هیچ سیگنال/پوزیشن جدیدی در این مسیر ساخته نمی‌شود.")

def run_price_refresh():
    """سبک‌ترین مسیر اسکن -- فقط قیمت بازار همهٔ باکت‌های open را تازه
    می‌کند (بدون فراخوانی هواشناسی)."""
    now = datetime.now(timezone.utc)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] آپدیت سبک قیمت پنل -- بدون فراخوانی هواشناسی...")
    refreshed = refresh_all_open_market_prices(now)
    refresh_all_open_market_prices_min(now)
    print(f"  قیمت {refreshed} بازار تازه شد.")

def run_loop():
    print(f"WeatherBet v3 -- {len(LOCATIONS)} cities | scan every {SCAN_INTERVAL // 60} min")
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
        except Exception as e:
            print(f"  Error: {e} -- retrying in 60s")
            time.sleep(60)
            continue
        time.sleep(SCAN_INTERVAL)

def run_serve():
    httpd = _start_local_server()
    print(f"WeatherBet v3 -- local server running at http://localhost:{SERVE_PORT}/dashboard.html")
    print("Open that URL (not the file directly) so the 'Lock this moment' button "
          "can attach to data/lock_requests.txt once.")
    try:
        run_loop()
    finally:
        httpd.shutdown()

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "backfill":
        print("Running one-time historical calibration backfill...")
        fc.backfill_calibration(LOCATIONS, lookback_days=CFG.get("calibration_lookback_days", 180))
        fc.backfill_calibration_min(LOCATIONS, lookback_days=CFG.get("calibration_lookback_days", 180))
        print("Backfill complete (max + min). You can now run: python weatherbot_v3.py serve")
    elif cmd == "once":
        run_once()
    elif cmd == "lite_scan":
        run_lite_scan()
    elif cmd == "price_refresh":
        run_price_refresh()
    elif cmd == "serve":
        run_serve()
    elif cmd == "run":
        run_loop()
    else:
        print("Usage: python weatherbot_v3.py [backfill|once|lite_scan|price_refresh|serve|run]")
