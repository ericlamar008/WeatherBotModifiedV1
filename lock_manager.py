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

GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "USERNAME/weatherbot")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
API_BASE = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
LOCKS_FILE = Path("data/locked_signals.json")

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}


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


def _fetch_open_issues():
    """همهٔ Issueهای باز ریپو را برمی‌گرداند (بدون فیلتر لیبل -- به دلیل
    غیرقابل‌اتکا ��ودن پارامتر labels= در لینک ایجاد issue، دیگر روی آن
    تکیه نمی‌کنیم). تشخیص نوع درخواست بر عهدهٔ توابع فراخواننده است."""
    issues = []
    page = 1
    while True:
        r = requests.get(
            f"{API_BASE}/issues",
            headers=HEADERS,
            params={"state": "open", "per_page": 100, "page": page},
            timeout=15,
        )
        r.raise_for_status()
        batch = r.json()
        issues.extend(i for i in batch if "pull_request" not in i)
        if len(batch) < 100:
            break
        page += 1
    return issues


def _fetch_live_entry_price(city, date, market_id, side, market_type="max"):
    """قیمت زندهٔ واقعی این market_id را در همین لحظه از Gamma API می‌گیرد
    و بر اساس side (YES/NO) قیمت ورود درست را برمی‌گرداند. در هر شکستی
    (شبکه، نبودن market_id در پاسخ) None برمی‌گرداند تا فراخواننده به‌صورت
    ایمن به قیمت دکمهٔ داشبورد برگردد -- این تابع هرگز استثنا پرتاب نمی‌کند.

    (فاز ۶) پارامتر market_type تعیین می‌کند کدام رویداد Polymarket
    (highest- یا lowest-) خوانده شود."""
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        month = MONTHS[dt.month - 1]
        fetch_fn = get_gamma_event_prices_min if market_type == "min" else get_gamma_event_prices
        prices = fetch_fn(city, month, dt.day, dt.year)
        live_yes_price = prices.get(str(market_id))
        if live_yes_price is None:
            return None
        if side == "NO":
            return round(1.0 - live_yes_price, 4)
        return round(live_yes_price, 4)
    except Exception:
        return None


def _fetch_live_exit_price(city, date, market_id, side, market_type="max", token_id=None):
    """(تغییر ۴) قیمت فروش (bid) برای خروج دستی. در هر شکستی (None, None)
    برمی‌گرداند تا فراخواننده به منطق قبلی برگردد؛ هرگز استثنا پرتاب نمی‌کند."""
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        month = MONTHS[dt.month - 1]
        fetch_fn = get_gamma_event_quotes_min if market_type == "min" else get_gamma_event_quotes
        quotes = fetch_fn(city, month, dt.day, dt.year)
        quote = quotes.get(str(market_id))
        if not quote:
            return None, None
        return pick_sell_price(side, quote, token_id)
    except Exception:
        return None, None

def _close_issue(number, comment=None, extra_label="processed"):
    try:
        if comment:
            requests.post(
                f"{API_BASE}/issues/{number}/comments",
                headers=HEADERS,
                json={"body": comment},
                timeout=15,
            )
        requests.patch(
            f"{API_BASE}/issues/{number}",
            headers=HEADERS,
            json={"state": "closed"},
            timeout=15,
        )
        requests.post(
            f"{API_BASE}/issues/{number}/labels",
            headers=HEADERS,
            json={"labels": [extra_label]},
            timeout=15,
        )
    except Exception as e:
        print(f"[lock_manager] هشدار: نشد issue شماره {number} را ببندم: {e}")


def _has_open_lock(locks, city, date, market_id):
    return any(
        l.get("city") == city and l.get("date") == date
        and l.get("market_id") == market_id and l.get("status") == "open"
        for l in locks
    )


def process_lock_issues():
    locks = _load_locks()
    existing_numbers = {l.get("issue_number") for l in locks}
    new_count = 0

    for issue in _fetch_open_issues():
        number = issue["number"]
        if number in existing_numbers:
            continue

        title = issue.get("title") or ""
        if not title.startswith("LOCK "):
            # این issue مربوط به قفل نیست (مثلاً یک باگ‌ریپورت دستی) --
            # نادیده گرفته می‌شود، بسته هم نمی‌شود.
            continue

        try:
            data = json.loads(issue["body"])
            market_id_raw = str(data["market_id"])
            market_id, _, token_id = market_id_raw.partition("|")
            city, date = data["city"], data["date"]
            dashboard_price = float(data["price"])
            market_type = data.get("market_type", "max")  # (فاز ۶) پیش‌فرض "max"
        except Exception as e:
            _close_issue(
                number,
                f"\u26a0\ufe0f فرمت این درخواست قفل قابل خواندن نبود ({e}). لطفاً فقط از دکمهٔ «قفل کن» در simple.html استفاده کنید.",
            )
            continue

        if _has_open_lock(locks, city, date, market_id):
            _close_issue(number, "\u2139\ufe0f این باکت از قبل قفل باز دارد -- درخواست تکراری نادیده گرفته شد.")
            continue

        side = data.get("side", "YES")

        live_price = _fetch_live_entry_price(city, date, market_id, side, market_type)
        live_bid, _ = _fetch_live_exit_price(city, date, market_id, side, market_type, token_id or None)
        if live_price is not None:
            entry_price = live_price
            price_source = "live"
        else:
            entry_price = dashboard_price
            price_source = "dashboard_fallback"

        entry = {
            "issue_number": number,
            "city": city,
            "date": date,
            "market_id": market_id,
            "market_type": market_type,  # (فاز ۶)
            "token_id": token_id or None,
            "side": side,
            "entry_price": entry_price,
            "entry_mid": live_price if live_price is not None else entry_price,
            "entry_bid": live_bid,
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
        if price_source == "dashboard_fallback":
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
    already_unlocked_issue_numbers = {l.get("unlock_issue_number") for l in locks if l.get("unlock_issue_number")}
    closed_count = 0

    for issue in _fetch_open_issues():
        number = issue["number"]
        if number in already_unlocked_issue_numbers:
            continue

        title = issue.get("title") or ""
        if not title.startswith("UNLOCK "):
            continue

        try:
            data = json.loads(issue["body"])
            city = data["city"]
            date = data["date"]
            market_id = str(data["market_id"])
        except Exception as e:
            _close_issue(number, f"\u26a0\ufe0f فرمت این درخواست حذف قفل قابل خواندن نبود ({e}).")
            continue

        target = None
        for l in locks:
            if l.get("city") == city and l.get("date") == date and l.get("market_id") == market_id and l.get("status") == "open":
                target = l
                break

        if target is None:
            _close_issue(number, "\u26a0\ufe0f هیچ قفل بازی با این مشخصات پیدا نشد (شاید قبلاً حذف یا resolve شده).")
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
        target["exit_price"] = current_price
        target["exit_price_source"] = exit_source
        target["closed_at"] = datetime.now(timezone.utc).isoformat()
        target["close_reason"] = "manual_unlock"
        target["unlock_issue_number"] = number

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
