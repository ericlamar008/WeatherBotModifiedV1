"""
lock_manager.py -- خواندن درخواست‌های قفل/حذف قفل از GitHub Issues و ثبت‌شان.
=====================================================================================
نسخهٔ نهایی (شامل رفع باگ قفل تکراری از فاز ۶ نقشهٔ‌راه قدیم).

--- PATCH قبلی ------------------------------------------------------------------
مشخص شد که لینک «ایجاد Issue جدید» در simple.html با پارامتر URL از نوع
`?labels=lock-request` ساخته می‌شود، اما GitHub همیشه این لیبل را واقعاً روی
issue تازه‌ساخته‌شده نمی‌نشاند (حتی وقتی لیبل از قبل در ریپو وجود دارد) --
این با بررسی مستقیم چند issue واقعی (بدنهٔ JSON صحیح، ولی «No labels» در
سایدبار) تأیید شد. نتیجه: `_fetch_open_issues(label=...)` قبلی هرگز این
issueها را پیدا نمی‌کرد، پس هیچ قفلی ثبت نمی‌شد و خود issue هم بسته نمی‌شد.

راه‌حل: دیگر هیچ فیلتر لیبلی روی GitHub API اعمال نمی‌شود. همهٔ issueهای باز
خوانده می‌شوند و تشخیص لاک/آنلاک صرفاً بر اساس پیشوند عنوان (issue titleهایی
که خود simple.html می‌سازد: "LOCK ..." / "UNLOCK ...") به‌علاوهٔ اعتبارسنجی
بدنهٔ JSON انجام می‌شود. بستن issue و اضافه‌کردن لیبل "processed" در انتها
دست‌نخورده می‌ماند چون آن از طریق فراخوانی مستقیم API انجام می‌شود، نه از
طریق URL prefill (که همان بخش غیرقابل‌اتکا بود).

--- PATCH (فاز ۱ نقشه‌راه) -----------------------------------------------------
مشکل: قیمتی که در دکمهٔ «قفل کن» simple.html ساخته می‌شود، از آخرین اسکن
(که می‌تواند تا ۶ ساعت قدیمی باشد) گرفته شده -- یعنی entry_price ثبت‌شده
می‌توانست با قیمت واقعی لحظهٔ کلیک کاربر فرق زیادی داشته باشد و باعث
می‌شد درصد سود/زیان بعدی (در price_monitor.py) نسبت به یک مبنای غلط
حساب شود.

راه‌حل: process_lock_issues() دیگر مستقیماً به data["price"] (قیمت دکمهٔ
داشبورد) اعتماد نمی‌کند. به‌جای آن، در همان لحظهٔ پردازش issue (که طبق
مشاهدهٔ واقعی فقط ۱۵ تا ۴۰ ثانیه بعد از کلیک کاربر است)، یک بار قیمت زندهٔ
واقعی این market_id را از Gamma API می‌گیرد و همان را entry_price ثبت
می‌کند. اگر بنا به هر دلیلی قیمت زنده در دسترس نبود، به‌صورت ایمن به همان
قیمت دکمهٔ داشبورد برمی‌گردد -- این مسیر هرگز ثبت قفل را کاملاً مسدود
نمی‌کند.

--- PATCH (فاز ۶ نقشه‌راه -- بازار دمای کمینه) -------------------------------
هر قفل حالا یک فیلد "market_type" ("max" یا "min") دارد که از payload
جاوااسکریپت داشبورد می‌آید (پیش‌فرض "max" برای سازگاری کامل با فرمت
قدیمی/قفل‌های موجود که این فیلد را ندارند). _fetch_live_entry_price حالا
بر اساس market_type، بین get_gamma_event_prices (حداکثر) و
get_gamma_event_prices_min (کمینه) سوییچ می‌کند تا قیمت زندهٔ صحیح از
رویداد Polymarket درست (highest- یا lowest-) گرفته شود.
process_unlock_issues، _close_issue، _has_open_lock، فرمت
داده‌ی locked_signals.json، مکانیزم لیبل/عنوان -- هیچ‌کدام تغییر نکرده‌اند.
-----------------------------------------------------------------------------
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

from clob_utils import (
    get_clob_book_bid, get_gamma_event_prices, get_gamma_event_prices_min,
    get_gamma_event_quotes, get_gamma_event_quotes_min, pick_sell_price,
)
import history_manager

try:
    from locations import MONTHS
except ImportError:
    MONTHS = [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ]

GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY") or "ericlamar008/WeatherBotModifiedV1"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
API_BASE = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
LOCKS_FILE = Path("data/locked_signals.json")

HEADERS = {"Accept": "application/vnd.github+json"}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"


def _load_locks():
    if LOCKS_FILE.exists():
        try:
            return json.loads(LOCKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_locks(locks):
    LOCKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCKS_FILE.write_text(json.dumps(locks, indent=2, ensure_ascii=False), encoding="utf-8")


from concurrent.futures import ThreadPoolExecutor

_OPEN_ISSUES_CACHE = None
_GAMMA_QUOTES_MEM_CACHE = {}


def _fetch_open_issues(use_cache=True):
    """همهٔ Issueهای باز ریپو را برمی‌گرداند (بدون فیلتر لیبل -- به دلیل
    غیرقابل‌اتکا بودن پارامتر labels= در لینک ایجاد issue، دیگر روی آن
    تکیه نمی‌کنیم). تشخیص نوع درخواست بر عهدهٔ توابع فراخواننده است."""
    global _OPEN_ISSUES_CACHE
    if use_cache and _OPEN_ISSUES_CACHE is not None:
        return _OPEN_ISSUES_CACHE
    if not GITHUB_TOKEN and not os.environ.get("GITHUB_ACTIONS"):
        # جلوگیری از معطلی شبکه وقتی توکن گیت‌هاب تنظیم نشده است
        pass
    issues = []
    page = 1
    while True:
        r = requests.get(
            f"{API_BASE}/issues",
            headers=HEADERS,
            params={"state": "open", "per_page": 100, "page": page},
            timeout=10,
        )
        r.raise_for_status()
        batch = r.json()
        issues.extend(i for i in batch if "pull_request" not in i)
        if len(batch) < 100:
            break
        page += 1
    _OPEN_ISSUES_CACHE = issues
    return issues


def _get_cached_gamma_quotes(city, date, market_type="max"):
    """دریافت یک‌بارهٔ تمام قیمت‌ها (price + bid + ask) برای یک رویداد شهر/تاریخ با کش حافظه
    تا برای ثبت قفل، ۲ درخواست جداگانه به پلی‌مارکت ارسال نشود."""
    key = (city, date, "min" if market_type == "min" else "max")
    if key in _GAMMA_QUOTES_MEM_CACHE:
        return _GAMMA_QUOTES_MEM_CACHE[key]
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        month = MONTHS[dt.month - 1]
        fetch_fn = get_gamma_event_quotes_min if market_type == "min" else get_gamma_event_quotes
        quotes = fetch_fn(city, month, dt.day, dt.year) or {}
    except Exception:
        quotes = {}
    _GAMMA_QUOTES_MEM_CACHE[key] = quotes
    return quotes


def _fetch_live_entry_price(city, date, market_id, side, market_type="max"):
    """قیمت زندهٔ واقعی این market_id را در همین لحظه از Gamma API می‌گیرد
    و بر اساس side (YES/NO) قیمت ورود درست را برمی‌گرداند."""
    try:
        quotes = _get_cached_gamma_quotes(city, date, market_type)
        q = quotes.get(str(market_id))
        live_yes_price = q.get("price") if q else None
        if live_yes_price is None:
            dt = datetime.strptime(date, "%Y-%m-%d")
            month = MONTHS[dt.month - 1]
            fetch_fn = get_gamma_event_prices_min if market_type == "min" else get_gamma_event_prices
            prices = fetch_fn(city, month, dt.day, dt.year)
            live_yes_price = prices.get(str(market_id))
        if live_yes_price is None:
            return None
        if side == "NO":
            return round(1.0 - float(live_yes_price), 4)
        return round(float(live_yes_price), 4)
    except Exception:
        return None


def _fetch_live_exit_price(city, date, market_id, side, market_type="max", token_id=None):
    """(تغییر ۴) قیمت فروش (bid) برای خروج دستی از روی کش یکپارچه Gamma Quotes."""
    try:
        quotes = _get_cached_gamma_quotes(city, date, market_type)
        quote = quotes.get(str(market_id))
        if not quote:
            return None, None
        return pick_sell_price(side, quote, token_id)
    except Exception:
        return None, None


def _close_issue(number, comment=None, extra_label="processed"):
    if not GITHUB_TOKEN:
        return
    try:
        def _post_comment():
            if comment:
                requests.post(
                    f"{API_BASE}/issues/{number}/comments",
                    headers=HEADERS,
                    json={"body": comment},
                    timeout=10,
                )

        def _patch_close_and_label():
            requests.patch(
                f"{API_BASE}/issues/{number}",
                headers=HEADERS,
                json={"state": "closed", "labels": [extra_label]},
                timeout=10,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(_post_comment)
            f2 = pool.submit(_patch_close_and_label)
            f1.result()
            f2.result()
    except Exception as e:
        print(f"[lock_manager] هشدار: نشد issue شماره {number} را ببندم: {e}")


def _has_open_lock(locks, city, date, market_id):
    return any(
        l.get("city") == city and l.get("date") == date
        and str(l.get("market_id")) == str(market_id) and l.get("status") == "open"
        for l in locks
    )


def _is_already_processed_lock_issue(locks, issue, city, date, market_id):
    """بررسی اینکه آیا دقیقاً همین Issue قبلاً پردازش شده است یا خیر.
    از issue_id یکتای گیت‌هاب و تطابق کامل (repo + issue_number + city + date + market_id)
    استفاده می‌کند تا در صورت انتقال پروژه به ریپوی جدید (که شمارهٔ Issueها دوباره از ۱
    شروع می‌شود)، هرگز قفل‌های جدید به خاطر تشابه شماره با تاریخچهٔ قدیمی رد نشوند."""
    issue_id = issue.get("id")
    number = issue.get("number")
    for l in locks:
        if issue_id and (l.get("issue_id") == issue_id or l.get("last_edit_issue_id") == issue_id):
            return True
        if (
            l.get("repo") == GITHUB_REPOSITORY
            and (l.get("issue_number") == number or l.get("last_edit_issue_number") == number)
            and l.get("city") == city
            and l.get("date") == date
            and str(l.get("market_id")) == str(market_id)
        ):
            return True
    return False


def _is_already_processed_unlock_issue(locks, issue, city, date, market_id):
    """معادل _is_already_processed_lock_issue برای درخواست‌های حذف قفل (UNLOCK)."""
    issue_id = issue.get("id")
    number = issue.get("number")
    for l in locks:
        if issue_id and l.get("unlock_issue_id") == issue_id:
            return True
        if (
            l.get("repo") == GITHUB_REPOSITORY
            and l.get("unlock_issue_number") == number
            and l.get("city") == city
            and l.get("date") == date
            and str(l.get("market_id")) == str(market_id)
        ):
            return True
    return False


def process_lock_issues():
    locks = _load_locks()
    new_count = 0

    for issue in _fetch_open_issues():
        number = issue["number"]
        title = issue.get("title") or ""
        if not title.startswith("LOCK "):
            # این issue مربوط به قفل نیست (مثلاً یک باگ‌ریپورت دستی) --
            # نادیده گرفته می‌شود، بسته هم نمی‌شود.
            continue

        try:
            data = json.loads(issue["body"])
            market_id_raw = str(data["market_id"])
            market_id, _, parsed_token_id = market_id_raw.partition("|")
            token_id = str(data.get("token_id") or parsed_token_id or "").strip() or None
            city, date = data["city"], data["date"]
            dashboard_price = float(data["price"])
            market_type = data.get("market_type") or "max"  # (فاز ۶) پیش‌فرض "max"
        except Exception as e:
            _close_issue(
                number,
                f"\u26a0\ufe0f فرمت این درخواست قفل قابل خواندن نبود ({e}). لطفاً فقط از دکمهٔ «قفل کن» در simple.html استفاده کنید.",
            )
            continue

        if _is_already_processed_lock_issue(locks, issue, city, date, market_id):
            _close_issue(number, "\u2139\ufe0f این درخواست قفل قبلاً ثبت شده بود و بسته شد.")
            continue

        existing_open_lock = next(
            (
                l for l in locks
                if l.get("city") == city
                and l.get("date") == date
                and str(l.get("market_id")) == str(market_id)
                and l.get("status") == "open"
            ),
            None,
        )
        is_edit_request = bool(data.get("mode") == "edit" or data.get("update_existing"))

        if existing_open_lock is not None and not is_edit_request:
            _close_issue(number, "\u2139\ufe0f این باکت از قبل قفل باز دارد -- درخواست تکراری نادیده گرفته شد.")
            continue

        side = data.get("side", existing_open_lock.get("side", "YES") if existing_open_lock else "YES")

        entry_order_type = data.get("entry_order_type") or (
            "CONDITIONAL_LIMIT_GTC"
            if data.get("entry_mode") == "conditional" or data.get("limit_buy_price") is not None
            else "IMMEDIATE_MARKETABLE"
        )
        is_conditional = (entry_order_type == "CONDITIONAL_LIMIT_GTC")

        live_price = _fetch_live_entry_price(city, date, market_id, side, market_type)
        live_bid, _ = _fetch_live_exit_price(city, date, market_id, side, market_type, token_id)

        if is_conditional:
            limit_buy_price = round(
                float(data.get("limit_buy_price") or data.get("trigger_price") or dashboard_price),
                4,
            )
            entry_price = limit_buy_price
            entry_mid = live_price if live_price is not None else dashboard_price
            entry_bid = None
            position_state = "ENTRY_PENDING"
            price_source = "conditional_limit"
        else:
            limit_buy_price = None
            position_state = data.get("position_state") or "WATCHING"
            if live_price is not None:
                entry_price = live_price
                price_source = "live"
            else:
                entry_price = dashboard_price
                price_source = "dashboard_fallback"
            entry_mid = live_price if live_price is not None else entry_price
            entry_bid = live_bid

        is_advanced_lock = bool(
            data.get("auto_exit_enabled") is True
            or data.get("smart_autopilot") is True
            or data.get("tp_price") is not None
            or data.get("sl_price") is not None
            or is_conditional
            or is_edit_request
        )

        smart_autopilot = bool(data.get("smart_autopilot", False)) if is_advanced_lock else False
        auto_exit_enabled = bool(data.get("auto_exit_enabled", True)) if is_advanced_lock else False
        tp_enabled = bool(data.get("tp_enabled", True)) if auto_exit_enabled else False
        sl_enabled = bool(data.get("sl_enabled", True)) if auto_exit_enabled else False
        metar_auto_stop = bool(data.get("metar_auto_stop", True)) if auto_exit_enabled else False
        auto_sell_99c = bool(data.get("auto_sell_99c", True)) if auto_exit_enabled else False
        trailing_stop_enabled = bool(data.get("trailing_stop_enabled", False)) if auto_exit_enabled else False
        trailing_offset_pct = float(data.get("trailing_offset_pct") or 10.0)

        ref_entry = entry_price if entry_price and entry_price > 0 else 0.20
        if auto_exit_enabled or is_edit_request:
            if data.get("tp_price") is not None:
                tp_price = round(float(data["tp_price"]), 4)
            elif data.get("tp_cents") is not None:
                tp_price = round(float(data["tp_cents"]) / 100.0, 4)
            elif data.get("tp_pct") is not None:
                tp_price = min(0.99, round(ref_entry * (1.0 + float(data["tp_pct"]) / 100.0), 4))
            else:
                tp_price = min(0.99, round(ref_entry * 1.35, 4))

            if data.get("sl_price") is not None:
                sl_price = round(float(data["sl_price"]), 4)
            elif data.get("sl_cents") is not None:
                sl_price = round(float(data["sl_cents"]) / 100.0, 4)
            elif data.get("sl_pct") is not None:
                sl_price = max(0.01, round(ref_entry * (1.0 - abs(float(data["sl_pct"])) / 100.0), 4))
            else:
                sl_price = max(0.01, round(ref_entry * 0.65, 4))

            tp_pct = round(((tp_price - ref_entry) / ref_entry) * 100.0, 1) if ref_entry > 0 else 35.0
            sl_pct = round(((ref_entry - sl_price) / ref_entry) * 100.0, 1) if ref_entry > 0 else 35.0
            usdc_amount = float(data.get("usdc_amount") or 15.0)
            quantity_shares = float(
                data.get("quantity_shares")
                or (round(usdc_amount / ref_entry, 2) if ref_entry > 0 else 0.0)
            )
        else:
            tp_price = None
            sl_price = None
            tp_pct = None
            sl_pct = None
            usdc_amount = float(data["usdc_amount"]) if data.get("usdc_amount") is not None else None
            quantity_shares = float(data["quantity_shares"]) if data.get("quantity_shares") is not None else None

        if existing_open_lock is not None and is_edit_request:
            existing_open_lock["execution_mode"] = data.get("execution_mode") or existing_open_lock.get("execution_mode") or "SHADOW"
            existing_open_lock["exit_policy"] = data.get("exit_policy") or existing_open_lock.get("exit_policy") or "MARKETABLE_FAK"
            existing_open_lock["smart_autopilot"] = smart_autopilot
            existing_open_lock["auto_exit_enabled"] = auto_exit_enabled
            existing_open_lock["tp_enabled"] = tp_enabled
            existing_open_lock["sl_enabled"] = sl_enabled
            existing_open_lock["tp_price"] = tp_price
            existing_open_lock["sl_price"] = sl_price
            existing_open_lock["tp_pct"] = tp_pct
            existing_open_lock["sl_pct"] = sl_pct
            existing_open_lock["trailing_stop_enabled"] = trailing_stop_enabled
            existing_open_lock["trailing_offset_pct"] = trailing_offset_pct
            existing_open_lock["metar_auto_stop"] = metar_auto_stop
            existing_open_lock["auto_sell_99c"] = auto_sell_99c
            existing_open_lock["max_slippage_pct"] = float(data.get("max_slippage_pct") or existing_open_lock.get("max_slippage_pct") or 2.0)
            existing_open_lock["tick_size"] = float(data.get("tick_size") or existing_open_lock.get("tick_size") or 0.001)
            existing_open_lock["usdc_amount"] = usdc_amount
            existing_open_lock["quantity_shares"] = quantity_shares
            if existing_open_lock.get("position_state") == "ENTRY_PENDING" and is_conditional:
                existing_open_lock["limit_buy_price"] = limit_buy_price
                existing_open_lock["entry_price"] = limit_buy_price
            existing_open_lock["rule_version"] = int(existing_open_lock.get("rule_version") or 1) + 1
            existing_open_lock["last_edit_issue_number"] = number
            existing_open_lock["last_edit_issue_id"] = issue.get("id")
            _close_issue(
                number,
                f"\u2705 تنظیمات پوزیشن بروزرسانی شد (v{existing_open_lock['rule_version']}): {city} {date} | TP={round(tp_price*100,1)}¢, SL={round(sl_price*100,1)}¢",
            )
            new_count += 1
            continue

        entry = {
            "issue_number": number,
            "issue_id": issue.get("id"),
            "repo": GITHUB_REPOSITORY,
            "city": city,
            "date": date,
            "market_id": market_id,
            "market_type": market_type,  # (فاز ۶)
            "token_id": token_id,
            "side": side,
            "entry_price": entry_price,
            "entry_mid": entry_mid,
            "entry_bid": entry_bid,
            "entry_order_type": entry_order_type,
            "limit_buy_price": limit_buy_price,
            "position_state": position_state,
            "execution_mode": data.get("execution_mode") or "SHADOW",
            "exit_policy": data.get("exit_policy") or "MARKETABLE_FAK",
            "smart_autopilot": smart_autopilot,
            "auto_exit_enabled": auto_exit_enabled,
            "tp_enabled": tp_enabled,
            "sl_enabled": sl_enabled,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "trailing_stop_enabled": trailing_stop_enabled,
            "trailing_offset_pct": trailing_offset_pct,
            "metar_auto_stop": metar_auto_stop,
            "auto_sell_99c": auto_sell_99c,
            "max_slippage_pct": float(data.get("max_slippage_pct") or 2.0),
            "tick_size": float(data.get("tick_size") or 0.001),
            "usdc_amount": usdc_amount,
            "quantity_shares": quantity_shares,
            "filled_shares": 0.0 if is_conditional else quantity_shares,
            "peak_bid": entry_bid if not is_conditional else None,
            "rule_version": 1,
            "locked_at": data.get("locked_at", datetime.now(timezone.utc).isoformat()),
            "status": "open",
            "last_price": None,
            "last_pct": None,
            "exit_price": None,
            "closed_at": None,
            "close_reason": None,
            "actual_temp": None,
        }
        locks.append(entry)

        diff_note = ""
        if price_source == "conditional_limit":
            diff_note = f" (خرید شرطی در قیمت هدف {limit_buy_price:.3f})"
        elif price_source == "dashboard_fallback":
            diff_note = " (قیمت زنده در دسترس نبود -- از قیمت داشبورد استفاده شد)"
        elif abs(entry_price - dashboard_price) > 0.001:
            diff_note = f" (قیمت داشبورد لحظهٔ کلیک: {dashboard_price:.3f})"

        _close_issue(
            number,
            f"\u2705 قفل ثبت شد: {entry['city']} {entry['date']} ({entry['side']}) @ {entry['entry_price']:.3f}{diff_note}",
        )
        new_count += 1

    _save_locks(locks)
    return new_count


def process_unlock_issues():
    locks = _load_locks()
    closed_count = 0

    for issue in _fetch_open_issues():
        number = issue["number"]
        title = issue.get("title") or ""
        if not title.startswith("UNLOCK "):
            continue

        try:
            data = json.loads(issue["body"])
            city = data["city"]
            date = data["date"]
            market_id, _, _ = str(data["market_id"]).partition("|")
        except Exception as e:
            _close_issue(number, f"\u26a0\ufe0f فرمت این درخواست حذف قفل قابل خواندن نبود ({e}).")
            continue

        if _is_already_processed_unlock_issue(locks, issue, city, date, market_id):
            _close_issue(number, "\u2139\ufe0f این درخواست حذف قفل قبلاً اعمال شده بود و بسته شد.")
            continue

        target = None
        for l in locks:
            if l.get("city") == city and l.get("date") == date and str(l.get("market_id")) == market_id and l.get("status") == "open":
                target = l
                break

        if target is None:
            _close_issue(number, "\u26a0\ufe0f هیچ قفل بازی با این مشخصات پیدا نشد (شاید قبلاً حذف یا resolve شده).")
            continue

        if target.get("position_state") == "ENTRY_PENDING":
            target["status"] = "cancelled_entry"
            target["position_state"] = "ENTRY_CANCELLED"
            target["exit_price"] = None
            target["closed_at"] = datetime.now(timezone.utc).isoformat()
            target["close_reason"] = "cancelled_entry"
            target["unlock_issue_number"] = number
            target["unlock_issue_id"] = issue.get("id")
            target["repo"] = GITHUB_REPOSITORY
            _close_issue(
                number,
                f"\u2705 سفارش خرید شرطی لغو شد: {city} {date} (پیش از پر شدن سفارش)",
            )
            closed_count += 1
            continue

        # خروج باید از همان منبع قیمت ورود خوانده شود: Gamma API (قیمت
        # outcomePrices که در وب‌سایت Polymarket دیده می‌شود). استفادهٔ
        # قبلی از بهترین bid دفتر سفارش CLOB در بازارهای کم‌عمق می‌توانست
        # قیمت خروج نادرست/بسیار پایین ثبت کند. اگر Gamma موقتاً در دسترس
        # نبود، فقط به‌عنوان fallback امن به CLOB برمی‌گردیم تا حذف قفل
        # کاملاً مسدود نشود.
        # (تغییر ۴) خروج با قیمت فروش (bid). اگر نشد، همان منطق قبلی (قیمت
        # نمایشی Gamma، بعد bid دفتر سفارش) بدون تغییر اجرا می‌شود.
        current_price, exit_source = _fetch_live_exit_price(
            city, date, market_id, target.get("side", "YES"),
            target.get("market_type", "max"), target.get("token_id")
        )
        if current_price is None:
            current_price = _fetch_live_entry_price(
                city, date, market_id, target.get("side", "YES"), target.get("market_type", "max")
            )
            exit_source = "last" if current_price is not None else None
        if current_price is None:
            current_price = get_clob_book_bid(target.get("token_id"))
            exit_source = "clob_bid" if current_price is not None else None
        if current_price is None:
            _close_issue(
                number,
                "\u26a0\ufe0f نشد قیمت زندهٔ این باکت را بگیرم (ممکن است شبکه یا پلی‌مارکت موقتاً در دسترس نباشد). "
                "دوباره یک درخواست حذف قفل جدید بسازید تا مجدد تلاش شود.",
            )
            continue

        target["status"] = "closed_manual"
        target["position_state"] = "CLOSED"
        target["exit_price"] = current_price
        target["exit_price_source"] = exit_source
        target["closed_at"] = datetime.now(timezone.utc).isoformat()
        target["close_reason"] = "manual_unlock"
        target["unlock_issue_number"] = number
        target["unlock_issue_id"] = issue.get("id")
        target["repo"] = GITHUB_REPOSITORY

        entry_price = target["entry_price"]
        pct = (current_price - entry_price) / entry_price * 100 if entry_price else 0.0
        _close_issue(
            number,
            f"\u2705 قفل حذف شد: {city} {date} -- ورود {entry_price:.3f} -> خروج {current_price:.3f} ({pct:+.1f}%)",
        )
        closed_count += 1

    _save_locks(locks)
    return closed_count


if __name__ == "__main__":
    n_locked = process_lock_issues()
    n_unlocked = process_unlock_issues()
    locks = _load_locks()
    n_history = history_manager.write_history_csv(locks)
    print(f"[lock_manager] {n_locked} قفل جدید ثبت شد, {n_unlocked} قفل حذ�� شد, {n_history} ردیف در تاریخچه")
