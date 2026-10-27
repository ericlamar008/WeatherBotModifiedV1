"""
daily_report.py -- ارسال گزارش روزانهٔ سود/زیان به تلگرام (فقط با اجرای
دستی یا زمان‌بندی مستقل خودش، بدون هیچ وابستگی به سایر بخش‌های ربات).
=====================================================================================
کاملاً جدید و مستقل -- data/locked_signals.json را فقط می‌خواند، هیچ
تغییری در آن یا هیچ فایل دیگری نمی‌دهد، و هیچ ماژول دیگری از ربات را
import نمی‌کند (به‌جز خواندن مستقیم فایل JSON). ریسک تداخل با بقیهٔ ربات
صفر است.

منطق: همان محاسبه‌ای که در سکشن «گزارش روزانه»ی dashboard_simple.py هست
(میانگین درصد برآیند هر قفل بسته‌شده، گروه‌بندی‌شده بر اساس روز واقعی
بسته‌شدن closed_at به وقت ایران -- نه تاریخ هدف بازار)، اینجا هم عیناً و
مستقل تکرار شده تا این اسکریپت به‌تنهایی و بدون وابستگی به سایر فایل‌های
پایتون ربات قابل اجرا باشد. فقط آخرین روزی که واقعاً معامله‌ای در آن
بسته شده را در پیام تلگرام گز��رش می‌کند (بدون اطلاعات اضافی -- فقط
تاریخ و درصد، طبق درخواست کاربر).
"""
import json
import os
from datetime import datetime
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
    IRAN_TZ = ZoneInfo("Asia/Tehran")
except Exception:
    IRAN_TZ = None

try:
    from locations import LOCATIONS, MONTHS
except ImportError:
    LOCATIONS, MONTHS = {}, [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ]

