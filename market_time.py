"""مرجع مرکزی زمان روز محلی برای WeatherBet؛ بدون تماس شبکه و بدون تغییر state.

این فایل را با نام market_time.py در ریشهٔ ریپو قرار دهید. weatherbot_v3.py،
price_monitor.py و dashboard_simple.py هر سه این ماژول را import می‌کنند.
"""
from datetime import datetime, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def _as_utc(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def city_timezone(loc):
    if ZoneInfo is not None and (loc or {}).get("tz"):
        try:
            return ZoneInfo(loc["tz"])
        except Exception:
            pass
    return timezone.utc


def local_day_end_utc(date_str, loc):
    """ابتدای روز بعد در timezone شهر را به UTC برمی‌گرداند."""
    target_day = datetime.strptime(date_str, "%Y-%m-%d").date()
    local_end = datetime.combine(target_day + timedelta(days=1), time.min, tzinfo=city_timezone(loc))
    return local_end.astimezone(timezone.utc)


def local_day_status(date_str, loc, now=None):
    """پایان روز محلی را از settlement رسمی جدا نگه می‌دارد."""
    end_utc = local_day_end_utc(date_str, loc)
    remaining_seconds = (end_utc - _as_utc(now)).total_seconds()
    return {
        "kind": "local_day_open" if remaining_seconds > 0 else "awaiting_settlement",
        "remaining_seconds": max(0.0, remaining_seconds),
        "local_day_end_utc": end_utc,
    }


def format_remaining(seconds):
    hours, minutes = divmod(max(0, int(round(seconds / 60.0))), 60)
    return f"{hours}h {minutes}m remaining"


def local_day_display_status(date_str, loc, now=None):
    status = local_day_status(date_str, loc, now)
    return {
        **status,
        "display": format_remaining(status["remaining_seconds"])
        if status["kind"] == "local_day_open"
        else "awaiting official settlement",
    }


def is_near_local_day_end(date_str, loc, now=None, hours=3.0):
    status = local_day_status(date_str, loc, now)
    return status["kind"] == "local_day_open" and 0 < status["remaining_seconds"] <= hours * 3600.0
