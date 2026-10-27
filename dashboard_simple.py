"""
dashboard_simple.py -- می‌سازد simple.html (و docs/simple.html):
نسخهٔ کامل، موبایل‌فرندلی و همگام‌شده با پیش‌نمایش AI Studio شامل:
  ۱) نوار ابزار بالایی با دکمهٔ «↻ بروزرسانی» و سوییچر بین ۳ تب:
     - داشبورد ساده (Simple)
     - داشبورد پیشرفته (v3 Pro)
     - 📊 دقت شهرها و کالیبراسیون هوشمند
  ۲) نمایش دقیق تغییرات درصدی در داخل پرانتز (با رنگ سبز/قرمز) برای هر ۳ ستون:
     - احتمال مدل (±X.X%)
     - احتمال بازار YES (±X.X%)
     - قیمت YES (±X.X%)
     در تمامی اسکن‌ها (سنگین ۶ساعته و سبک ۱۰ دقیقه‌ای).
  ۳) گیت کالیبراسیون هوشمند (Profit-Only Gate)، ترمز چرخش فصل، ترکیب هوشمند
     زیرمدل‌ها (ECMWF, GFS, GEM, ICON) و نشان وضعیت فرم اخیر هر شهر.
"""
import csv
import json
import math
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import history_manager

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from market_time import local_day_status

try:
    from locations import LOCATIONS, MONTHS
except ImportError:
    LOCATIONS, MONTHS = {}, [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ]

GITHUB_REPO = "ericlamar008/weatherbot"
DATA_DIR = Path("data")
MARKETS_DIR = DATA_DIR / "markets"
LOCKS_FILE = DATA_DIR / "locked_signals.json"
ACCURACY_CSV = DATA_DIR / "accuracy_report.csv"
CALIBRATION_FILE = DATA_DIR / "calibration.json"
LAST_SCAN_FILE = DATA_DIR / "last_scan.json"
OUTPUT_FILE = Path("simple.html")
DOCS_OUTPUT_FILE = Path("docs/simple.html")
IRAN_TZ_NAME = "Asia/Tehran"

RESOLVED_LIKE_STATUSES = {"resolved", "resolved_no_signal", "expired_no_signal"}
BUCKET_PLAUSIBILITY_MIN = 0.05
EV_GATE_THRESHOLD = 0.10

US_CITIES = {
    "atlanta", "austin", "chicago", "dallas", "denver",
    "houston", "los-angeles", "miami", "nyc",
}

NAME_TO_SLUG = {loc["name"].lower(): slug for slug, loc in LOCATIONS.items()}


def _get_city_slug_from_name(name_str):
    low = (name_str or "").strip().lower()
    if low in NAME_TO_SLUG:
        return NAME_TO_SLUG[low]
    return low.replace(" ", "-")


def _is_exact_bucket_hit(forecast, actual, is_fahrenheit):
    if is_fahrenheit:
        return abs(forecast - actual) <= 1.0
    return round(forecast) == round(actual)


def _is_within_one_bucket_hit(forecast, actual, is_fahrenheit):
    if is_fahrenheit:
        return abs(forecast - actual) <= 2.5
    return abs(forecast - actual) <= 1.25


def _student_t_cdf5(t):
    s5 = math.sqrt(5.0)
    z = t / s5
    denom = 1.0 + z * z
    exact = 0.5 + (1.0 / math.pi) * (math.atan(z) + (z / denom) * (1.0 + 2.0 / (3.0 * denom)))
    return max(0.0001, min(0.9999, exact))