LOCKS_FILE = Path("data/locked_signals.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def _load_locks():
    if not LOCKS_FILE.exists():
        return []
    try:
        return json.loads(LOCKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _daily_pnl():
    """همان منطق سکشن داشبورد -- برای هر روز تقویمی (ایران) که در آن
    حداقل یک قفل واقعاً بسته شده، هم لیست تک‌تک قفل‌ها (l) و هم درصدشان
    را برمی‌گرداند -- تا هم برآیند کل، هم جزئیات هر بازار قابل‌گزارش
    باشد."""
    locks = _load_locks()
    daily = {}
    for l in locks:
        if l.get("status") not in ("closed_manual", "closed_resolved"):
            continue
        closed_at = l.get("closed_at")
        entry = l.get("entry_price")
        exit_price = l.get("exit_price")
        if not closed_at or not entry or exit_price is None:
            continue
        try:
            dt_utc = datetime.fromisoformat(closed_at)
        except Exception:
            continue
        if IRAN_TZ is not None:
            try:
                local_date = dt_utc.astimezone(IRAN_TZ).strftime("%Y-%m-%d")
            except Exception:
                local_date = dt_utc.strftime("%Y-%m-%d")
        else:
            local_date = dt_utc.strftime("%Y-%m-%d")
        daily.setdefault(local_date, []).append(l)
    return daily


def _load_market_for_lock(l):
    """(درخواست کاربر) بارگذاری فایل بازار مربوط به یک قفل بسته‌شده --
    برای استخراج برچسب دقیق باکت، دقیقاً مشابه چیزی که در پیام تلگرام
    نیم‌ساعته (price_monitor.py) استفاده می‌شود."""
    city, date = l.get("city"), l.get("date")
    suffix = "_min" if l.get("market_type") == "min" else ""
    p = Path("data/markets") / f"{city}_{date}{suffix}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _find_range(market, market_id):
    if not market:
        return None
    for b in market.get("full_distribution", []) or []:
        if str(b.get("market_id")) == str(market_id):
            return b.get("range")
    return None


def _label_for_range(rng, unit_sym):
    """عیناً همان تابع _label_for_range در price_monitor.py."""
    if not rng:
        return "?"
    low, high = rng
    if low <= -998 and high >= 998:
        return f"?{unit_sym}"
    if low <= -998:
        return f"\u2264{high}{unit_sym}"
    if high >= 998:
        return f"\u2265{low}{unit_sym}"
    if low == high:
        return f"{low}{unit_sym}"
    return f"{low}-{high}{unit_sym}"


def _build_polymarket_url(l):
    """(درخواست کاربر) لینک بازار Polymarket -- عیناً معادل
    _build_polymarket_url/_build_polymarket_url_min در price_monitor.py،
    بر اساس market_type همان قفل."""
    city, date_str, is_min = l.get("city"), l.get("date"), l.get("market_type") == "min"
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        month = MONTHS[dt.month - 1]
        kind = "lowest" if is_min else "highest"
        return f"https://polymarket.com/event/{kind}-temperature-in-{city}-on-{month}-{dt.day}-{dt.year}"
    except Exception:
        return "https://polymarket.com"


def _time_range_str(l):
    """(درخواست کاربر) بازهٔ زمانی «از ساعت X تا ساعت Y» به وقت ایران،
    از locked_at تا closed_at."""
    locked_at, closed_at = l.get("locked_at"), l.get("closed_at")
    if not locked_at or not closed_at:
        return "-"
    try:
        dt_from = datetime.fromisoformat(locked_at)
        dt_to = datetime.fromisoformat(closed_at)
        if IRAN_TZ is not None:
            dt_from = dt_from.astimezone(IRAN_TZ)
            dt_to = dt_to.astimezone(IRAN_TZ)
        return f"{dt_from.strftime('%H:%M')} \u2192 {dt_to.strftime('%H:%M')}"
    except Exception:
        return "-"


import math

def _round_half_up(val):
    if val >= 0:
        return int(math.floor(val + 0.5))
    return -int(math.floor(abs(val) + 0.5))


def _to_cents(price_val):
    if price_val is None:
        return None
    return _round_half_up(float(price_val) * 100.0)


def _calc_cent_consistent_pct(entry_price, exit_price):
    entry_c = _to_cents(entry_price)
    exit_c = _to_cents(exit_price)
    if entry_c is None or exit_c is None or entry_c <= 0:
        return entry_c, exit_c, None, None
    diff_c = exit_c - entry_c
    if diff_c == 0:
        return entry_c, exit_c, 0.0, 0
    pct_exact = (diff_c / float(entry_c)) * 100.0
    return entry_c, exit_c, round(pct_exact, 1), _round_half_up(pct_exact)


def build_report_text():
    """(درخواست کاربر) علاوه بر برآیند کلی، اطلاعات کامل هر بازار/قفل
    همان روز گروه‌بندی‌شده بر اساس شهر گزارش می‌شود -- هم‌سبک با پیام
    تلگرام نیم‌ساعتهٔ price_monitor.py."""
    daily = _daily_pnl()
    if not daily:
        return "\U0001F4C5 گزارش روزانه\n\nهنوز هیچ معامله‌ای بسته نشده -- چیزی برای گزارش نیست."

    latest_date = max(daily.keys())
    locks_today = daily[latest_date]

    by_city = {}
    for l in locks_today:
        by_city.setdefault((l.get("city"), l.get("date")), []).append(l)

    pcts = []
    city_blocks = []
    for (city, date), group in sorted(by_city.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        name = LOCATIONS.get(city, {}).get("name", city)
        link = _build_polymarket_url(group[0])
        block_lines = [f'<a href="{link}">{name}</a> \u2014 {date}']

        for l in group:
            entry = l.get("entry_price")
            exit_price = l.get("exit_price")
            entry_c, exit_c, pct, pct_int = _calc_cent_consistent_pct(entry, exit_price)
            if pct is not None:
                pcts.append(pct)

            market = _load_market_for_lock(l)
            unit_sym = market.get("unit", "") if market else ""
            forecast_mean = market.get("forecast_mean") if market else None
            rng = _find_range(market, l.get("market_id"))
            label = _label_for_range(rng, unit_sym)

            reason = "resolve خودکار" if l.get("status") == "closed_resolved" else "حذف دستی"
            entry_str = f"{entry_c}\u00a2" if entry_c is not None else "-"
            exit_str = f"{exit_c}\u00a2" if exit_c is not None else "-"
            pct_sign = "+" if (pct_int or 0) >= 0 else ""
            pct_str = f"{pct_sign}{pct_int}%" if pct_int is not None else "-"
            color = "\U0001F7E2" if (pct or 0) >= 0 else "\U0001F534"

            block_lines.append(f" {color} {label}: {entry_str} \u2192 {exit_str} ({pct_str})")
            if forecast_mean is not None:
                block_lines.append(f"    \U0001F321 پیش\u200cبینی مدل: {forecast_mean:.1f}{unit_sym}")
            block_lines.append(f"    \u23F1 {_time_range_str(l)} ({reason})")

        city_blocks.append("\n".join(block_lines))

    avg_pct = sum(pcts) / len(pcts) if pcts else 0.0
    sign = "\U0001F7E2" if avg_pct >= 0 else "\U0001F534"
    header = f"\U0001F4C5 گزارش روزانه \u2014 {latest_date}\n\n{sign} برآیند کل: {avg_pct:+.1f}% ({len(locks_today)} معامله)"
    return header + "\n\n" + "\n\n".join(city_blocks)


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[daily_report] توکن یا چت آیدی تلگرام تنظیم نشده -- پیام فقط چاپ می���شود:\n", text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except Exception as e:
        print(f"[daily_report] هشدار: ارسال تلگرام ناموفق بود: {e}")


if __name__ == "__main__":
    report_text = build_report_text()
    send_telegram(report_text)
    print(report_text)
