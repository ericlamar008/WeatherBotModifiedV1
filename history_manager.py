"""
history_manager.py -- بستن خودکار قفل‌های resolve‌شده + ساخت تاریخچه و CSV.
=====================================================================================
کاملاً جدید و مستقل. resolution.py فعلی را فقط می‌خواند، هیچ تغییری در آن
نمی‌دهد و weatherbot_v3.py یا strategy.py را صدا نمی‌زند.

--- PATCH (رفع خطای واقعی "cannot pull with rebase: You have unstaged
changes") --------------------------------------------------------------------
مشاهده شد که بعد از اولین UNLOCK واقعی (بعد از این‌که چند LOCK بدون مشکل کار
کرده بودند)، Workflow ثبت قفل بلافاصله بعد از یک کامیت موفق با خطای
"You have unstaged changes" روی git pull --rebase شکست می‌خورد.

ریشهٔ علت: write_history_csv() فقط وقتی یک قفل واقعاً بسته می‌شود (یعنی فقط
روی UNLOCK یا resolve خودکار، نه روی LOCK) یک ردیف جدید به data/lock_history.csv
اضافه می‌کند. csv.writer پایتون به‌طور پیش‌فرض خطوط را با کاراکتر پایان خط
ویندوزی ("\\r\\n") می‌نویسد، نه لینوکسی ("\\n"). وقتی این فایل commit می‌شود،
عدم تطابق بین کاراکتر پایان خط واقعی روی دیسک و آنچه گیت هنگام نرمال‌سازی
متن انتظار دارد، می‌تواند بلافاصله بعد از یک کامیت موفق، فایل را دوباره
"تغییریافته" نشان دهد (یک پدیدهٔ شناخته‌شدهٔ گیت) -- که جلوی مرحلهٔ بعدی
(pull --rebase) در Workflow را می‌گیرد. چون LOCK هیچ‌وقت ردیف جدیدی به این
CSV اضافه نمی‌کند، این مشکل تا اولین UNLOCK واقعی خودش را نشان نمی‌داد.

راه‌حل: csv.writer اکنون صریحاً با lineterminator="\\n" فراخوانی می‌شود تا
فایل همیشه با کاراکتر پایان خط لینوکسی (همان چیزی که گیت انتظار دارد)
نوشته شود -- این ناهماهنگی را کاملاً از ریشه حذف می‌کند. هیچ تغییر دیگری
(محتوای ستون‌ها، ترتیب ردیف‌ها، انکودینگ utf-8-sig) اعمال نشده است.

--- PATCH (فاز ۶ نقشه‌راه -- بازار دمای کمینه) -------------------------------
افزوده شد: _load_market_min() و _load_market_for_lock() که بر اساس
l.get("market_type") (پیش‌فرض "max" برای سازگاری کامل با قفل‌های قدیمی‌تر
که این فیلد را ندارند) فایل مارکت درست را انتخاب می‌کند. در
check_and_close_resolved_locks، write_history_csv و get_history_rows،
هر جا قبلاً _load_market(city, date) مستقیم صدا زده می‌شد، حالا
_load_market_for_lock(l) صدا زده می‌شود، و در check_and_close_resolved_locks
تابع settlement هم بر اساس market_type انتخاب می‌شود
(res.get_polymarket_settlement_min در برابر res.get_polymarket_settlement).
هیچ تغییر دیگری در منطق/فرمت خروجی اعمال نشده است.
=====================================================================================
"""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

try:
    from locations import LOCATIONS, MONTHS
except ImportError:
    LOCATIONS, MONTHS = {}, [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ]

import math

try:
    import resolution as res
except Exception:
    res = None


def _round_half_up(val):
    if val >= 0:
        return int(math.floor(val + 0.5))
    return -int(math.floor(abs(val) + 0.5))


def _to_cents(price_val):
    if price_val is None:
        return None
    return _round_half_up(float(price_val) * 100.0)


def _calc_cent_consistent_pct(entry_price, exit_price):
    ec = _to_cents(entry_price)
    xc = _to_cents(exit_price)
    if ec is None or xc is None or ec <= 0:
        return ec, xc, None
    diff = xc - ec
    if diff == 0:
        return ec, xc, 0.0
    return ec, xc, round((diff / float(ec)) * 100.0, 1)


MARKETS_DIR = Path("data/markets")
CSV_FILE = Path("data/lock_history.csv")

CSV_HEADERS = [
    "تاریخ", "شهر", "دمای سیگنال", "دمای نهایی",
    "قیمت خرید (سنت)", "قیمت فروش (سنت)", "برآیند (%)",
]