def _build_city_form_profiles(all_markets):
    """تحلیل ۱۴ روز اخیر در برابر تاریخچهٔ قدیمی‌تر، تشخیص چرخش فصل،
    مقایسهٔ کالیبره با خام (Shadow) و زیرمدل‌ها برای هر ۲۶ شهر."""
    acc_records = []
    if ACCURACY_CSV.exists():
        try:
            with ACCURACY_CSV.open("r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                headers = next(reader, None)
                for cols in reader:
                    if len(cols) >= 9 and cols[4] != "" and cols[6] != "":
                        city_name = cols[0].strip()
                        slug = _get_city_slug_from_name(city_name)
                        mtype = "min" if cols[2].strip() == "کمینه" else "max"
                        try:
                            f_val = float(cols[4])
                            a_val = float(cols[6])
                            abs_err = float(cols[7]) if cols[7] != "" else abs(f_val - a_val)
                        except ValueError:
                            continue
                        acc_records.append({
                            "city": slug,
                            "city_name": LOCATIONS.get(slug, {}).get("name", city_name),
                            "date": cols[1].strip(),
                            "market_type": mtype,
                            "forecast_mean": f_val,
                            "actual_temp": a_val,
                            "abs_error": abs_err,
                            "signed_bias": a_val - f_val,
                        })
        except Exception:
            pass

    market_by_key = {}
    for m in all_markets:
        mtype = "min" if m.get("market_type") == "min" else "max"
        market_by_key[(m.get("city"), m.get("date"), mtype)] = m
        if m.get("status") in RESOLVED_LIKE_STATUSES and m.get("forecast_mean") is not None and m.get("actual_temp") is not None:
            exists = any(
                r["city"] == m.get("city") and r["date"] == m.get("date") and r["market_type"] == mtype
                for r in acc_records
            )
            if not exists:
                f_val = float(m["forecast_mean"])
                a_val = float(m["actual_temp"])
                acc_records.append({
                    "city": m.get("city"),
                    "city_name": LOCATIONS.get(m.get("city"), {}).get("name", m.get("city_name", m.get("city"))),
                    "date": m.get("date"),
                    "market_type": mtype,
                    "forecast_mean": f_val,
                    "actual_temp": a_val,
                    "abs_error": abs(f_val - a_val),
                    "signed_bias": a_val - f_val,
                })

    grouped = defaultdict(list)
    for r in acc_records:
        grouped[(r["city"], r["market_type"])].append(r)

    profiles = {}
    for (slug, mtype), records in grouped.items():
        records.sort(key=lambda x: x["date"])
        is_us = slug in US_CITIES
        unit = "F" if is_us else "C"
        recent_cutoff = "2026-10-10"
        old_cutoff = "2026-10-05"
        recent = [r for r in records if r["date"] >= recent_cutoff]
        older = [r for r in records if r["date"] < old_cutoff]
        active_recent = recent if len(recent) >= 3 else records[-14:]

        r_hits = sum(1 for r in active_recent if _is_exact_bucket_hit(r["forecast_mean"], r["actual_temp"], is_us))
        r_w1 = sum(1 for r in active_recent if _is_within_one_bucket_hit(r["forecast_mean"], r["actual_temp"], is_us))
        r_mae = sum(r["abs_error"] for r in active_recent) / max(1, len(active_recent))
        r_bias = sum(r["signed_bias"] for r in active_recent) / max(1, len(active_recent))

        recent_hit_pct = round(r_hits / len(active_recent) * 100, 1) if active_recent else 0.0
        recent_w1_pct = round(r_w1 / len(active_recent) * 100, 1) if active_recent else 0.0

        old_hit_pct = None
        old_mae = None
        old_bias = None
        if len(older) >= 3:
            o_hits = sum(1 for r in older if _is_exact_bucket_hit(r["forecast_mean"], r["actual_temp"], is_us))
            old_hit_pct = round(o_hits / len(older) * 100, 1)
            old_mae = round(sum(r["abs_error"] for r in older) / len(older), 2)
            old_bias = round(sum(r["signed_bias"] for r in older) / len(older), 2)

        hit_delta = round(recent_hit_pct - old_hit_pct, 1) if old_hit_pct is not None else None

        # Shadow & Sub-models
        shadow_errs, cal_errs = [], []
        shadow_hits, cal_hits = 0, 0
        submodel_errs = defaultdict(list)

        for r in records:
            if r["date"] < "2026-09-25":
                continue
            mkt = market_by_key.get((slug, r["date"], mtype))
            if not mkt:
                continue
            actual = r["actual_temp"]
            f_cal = mkt.get("forecast_mean_legacy", mkt.get("forecast_mean"))
            f_sh = mkt.get("forecast_mean_shadow")
            if f_cal is not None and f_sh is not None:
                c_e = abs(float(f_cal) - actual)
                s_e = abs(float(f_sh) - actual)
                cal_errs.append(c_e)
                shadow_errs.append(s_e)
                if _is_exact_bucket_hit(float(f_cal), actual, is_us):
                    cal_hits += 1
                if _is_exact_bucket_hit(float(f_sh), actual, is_us):
                    shadow_hits += 1
            models_raw = mkt.get("model_means_raw") or {}
            for mname, mval in models_raw.items():
                if mval is not None:
                    submodel_errs[mname].append(abs(float(mval) - actual))

        raw_shadow_mae = round(sum(shadow_errs) / len(shadow_errs), 2) if len(shadow_errs) >= 2 else None
        calibrated_mae = round(sum(cal_errs) / len(cal_errs), 2) if len(cal_errs) >= 2 else None
        raw_shadow_hit_pct = round(shadow_hits / len(shadow_errs) * 100, 1) if len(shadow_errs) >= 2 else None
        calibrated_hit_pct = round(cal_hits / len(cal_errs) * 100, 1) if len(cal_errs) >= 2 else None

        submodel_maes = {}
        submodel_weights = {}
        best_submodel, worst_submodel = None, None
        best_mae, worst_mae = 999.0, -1.0
        for mname, errs in submodel_errs.items():
            if len(errs) >= 2:
                m_mae = round(sum(errs) / len(errs), 2)
                submodel_maes[mname] = m_mae
                if m_mae < best_mae:
                    best_mae = m_mae
                    best_submodel = mname
                if m_mae > worst_mae:
                    worst_mae = m_mae
                    worst_submodel = mname

        if submodel_maes:
            inv_sum = sum(1.0 / max(0.25, v * v) for v in submodel_maes.values())
            for mname, v in submodel_maes.items():
                submodel_weights[mname] = round((1.0 / max(0.25, v * v)) / inv_sum, 3)

        sign_flip = bool(
            old_bias is not None and abs(old_bias) >= 0.45 and abs(r_bias) >= 0.45 and (old_bias * r_bias < 0)
        )

        gate_decision = "use_calibrated"
        gate_reason = "کالیبراسیون فعلی دقیق و سودده است"
        if raw_shadow_mae is not None and calibrated_mae is not None and calibrated_mae > raw_shadow_mae + 0.08:
            gate_decision = "revert_to_shadow"
            gate_reason = f"گیت سود-محور: مدل خام (MAE={raw_shadow_mae}°) دقیق‌تر از کالیبره ({calibrated_mae}°) بود → بازگشت خودکار به خام"
        elif sign_flip:
            gate_decision = "sign_flip_damped"
            gate_reason = f"چرخش فصل شناسایی شد (بایاس قدیم {old_bias:+.2f}° در برابر جدید {r_bias:+.2f}°) → تعدیل بایاس قدیمی"
        elif best_submodel and best_mae + 0.25 < r_mae:
            gate_decision = "smart_submodel_blend"
            gate_reason = f"وزن‌دهی هوشمند زیرمدل‌ها (برترین مدل اخیر: {best_submodel.upper()} با خطای {best_mae}°)"

        if recent_hit_pct >= 60:
            form_status = "hot"
            form_label = f"🔥 روی فرم ({recent_hit_pct}%)"
        elif hit_delta is not None and hit_delta <= -20:
            form_status = "cold_drop"
            form_label = f"⚠️ افت اخیر ({recent_hit_pct}% vs {old_hit_pct}% قدیم)"
        elif recent_hit_pct >= 45:
            form_status = "stable"
            form_label = f"✅ باثبات ({recent_hit_pct}%)"
        else:
            form_status = "weak"
            form_label = f"❄️ ضعیف ({recent_hit_pct}%)"

        profiles[(slug, mtype)] = {
            "city": slug,
            "city_name": LOCATIONS.get(slug, {}).get("name", slug),
            "market_type": mtype,
            "unit": unit,
            "is_us": is_us,
            "recent_n": len(active_recent),
            "recent_hit_pct": recent_hit_pct,
            "recent_within1_pct": recent_w1_pct,
            "recent_mae": round(r_mae, 2),
            "recent_bias": round(r_bias, 2),
            "old_n": len(older),
            "old_hit_pct": old_hit_pct,
            "old_mae": old_mae,
            "old_bias": old_bias,
            "hit_delta_pct": hit_delta,
            "form_status": form_status,
            "form_label": form_label,
            "raw_shadow_mae": raw_shadow_mae,
            "calibrated_mae": calibrated_mae,
            "raw_shadow_hit_pct": raw_shadow_hit_pct,
            "calibrated_hit_pct": calibrated_hit_pct,
            "gate_decision": gate_decision,
            "gate_reason": gate_reason,
            "sign_flip_detected": sign_flip,
            "best_submodel": best_submodel,
            "worst_submodel": worst_submodel,
            "submodel_maes": submodel_maes,
            "submodel_weights": submodel_weights,
        }

    return profiles


def _enrich_market_with_smart_gate(m, profile):
    m["city_form"] = profile
    f_cal = m.get("forecast_mean")
    f_sh = m.get("forecast_mean_shadow")
    models_raw = m.get("model_means_raw") or {}
    sigma = float(m.get("sigma") or (1.8 if m.get("unit") == "F" else 1.2))
    smart_mean = f_cal

    if profile and f_cal is not None:
        gd = profile["gate_decision"]
        if gd == "revert_to_shadow" and f_sh is not None:
            smart_mean = round(float(f_sh), 2)
        elif gd == "smart_submodel_blend" and models_raw:
            w_sum, v_sum = 0.0, 0.0
            for mk, mv in models_raw.items():
                if mv is not None:
                    w = profile["submodel_weights"].get(mk, 0.25)
                    v_sum += float(mv) * w
                    w_sum += w
            if w_sum > 0:
                smart_mean = round(0.5 * float(f_cal) + 0.5 * (v_sum / w_sum), 2)
        elif gd == "sign_flip_damped":
            if f_sh is not None:
                smart_mean = round(0.5 * float(f_cal) + 0.5 * float(f_sh), 2)
            else:
                smart_mean = round(float(f_cal) + profile["recent_bias"] * 0.4, 2)

    m["smart_forecast_mean"] = smart_mean
    m["smart_sigma"] = sigma

    full_dist = m.get("full_distribution") or []
    if full_dist and smart_mean is not None:
        raw_probs = []
        for b in full_dist:
            rng = b.get("range") or [-999, 999]
            low, high = rng[0], rng[1]
            c_high = 1.0 if high >= 998 else _student_t_cdf5((high + 0.5 - smart_mean) / sigma)
            c_low = 0.0 if low <= -998 else _student_t_cdf5((low - 0.5 - smart_mean) / sigma)
            raw_probs.append(max(0.0005, c_high - c_low))
        p_sum = sum(raw_probs) or 1.0
        best_id, best_belief = None, -1.0
        for idx, b in enumerate(full_dist):
            smp = round(raw_probs[idx] / p_sum, 4)
            yp = float(b.get("yes_price") or b.get("ask") or 0.001)
            sbp = round(0.5 * smp + 0.5 * yp, 4)
            sev = round((sbp - yp) / yp, 4) if yp > 0.001 else b.get("ev")
            b["smart_model_prob"] = smp
            b["smart_belief_prob"] = sbp
            b["smart_ev"] = sev
            if sbp > best_belief:
                best_belief = sbp
                best_id = str(b.get("market_id"))
        m["smart_main_market_id"] = best_id
        for b in full_dist:
            b["is_smart_main"] = (str(b.get("market_id")) == best_id)

    return m


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive, nosnippet, noimageindex">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<script>
(function(){
  try {
    var url = new URL(window.location.href);
    if (!url.searchParams.has("_r")) {
      url.searchParams.set("_r", Date.now().toString());
      window.location.replace(url.toString());
    }
  } catch (e) {}
})();
</script>
<title>WeatherBet -- سامانه هوشمند پیش‌بینی دما</title>
<style>
* { box-sizing: border-box; }
:root{
  --bg: #eef1f6;
  --surface: #ffffff;
  --surface-2: #f6f8fb;
  --border: #dde3ec;
  --text: #1f2430;
  --text-dim: #6b7684;
  --accent: #2563eb;
  --accent-dim: #eaf1ff;
  --green: #16a34a;
  --green-bg: #e8f8ee;
  --red: #dc2626;
  --red-bg: #fdeceb;
  --amber: #b45309;
  --amber-bg: #fef3e0;
  --blue: #2563eb;
  --blue-bg: #eaf1ff;
  --purple: #9333ea;
  --purple-bg: #f3e8ff;
}
body{
  background:var(--bg);color:var(--text);
  font-family:"Vazirmatn","IRANSans","Segoe UI",Tahoma,Arial,sans-serif;
  padding:0;margin:0;font-size:14.5px;line-height:1.55;
  -webkit-font-smoothing:antialiased;
}
.top-navbar{
  position:sticky;top:0;z-index:100;
  background:#14171f;border-bottom:1px solid #262b38;color:#fff;
  padding:10px 14px;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;
  box-shadow:0 2px 10px rgba(0,0,0,0.18);
}
.brand-wrap{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.brand-title{font-weight:700;font-size:15px;color:#fff}
.brand-meta{font-size:11.5px;color:#9aa0a6}
.nav-tabs{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.tab-btn{
  border:1px solid #2e3444;background:#1e222d;color:#cbd5e1;
  border-radius:8px;padding:7px 12px;font-size:12px;font-weight:600;cursor:pointer;
  font-family:inherit;transition:all .15s ease;
}
.tab-btn.active-blue{background:#2563eb;color:#fff;border-color:#3b82f6}
.tab-btn.active-purple{background:#9333ea;color:#fff;border-color:#a855f7}
.refresh-btn{
  border:1px solid #16a34a88;background:#16a34a22;color:#4ade80;
  border-radius:8px;padding:7px 12px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;
}
.refresh-btn:hover{background:#16a34a44}
.container{max-width:1280px;margin:0 auto;padding:14px}
h1{font-size:19px;margin:6px 0 8px;font-weight:700;color:var(--text)}
h2{font-size:15.5px;margin-top:26px;border-bottom:1px solid var(--border);padding-bottom:8px;scroll-margin-top:64px;font-weight:700}
.smart-bar{
  background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:10px 14px;margin-bottom:12px;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;
}
.smart-toggles{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.toggle-label{
  display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;
  padding:6px 10px;border-radius:8px;border:1px solid var(--border);background:var(--surface-2);cursor:pointer;
}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
.toolbar input[type=text]{flex:1;min-width:140px;background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:10px;padding:10px 14px;font-size:13.5px}
.jump-btn{display:inline-block;background:var(--accent);color:#fff;border-radius:10px;padding:9px 14px;font-size:12.5px;font-weight:600;text-decoration:none;white-space:nowrap}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
details.city-block{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:8px 12px;margin-bottom:10px;box-shadow:0 1px 3px rgba(20,30,60,0.05)}
details.date-block{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:8px 12px;margin:8px 0}
summary{cursor:pointer;font-size:14px;color:var(--text);list-style:none;padding:7px 4px;font-weight:500}
summary::-webkit-details-marker{display:none}
summary::before{content:"";display:inline-block;width:0;height:0;border-top:4px solid transparent;border-bottom:4px solid transparent;border-left:5px solid var(--text-dim);margin-left:8px;vertical-align:middle;transition:transform .12s ease}
details[open]>summary::before{transform:rotate(90deg)}
.main-badge{background:var(--green-bg);color:var(--green);border:1px solid #16a34a55;border-radius:6px;padding:2px 8px;font-size:10.5px;font-weight:600;white-space:nowrap;margin-right:5px}
.form-badge{border-radius:6px;padding:2px 8px;font-size:10.5px;font-weight:600;white-space:nowrap;margin-right:5px;border:1px solid}
.form-hot{background:var(--green-bg);color:var(--green);border-color:#16a34a55}
.form-stable{background:var(--blue-bg);color:var(--blue);border-color:#2563eb44}
.form-cold_drop{background:var(--amber-bg);color:var(--amber);border-color:#b4530955}
.form-weak{background:var(--red-bg);color:var(--red);border-color:#dc262644}
.gate-badge{background:var(--purple-bg);color:var(--purple);border:1px solid #9333ea44;border-radius:6px;padding:2px 7px;font-size:10px;font-weight:600;margin-right:5px}
.signal-badge{border-radius:6px;padding:2px 8px;font-size:10.5px;font-weight:600;white-space:nowrap;margin-right:6px;border:1px solid}
.signal-blue{background:var(--blue-bg);color:var(--blue);border-color:#2563eb55}
.signal-purple{background:var(--purple-bg);color:var(--purple);border-color:#9333ea55}
.signal-green{background:var(--green-bg);color:var(--green);border-color:#16a34a55}
.time-note{color:var(--text-dim);font-size:11.5px;direction:ltr;unicode-bidi:embed;display:inline-block}
.ltr-inline{direction:ltr;unicode-bidi:embed;display:inline-block}
td.ltr-inline,th.ltr-inline{display:table-cell}
.chg-pos{color:var(--green);font-weight:600;font-size:11.5px;direction:ltr;unicode-bidi:embed;display:inline-block;margin-right:3px}
.chg-neg{color:var(--red);font-weight:600;font-size:11.5px;direction:ltr;unicode-bidi:embed;display:inline-block;margin-right:3px}
.chg-zero{color:var(--text-dim);font-size:11px;direction:ltr;unicode-bidi:embed;display:inline-block;margin-right:3px}
.submodel-bar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:6px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 10px;margin-top:6px;font-size:11.5px;color:var(--text-dim)}
.sm-chip{padding:2px 6px;border-radius:5px;border:1px solid var(--border);background:var(--surface-2);color:var(--text);direction:ltr;unicode-bidi:embed;display:inline-block;margin-left:4px}
.sm-best{background:var(--green-bg);color:var(--green);border-color:#16a34a55;font-weight:600}
.sm-worst{background:var(--red-bg);color:var(--red);border-color:#dc262644;text-decoration:line-through;opacity:0.75}
.locked-section{border:1px solid #16a34a55;background:var(--green-bg)}
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin-top:8px;border-radius:8px;border:1px solid var(--border)}
table{width:100%;min-width:520px;border-collapse:collapse;font-size:12.8px;background:var(--surface);font-variant-numeric:tabular-nums}
th,td{padding:9px 7px;text-align:center;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--text-dim);font-weight:600;font-size:11.5px;background:var(--surface-2)}
tr:last-child td{border-bottom:none}
.lock-btn,.unlock-btn{border:none;border-radius:9px;padding:8px 14px;font-size:12px;font-weight:600;cursor:pointer;min-height:36px}
.lock-btn{background:var(--accent);color:#fff}
.unlock-btn{background:var(--red);color:#fff}
.locked-badge{color:var(--green);font-size:11.5px;font-weight:600;display:block;margin-bottom:3px}
.lock-status{display:block;font-size:10.5px;margin-top:4px;line-height:1.35;max-width:170px;margin-left:auto;margin-right:auto}
.empty{color:var(--text-dim);font-style:italic;padding:14px 4px}
.win{color:var(--green);font-weight:600}
.loss{color:var(--red);font-weight:600}
.history-filters{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.history-filters select{background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:10px;padding:8px 12px;font-size:12.5px}
.download-btn{display:inline-block;margin:8px 0;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:9px 16px;font-size:12.5px;font-weight:600}
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-bottom:14px}
.kpi-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px}
.kpi-title{font-size:11.5px;font-weight:700;margin-bottom:4px}
.kpi-val{font-size:21px;font-weight:800}
.kpi-sub{font-size:11px;color:var(--text-dim);margin-top:4px}
@media (max-width: 600px){
  .top-navbar{padding:8px 10px}
  .tab-btn,.refresh-btn{padding:6px 9px;font-size:11px}
  .container{padding:10px}
  th,td{padding:8px 5px;font-size:11.8px}
}
</style>
</head>
<body>
<div class="top-navbar">
  <div class="brand-wrap">
    <span class="brand-title">🌡️ WeatherBet — سامانه هوشمند پیش‌بینی دما</span>
    <span class="brand-meta">آخرین آپدیت: LASTUPDATE (ایران) | <span class="time-note relative-time" data-ts="LASTSCANISO" data-kind="LASTSCANKIND">LASTSCANFALLBACK</span></span>
  </div>
  <div class="nav-tabs">
    <button id="tabBtnSimple" class="tab-btn active-blue" onclick="switchTab('simple')">داشبورد ساده (Simple)</button>
    <button id="tabBtnPro" class="tab-btn" onclick="switchTab('pro')">داشبورد پیشرفته (v3 Pro)</button>
    <button id="tabBtnAccuracy" class="tab-btn" onclick="switchTab('accuracy')">📊 دقت شهرها و کالیبراسیون هوشمند</button>
    <button id="refreshBtn" class="refresh-btn" onclick="refreshDashboardNow()">↻ بروزرسانی</button>
  </div>
</div>

<div class="container">
  <!-- TAB 1: SIMPLE DASHBOARD -->
  <div id="view-simple">
    <div class="smart-bar">
      <div>
        <b style="font-size:13.5px">🛡️ گیت کالیبراسیون هوشمند ۱۴ روزه + ردیابی دوگانه تغییرات (۱۰ دقیقه‌ای و ۶ ساعته)</b>
        <div style="font-size:11.5px;color:var(--text-dim)">در هر آپدیت سبک و سنگین، درصد تغییرات احتمال مدل، احتمال بازار و قیمت YES در داخل پرانتز (نسبت به اسکن ۱۰ دقیقه‌ای قبلی و اسکن ۶ ساعته قبلی) نمایش داده می‌شود.</div>
      </div>
      <div class="smart-toggles">
        <label class="toggle-label" style="border-color:#16a34a66">
          <input type="checkbox" id="useSmartGateToggle" checked onchange="filterCities()">
          🛡️ اعمال گیت کالیبراسیون هوشمند (پیشنهادی)
        </label>
        <label class="toggle-label">
          <input type="checkbox" id="onlyGoodCitiesToggle" onchange="filterCities()">
          🔥 فقط شهرهای روی فرم (≥۵۰٪)
        </label>
        <label class="toggle-label">
          <input type="checkbox" id="onlyUsCitiesToggle" onchange="filterCities()">
          🇺🇸 فقط شهرهای آمریکا
        </label>
        <label class="toggle-label">
          <input type="checkbox" id="showAllBucketsToggle" onchange="toggleLowPlausibilityBuckets()">
          نمایش باکت‌های زیر ۵٪
        </label>
      </div>
    </div>

    <div class="toolbar">
      <input type="text" id="citySearch" placeholder="جستجوی شهر (مثلاً Atlanta, Chicago, Cape Town, Seoul)..." oninput="filterCities()">
      <a class="jump-btn" href="#max-section">🔥 حداکثر</a>
      <a class="jump-btn" href="#min-section">❄️ حداقل</a>
      <a class="jump-btn" href="#daily-section">📅 آمار روزانه</a>
      <a class="jump-btn" href="#history-section">مشاهدهٔ نتایج ↓</a>
    </div>

    <h2 id="max-section">🔥 دمای حداکثر</h2>
    BODYHTML
    <h2 id="min-section">❄️ دمای حداقل</h2>
    MIN_BODYHTML
    DAILYPNLHTML
    <h2 id="history-section">تاریخچهٔ معاملات</h2>
    <a class="download-btn" href="lock_history.csv" download>⬇ دانلود CSV کامل</a>
    <div class="history-filters">
      <select id="historyCityFilter" onchange="filterHistory()"><option value="">همهٔ شهرها</option>HISTORYCITYOPTIONS</select>
      <select id="historyDateFilter" onchange="filterHistory()"><option value="">همهٔ تاریخ‌ها</option>HISTORYDATEOPTIONS</select>
    </div>
    <div class="table-scroll">
    HISTORYHTML
    </div>
  </div>

  <!-- TAB 2: PRO TERMINAL DASHBOARD -->
  <div id="view-pro" style="display:none">
    PRO_TAB_HTML
  </div>

  <!-- TAB 3: ACCURACY & SMART CALIBRATION -->
  <div id="view-accuracy" style="display:none">
    ACCURACY_TAB_HTML
  </div>
</div>

<script>
function switchTab(mode) {
  var vs = document.getElementById('view-simple');
  var vp = document.getElementById('view-pro');
  var va = document.getElementById('view-accuracy');
  var bs = document.getElementById('tabBtnSimple');
  var bp = document.getElementById('tabBtnPro');
  var ba = document.getElementById('tabBtnAccuracy');
  vs.style.display = (mode === 'simple') ? '' : 'none';
  if (vp) vp.style.display = (mode === 'pro') ? '' : 'none';
  va.style.display = (mode === 'accuracy') ? '' : 'none';
  bs.className = (mode === 'simple') ? 'tab-btn active-blue' : 'tab-btn';
  if (bp) bp.className = (mode === 'pro') ? 'tab-btn active-blue' : 'tab-btn';
  ba.className = (mode === 'accuracy') ? 'tab-btn active-purple' : 'tab-btn';
}
function refreshDashboardNow() {
  var btn = document.getElementById('refreshBtn');
  if (btn) { btn.textContent = '⏳ در حال بروزرسانی...'; btn.disabled = true; }
  fetch('/api/refresh-prices', { method: 'POST' })
    .catch(function(){})
    .finally(function(){
      var url = new URL(window.location.href);
      url.searchParams.set('_r', Date.now().toString());
      window.location.replace(url.toString());
    });
}
function filterCities() {
  var q = (document.getElementById('citySearch').value || '').trim().toLowerCase();
  var onlyGood = document.getElementById('onlyGoodCitiesToggle') && document.getElementById('onlyGoodCitiesToggle').checked;
  var onlyUs = document.getElementById('onlyUsCitiesToggle') && document.getElementById('onlyUsCitiesToggle').checked;
  document.querySelectorAll('.city-block[data-city-name]').forEach(function(block) {
    var name = (block.getAttribute('data-city-name') || '').toLowerCase();
    var slug = (block.getAttribute('data-city-slug') || '').toLowerCase();
    var hitPct = parseFloat(block.getAttribute('data-hit-pct') || '100');
    var isUs = block.getAttribute('data-is-us') === '1';
    var okName = !q || name.indexOf(q) !== -1 || slug.indexOf(q) !== -1;
    var okGood = !onlyGood || hitPct >= 50;
    var okUs = !onlyUs || isUs;
    block.style.display = (okName && okGood && okUs) ? '' : 'none';
  });
}
function toggleLowPlausibilityBuckets() {
  var showAll = document.getElementById('showAllBucketsToggle') && document.getElementById('showAllBucketsToggle').checked;
  document.querySelectorAll('tr.low-plausibility-row').forEach(function(row) {
    row.style.display = showAll ? '' : 'none';
  });
}
var currentAccType = 'max';
var currentAccRegion = 'all';
function setAccuracyFilter(kind, val) {
  if (kind === 'type') currentAccType = val;
  if (kind === 'region') currentAccRegion = val;
  document.querySelectorAll('[data-acc-type-btn]').forEach(function(b) {
    b.className = (b.getAttribute('data-acc-type-btn') === currentAccType) ? 'tab-btn active-blue' : 'tab-btn';
  });
  document.querySelectorAll('[data-acc-region-btn]').forEach(function(b) {
    b.className = (b.getAttribute('data-acc-region-btn') === currentAccRegion) ? 'tab-btn active-blue' : 'tab-btn';
  });
  filterAccuracyTable();
}
function filterAccuracyTable() {
  var qEl = document.getElementById('accCitySearch');
  var q = qEl ? (qEl.value || '').trim().toLowerCase() : '';
  document.querySelectorAll('#accuracyLeaderboardTable tbody tr').forEach(function(row) {
    var cname = (row.getAttribute('data-city-name') || '').toLowerCase();
    var mtype = row.getAttribute('data-mtype') || 'max';
    var isUs = row.getAttribute('data-is-us') === '1';
    var okQ = !q || cname.indexOf(q) !== -1;
    var okType = (currentAccType === 'all') || (mtype === currentAccType);
    var okReg = (currentAccRegion === 'all') || (currentAccRegion === 'us' && isUs) || (currentAccRegion === 'non_us' && !isUs);
    row.style.display = (okQ && okType && okReg) ? '' : 'none';
  });
}
function filterHistory() {
  var city = document.getElementById('historyCityFilter').value;
  var date = document.getElementById('historyDateFilter').value;
  document.querySelectorAll('#historyTable tbody tr').forEach(function(row) {
    var okCity = !city || row.getAttribute('data-city') === city;
    var okDate = !date || row.getAttribute('data-date') === date;
    row.style.display = (okCity && okDate) ? '' : 'none';
  });
}
function setLockStatus(marketId, text, color) {
  var el = document.getElementById("lockstatus-" + marketId);
  if (el) { el.textContent = text; el.style.color = color || "#9aa0a6"; }
}
function lockBucket(city, cityName, date, marketId, tokenId, side, price, label, marketType) {
  setLockStatus(marketId, "⏳ در حال باز شدن گیت‌هاب...", "#eab308");
  var repo = "GITHUB_REPO_PLACEHOLDER";
  var payload = { city: city, date: date, market_id: marketId + "|" + tokenId, side: side, price: price, market_type: (marketType || "max") };
  var title = encodeURIComponent("LOCK " + cityName + " " + date + " " + label + " " + side + " @ " + price);
  var body = encodeURIComponent(JSON.stringify(payload));
  var url = "https://github.com/" + repo + "/issues/new?title=" + title + "&body=" + body + "&labels=lock-request";
  var win = window.open(url, "_blank");
  if (win) {
    setLockStatus(marketId, "⚠️ در تب جدید حتماً روی «Submit new issue» کلیک کنید تا قفل ثبت شود!", "#f59e0b");
  } else {
    setLockStatus(marketId, "❌ مرورگر پاپ‌آپ را مسدود کرد -- اجازه بدهید و دوباره امتحان کنید.", "#f87171");
  }
}
function unlockBucket(city, cityName, date, marketId, label) {
  setLockStatus(marketId, "⏳ در حال باز شدن گیت‌هاب...", "#eab308");
  var repo = "GITHUB_REPO_PLACEHOLDER";
  var payload = { city: city, date: date, market_id: marketId };
  var title = encodeURIComponent("UNLOCK " + cityName + " " + date + " " + label);
  var body = encodeURIComponent(JSON.stringify(payload));
  var url = "https://github.com/" + repo + "/issues/new?title=" + title + "&body=" + body + "&labels=unlock-request";
  var win = window.open(url, "_blank");
  if (win) {
    setLockStatus(marketId, "⚠️ در تب جدید حتماً روی «Submit new issue» کلیک کنید تا حذف شود!", "#f59e0b");
  } else {
    setLockStatus(marketId, "❌ مرورگر پاپ‌آپ را مسدود کرد -- اجازه بدهید و دوباره امتحان کنید.", "#f87171");
  }
}
function computeRelativeTimes() {
  var now = new Date();
  document.querySelectorAll('.relative-time').forEach(function(el) {
    var ts = el.getAttribute('data-ts');
    if (!ts) { return; }
    var then = new Date(ts);
    if (isNaN(then.getTime())) { return; }
    var diffMin = Math.round((now - then) / 60000);
    if (diffMin < 0) { diffMin = 0; }
    var h = Math.floor(diffMin / 60);
    var m = diffMin % 60;
    var kind = el.getAttribute('data-kind') || '';
    el.textContent = h + 'h ' + m + 'm ago' + (kind ? (' -- ' + kind) : '');
  });
}
window.addEventListener('DOMContentLoaded', computeRelativeTimes);
</script>
</body>
</html>
"""


def _load_all_markets():
    out = []
    if not MARKETS_DIR.exists():
        return out
    for f in MARKETS_DIR.glob("*.json"):
        if f.name.endswith("_min.json"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            d["market_type"] = "max"
            out.append(d)
        except Exception:
            pass
    return out


def _load_all_min_markets():
    out = []
    if not MARKETS_DIR.exists():
        return out
    for f in MARKETS_DIR.glob("*_min.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            d["market_type"] = "min"
            out.append(d)
        except Exception:
            pass
    return out


def _load_locks():
    if not LOCKS_FILE.exists():
        return []
    try:
        return json.loads(LOCKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _load_locked_keys(locks):
    return {
        (l.get("city"), l.get("date"), str(l.get("market_id")))
        for l in locks
        if l.get("status") == "open"
    }


def _main_signal_market_id(market):
    if market.get("smart_main_market_id"):
        return str(market["smart_main_market_id"])
    allocation = market.get("committed_allocation") or market.get("live_allocation") or []
    for a in allocation:
        if a.get("role") == "main_signal":
            return str(a.get("market_id"))
    return None


def _label_for_range(low, high, unit_sym):
    if low <= -998 and high >= 998:
        return f"?{unit_sym}"
    if low <= -998:
        return f"\u2264{high}{unit_sym}"
    if high >= 998:
        return f"\u2265{low}{unit_sym}"
    if low == high:
        return f"{low}{unit_sym}"
    return f"{low}-{high}{unit_sym}"


def _hours_left_str(hours):
    if hours is None:
        return ""
    total_minutes = int(round(hours * 60))
    h, m = divmod(total_minutes, 60)
    return f"{h}h {m}m remaining"


def _local_day_time_label(city, date, now):
    loc = LOCATIONS.get(city, {})
    timing = local_day_status(date, loc, now)
    if timing["kind"] == "local_day_open":
        return _hours_left_str(timing["remaining_seconds"] / 3600.0), False
    return "awaiting official settlement", True


def _find_locked_bucket_label(mkt, market_id, unit_sym):
    if not mkt:
        return "-"
    for b in mkt.get("full_distribution", []) or []:
        if str(b.get("market_id")) == str(market_id):
            rng = b.get("range")
            if not rng:
                return "-"
            low, high = rng
            return _label_for_range(low, high, unit_sym)
    return "-"


def _latest_scan_info():
    if not LAST_SCAN_FILE.exists():
        return None, None
    try:
        data = json.loads(LAST_SCAN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    timestamps = []
    for kind in ("full", "lite"):
        ts = data.get(kind)
        if not ts:
            continue
        try:
            timestamps.append((kind, datetime.fromisoformat(ts)))
        except Exception:
            continue
    if not timestamps:
        return None, None
    kind, latest = max(timestamps, key=lambda kv: kv[1])
    kind_label = "اسکن کامل" if kind == "full" else "اسکن سبک"
    return latest.isoformat(), kind_label


def _iran_time_str(dt_utc):
    if ZoneInfo is not None:
        try:
            local_dt = dt_utc.astimezone(ZoneInfo(IRAN_TZ_NAME))
            return local_dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    return dt_utc.strftime("%Y-%m-%d %H:%M") + " UTC"


def _build_polymarket_url(city, date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        month = MONTHS[dt.month - 1]
        return f"https://polymarket.com/event/highest-temperature-in-{city}-on-{month}-{dt.day}-{dt.year}"
    except Exception:
        return "https://polymarket.com"


def _build_polymarket_url_min(city, date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        month = MONTHS[dt.month - 1]
        return f"https://polymarket.com/event/lowest-temperature-in-{city}-on-{month}-{dt.day}-{dt.year}"
    except Exception:
        return "https://polymarket.com"


def _signal_badge_html(is_main, ev_val):
    ev_val = ev_val if ev_val is not None else 0.0
    if is_main and ev_val >= EV_GATE_THRESHOLD:
        return '<span class="signal-badge signal-green">\U0001F7E2 سیگنال نهایی</span>'
    if is_main:
        return '<span class="signal-badge signal-blue">\U0001F535 محتمل\u200cترین</span>'
    if ev_val >= EV_GATE_THRESHOLD:
        return '<span class="signal-badge signal-purple">\U0001F7E3 ارزش بالا</span>'
    return ''


def _format_pct_paren(pct_val, pct_6h_val=None):
    """فرمت کردن درصد تغییرات در داخل پرانتز (هم تغییر اسکن ۱۰ دقیقه‌ای و هم اسکن ۶ ساعته در صورت تفاوت)."""
    if pct_val is None and pct_6h_val is None:
        return ""
    v10 = float(pct_val if pct_val is not None else (pct_6h_val or 0.0))
    v6 = float(pct_6h_val) if pct_6h_val is not None else None
    if v6 is not None and abs(v6 - v10) >= 0.05 and abs(v6) >= 0.05:
        c10 = "chg-zero" if abs(v10) < 0.05 else ("chg-pos" if v10 > 0 else "chg-neg")
        c6 = "chg-zero" if abs(v6) < 0.05 else ("chg-pos" if v6 > 0 else "chg-neg")
        s10 = "0.0%" if abs(v10) < 0.05 else f"{v10:+.1f}%"
        s6 = "0.0%" if abs(v6) < 0.05 else f"{v6:+.1f}%"
        return (
            f' <span class="{c10}" title="تغییر اسکن ۱۰ دقیقه‌ای قبلی">({s10})</span>'
            f'<div style="font-size:10.5px;color:var(--text-dim);line-height:1.2;margin-top:2px;direction:ltr" '
            f'title="تغییر نسبت به اسکن ۶ ساعته قبلی">(6h: <span class="{c6}">{s6}</span>)</div>'
        )
    val = v10 if abs(v10) >= 0.05 else (v6 if v6 is not None else 0.0)
    if abs(val) < 0.05:
        return ' <span class="chg-zero">(0.0%)</span>'
    css = "chg-pos" if val > 0 else "chg-neg"
    return f' <span class="{css}">({val:+.1f}%)</span>'


def _bucket_table(city_slug, city_name, date, unit_sym, full_distribution, locked_keys, main_signal_id, market_type="max"):
    """جدول باکت‌ها همراه با نمایش دقیق تغییرات درصدی در داخل پرانتز (۱۰ دقیقه‌ای و ۶ ساعته)."""
    if not full_distribution:
        return '<div class="empty">داده‌ای موجود نیست.</div>'

    rows = [
        "<table><tr><th>باکت</th><th>احتمال مدل</th><th>احتمال بازار (YES)</th>"
        "<th>قیمت YES</th><th>باور نهایی</th><th>EV</th><th>عملیات</th></tr>"
    ]
    has_visible = False
    for b in full_distribution:
        is_plausible = max(b.get("smart_model_prob") or b.get("model_prob") or 0.0, b.get("yes_price") or 0.0) >= BUCKET_PLAUSIBILITY_MIN
        if is_plausible:
            has_visible = True
        low, high = b.get("range", [None, None])
        label = _label_for_range(low, high, unit_sym)
        model_prob = b.get("smart_model_prob") if b.get("smart_model_prob") is not None else b.get("model_prob")
        raw_model_prob = b.get("model_prob")
        yes_price = b.get("yes_price")

        model_change = b.get("model_prob_6h_change")
        if model_change is None or abs(float(model_change)) < 0.0005:
            model_change = b.get("model_prob_change")
        if (model_change is None or abs(float(model_change)) < 0.0005) and model_prob is not None and raw_model_prob is not None:
            if abs(float(model_prob) - float(raw_model_prob)) >= 0.0005:
                model_change = float(model_prob) - float(raw_model_prob)

        price_change = b.get("yes_price_change")
        if (price_change is None or abs(float(price_change)) < 0.0005) and b.get("yes_price_6h_change") is not None:
            price_change = b.get("yes_price_6h_change")
        if (price_change is None or abs(float(price_change)) < 0.0005) and yes_price is not None:
            ref_6h = b.get("scan6h_yes_price") if b.get("scan6h_yes_price") is not None else b.get("ask")
            if ref_6h is not None and abs(float(yes_price) - float(ref_6h)) >= 0.0005:
                price_change = round(float(yes_price) - float(ref_6h), 4)

        price_pct_change = b.get("yes_price_pct_change")
        if (price_pct_change is None or abs(float(price_pct_change)) < 0.05) and b.get("yes_price_6h_pct_change") is not None:
            price_pct_change = b.get("yes_price_6h_pct_change")
        if (price_pct_change is None or abs(float(price_pct_change)) < 0.05) and price_change is not None and yes_price is not None:
            prev_p = float(yes_price) - float(price_change)
            if prev_p > 0.001 and abs(float(price_change)) >= 0.0005:
                price_pct_change = round((float(price_change) / prev_p) * 100.0, 1)
            elif abs(float(price_change)) >= 0.0005:
                price_pct_change = round(float(price_change) * 100.0, 1)
            else:
                price_pct_change = 0.0

        p_10m_chg = b.get("yes_price_10m_change", price_change)
        p_6h_chg = b.get("yes_price_6h_change", price_change)
        p_10m_pct = b.get("yes_price_10m_pct_change", price_pct_change)
        p_6h_pct = b.get("yes_price_6h_pct_change", price_pct_change)

        model_str = f"{model_prob * 100:.1f}%" if model_prob is not None else "-"
        if model_prob is not None and model_change is not None:
            model_str += _format_pct_paren(float(model_change) * 100.0)

        market_str = f"{yes_price * 100:.1f}%" if yes_price is not None else "-"
        if yes_price is not None and (p_10m_chg is not None or p_6h_chg is not None):
            market_str += _format_pct_paren(
                float(p_10m_chg) * 100.0 if p_10m_chg is not None else None,
                float(p_6h_chg) * 100.0 if p_6h_chg is not None else None,
            )

        price_str = f"{yes_price:.3f}" if yes_price is not None else "-"
        if yes_price is not None and (p_10m_pct is not None or p_6h_pct is not None):
            price_str += _format_pct_paren(p_10m_pct, p_6h_pct)

        belief_val = b.get("smart_belief_prob") if b.get("smart_belief_prob") is not None else b.get("belief_prob")
        belief_str = f"{belief_val * 100:.1f}%" if belief_val is not None else "-"

        ev_val = b.get("smart_ev") if b.get("smart_ev") is not None else b.get("ev")
        if ev_val is None:
            ev_str = "-"
        else:
            ev_pct = ev_val * 100
            ev_css = "win" if ev_pct > 0 else ("loss" if ev_pct < 0 else "")
            ev_sign = "+" if ev_pct > 0 else ""
            ev_str = f'<span class="{ev_css}">{ev_sign}{ev_pct:.1f}%</span>' if ev_css else f"{ev_pct:.1f}%"

        market_id = str(b.get("market_id", ""))
        yes_token = b.get("yes_token_id", "")

        is_main = main_signal_id is not None and market_id == main_signal_id
        label_html = f'<span class="ltr-inline">{label}</span> ' + _signal_badge_html(is_main, ev_val)

        is_locked = (city_slug, date, market_id) in locked_keys
        action_html = ""
        status_span = f'<span class="lock-status" id="lockstatus-{market_id}"></span>' if market_id else ""
        if yes_price is not None and market_id:
            if is_locked:
                unlock_call = "unlockBucket('{0}','{1}','{2}','{3}','{4}')".format(
                    city_slug, city_name, date, market_id, label
                )
                action_html = (
                    '<span class="locked-badge">قفل \u2714</span>'
                    f'<button class="unlock-btn" onclick="{unlock_call}">حذف قفل</button>'
                    f'{status_span}'
                )
            else:
                lock_call = "lockBucket('{0}','{1}','{2}','{3}','{4}','YES',{5},'{6}','{7}')".format(
                    city_slug, city_name, date, market_id, yes_token, yes_price, label, market_type
                )
                action_html = (
                    f'<button class="lock-btn" onclick="{lock_call}">قفل کن</button>'
                    f'{status_span}'
                )

        row_cls = "" if is_plausible else " class='low-plausibility-row' style='display:none'"
        rows.append(
            f"<tr{row_cls}><td>{label_html}</td><td class='ltr-inline'>{model_str}</td><td class='ltr-inline'>{market_str}</td>"
            f"<td class='ltr-inline'>{price_str}</td><td class='ltr-inline'>{belief_str}</td><td class='ltr-inline'>{ev_str}</td><td>{action_html}</td></tr>"
        )
    if not has_visible:
        return '<div class="empty">داده‌ای موجود نیست.</div>'
    rows.append("</table>")
    return "".join(rows)


def _load_market_by_key(city, date):
    p = MARKETS_DIR / f"{city}_{date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_market_by_key_min(city, date):
    p = MARKETS_DIR / f"{city}_{date}_min.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _locked_signals_section_html(locks):
    open_locks = [l for l in locks if l.get("status") == "open"]
    if not open_locks:
        return ""

    now = datetime.now(timezone.utc)
    visible_rows = []

    for l in sorted(open_locks, key=lambda x: (x.get("city", ""), x.get("date", ""))):
        city = l.get("city", "")
        date = l.get("date", "")
        is_min = l.get("market_type") == "min"

        mkt = _load_market_by_key_min(city, date) if is_min else _load_market_by_key(city, date)
        if mkt is not None and mkt.get("status") in RESOLVED_LIKE_STATUSES:
            continue

        time_str, awaiting_settlement = _local_day_time_label(city, date, now)
        unit_sym = mkt.get("unit", "") if mkt else ""
        bucket_label = _find_locked_bucket_label(mkt, l.get("market_id"), unit_sym)

        name = LOCATIONS.get(city, {}).get("name", city)
        link = _build_polymarket_url_min(city, date) if is_min else _build_polymarket_url(city, date)
        name_html = f'<a href="{link}" target="_blank" rel="noopener">{name}</a>'

        entry = l.get("entry_price")
        entry_str = f"{entry:.3f}" if entry is not None else "-"
        last = l.get("last_price")
        last_str = f"{last:.3f}" if last is not None else "-"

        pct = l.get("last_pct")
        if pct is None:
            pct_html = "-"
        else:
            css = "win" if pct >= 0 else "loss"
            pct_html = f'<span class="{css}">{pct:+.1f}%</span>'

        last_checked = l.get("last_checked_at")
        if last_checked:
            updated_html = f'<span class="relative-time" data-ts="{last_checked}"></span>'
        else:
            updated_html = "-"

        market_id = str(l.get("market_id", ""))
        unlock_call = "unlockBucket('{0}','{1}','{2}','{3}','{4}')".format(
            city, name, date, market_id, bucket_label
        )
        action_html = (
            f'<button class="unlock-btn" onclick="{unlock_call}">حذف قفل</button>'
            f'<span class="lock-status" id="lockstatus-{market_id}-active"></span>'
        )

        visible_rows.append(
            f"<tr><td>{name_html}</td><td>{date}</td><td>{bucket_label}</td>"
            f"<td>{entry_str}</td><td>{last_str}</td><td>{pct_html}</td>"
            f"<td>{time_str}</td><td>{updated_html}</td><td>{action_html}</td></tr>"
        )

    if not visible_rows:
        return ""

    rows = [
        "<table><tr><th>شهر</th><th>تاریخ</th><th>دمای سیگنال قفل‌شده</th>"
        "<th>قیمت قفل‌شده</th><th>قیمت فعلی</th><th>درصد تغییر</th>"
        "<th>تا پایان روز محلی</th><th>آخرین به‌روزرسانی</th><th>عملیات</th></tr>"
    ] + visible_rows
    rows.append("</table>")
    table_html = "".join(rows)

    return (
        "<details class='city-block locked-section' open>"
        f"<summary><b>\U0001F512 سیگنال‌های قفل‌شدهٔ فعال</b> ({len(visible_rows)})</summary>"
        f"<div class='table-scroll'>{table_html}</div>"
        "</details>"
    )


def _submodel_bar_html(m):
    models_raw = m.get("model_means_raw") or {}
    prof = m.get("city_form")
    unit_sym = m.get("unit", "")
    if not models_raw and not prof:
        return ""
    chips = []
    best_m = prof.get("best_submodel") if prof else None
    worst_m = prof.get("worst_submodel") if prof else None
    for mname, mval in models_raw.items():
        if mval is None:
            continue
        cls = "sm-chip"
        if best_m == mname:
            cls += " sm-best"
        elif worst_m == mname and best_m != mname:
            cls += " sm-worst"
        chips.append(f'<span class="{cls}">{mname.upper()}: {float(mval):.1f}{unit_sym}</span>')
    left_html = f"<div><b>زیرمدل‌های هواشناسی:</b> {''.join(chips)}</div>" if chips else ""
    right_html = f"<div>💡 <b>وضعیت گیت:</b> {prof['gate_reason']}</div>" if prof else ""
    return f'<div class="submodel-bar">{left_html}{right_html}</div>'


def _date_block_common_html(m, city_slug, city_name, locked_keys, market_type="max"):
    date = m.get("date", "")
    unit_sym = m.get("unit", "")
    main_signal_id = _main_signal_market_id(m)
    link = _build_polymarket_url_min(city_slug, date) if market_type == "min" else _build_polymarket_url(city_slug, date)
    if m.get("time_status") == "awaiting_settlement":
        time_note = "awaiting official settlement"
    else:
        time_note = _hours_left_str(m.get("hours_left"))

    f_legacy = m.get("forecast_mean")
    f_smart = m.get("smart_forecast_mean", f_legacy)
    mean_html = ""
    if f_smart is not None:
        corr_html = ""
        if f_legacy is not None and abs(float(f_smart) - float(f_legacy)) >= 0.08:
            corr_html = f' <span style="color:var(--purple);font-weight:600">← اصلاح از {float(f_legacy):.1f}{unit_sym}</span>'
        mean_html = f' <span style="font-size:12px;color:var(--text-dim)">(پیش‌بینی هوشمند: <b class="ltr-inline" style="color:var(--text)">{float(f_smart):.1f}{unit_sym}</b>{corr_html})</span>'

    summary = (
        f'<a href="{link}" target="_blank" rel="noopener">{city_name} ↗</a> \u2014 {date}'
        f'{mean_html}  <span class="time-note">{time_note}</span>'
    )
    sm_bar = _submodel_bar_html(m)
    table = _bucket_table(
        city_slug, city_name, date, unit_sym,
        m.get("full_distribution"), locked_keys, main_signal_id, market_type=market_type,
    )
    return f"<details class='date-block' open><summary>{summary}</summary>{sm_bar}<div class='table-scroll'>{table}</div></details>"


def _date_block_html(m, city_slug, city_name, locked_keys):
    return _date_block_common_html(m, city_slug, city_name, locked_keys, market_type="max")


def _date_block_html_min(m, city_slug, city_name, locked_keys):
    return _date_block_common_html(m, city_slug, city_name, locked_keys, market_type="min")


DAILY_PNL_VISIBLE_DAYS = 5


def _daily_pnl_section_html(locks):
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
        if ZoneInfo is not None:
            try:
                local_date = dt_utc.astimezone(ZoneInfo(IRAN_TZ_NAME)).strftime("%Y-%m-%d")
            except Exception:
                local_date = dt_utc.strftime("%Y-%m-%d")
        else:
            local_date = dt_utc.strftime("%Y-%m-%d")
        daily.setdefault(local_date, []).append(l)

    if not daily:
        return ""

    def _day_block_html(date, day_locks, open_attr):
        pcts = []
        rows = []
        for l in day_locks:
            entry = l.get("entry_price")
            exit_price = l.get("exit_price")
            pct = (exit_price - entry) / entry * 100 if entry and exit_price is not None else None
            if pct is not None:
                pcts.append(pct)

            is_min = l.get("market_type") == "min"
            mkt = _load_market_by_key_min(l.get("city"), l.get("date")) if is_min else _load_market_by_key(l.get("city"), l.get("date"))
            unit_sym = mkt.get("unit", "") if mkt else ""
            label = _find_locked_bucket_label(mkt, l.get("market_id"), unit_sym)

            name = LOCATIONS.get(l.get("city"), {}).get("name", l.get("city"))
            reason = "resolve خودکار" if l.get("status") == "closed_resolved" else "حذف دستی"
            entry_str = f"{entry:.3f}" if entry is not None else "-"
            exit_str = f"{exit_price:.3f}" if exit_price is not None else "-"
            css = "win" if (pct or 0) >= 0 else "loss"
            pct_str = f"<span class='{css}'>{pct:+.1f}%</span>" if pct is not None else "-"

            rows.append(
                f"<tr><td>{name}</td><td>{l.get('date')}</td><td>{label}</td>"
                f"<td>{entry_str}</td><td>{exit_str}</td><td>{pct_str}</td><td>{reason}</td></tr>"
            )

        avg_pct = sum(pcts) / len(pcts) if pcts else 0.0
        summary_css = "win" if avg_pct >= 0 else "loss"
        table_html = (
            "<table><tr><th>شهر</th><th>تاریخ</th><th>باکت</th><th>ورود</th>"
            "<th>خروج</th><th>درصد</th><th>دلیل</th></tr>" + "".join(rows) + "</table>"
        )
        return (
            f"<details class='date-block'{open_attr}>"
            f"<summary>{date} \u2014 <span class='{summary_css}'>{avg_pct:+.1f}%</span></summary>"
            f"<div class='table-scroll'>{table_html}</div>"
            "</details>"
        )

    sorted_dates = sorted(daily.keys(), reverse=True)
    visible_dates = sorted_dates[:DAILY_PNL_VISIBLE_DAYS]
    older_dates = sorted_dates[DAILY_PNL_VISIBLE_DAYS:]

    blocks = [_day_block_html(d, daily[d], " open") for d in visible_dates]

    if older_dates:
        older_blocks = "".join(_day_block_html(d, daily[d], "") for d in older_dates)
        blocks.append(
            "<details class='city-block'>"
            f"<summary>نمایش روزهای قدیمی\u200cتر ({len(older_dates)} روز)</summary>"
            f"{older_blocks}"
            "</details>"
        )

    return "<h2 id='daily-section'>\U0001F4C5 گزارش روزانه</h2>" + "".join(blocks)


def _history_table_html(locks):
    rows = history_manager.get_history_rows(locks)
    cities = sorted({r["city_name"] for r in rows})
    dates = sorted({r["date"] for r in rows}, reverse=True)
    city_options = "".join(f'<option value="{c}">{c}</option>' for c in cities)
    date_options = "".join(f'<option value="{d}">{d}</option>' for d in dates)

    if not rows:
        table_html = '<div class="empty">هنوز هیچ معامله‌ای بسته نشده.</div>'
    else:
        parts = [
            "<table id='historyTable'><tr><th>تاریخ</th><th>شهر</th><th>دمای سیگنال</th>"
            "<th>دمای نهایی</th><th>خرید (سنت)</th><th>فروش (سنت)</th><th>برآیند</th></tr><tbody>"
        ]
        for r in rows:
            pct = r["outcome_pct"]
            if pct is None:
                pct_html = "-"
            else:
                css = "win" if pct >= 0 else "loss"
                pct_html = f'<span class="{css}">{pct:+.1f}%</span>'
            sell = r["sell_cents"] if r["sell_cents"] is not None else "-"
            parts.append(
                f"<tr data-city=\"{r['city_name']}\" data-date=\"{r['date']}\">"
                f"<td>{r['date']}</td><td>{r['city_name']}</td><td>{r['signal_label']}</td>"
                f"<td>{r['final_temp']}</td><td>{r['buy_cents']}</td><td>{sell}</td><td>{pct_html}</td></tr>"
            )
        parts.append("</tbody></table>")
        table_html = "".join(parts)

    return table_html, city_options, date_options


def _gate_badge_html(gd):
    if gd == "revert_to_shadow":
        return '<span class="gate-badge">🔄 بازگشت به خام</span>'
    if gd == "sign_flip_damped":
        return '<span class="gate-badge">🛑 ترمز چرخش فصل</span>'
    if gd == "smart_submodel_blend":
        return '<span class="gate-badge">🧠 ترکیب هوشمند زیرمدل‌ها</span>'
    return ''


def _build_group_section(markets, date_block_fn, profiles, market_type="max"):
    locks = _load_locks()
    locked_keys = _load_locked_keys(locks)

    groups = defaultdict(list)
    for m in markets:
        groups[m.get("city", "")].append(m)
    for slug in groups:
        groups[slug].sort(key=lambda m: m.get("date", ""))

    def group_has_signal(mlist):
        return any(_main_signal_market_id(m) is not None for m in mlist)

    city_names = {slug: LOCATIONS.get(slug, {}).get("name", slug) for slug in groups}
    sorted_slugs = sorted(
        groups.keys(),
        key=lambda s: (0 if group_has_signal(groups[s]) else 1, city_names[s]),
    )

    parts = []
    if not sorted_slugs:
        return '<div class="empty">هیچ بازار بازی برای نمایش وجود ندارد.</div>'

    for slug in sorted_slugs:
        city_name = city_names[slug]
        mlist = groups[slug]
        has_signal = group_has_signal(mlist)
        prof = profiles.get((slug, market_type)) or profiles.get((slug, "max"))
        us_flag = " 🇺🇸" if slug in US_CITIES else ""
        badge = '<span class="main-badge">سیگنال فعال</span>' if has_signal else ""
        form_badge = ""
        gate_badge = ""
        hit_pct = 100
        if prof:
            hit_pct = prof["recent_hit_pct"]
            form_badge = f'<span class="form-badge form-{prof["form_status"]}">{prof["form_label"]}</span>'
            gate_badge = _gate_badge_html(prof["gate_decision"])

        inner = "".join(date_block_fn(m, slug, city_name, locked_keys) for m in mlist)
        count_note = f" &nbsp;({len(mlist)} تاریخ)" if len(mlist) > 1 else ""
        summary = f"<b>{city_name}</b>{us_flag} {badge} {form_badge} {gate_badge}{count_note}"

        is_us_attr = "1" if slug in US_CITIES else "0"
        parts.append(
            f"<details class='city-block' data-city-name='{city_name}' data-city-slug='{slug}' data-hit-pct='{hit_pct}' data-is-us='{is_us_attr}'>"
            f"<summary>{summary}</summary>{inner}</details>"
        )

    return "".join(parts)


def _build_pro_tab_html(all_markets, open_max, open_min, profiles):
    resolved = [m for m in all_markets if m.get("status") == "resolved"]
    wins = sum(1 for m in resolved if m.get("resolved_outcome") == "win")
    losses = len(resolved) - wins
    pnl = round(sum(float(m.get("pnl_units") or 0.0) for m in resolved), 1)
    win_rate = round(wins / len(resolved) * 100) if resolved else 0

    sig_cards = []
    for m in (open_max + open_min):
        alloc = m.get("committed_allocation") or m.get("live_allocation") or []
        if not alloc:
            continue
        cname = LOCATIONS.get(m.get("city"), {}).get("name", m.get("city_name", m.get("city")))
        mtype_lbl = "❄️ کمینه" if m.get("market_type") == "min" else "🔥 بیشینه"
        unit_sym = m.get("unit", "")
        legs_html = []
        for leg in alloc:
            b_lbl = _find_locked_bucket_label(m, leg.get("market_id"), unit_sym)
            legs_html.append(
                f"<span class='sm-chip'>{leg.get('role','leg')}: {b_lbl} ({leg.get('side','YES')} @ {float(leg.get('price') or 0):.3f}) | {float(leg.get('units') or 0):.1f}u</span>"
            )
        sig_cards.append(
            f"<div class='kpi-card' style='margin-bottom:8px'>"
            f"<div style='display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px'>"
            f"<b>{cname} — {m.get('date')} ({mtype_lbl})</b>"
            f"<span class='ltr-inline'>Mean: {float(m.get('smart_forecast_mean') or m.get('forecast_mean') or 0):.1f}{unit_sym} | σ: {float(m.get('sigma') or 0):.2f}</span>"
            f"</div>"
            f"<div style='margin-top:6px'>{''.join(legs_html)}</div>"
            f"</div>"
        )

    return f"""
    <h1>⚡ ترمینال پیشرفته پرتفوی و تخصیص هوشمند (WeatherBet v3 Pro)</h1>
    <div class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-title" style="color:var(--blue)">موجودی شبیه‌سازی پرتفوی</div>
        <div class="kpi-val ltr-inline">{100 + pnl:.1f} u</div>
        <div class="kpi-sub">سود/زیان خالص: <span class="ltr-inline">{pnl:+.1f} u</span></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-title" style="color:var(--green)">نرخ برد سیگنال‌های پرتفوی</div>
        <div class="kpi-val">{win_rate}%</div>
        <div class="kpi-sub">{wins} برد / {losses} باخت ({len(resolved)} بازار تسویه‌شده)</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-title" style="color:var(--purple)">بازارهای باز در حال رصد</div>
        <div class="kpi-val">{len(open_max) + len(open_min)} بازار</div>
        <div class="kpi-sub">{len(open_max)} بیشینه | {len(open_min)} کمینه</div>
      </div>
    </div>
    <h2>🎯 تخصیص‌های فعال استراتژی نردبانی (Ladder Allocations)</h2>
    {''.join(sig_cards) if sig_cards else '<div class="empty">در حال حاضر پوزیشن پرتفوی باز ثبت نشده است — از تب داشبورد ساده می‌توانید هر باکت را مستقیماً قفل کنید.</div>'}
    """


def _build_accuracy_tab_html(profiles):
    prof_list = list(profiles.values())
    prof_list.sort(key=lambda p: (-p["recent_hit_pct"], p["recent_mae"]))

    max_profs = [p for p in prof_list if p["market_type"] == "max"]
    hot_count = sum(1 for p in max_profs if p["recent_hit_pct"] >= 60)
    us_list = [p for p in max_profs if p["is_us"]]
    us_avg = round(sum(p["recent_hit_pct"] for p in us_list) / max(1, len(us_list)), 1)
    reverted_count = sum(1 for p in max_profs if p["gate_decision"] == "revert_to_shadow" or (p["hit_delta_pct"] is not None and p["hit_delta_pct"] > 0))
    flips_count = sum(1 for p in max_profs if p["sign_flip_detected"])

    rows = []
    for idx, p in enumerate(prof_list, 1):
        us_flag = " 🇺🇸" if p["is_us"] else ""
        is_us_attr = "1" if p["is_us"] else "0"
        mtype = p["market_type"]
        mtype_label = "🔥 بیشینه" if mtype == "max" else "❄️ کمینه"
        old_str = f"{p['old_hit_pct']}% (MAE {p['old_mae']}°)" if p["old_hit_pct"] is not None else "داده جدید"
        delta = p["hit_delta_pct"]
        if delta is None:
            delta_html = "-"
        elif delta > 0:
            delta_html = f'<span class="win">+{delta}% ↑</span>'
        elif delta < 0:
            delta_html = f'<span class="loss">{delta}% ↓</span>'
        else:
            delta_html = "0%"

        cal_vs_raw = "هم‌سطح / اصلاح‌شده"
        if p["calibrated_mae"] is not None and p["raw_shadow_mae"] is not None:
            diff = round(p["raw_shadow_mae"] - p["calibrated_mae"], 2)
            if diff > 0.05:
                cal_vs_raw = f'<span class="win">+{diff}° به نفع کالیبره</span>'
            elif diff < -0.05:
                cal_vs_raw = f'<span class="win">اصلاح‌شده با Shadow ({p["raw_shadow_mae"]}°)</span>'

        best_sm = p["best_submodel"].upper() if p["best_submodel"] else "-"
        display_style = "" if mtype == "max" else " style='display:none'"
        rows.append(
            f"<tr data-city-name='{p['city_name']}' data-mtype='{mtype}' data-is-us='{is_us_attr}'{display_style}>"
            f"<td>{idx}</td>"
            f"<td><b>{p['city_name']}</b>{us_flag}</td>"
            f"<td>{mtype_label}</td>"
            f"<td><b>{p['recent_hit_pct']}%</b> ({p['recent_n']} روز)</td>"
            f"<td>{p['recent_within1_pct']}%</td>"
            f"<td class='ltr-inline'>{p['recent_mae']}°{p['unit']}</td>"
            f"<td>{old_str}</td>"
            f"<td class='ltr-inline'>{delta_html}</td>"
            f"<td class='ltr-inline'>{cal_vs_raw}</td>"
            f"<td>{best_sm}</td>"
            f"<td><span class='form-badge form-{p['form_status']}'>{p['form_label']}</span> {_gate_badge_html(p['gate_decision'])}</td>"
            f"</tr>"
        )

    table_html = (
        "<div class='table-scroll'><table id='accuracyLeaderboardTable'>"
        "<thead><tr><th>#</th><th>شهر</th><th>نوع</th><th>دقت باکت دقیق (۱۴ روز اخیر)</th><th>دقت ±۱ باکت</th>"
        "<th>خطای MAE اخیر</th><th>عملکرد قدیم</th><th>تغییر فرم</th>"
        "<th>کالیبره در برابر خام</th><th>بهترین زیرمدل</th><th>وضعیت و تصمیم گیت</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table></div>"
    )

    return f"""
    <h1>📊 جدول دقت پیش‌بینی شهرها و گیت کالیبراسیون هوشمند</h1>
    <div class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-title" style="color:var(--green)">🔥 شهرهای روی فرم (≥۶۰٪ دقت اخیر)</div>
        <div class="kpi-val">{hot_count} شهر</div>
        <div class="kpi-sub">شامل شهرهای اصلاح‌شده با گیت کالیبراسیون هوشمند ۱۴ روزه</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-title" style="color:var(--blue)">🇺🇸 میانگین دقت شهرهای آمریکا</div>
        <div class="kpi-val">{us_avg}%</div>
        <div class="kpi-sub">با احتساب باکت‌های ۲ درجه‌ای فارنهایت و اصلاح کالیبراسیون</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-title" style="color:var(--purple)">🔄 شهرهای ارتقایافته پس از اصلاح</div>
        <div class="kpi-val">{reverted_count} شهر</div>
        <div class="kpi-sub">حذف اورکورکشن و بازگشت شهرهای آسیب‌دیده به دقت بالا</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-title" style="color:var(--amber)">🛑 پایش چرخش فصلی (۵ روز به ۳۰ روز)</div>
        <div class="kpi-val">{flips_count} شهر</div>
        <div class="kpi-sub">محافظت خودکار در برابر تغییر علامت بایاس فصلی</div>
      </div>
    </div>
    <div class="toolbar" style="margin-bottom:12px">
      <input type="text" id="accCitySearch" placeholder="جستجوی شهر در جدول دقت..." oninput="filterAccuracyTable()">
      <button class="tab-btn active-blue" data-acc-type-btn="max" onclick="setAccuracyFilter('type','max')">🔥 دمای حداکثر (Max)</button>
      <button class="tab-btn" data-acc-type-btn="min" onclick="setAccuracyFilter('type','min')">❄️ دمای حداقل (Min)</button>
      <button class="tab-btn" data-acc-type-btn="all" onclick="setAccuracyFilter('type','all')">همه بازارها</button>
      <button class="tab-btn active-blue" data-acc-region-btn="all" onclick="setAccuracyFilter('region','all')">🌍 همه کشورها</button>
      <button class="tab-btn" data-acc-region-btn="us" onclick="setAccuracyFilter('region','us')">🇺🇸 فقط شهرهای آمریکا</button>
      <button class="tab-btn" data-acc-region-btn="non_us" onclick="setAccuracyFilter('region','non_us')">🌐 غیرآمریکایی (°C)</button>
    </div>
    {table_html}
    """


def build_simple_dashboard():
    all_max = _load_all_markets()
    all_min = _load_all_min_markets()
    profiles = _build_city_form_profiles(all_max + all_min)

    markets = [
        _enrich_market_with_smart_gate(m, profiles.get((m.get("city"), "max")))
        for m in all_max
        if m.get("status") == "open" and m.get("full_distribution")
    ]
    min_markets = [
        _enrich_market_with_smart_gate(m, profiles.get((m.get("city"), "min")) or profiles.get((m.get("city"), "max")))
        for m in all_min
        if m.get("status") == "open" and m.get("full_distribution")
    ]
    locks = _load_locks()

    body_html = _locked_signals_section_html(locks) + _build_group_section(markets, _date_block_html, profiles, market_type="max")
    min_body_html = _build_group_section(min_markets, _date_block_html_min, profiles, market_type="min")

    history_html, history_city_options, history_date_options = _history_table_html(locks)
    daily_pnl_html = _daily_pnl_section_html(locks)
    pro_tab_html = _build_pro_tab_html(all_max + all_min, markets, min_markets, profiles)
    accuracy_tab_html = _build_accuracy_tab_html(profiles)

    scan_iso, scan_kind = _latest_scan_info()
    reference_dt = datetime.fromisoformat(scan_iso) if scan_iso else datetime.now(timezone.utc)

    html = HTML_TEMPLATE
    html = html.replace("LASTUPDATE", _iran_time_str(reference_dt))
    html = html.replace("LASTSCANISO", scan_iso or "")
    html = html.replace("LASTSCANKIND", scan_kind or "")
    html = html.replace("LASTSCANFALLBACK", "بدون سابقهٔ اسکن" if not scan_iso else "")
    html = html.replace("MIN_BODYHTML", min_body_html)
    html = html.replace("BODYHTML", body_html)
    html = html.replace("DAILYPNLHTML", daily_pnl_html)
    html = html.replace("HISTORYHTML", history_html)
    html = html.replace("HISTORYCITYOPTIONS", history_city_options)
    html = html.replace("HISTORYDATEOPTIONS", history_date_options)
    html = html.replace("PRO_TAB_HTML", pro_tab_html)
    html = html.replace("ACCURACY_TAB_HTML", accuracy_tab_html)
    html = html.replace("GITHUB_REPO_PLACEHOLDER", GITHUB_REPO)

    OUTPUT_FILE.write_text(html, encoding="utf-8")
    if DOCS_OUTPUT_FILE.parent.exists():
        try:
            shutil.copyfile(OUTPUT_FILE, DOCS_OUTPUT_FILE)
        except Exception:
            pass
    return str(OUTPUT_FILE.resolve())


if __name__ == "__main__":
    path = build_simple_dashboard()
    print(f"simple.html ساخته شد: {path}")
