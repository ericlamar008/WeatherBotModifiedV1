"""
export_accuracy_report.py -- ساخت گزارش یکپارچهٔ دقت پیش‌بینی و عملکرد معاملاتی.
=====================================================================================
این فایل کاملاً جدید و مستقل است. هیچ فایل موجودی را نمی‌خواند/تغییر نمی‌دهد
به‌جز خواندن (فقط خواندن، read-only) از data/markets/*.json.

چرا لازم بود: در پروژه هیچ خروجی یکپارچه‌ای از دقت پیش‌بینی/عملکرد به تفکیک
شهر وجود نداشت -- هر بازار یک فایل JSON جدا بود. dashboard.py یک نسخهٔ
نمایشی از این محاسبه دارد (compute_city_forecast_accuracy) ولی هیچ‌جا آن
را به فایل خروجی نمی‌دهد.

خروجی: data/accuracy_report.csv -- یک ردیف به‌ازای هر بازار resolve‌شده
(چه سیگنال معاملاتی داشته چه نه -- چون هدف این گزارش سنجش دقت خالص مدل
هم هست، نه فقط سود/زیان معاملاتی).

نحوهٔ اجرا:
    python export_accuracy_report.py
یا اضافه‌کردنش به scan_daily.yml (اختیاری، بعداً با تأیید شما) تا خودکار
هر روز به‌روز شود.
"""
import csv
import json
from pathlib import Path

MARKETS_DIR = Path("data/markets")
OUTPUT_FILE = Path("data/accuracy_report.csv")

HEADERS = [
    "شهر", "تاریخ", "نوع بازار", "افق زمانی (روز)", "پیش‌بینی مدل", "سیگما", "دمای واقعی",
    "خطای مطلق", "بایاس علامت‌دار", "نتیجه", "سود/زیان (واحد)",
    "اطمینان", "احتمال موفقیت",
]


def _load_all_markets():
    out = []
    if not MARKETS_DIR.exists():
        print(f"[export_accuracy_report] پوشهٔ {MARKETS_DIR} پیدا نشد.")
        return out
    for f in MARKETS_DIR.glob("*.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[export_accuracy_report] هشدار: نشد {f.name} را بخوانم: {e}")
    return out


def build_accuracy_report():
    markets = _load_all_markets()
    rows = []

    for m in markets:
        # فقط بازارهایی که واقعاً resolve شده‌اند (چه سیگنال داشته چه نه) --
        # بازارهای هنوز باز یا expired_no_signal دمای واقعی معتبر ندارند.
        if m.get("status") not in ("resolved", "resolved_no_signal"):
            continue

        forecast = m.get("forecast_mean")
        actual = m.get("actual_temp")

        error = round(abs(forecast - actual), 2) if forecast is not None and actual is not None else ""
        signed_bias = round(actual - forecast, 2) if forecast is not None and actual is not None else ""

        outcome = m.get("resolved_outcome")
        outcome_display = outcome if outcome else "بدون سیگنال"

        rows.append([
            m.get("city_name", m.get("city", "")),
            m.get("date", ""),
            "کمینه" if m.get("market_type") == "min" else "بیشینه",
            m.get("horizon_days", ""),
            forecast if forecast is not None else "",
            m.get("sigma", ""),
            actual if actual is not None else "",
            error,
            signed_bias,
            outcome_display,
            m.get("pnl_units", "") if m.get("pnl_units") is not None else "",
            m.get("confidence", "") if m.get("confidence") is not None else "",
            m.get("success_probability", "") if m.get("success_probability") is not None else "",
        ])

    # جدیدترین تاریخ اول -- برای مرور راحت‌تر
    rows.sort(key=lambda r: r[1], reverse=True)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)
        writer.writerows(rows)

    return len(rows)


if __name__ == "__main__":
    n = build_accuracy_report()
    print(f"[export_accuracy_report] {n} ردیف در {OUTPUT_FILE} نوشته شد.")
    if n == 0:
        print("[export_accuracy_report] هیچ بازار resolve‌شده‌ای پیدا نشد -- "
              "مطمئن شوید این اسکریپت را در همان پوشه‌ای اجرا می‌کنید که data/markets/ در آن است.")