def _load_market(city, date):
    p = MARKETS_DIR / f"{city}_{date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _load_market_min(city, date):
    """(فاز ۶) معادل _load_market ولی برای فایل بازار کمینه."""
    p = MARKETS_DIR / f"{city}_{date}_min.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _load_market_for_lock(l):
    """(فاز ۶) انتخاب خودکار فایل درست بر اساس market_type قفل (پیش‌فرض
    "max" برای سازگاری کامل با قفل‌های قدیمی‌تر که این فیلد را ندارند)."""
    city, date = l["city"], l["date"]
    if l.get("market_type") == "min":
        return _load_market_min(city, date)
    return _load_market(city, date)

def _find_range(market, market_id):
    if not market:
        return None
    for b in market.get("full_distribution", []):
        if str(b.get("market_id")) == str(market_id):
            return b.get("range")
    return None

def _label_for_range(rng, unit_sym):
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

def check_and_close_resolved_locks(locks, now=None):
    """
    قفل‌های بازی که بازارشان قبلاً resolve شده را می‌بندد. اگر پلی‌مارکت در
    دسترس نبود یا آن باکت خاص هنوز settle نشده، همان‌طور باز رها می‌شود.
    برمی‌گرداند تعداد قفل‌هایی که در همین دور بسته شدند.

    (فاز ۶) settlement_fn و market بر اساس market_type هر قفل انتخاب
    می‌شوند -- بدون تغییر رفتار برای قفل‌های حداکثر موجود.
    """
    closed_count = 0
    for l in locks:
        if l.get("status") != "open":
            continue

        city, date, market_id = l["city"], l["date"], str(l["market_id"])
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
        except Exception:
            continue

        settlement_fn = res.get_polymarket_settlement_min if l.get("market_type") == "min" else res.get_polymarket_settlement
        try:
            settlement = settlement_fn(city, MONTHS[dt.month - 1], dt.day, dt.year)
        except Exception:
            settlement = None

        if not settlement:
            continue
        if market_id not in settlement:
            continue

        won = settlement[market_id]
        market = _load_market_for_lock(l)
        actual_temp = None
        if market:
            try:
                actual_temp = res.get_actual_temp_from_settlement(market, settlement)
            except Exception:
                actual_temp = None

        l["status"] = "closed_resolved"
        l["exit_price"] = 1.0 if won else 0.0
        l["closed_at"] = (now or datetime.now(timezone.utc)).isoformat()
        l["close_reason"] = "auto_resolved"
        l["actual_temp"] = actual_temp
        closed_count += 1

    return closed_count

def write_history_csv(locks):
    """از همهٔ قفل‌های بسته‌شده (دستی یا خودکار) فایل CSV می‌سازد.
    lineterminator="\\n" صریحاً تنظیم شده تا کاراکتر پایان خط همیشه لینوکسی
    باشد -- رفع علت اصلی خطای "unstaged changes" بعد از commit (توضیح کامل
    در docstring بالای فایل)."""
    rows = []
    for l in locks:
        if l.get("status") not in ("closed_manual", "closed_resolved"):
            continue

        city = l["city"]
        city_name = LOCATIONS.get(city, {}).get("name", city)
        unit_sym = LOCATIONS.get(city, {}).get("unit", "")
        market = _load_market_for_lock(l)
        rng = _find_range(market, l["market_id"])
        signal_label = _label_for_range(rng, unit_sym)

        final_temp = l.get("actual_temp")
        final_temp_str = f"{final_temp}{unit_sym}" if final_temp is not None else ""

        buy_c, sell_c, pct = _calc_cent_consistent_pct(l.get("entry_price"), l.get("exit_price"))
        buy_cents = buy_c if buy_c is not None else ""
        sell_cents = sell_c if sell_c is not None else ""
        outcome_pct = pct if pct is not None else ""

        rows.append([l["date"], city_name, signal_label, final_temp_str, buy_cents, sell_cents, outcome_pct])

    rows.sort(key=lambda r: r[0], reverse=True)

    CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CSV_FILE.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(CSV_HEADERS)
        writer.writerows(rows)
    return len(rows)

def get_history_rows(locks):
    """همان دادهٔ CSV را به‌شکل لیست دیکشنری برمی‌گرداند -- برای رندر در simple.html."""
    out = []
    for l in locks:
        if l.get("status") not in ("closed_manual", "closed_resolved"):
            continue
        city = l["city"]
        city_name = LOCATIONS.get(city, {}).get("name", city)
        unit_sym = LOCATIONS.get(city, {}).get("unit", "")
        market = _load_market_for_lock(l)
        rng = _find_range(market, l["market_id"])
        signal_label = _label_for_range(rng, unit_sym)
        final_temp = l.get("actual_temp")
        buy_c, sell_c, outcome_pct = _calc_cent_consistent_pct(l.get("entry_price"), l.get("exit_price"))
        out.append({
            "date": l["date"],
            "city_name": city_name,
            "signal_label": signal_label,
            "final_temp": f"{final_temp}{unit_sym}" if final_temp is not None else "-",
            "buy_cents": buy_c,
            "sell_cents": sell_c,
            "outcome_pct": outcome_pct,
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out
