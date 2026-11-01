"""
dashboard_simple.py -- می‌سازد simple.html (و docs/simple.html):
نسخهٔ کامل، موبایل‌فرندلی و همگام‌شده با پیش‌نمایش AI Studio شامل:
  ۱) نوار ابزار بالایی با دکمهٔ «↻ بروزرسانی» و سوییچر بین ۲ تب:
     - داشبورد ساده (Simple)
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
import os
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

GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY") or "ericlamar008/WeatherBotModifiedV1"
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

    cal_data = {}
    cal_path = Path("data/calibration.json")
    if cal_path.exists():
        try:
            cal_data = json.loads(cal_path.read_text(encoding="utf-8"))
        except Exception:
            cal_data = {}

    grouped = defaultdict(list)
    for r in acc_records:
        grouped[(r["city"], r["market_type"])].append(r)

    # Ensure every city in LOCATIONS has a profile even if local market JSONs were partially synced
    for c_slug in LOCATIONS:
        if (c_slug, "max") not in grouped and f"{c_slug}_D0" in cal_data:
            grouped[(c_slug, "max")] = []

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
        r_mae = sum(r["abs_error"] for r in active_recent) / max(1, len(active_recent)) if active_recent else (1.2 if is_us else 0.65)
        r_bias = sum(r["signed_bias"] for r in active_recent) / max(1, len(active_recent)) if active_recent else 0.0

        recent_n_eff = len(active_recent)
        recent_hit_pct = round(r_hits / len(active_recent) * 100, 1) if active_recent else 0.0
        recent_w1_pct = round(r_w1 / len(active_recent) * 100, 1) if active_recent else 0.0

        old_n_eff = len(older)
        old_hit_pct = None
        old_mae = None
        old_bias = None
        if len(older) >= 3:
            o_hits = sum(1 for r in older if _is_exact_bucket_hit(r["forecast_mean"], r["actual_temp"], is_us))
            old_hit_pct = round(o_hits / len(older) * 100, 1)
            old_mae = round(sum(r["abs_error"] for r in older) / len(older), 2)
            old_bias = round(sum(r["signed_bias"] for r in older) / len(older), 2)

        # Blend with verified 14-day & 55-day statistics in data/calibration.json when local market JSONs are < 10
        cal_key = f"{slug}_min_D0" if mtype == "min" else f"{slug}_D0"
        cal_entry = cal_data.get(cal_key)
        if cal_entry and len(active_recent) < 10:
            cal_mae = float(cal_entry.get("live_mae") or cal_entry.get("base_mae") or r_mae)
            cal_n_total = int(cal_entry.get("n_live") or cal_entry.get("n_samples") or 30)
            cal_n_rec = max(int(cal_entry.get("recent_14d_n") or 14), 10)
            s_eff = max(0.28, cal_mae / 0.9488)
            w0 = 1.0 if is_us else 0.52
            w1 = 3.0 if is_us else 1.52
            cal_hit_prob = max(0.15, min(0.92, 2.0 * _student_t_cdf5(w0 / s_eff) - 1.0))
            cal_w1_prob = max(cal_hit_prob, min(0.99, 2.0 * _student_t_cdf5(w1 / s_eff) - 1.0))

            prior_rec_n = max(0, cal_n_rec - len(active_recent))
            recent_n_eff = len(active_recent) + prior_rec_n
            if recent_n_eff > 0:
                r_mae = (sum(r["abs_error"] for r in active_recent) + prior_rec_n * cal_mae) / recent_n_eff
                cal_rec_bias = -float(cal_entry.get("recent_14d_bias") or cal_entry.get("live_bias_raw") or 0.0)
                r_bias = (sum(r["signed_bias"] for r in active_recent) + prior_rec_n * cal_rec_bias) / recent_n_eff
                recent_hit_pct = round((r_hits + prior_rec_n * cal_hit_prob) / recent_n_eff * 100, 1)
                recent_w1_pct = round((r_w1 + prior_rec_n * cal_w1_prob) / recent_n_eff * 100, 1)

            if old_hit_pct is None and cal_n_total > recent_n_eff:
                old_n_eff = cal_n_total - len(active_recent)
                old_mae = round(float(cal_entry.get("base_mae") or cal_mae), 2)
                s_old = max(0.28, old_mae / 0.9488)
                old_hit_pct = round(max(0.15, min(0.92, 2.0 * _student_t_cdf5(w0 / s_old) - 1.0)) * 100, 1)
                old_bias = round(-float(cal_entry.get("live_bias_raw") or cal_entry.get("base_bias_raw") or 0.0), 2)

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
        submodel_rmses = {}
        submodel_weights = {}
        best_submodel, worst_submodel = None, None
        best_mae, worst_mae = 999.0, -1.0
        for mname, errs in submodel_errs.items():
            if len(errs) >= 2:
                # وزن‌دهی نمایی (EWMA) با اولویت روزهای اخیر برای محاسبه RMSE و BMA
                n_e = len(errs)
                ew_w = [math.exp(-0.10 * (n_e - 1 - idx)) for idx in range(n_e)]
                w_tot = sum(ew_w) or 1.0
                m_mae = round(sum(e * w for e, w in zip(errs, ew_w)) / w_tot, 2)
                m_rmse = round(math.sqrt(sum((e * e) * w for e, w in zip(errs, ew_w)) / w_tot), 2)
                submodel_maes[mname] = m_mae
                submodel_rmses[mname] = m_rmse
                if m_mae < best_mae:
                    best_mae = m_mae
                    best_submodel = mname
                if m_mae > worst_mae:
                    worst_mae = m_mae
                    worst_submodel = mname

        if submodel_rmses:
            min_rmse = max(0.25, min(submodel_rmses.values()))
            raw_w = {}
            for mname, v in submodel_rmses.items():
                ratio = v / min_rmse
                penalty = 0.20 if ratio > 1.35 else (0.55 if ratio > 1.18 else 1.0)
                raw_w[mname] = (1.0 / (v * v + 0.12)) * penalty
            inv_sum = sum(raw_w.values()) or 1.0
            for mname, rw in raw_w.items():
                submodel_weights[mname] = round(rw / inv_sum, 3)

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
        elif best_submodel and best_mae + 0.20 < r_mae:
            gate_decision = "smart_submodel_blend"
            gate_reason = f"وزن‌دهی هوشمند زیرمدل‌ها (برترین مدل اخیر: {best_submodel.upper()} با خطای {best_mae}°)"

        if recent_hit_pct >= 58 or (recent_hit_pct >= 48 and r_mae <= (1.15 if is_us else 0.58)):
            form_status = "hot"
            form_label = f"🔥 روی فرم ({recent_hit_pct}%)"
        elif hit_delta is not None and hit_delta <= -20:
            form_status = "cold_drop"
            form_label = f"⚠️ افت اخیر ({recent_hit_pct}% vs {old_hit_pct}% قدیم)"
        elif recent_hit_pct >= 42:
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
            "recent_n": recent_n_eff,
            "recent_hit_pct": recent_hit_pct,
            "recent_within1_pct": recent_w1_pct,
            "recent_mae": round(r_mae, 2),
            "recent_bias": round(r_bias, 2),
            "old_n": old_n_eff,
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
    unit = m.get("unit") or "C"
    base_sigma = float(m.get("sigma") or (1.8 if unit == "F" else 1.2))
    smart_mean = f_cal

    # ۱) وزن‌دهی بیزین پویا (Dynamic BMA) و انحراف معیار پویا (Dynamic Sigma)
    bma_weights = (profile.get("submodel_weights") if profile else None) or {}
    valid_models = {mk: float(mv) for mk, mv in models_raw.items() if mv is not None}
    bma_mean = None
    model_spread = m.get("model_spread")

    if valid_models:
        w_sum, v_sum = 0.0, 0.0
        for mk, mv in valid_models.items():
            w = float(bma_weights.get(mk, 1.0 / len(valid_models)))
            v_sum += mv * w
            w_sum += w
        if w_sum > 0:
            bma_mean = v_sum / w_sum
            if model_spread is None and len(valid_models) >= 2:
                var_s = sum((float(bma_weights.get(mk, 1.0 / len(valid_models))) / w_sum) * ((mv - bma_mean) ** 2) for mk, mv in valid_models.items())
                model_spread = round(math.sqrt(max(0.0, var_s)), 3)

    m["model_spread"] = model_spread

    if profile and f_cal is not None:
        gd = profile["gate_decision"]
        if gd == "revert_to_shadow" and f_sh is not None:
            smart_mean = round(float(f_sh), 2)
        elif gd == "smart_submodel_blend" and bma_mean is not None:
            smart_mean = round(0.45 * float(f_cal) + 0.55 * float(bma_mean), 2)
        elif gd == "sign_flip_damped":
            if f_sh is not None:
                smart_mean = round(0.5 * float(f_cal) + 0.5 * float(f_sh), 2)
            else:
                smart_mean = round(float(f_cal) + profile["recent_bias"] * 0.4, 2)
        elif bma_mean is not None:
            smart_mean = round(0.65 * float(f_cal) + 0.35 * float(bma_mean), 2)

    # تعدیل پویا بر اساس واگرایی زیرمدل‌ها (Dynamic Sigma)
    sigma = base_sigma
    if model_spread is not None and model_spread > 0:
        spread_thresh = 1.1 if unit == "F" else 0.6
        if model_spread > spread_thresh:
            infl = min(0.50, 0.22 * (model_spread - spread_thresh))
            sigma = round(base_sigma * (1.0 + infl), 2)

    m["smart_forecast_mean"] = smart_mean
    m["smart_sigma"] = sigma

    is_min = (m.get("market_type") == "min")
    obs_bound = m.get("observed_intraday_temp")
    comp_sigma = max(0.55 if unit == "C" else 0.95, sigma * 0.78)
    raw_consensus = (sum(valid_models.values()) / len(valid_models)) if valid_models else None
    cal_shift = (float(smart_mean) - raw_consensus) if (smart_mean is not None and raw_consensus is not None) else 0.0

    full_dist = m.get("full_distribution") or []
    if full_dist and smart_mean is not None:
        raw_probs = []
        eliminated_flags = []
        for b in full_dist:
            rng = b.get("range") or [-999, 999]
            low, high = rng[0], rng[1]

            # ۲) فیلتر کف/سقف دمای واقعی ثبت‌شده در روز جاری (Intraday METAR Floor/Ceiling)
            already_elim = bool(b.get("intraday_eliminated", False))
            if not already_elim and obs_bound is not None:
                try:
                    ob_val = float(obs_bound)
                    if not is_min and high < 998 and (high + 0.49) < ob_val:
                        already_elim = True
                    elif is_min and low > -998 and (low - 0.49) > ob_val:
                        already_elim = True
                except Exception:
                    pass

            if already_elim:
                eliminated_flags.append(True)
                raw_probs.append(0.0)
                continue

            eliminated_flags.append(False)
            c_high = 1.0 if high >= 998 else _student_t_cdf5((high + 0.5 - smart_mean) / sigma)
            c_low = 0.0 if low <= -998 else _student_t_cdf5((low - 0.5 - smart_mean) / sigma)
            p_cons = max(0.0005, c_high - c_low)

            # ۳) توزیع ترکیبی چندمدلی (Multi-Model Mixture / GMM) برای حفظ دوکوهانگی
            if len(valid_models) >= 2:
                p_mix_sub = 0.0
                w_tot = 0.0
                for mk, mv in valid_models.items():
                    w = float(bma_weights.get(mk, 1.0 / len(valid_models)))
                    m_loc = mv + cal_shift
                    ch_m = 1.0 if high >= 998 else _student_t_cdf5((high + 0.5 - m_loc) / comp_sigma)
                    cl_m = 0.0 if low <= -998 else _student_t_cdf5((low - 0.5 - m_loc) / comp_sigma)
                    p_mix_sub += w * max(0.0005, ch_m - cl_m)
                    w_tot += w
                if w_tot > 0:
                    p_mix_sub /= w_tot
                    p_final = 0.55 * p_cons + 0.45 * p_mix_sub
                else:
                    p_final = p_cons
            else:
                p_final = p_cons

            raw_probs.append(max(0.0005, p_final))

        p_sum = sum(raw_probs)
        best_id, best_belief = None, -1.0
        for idx, b in enumerate(full_dist):
            is_elim = eliminated_flags[idx]
            b["intraday_eliminated"] = is_elim
            if is_elim:
                smp = 0.0
            elif p_sum > 0:
                smp = round(raw_probs[idx] / p_sum, 4)
            else:
                smp = round(float(b.get("model_prob") or 0.0), 4)

            yp = float(b.get("yes_price") or b.get("ask") or 0.001)
            best_bid = b.get("best_bid")
            best_ask = b.get("best_ask")
            spread = b.get("bid_ask_spread")
            if spread is None and best_bid is not None and best_ask is not None:
                try:
                    spread = round(max(0.0, float(best_ask) - float(best_bid)), 4)
                except Exception:
                    spread = 0.0
            b["bid_ask_spread"] = spread or 0.0

            # ۴) جریمه اسپرد اردربوک و محاسبه پتانسیل رشد نوسان‌گیری (Swing Upside)
            eff_ask = float(b.get("effective_ask") or yp)
            if spread and spread > 0.03 and eff_ask <= yp:
                eff_ask = min(0.99, round(yp + 0.35 * (spread - 0.03), 4))

            if is_elim:
                sbp = 0.0
                sev = -1.0
                swing_edge = round(-yp, 4)
                swing_upside_pct = -100.0
            else:
                sbp = round(0.5 * smp + 0.5 * yp, 4)
                sev = round((sbp - eff_ask) / eff_ask, 4) if eff_ask > 0.001 else b.get("ev")
                swing_edge = round(smp - yp, 4)
                swing_upside_pct = round((swing_edge / yp) * 100.0, 1) if yp >= 0.01 else 0.0

            b["smart_model_prob"] = smp
            b["smart_belief_prob"] = sbp
            b["smart_ev"] = sev
            b["swing_edge"] = swing_edge
            b["swing_upside_pct"] = swing_upside_pct
            if not is_elim and sbp > best_belief:
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
<title>WeatherBet -- داشبورد ساده</title>
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
.container{max-width:1280px;margin:0 auto;padding:16px 20px}
.top-header-bar{
  display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;
  padding-bottom:12px;margin-bottom:16px;border-bottom:1px solid var(--border);
}
.header-left{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.header-title{font-size:19px;font-weight:700;margin:0;color:var(--text)}
.meta-pill{
  font-size:12px;padding:4px 10px;border-radius:6px;font-weight:500;
  background:var(--surface);color:var(--text-dim);border:1px solid var(--border);
}
.gate-pill{
  font-size:12px;padding:4px 10px;border-radius:6px;font-weight:600;
  background:var(--green-bg);color:var(--green);border:1px solid #16a34a55;
}
.nav-tabs{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tab-btn{
  border:1px solid var(--border);background:var(--surface);color:var(--text);
  border-radius:8px;padding:7px 13px;font-size:12px;font-weight:600;cursor:pointer;
  font-family:inherit;transition:all .15s ease;
}
.tab-btn:hover{background:#f8fafc}
.tab-btn.active-blue{background:#2563eb;color:#fff;border-color:#2563eb;box-shadow:0 1px 2px rgba(37,99,235,0.2)}
.refresh-btn{
  border:1px solid var(--border);background:var(--surface);color:#2563eb;
  border-radius:8px;padding:7px 13px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;
}
.refresh-btn:hover{background:#f8fafc}
h1{font-size:19px;margin:6px 0 8px;font-weight:700;color:var(--text)}
h2{font-size:15.5px;margin-top:24px;margin-bottom:12px;border-bottom:1px solid var(--border);padding-bottom:8px;scroll-margin-top:24px;font-weight:700}
.subtitle-bar{font-size:12.5px;color:var(--text-dim);margin-bottom:14px;line-height:1.65}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
.toolbar input[type=text]{
  flex:1;min-width:180px;background:var(--surface);border:1px solid var(--border);
  color:var(--text);border-radius:10px;padding:10px 14px;font-size:13.5px;
}
.jump-btn{
  display:inline-block;background:var(--accent);color:#fff;border-radius:10px;
  padding:10px 14px;font-size:12.5px;font-weight:600;text-decoration:none;white-space:nowrap;
}
.jump-btn:hover{background:#1d4ed8;text-decoration:none}
.smart-toggles{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:16px}
.toggle-label{
  display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:500;
  padding:8px 12px;border-radius:10px;border:1px solid var(--border);background:var(--surface);cursor:pointer;user-select:none;
}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
details.city-block{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:10px 14px;margin-bottom:10px;box-shadow:0 1px 2px rgba(20,30,60,0.04)}
details.date-block{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:10px 14px;margin:8px 0}
summary{cursor:pointer;font-size:14px;color:var(--text);list-style:none;padding:6px 4px;font-weight:500}
summary::-webkit-details-marker{display:none}
summary::before{content:"";display:inline-block;width:0;height:0;border-top:4px solid transparent;border-bottom:4px solid transparent;border-left:5px solid var(--text-dim);margin-left:8px;vertical-align:middle;transition:transform .12s ease}
details[open]>summary::before{transform:rotate(90deg)}
.main-badge{background:var(--green-bg);color:var(--green);border:1px solid #16a34a55;border-radius:6px;padding:2px 8px;font-size:10.5px;font-weight:600;white-space:nowrap;margin-right:5px}
.form-badge{border-radius:6px;padding:2px 8px;font-size:10.5px;font-weight:600;white-space:nowrap;margin-right:5px;border:1px solid}
.form-hot{background:var(--green-bg);color:var(--green);border-color:#16a34a55}
.form-stable{background:var(--blue-bg);color:var(--blue);border-color:#2563eb44}
.form-cold_drop{background:var(--amber-bg);color:var(--amber);border-color:#b4530955}
.form-weak{background:var(--red-bg);color:var(--red);border-color:#dc262644}
.gate-badge{background:var(--purple-bg);color:var(--purple);border:1px solid #9333ea44;border-radius:6px;padding:2px 7px;font-size:10.5px;font-weight:600;margin-right:5px}
.signal-badge{border-radius:6px;padding:2px 8px;font-size:10.5px;font-weight:600;white-space:nowrap;margin-right:6px;border:1px solid;display:inline-block}
.signal-blue{background:var(--blue-bg);color:var(--blue);border-color:#2563eb55}
.signal-purple{background:var(--purple-bg);color:var(--purple);border-color:#9333ea55}
.signal-green{background:var(--green-bg);color:var(--green);border-color:#16a34a55}
.time-note{color:var(--text-dim);font-size:11.5px;direction:ltr;unicode-bidi:embed;display:inline-block}
.ltr-inline{direction:ltr;unicode-bidi:embed;display:inline-block}
td.ltr-inline,th.ltr-inline{display:table-cell}
.chg-pos{color:var(--green);font-weight:600;font-size:11.5px;direction:ltr;unicode-bidi:embed;display:inline-block;margin-right:3px}
.chg-neg{color:var(--red);font-weight:600;font-size:11.5px;direction:ltr;unicode-bidi:embed;display:inline-block;margin-right:3px}
.chg-zero{color:var(--text-dim);font-size:11px;direction:ltr;unicode-bidi:embed;display:inline-block;margin-right:3px}
.submodel-bar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:6px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px;margin-top:8px;font-size:11.5px;color:var(--text-dim)}
.sm-chip{padding:2px 8px;border-radius:6px;border:1px solid var(--border);background:var(--surface-2);color:var(--text);direction:ltr;unicode-bidi:embed;display:inline-block;margin-left:4px;font-size:11px}
.sm-best{background:var(--green-bg);color:var(--green);border-color:#16a34a55;font-weight:600}
.sm-worst{background:var(--red-bg);color:var(--red);border-color:#dc262644;text-decoration:line-through;opacity:0.75}
.locked-section{border:1px solid #16a34a55;background:var(--green-bg)}
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin-top:10px;border-radius:8px;border:1px solid var(--border);background:var(--surface)}
table{width:100%;min-width:580px;border-collapse:collapse;font-size:13px;background:var(--surface);font-variant-numeric:tabular-nums}
th,td{padding:10px 8px;text-align:center;border-bottom:1px solid var(--border);white-space:nowrap;vertical-align:middle}
th{color:var(--text-dim);font-weight:600;font-size:11.5px;background:var(--surface-2)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(246,248,251,0.65)}
.smart-main-row td{background:rgba(232,248,238,0.45)}
.lock-btn,.unlock-btn,.edit-btn{border:none;border-radius:9px;padding:7px 13px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;transition:all .15s}
.lock-btn{background:var(--accent);color:#fff;box-shadow:0 1px 2px rgba(37,99,235,0.2)}
.lock-btn:hover{background:#1d4ed8}
.edit-btn{background:var(--accent);color:#fff;padding:5px 10px;font-size:11px;border-radius:8px}
.edit-btn:hover{background:#1d4ed8}
.unlock-btn{background:var(--red);color:#fff;padding:5px 10px;font-size:11px;border-radius:8px}
.unlock-btn:hover{background:#b91c1c}
.locked-badge{color:var(--green);font-size:11px;font-weight:700;display:block;margin-bottom:4px}
.lock-status{display:block;font-size:10.5px;margin-top:4px;line-height:1.35;max-width:170px;margin-left:auto;margin-right:auto}
.empty{color:var(--text-dim);font-style:italic;padding:14px 4px;text-align:center}
.win{color:var(--green);font-weight:600}
.loss{color:var(--red);font-weight:600}
.history-filters{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0;align-items:center;justify-content:space-between}
.history-filters select{background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:10px;padding:8px 12px;font-size:12.5px;font-family:inherit}
.download-btn{display:inline-block;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:8px 16px;font-size:12.5px;font-weight:600;text-decoration:none}
.download-btn:hover{border-color:var(--accent);text-decoration:none}
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-bottom:14px}
.kpi-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px}
.kpi-title{font-size:11.5px;font-weight:700;margin-bottom:4px}
.kpi-val{font-size:21px;font-weight:800}
.kpi-sub{font-size:11px;color:var(--text-dim);margin-top:4px}

/* OrderConfigModal Styles (100% parity with AI Studio Preview) */
.modal-backdrop{
  position:fixed;inset:0;z-index:999;background:rgba(15,23,42,0.62);
  backdrop-filter:blur(3px);display:none;align-items:center;justify-content:center;
  padding:12px;overflow-y:auto;
}
.modal-backdrop.open{display:flex}
.modal-card{
  background:#fff;border:1px solid var(--border);border-radius:16px;
  box-shadow:0 24px 48px rgba(0,0,0,0.28);width:100%;max-width:768px;
  overflow:hidden;margin:auto;
}
.modal-header{
  background:#0f172a;color:#fff;padding:16px 20px;
  display:flex;align-items:center;justify-content:space-between;gap:12px;
}
.modal-close-btn{
  width:32px;height:32px;border-radius:8px;background:rgba(255,255,255,0.12);
  color:#fff;border:none;font-size:16px;font-weight:700;cursor:pointer;display:flex;align-items:center;justify-content:center;
}
.modal-close-btn:hover{background:rgba(255,255,255,0.22)}
.modal-account-bar{
  background:#f8fafc;border-bottom:1px solid var(--border);padding:8px 20px;
  display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;font-size:11.5px;
}
.modal-body{padding:16px 20px;max-height:82vh;overflow-y:auto}
.modal-grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.modal-grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.modal-grid-2{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
.stat-mini{background:#fff;border:1px solid #e5eaf2;border-radius:8px;padding:8px;text-align:center}
.autopilot-box{
  border-radius:12px;padding:14px;border:1px solid var(--border);background:var(--surface-2);margin-top:12px;transition:all .15s;
}
.autopilot-box.active{background:var(--green-bg);border-color:#16a34a88}
.mode-btn{
  padding:8px 10px;border-radius:8px;font-size:11.5px;font-weight:700;cursor:pointer;
  border:1px solid var(--border);background:#fff;color:#4b5563;font-family:inherit;width:100%;transition:all .15s;
}
.mode-btn.active-blue{background:#2563eb;color:#fff;border-color:#2563eb}
.mode-btn.active-purple{background:#9333ea;color:#fff;border-color:#9333ea}
.mode-btn.active-green{background:#16a34a;color:#fff;border-color:#16a34a}
.mode-btn.active-red{background:#dc2626;color:#fff;border-color:#dc2626}
.mode-btn.active-dark{background:#1f2430;color:#fff;border-color:#1f2430}
.modal-input{
  width:100%;background:#fff;border:1px solid var(--border);border-radius:8px;
  padding:8px 10px;font-size:13.5px;font-weight:700;text-align:center;font-family:inherit;
}
.preset-btn{
  padding:4px 9px;border-radius:6px;font-size:11.5px;font-weight:700;cursor:pointer;
  border:1px solid var(--border);background:#fff;color:var(--text);font-family:inherit;
}
.preset-btn.active{background:#2563eb;color:#fff;border-color:#2563eb}
.rr-bar{width:100%;height:10px;background:#e5e7eb;border-radius:999px;overflow:hidden;display:flex;margin-top:6px}
.modal-footer{
  display:flex;align-items:center;justify-content:flex-end;gap:10px;
  padding-top:12px;margin-top:14px;border-top:1px solid var(--border);
}
.submit-order-btn{
  border:none;border-radius:12px;padding:10px 20px;font-size:13px;font-weight:700;
  color:#fff;cursor:pointer;font-family:inherit;background:#16a34a;transition:all .15s;
}
.submit-order-btn.purple{background:#9333ea}
.submit-order-btn:hover{filter:brightness(0.93)}
@media (max-width: 680px){
  .container{padding:12px}
  .modal-grid-4,.modal-grid-3,.modal-grid-2{grid-template-columns:1fr}
  th,td{padding:8px 5px;font-size:11.8px}
}
</style>
</head>
<body>
<div class="container">
  <!-- Top Mode Switcher Bar (Matches AI Studio Preview) -->
  <div class="top-header-bar">
    <div class="header-left">
      <h1 id="mainPageTitle" class="header-title">WeatherBet -- داشبورد ساده</h1>
      <span class="meta-pill">OPEN_MARKETS_COUNT بازار باز • OPEN_LOCKS_COUNT قفل فعال</span>
      <span class="gate-pill">🛡️ گیت کالیبراسیون هوشمند (Champion-Challenger 14d) فعال</span>
    </div>
    <div class="nav-tabs">
      <button id="tabBtnSimple" class="tab-btn active-blue" onclick="switchTab('simple')">داشبورد ساده (Simple)</button>
      <button id="tabBtnAccuracy" class="tab-btn" onclick="switchTab('accuracy')">📊 دقت شهرها و کالیبراسیون هوشمند</button>
      <button id="refreshBtn" class="refresh-btn" onclick="refreshDashboardNow()">↻ بروزرسانی</button>
    </div>
  </div>

  <!-- TAB 1: SIMPLE DASHBOARD -->
  <div id="view-simple">
    <div class="subtitle-bar">
      آخرین به‌روزرسانی: <b style="color:var(--text)">LASTUPDATE</b> (به وقت ایران)
      <span class="time-note relative-time" data-ts="LASTSCANISO" data-kind="LASTSCANKIND">LASTSCANFALLBACK</span>
      <br>
      مجهز به <b>ردیابی تغییرات اسکن‌های ۶ ساعته نسبت به اسکن ۶ ساعتهٔ قبلی</b> + <b>گیت کالیبراسیون هوشمند ۱۴ روزه</b> (حذف اورکورکشن فصل پاییز، وزن‌دهی زیرمدل‌ها و تصحیح باکت‌های ۲ درجه‌ای آمریکا)
    </div>

    <div class="toolbar">
      <input type="text" id="citySearch" placeholder="جستجوی شهر (مثلاً Atlanta, Chicago, Cape Town, Seoul)..." oninput="filterCities()">
      <a class="jump-btn" href="#max-section">🔥 حداکثر (MAX_CITIES_COUNT)</a>
      <a class="jump-btn" href="#min-section">❄️ حداقل (MIN_CITIES_COUNT)</a>
      <a class="jump-btn" href="#daily-section">📅 آمار روزانه</a>
      <a class="jump-btn" href="#history-section">مشاهدهٔ نتایج ↓</a>
    </div>

    <div class="smart-toggles">
      <label class="toggle-label" style="border-color:#16a34a66;font-weight:600">
        <input type="checkbox" id="useSmartGateToggle" checked onchange="filterCities()">
        🛡️ اعمال گیت کالیبراسیون هوشمند (پیشنهادی)
      </label>
      <label class="toggle-label">
        <input type="checkbox" id="onlyGoodCitiesToggle" onchange="filterCities()">
        🔥 فقط شهرهای روی فرم (دقت اخیر بالای ۵۰٪)
      </label>
      <label class="toggle-label">
        <input type="checkbox" id="onlyUsCitiesToggle" onchange="filterCities()">
        🇺🇸 فقط شهرهای آمریکا
      </label>
      <label class="toggle-label" style="color:var(--text-dim)">
        <input type="checkbox" id="showAllBucketsToggle" onchange="toggleLowPlausibilityBuckets()">
        نمایش باکت‌های زیر ۵٪
      </label>
    </div>

    LOCKED_SECTION_HTML

    <h2 id="max-section">🔥 دمای حداکثر (MAX_CITIES_COUNT شهر)</h2>
    BODYHTML
    <h2 id="min-section">❄️ دمای حداقل (MIN_CITIES_COUNT شهر)</h2>
    MIN_BODYHTML
    DAILYPNLHTML
    <h2 id="history-section">تاریخچهٔ معاملات</h2>
    <div class="history-filters">
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <select id="historyCityFilter" onchange="filterHistory()"><option value="">همهٔ شهرها</option>HISTORYCITYOPTIONS</select>
        <select id="historyDateFilter" onchange="filterHistory()"><option value="">همهٔ تاریخ‌ها</option>HISTORYDATEOPTIONS</select>
        <button type="button" id="clearHistoryFilterBtn" onclick="clearHistoryFilters()" style="display:none;background:#f6f8fb;border:1px solid var(--border);border-radius:10px;padding:7px 12px;font-size:12px;color:var(--text-dim);cursor:pointer;font-family:inherit">✕ پاک کردن فیلتر</button>
      </div>
      <a class="download-btn" href="lock_history.csv" download>⬇ دانلود CSV کامل</a>
    </div>
    <div class="table-scroll">
    HISTORYHTML
    </div>
  </div>

  <!-- TAB 2: ACCURACY & SMART CALIBRATION -->
  <div id="view-accuracy" style="display:none">
    ACCURACY_TAB_HTML
  </div>
</div>

<!-- =========================================================================
     INTERACTIVE ORDER CONFIG & SMART AUTO-PILOT MODAL (100% Preview Parity)
     ========================================================================= -->
<div id="orderConfigModal" class="modal-backdrop" onclick="closeOrderModal()">
  <div class="modal-card" onclick="event.stopPropagation()">
    <div class="modal-header">
      <div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span id="modalHeaderTitle" style="font-size:16.5px;font-weight:700">⚡ ثبت سفارش هوشمند و تنظیم اتوماتیک سیو سود / حد ضرر</span>
          <span id="modalTempTypeBadge" class="ltr-inline" style="padding:2px 8px;border-radius:6px;font-size:11px;font-weight:700;background:rgba(59,130,246,0.2);color:#93c5fd;border:1px solid rgba(96,165,250,0.3)">🔥 MAX TEMP</span>
        </div>
        <div style="font-size:12px;color:#cbd5e1;margin-top:4px;display:flex;flex-wrap:wrap;gap:8px;align-items:center">
          <span>شهر: <b id="modalCityName" style="color:#fff">-</b></span>
          <span>•</span>
          <span class="ltr-inline">تاریخ: <b id="modalDate" style="color:#fff">-</b></span>
          <span>•</span>
          <span>باکت دما: <b id="modalBucketLabel" class="ltr-inline" style="color:#fcd34d">-</b></span>
        </div>
      </div>
      <button type="button" class="modal-close-btn" onclick="closeOrderModal()">✕</button>
    </div>

    <div class="modal-account-bar">
      <div>
        <b style="color:var(--text)">🦊 وضعیت حساب پلی‌مارکت (MetaMask):</b>
        <span style="color:var(--blue);font-weight:600">آماده اجرا در حالت SHADOW (پایش زندهٔ اوردربوک در اسکن‌های گیت‌هاب)</span>
      </div>
      <div class="ltr-inline" style="font-size:11px;color:var(--text-dim)">Chain: Polygon (137) • Auto-Exit Engine Ready</div>
    </div>

    <div class="modal-body">
      <!-- Telemetry Summary Bar -->
      <div class="modal-grid-4" style="background:var(--surface-2);border:1px solid var(--border);border-radius:12px;padding:10px">
        <div class="stat-mini">
          <div style="font-size:11px;color:var(--text-dim)">قیمت فعلی بازار (<span id="statSideLabel">YES</span>)</div>
          <div id="statMarketPrice" class="ltr-inline" style="font-size:14px;font-weight:700;margin-top:2px">-</div>
        </div>
        <div class="stat-mini">
          <div style="font-size:11px;color:var(--text-dim)">ارزش منصفانه مدل هوشمند</div>
          <div id="statModelFair" class="ltr-inline" style="font-size:14px;font-weight:700;color:var(--blue);margin-top:2px">-</div>
        </div>
        <div class="stat-mini">
          <div style="font-size:11px;color:var(--text-dim)">باور نهایی (Belief)</div>
          <div id="statBelief" class="ltr-inline" style="font-size:14px;font-weight:700;margin-top:2px">-</div>
        </div>
        <div class="stat-mini">
          <div style="font-size:11px;color:var(--text-dim)">امید ریاضی سود (EV)</div>
          <div id="statEv" class="ltr-inline" style="font-size:14px;font-weight:700;margin-top:2px">-</div>
        </div>
      </div>

      <!-- Smart Auto-Pilot Toggle Card -->
      <div id="autopilotCard" class="autopilot-box active">
        <div style="display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px">
          <label style="display:flex;align-items:center;gap:10px;cursor:pointer;user-select:none">
            <input type="checkbox" id="modalSmartAutopilot" checked onchange="onToggleSmartAutopilot(this.checked)" style="width:17px;height:17px;accent-color:#16a34a;cursor:pointer">
            <div>
              <div style="font-size:13.5px;font-weight:700;display:flex;align-items:center;gap:6px;flex-wrap:wrap">
                <span>🤖 تعیین اتوماتیک هوشمند (Smart Auto-Pilot)</span>
                <span style="font-size:10.5px;background:#16a34a;color:#fff;padding:2px 8px;border-radius:6px">پیشنهادی برای جلوگیری از پیچیدگی</span>
              </div>
              <div style="font-size:11.5px;color:#4b5563;margin-top:2px">
                محاسبهٔ خودکار بهترین نقطهٔ ورود، سیو سود منطبق با ارزش منصفانهٔ مدل کالیبره، حد ضرر پله‌ای و محافظ دمای فرودگاه (METAR)
              </div>
            </div>
          </label>
          <button type="button" id="btnSwitchManual" onclick="onToggleSmartAutopilot(false)" style="background:#fff;border:1px solid var(--border);border-radius:8px;padding:5px 10px;font-size:11px;font-weight:600;cursor:pointer;font-family:inherit">
            ⚙️ تغییر به تنظیم دستی
          </button>
        </div>

        <div id="autopilotPreviewGrid" class="modal-grid-4" style="margin-top:12px;padding-top:10px;border-top:1px solid #16a34a33;font-size:12px">
          <div style="background:rgba(255,255,255,0.92);border-radius:8px;padding:6px 10px;border:1px solid #16a34a33">
            <span id="apEntryLabel" style="color:var(--text-dim);display:block;font-size:10.5px">قیمت ورود بازار:</span>
            <b id="apEntryVal" class="ltr-inline" style="font-size:13.5px">-</b>
          </div>
          <div style="background:rgba(255,255,255,0.92);border-radius:8px;padding:6px 10px;border:1px solid #16a34a33">
            <span style="color:var(--text-dim);display:block;font-size:10.5px">🎯 حد سود هوشمند (TP):</span>
            <b id="apTpVal" class="ltr-inline" style="font-size:13.5px;color:var(--green)">-</b>
          </div>
          <div style="background:rgba(255,255,255,0.92);border-radius:8px;padding:6px 10px;border:1px solid #16a34a33">
            <span style="color:var(--text-dim);display:block;font-size:10.5px">🛑 حد ضرر پله‌ای (SL):</span>
            <b id="apSlVal" class="ltr-inline" style="font-size:13.5px;color:var(--red)">-</b>
          </div>
          <div style="background:rgba(255,255,255,0.92);border-radius:8px;padding:6px 10px;border:1px solid #16a34a33">
            <span style="color:var(--text-dim);display:block;font-size:10.5px">🛡️ محافظ‌های آب‌وهوا:</span>
            <b style="font-size:11px;color:var(--blue)">METAR + فروش 99.5¢ فعال</b>
          </div>
        </div>
      </div>

      <!-- Row 1: Execution Mode + Entry Order Type -->
      <div class="modal-grid-2" style="margin-top:12px">
        <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:12px;padding:12px">
          <div style="font-size:12px;font-weight:700;margin-bottom:8px">۱. حالت اجرای ربات (Execution Mode)</div>
          <div class="modal-grid-3" style="gap:6px">
            <button type="button" id="execBtnPAPER" class="mode-btn" onclick="setModalExecMode('PAPER')">🧪 PAPER (تست)</button>
            <button type="button" id="execBtnSHADOW" class="mode-btn active-blue" onclick="setModalExecMode('SHADOW')">🛡️ SHADOW (پیشنهادی)</button>
            <button type="button" id="execBtnLIVE" class="mode-btn" onclick="setModalExecMode('LIVE')">🔥 LIVE (پول واقعی)</button>
          </div>
          <div id="execModeDesc" style="font-size:10.5px;color:var(--text-dim);margin-top:6px">
            در حالت SHADOW، ربات روی اوردربوک زنده پلی‌مارکت تصمیم می‌گیرد و شبیه‌سازی واقعی انجام می‌دهد (بدون کسر پول).
          </div>
        </div>

        <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:12px;padding:12px">
          <div style="font-size:12px;font-weight:700;margin-bottom:8px">۲. نوع ورود به معامله (فوری یا خرید شرطی)</div>
          <div class="modal-grid-2" style="gap:6px">
            <button type="button" id="orderTypeImmediate" class="mode-btn active-blue" onclick="setModalOrderType('IMMEDIATE_MARKETABLE')">⚡ خرید فوری در قیمت بازار</button>
            <button type="button" id="orderTypeConditional" class="mode-btn" onclick="setModalOrderType('CONDITIONAL_LIMIT_GTC')">⏳ خرید شرطی (Limit Buy)</button>
          </div>
          <div id="orderTypeDesc" style="font-size:10.5px;color:var(--text-dim);margin-top:6px">
            همین لحظه در قیمت فعلی بازار خریداری شده و بلافاصله چتر TP/SL فعال می‌شود.
          </div>
        </div>
      </div>

      <!-- Row 2: Side + Entry Price (Cents) + USDC Amount -->
      <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:12px;padding:12px;margin-top:12px">
        <div class="modal-grid-3" style="align-items:end">
          <div>
            <label style="display:block;font-size:12px;font-weight:700;margin-bottom:4px">سمت معامله (Side)</label>
            <div class="modal-grid-2" style="gap:6px">
              <button type="button" id="sideBtnYES" class="mode-btn active-green" onclick="setModalSide('YES')">خرید YES</button>
              <button type="button" id="sideBtnNO" class="mode-btn" onclick="setModalSide('NO')">خرید NO</button>
            </div>
          </div>
          <div>
            <label id="entryInputLabel" style="display:block;font-size:12px;font-weight:700;margin-bottom:4px">قیمت مبنای ورود (سنت ¢)</label>
            <div style="display:flex;align-items:center;gap:6px">
              <input type="number" id="modalEntryCents" class="modal-input ltr-inline" step="0.1" min="0.5" max="99.5" oninput="onManualEntryCentsChange(this.value)">
              <span style="font-size:12px;font-weight:700;color:var(--text-dim)">¢</span>
            </div>
          </div>
          <div>
            <label style="display:block;font-size:12px;font-weight:700;margin-bottom:4px">مبلغ سرمایه‌گذاری ($USDC)</label>
            <div style="display:flex;align-items:center;gap:6px">
              <input type="number" id="modalUsdcAmount" class="modal-input ltr-inline" step="1" min="1" value="15" oninput="onModalUsdcChange(this.value)">
              <span style="font-size:12px;font-weight:700;color:var(--text-dim)">$</span>
            </div>
          </div>
        </div>

        <div style="display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;margin-top:10px;padding-top:10px;border-top:1px solid var(--border)">
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
            <span style="font-size:11px;color:var(--text-dim)">انتخاب سریع مبلغ:</span>
            <button type="button" class="preset-btn" onclick="onModalUsdcChange(5)">$5</button>
            <button type="button" class="preset-btn" onclick="onModalUsdcChange(10)">$10</button>
            <button type="button" class="preset-btn active" onclick="onModalUsdcChange(15)">$15</button>
            <button type="button" class="preset-btn" onclick="onModalUsdcChange(25)">$25</button>
            <button type="button" class="preset-btn" onclick="onModalUsdcChange(50)">$50</button>
            <button type="button" class="preset-btn" onclick="onModalUsdcChange(100)">$100</button>
          </div>
          <div style="font-size:12px;background:#fff;border:1px solid var(--border);border-radius:8px;padding:4px 12px">
            تعداد سهام دریافتی: <b id="modalSharesDisplay" class="ltr-inline" style="color:var(--blue)">-</b>
          </div>
        </div>
      </div>

      <!-- Manual / Advanced TP & SL Controls (Shown when Smart Auto-Pilot is off) -->
      <div id="manualTpSlSection" style="display:none;background:var(--surface-2);border:1px solid var(--border);border-radius:12px;padding:12px;margin-top:12px">
        <div style="display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;padding-bottom:8px;border-bottom:1px solid var(--border);margin-bottom:10px">
          <label style="display:flex;align-items:center;gap:6px;font-size:12px;font-weight:700;cursor:pointer">
            <input type="checkbox" id="modalAutoExitEnabled" checked onchange="recalcModalPreview()">
            فعال‌سازی موتور خروج خودکار (Auto-Exit TP / SL)
          </label>
          <button type="button" onclick="applyModelFairTp()" style="background:var(--blue-bg);color:var(--blue);border:1px solid #2563eb55;border-radius:8px;padding:4px 10px;font-size:11px;font-weight:700;cursor:pointer;font-family:inherit">
            🎯 تنظیم TP روی ارزش منصفانه مدل
          </button>
        </div>

        <div class="modal-grid-2">
          <!-- Take Profit Box -->
          <div style="background:#fff;border:1px solid #16a34a55;border-radius:12px;padding:10px">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
              <label style="display:flex;align-items:center;gap:6px;font-size:12px;font-weight:700;color:var(--green);cursor:pointer">
                <input type="checkbox" id="modalTpEnabled" checked onchange="recalcModalPreview()">
                🎯 حد سیو سود (Take-Profit)
              </label>
              <span id="modalTpProfitLabel" class="ltr-inline" style="font-size:11px;font-weight:700;color:var(--green)">+$0.00</span>
            </div>
            <div class="modal-grid-2" style="gap:8px">
              <div>
                <span style="display:block;font-size:10.5px;color:var(--text-dim);margin-bottom:3px">قیمت هدف (سنت ¢)</span>
                <input type="number" id="modalTpCents" class="modal-input ltr-inline" step="0.5" min="1" max="99.5" oninput="onManualTpCentsChange(this.value)">
              </div>
              <div>
                <span style="display:block;font-size:10.5px;color:var(--text-dim);margin-bottom:3px">درصد سود هدف (%)</span>
                <input type="number" id="modalTpPct" class="modal-input ltr-inline" step="1" min="1" max="900" oninput="onManualTpPctChange(this.value)">
              </div>
            </div>
            <div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:8px">
              <button type="button" class="preset-btn" style="background:var(--green-bg);color:var(--green);border-color:#16a34a33" onclick="onManualTpPctChange(20)">+20%</button>
              <button type="button" class="preset-btn" style="background:var(--green-bg);color:var(--green);border-color:#16a34a33" onclick="onManualTpPctChange(35)">+35%</button>
              <button type="button" class="preset-btn" style="background:var(--green-bg);color:var(--green);border-color:#16a34a33" onclick="onManualTpPctChange(50)">+50%</button>
              <button type="button" class="preset-btn" style="background:var(--green-bg);color:var(--green);border-color:#16a34a33" onclick="onManualTpPctChange(75)">+75%</button>
              <button type="button" class="preset-btn" style="background:var(--green-bg);color:var(--green);border-color:#16a34a33" onclick="onManualTpPctChange(100)">+100%</button>
            </div>
          </div>

          <!-- Stop Loss Box -->
          <div style="background:#fff;border:1px solid #dc262655;border-radius:12px;padding:10px">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
              <label style="display:flex;align-items:center;gap:6px;font-size:12px;font-weight:700;color:var(--red);cursor:pointer">
                <input type="checkbox" id="modalSlEnabled" checked onchange="recalcModalPreview()">
                🛑 حد ضرر خودکار (Stop-Loss)
              </label>
              <span id="modalSlLossLabel" class="ltr-inline" style="font-size:11px;font-weight:700;color:var(--red)">-$0.00</span>
            </div>
            <div class="modal-grid-2" style="gap:8px">
              <div>
                <span style="display:block;font-size:10.5px;color:var(--text-dim);margin-bottom:3px">قیمت استاپ (سنت ¢)</span>
                <input type="number" id="modalSlCents" class="modal-input ltr-inline" step="0.5" min="0.5" max="98" oninput="onManualSlCentsChange(this.value)">
              </div>
              <div>
                <span style="display:block;font-size:10.5px;color:var(--text-dim);margin-bottom:3px">درصد افت مجاز (%)</span>
                <input type="number" id="modalSlPct" class="modal-input ltr-inline" step="1" min="1" max="95" oninput="onManualSlPctChange(this.value)">
              </div>
            </div>
            <div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:8px">
              <button type="button" class="preset-btn" style="background:var(--red-bg);color:var(--red);border-color:#dc262633" onclick="onManualSlPctChange(15)">-15%</button>
              <button type="button" class="preset-btn" style="background:var(--red-bg);color:var(--red);border-color:#dc262633" onclick="onManualSlPctChange(25)">-25%</button>
              <button type="button" class="preset-btn" style="background:var(--red-bg);color:var(--red);border-color:#dc262633" onclick="onManualSlPctChange(30)">-30%</button>
              <button type="button" class="preset-btn" style="background:var(--red-bg);color:var(--red);border-color:#dc262633" onclick="onManualSlPctChange(40)">-40%</button>
              <button type="button" class="preset-btn" style="background:var(--red-bg);color:var(--red);border-color:#dc262633" onclick="onManualSlPctChange(50)">-50%</button>
            </div>
          </div>
        </div>

        <!-- Weather-Smart Guards & Execution Policy -->
        <div class="modal-grid-2" style="margin-top:10px">
          <div style="background:#fff;border:1px solid var(--border);border-radius:12px;padding:10px;font-size:12px">
            <div style="font-weight:700;margin-bottom:6px">🌡️ محافظ‌های هوشمند بازار آب‌وهوا</div>
            <label style="display:flex;align-items:center;gap:6px;margin-bottom:6px;cursor:pointer">
              <input type="checkbox" id="modalMetarStop" checked>
              <span>خروج فوری در صورت عبور دمای واقعی فرودگاه (METAR) از باکت</span>
            </label>
            <label style="display:flex;align-items:center;gap:6px;margin-bottom:6px;cursor:pointer">
              <input type="checkbox" id="modalAutoSell99" checked>
              <span>فروش خودکار در 99.5¢ (نقد کردن سود قبل از تسویه UMA)</span>
            </label>
            <div style="display:flex;align-items:center;justify-content:space-between;gap:6px">
              <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
                <input type="checkbox" id="modalTrailingEnabled">
                <span>حد ضرر متحرک (Trailing Stop)</span>
              </label>
              <div style="display:flex;align-items:center;gap:4px">
                <input type="number" id="modalTrailingPct" value="8" min="2" max="50" style="width:52px;border:1px solid var(--border);border-radius:6px;padding:3px;text-align:center;font-weight:700" class="ltr-inline">
                <span>%</span>
              </div>
            </div>
          </div>

          <div style="background:#fff;border:1px solid var(--border);border-radius:12px;padding:10px;font-size:12px">
            <div style="font-weight:700;margin-bottom:6px">⚙️ سیاست ارسال سفارش فروش به CLOB</div>
            <select id="modalExitPolicy" style="width:100%;border:1px solid var(--border);border-radius:8px;padding:6px;font-size:11.5px;font-weight:600;background:var(--surface-2);font-family:inherit;margin-bottom:8px">
              <option value="MARKETABLE_FAK">MARKETABLE_FAK (پیشنهادی - فروش فوری تا کف مجاز)</option>
              <option value="MARKETABLE_FOK">MARKETABLE_FOK (یا کل حجم یکجا یا لغو)</option>
              <option value="STRICT_LIMIT">STRICT_LIMIT (سفارش لیمیت GTC در اوردربوک)</option>
            </select>
            <div class="modal-grid-2" style="gap:8px">
              <div>
                <span style="display:block;font-size:10.5px;color:var(--text-dim)">حداکثر اسلیپیج (%)</span>
                <input type="number" id="modalSlippagePct" value="2.0" step="0.5" min="0.5" max="15" class="modal-input ltr-inline" style="padding:4px">
              </div>
              <div>
                <span style="display:block;font-size:10.5px;color:var(--text-dim)">گام قیمت (Tick Size)</span>
                <select id="modalTickSize" class="modal-input ltr-inline" style="padding:4px">
                  <option value="0.01">0.01 (1¢)</option>
                  <option value="0.001">0.001 (0.1¢)</option>
                </select>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Visual SL <-- Entry --> TP Risk/Reward Bar -->
      <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:12px;padding:10px 14px;margin-top:12px">
        <div style="display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;font-size:12px;gap:6px">
          <span id="rrSlText" class="ltr-inline" style="font-weight:700;color:var(--red)">🛑 SL: -</span>
          <span id="rrEntryText" class="ltr-inline" style="font-weight:700;color:var(--text)">⚡ Entry: -</span>
          <span id="rrTpText" class="ltr-inline" style="font-weight:700;color:var(--green)">🎯 TP: -</span>
        </div>
        <div class="rr-bar">
          <div style="background:#dc2626;width:32%"></div>
          <div style="background:#2563eb;width:8%"></div>
          <div style="background:#16a34a;width:60%"></div>
        </div>
        <div id="rrRatioText" style="font-size:11px;color:var(--text-dim);text-align:center;margin-top:5px"></div>
      </div>

      <!-- Live Tick Simulator (Shown in Edit Mode to test PAPER/SHADOW triggers immediately) -->
      <div id="modalTickSimulatorSection" style="display:none;background:#fffbeb;border:1px solid rgba(245,158,11,0.45);border-radius:12px;padding:12px 14px;margin-top:12px">
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:6px">
          <div style="font-size:12px;font-weight:700;color:#b45309">
            🧪 شبیه‌ساز زندهٔ تیک بازار و دمای فرودگاه (تست عملکرد موتور قبل از پول واقعی)
          </div>
          <span id="simStateBadge" class="ltr-inline" style="font-size:11px;background:#fff;border:1px solid rgba(245,158,11,0.35);color:#b45309;padding:2px 8px;border-radius:6px;font-weight:600">
            State: WATCHING
          </span>
        </div>
        <p id="simHelpText" style="font-size:11.5px;color:#78350f;margin:0 0 8px 0">
          یک قیمت Best Bid فرضی وارد کنید تا عملکرد موتور Auto-Exit (سیو سود، حد ضرر یا METAR) را در لحظه تست کنید:
        </p>
        <div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px">
          <div style="display:flex;align-items:center;gap:4px;background:#fff;border:1px solid rgba(245,158,11,0.45);border-radius:8px;padding:5px 10px">
            <span id="simPriceLabel" style="font-size:12px;color:#78350f">قیمت خریدار بازار (Best Bid ¢):</span>
            <input type="number" id="simBidCents" step="0.5" min="0.5" max="99.9" value="35.0" class="ltr-inline" style="width:72px;font-weight:700;font-size:13.5px;text-align:center;border:none;outline:none">
            <span style="font-size:12px;font-weight:700;color:#78350f">¢</span>
          </div>
          <label style="display:flex;align-items:center;gap:6px;background:#fff;border:1px solid rgba(245,158,11,0.45);border-radius:8px;padding:6px 10px;font-size:12px;color:#78350f;cursor:pointer">
            <input type="checkbox" id="simMetarElim">
            🌡️ شبیه‌سازی عبور دمای METAR از سقف باکت
          </label>
          <button type="button" id="simRunBtn" onclick="runModalSimulateTick()" style="background:#d97706;color:#fff;border:none;border-radius:8px;padding:7px 14px;font-size:12px;font-weight:700;cursor:pointer;font-family:inherit">
            ⚡ ارسال تیک به موتور ربات
          </button>
        </div>
        <div id="simResultBox" style="display:none;margin-top:8px;padding:8px 12px;border-radius:8px;font-size:12px;font-weight:600"></div>
      </div>

      <div id="modalFeedbackBox" style="display:none;margin-top:10px;padding:10px 12px;border-radius:10px;font-size:12px;font-weight:600"></div>

      <div class="modal-footer">
        <button type="button" onclick="closeOrderModal()" style="background:#fff;color:#4b5563;border:1px solid var(--border);border-radius:10px;padding:9px 16px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit">انصراف</button>
        <button type="button" id="modalSubmitBtn" class="submit-order-btn" onclick="submitOrderModal()">⚡ ثبت سفارش خرید فوری + فعال‌سازی TP/SL</button>
      </div>
    </div>
  </div>
</div>

<script>
function switchTab(mode) {
  var vs = document.getElementById('view-simple');
  var va = document.getElementById('view-accuracy');
  var bs = document.getElementById('tabBtnSimple');
  var ba = document.getElementById('tabBtnAccuracy');
  var title = document.getElementById('mainPageTitle');
  vs.style.display = (mode === 'simple') ? '' : 'none';
  va.style.display = (mode === 'accuracy') ? '' : 'none';
  bs.className = (mode === 'simple') ? 'tab-btn active-blue' : 'tab-btn';
  ba.className = (mode === 'accuracy') ? 'tab-btn active-blue' : 'tab-btn';
  if (title) {
    title.textContent = (mode === 'accuracy')
      ? 'WeatherBet -- کالیبراسیون هوشمند و رتبه‌بندی دقت شهرها'
      : 'WeatherBet -- داشبورد ساده';
  }
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
  var useSmart = !document.getElementById('useSmartGateToggle') || document.getElementById('useSmartGateToggle').checked;
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
  document.querySelectorAll('.smart-val').forEach(function(el) {
    el.style.display = useSmart ? '' : 'none';
  });
  document.querySelectorAll('.legacy-val').forEach(function(el) {
    el.style.display = useSmart ? 'none' : '';
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
  document.querySelectorAll('#rawAccuracyTable tbody tr').forEach(function(row) {
    var cname = (row.getAttribute('data-city-name') || '').toLowerCase();
    row.style.display = (!q || cname.indexOf(q) !== -1) ? '' : 'none';
  });
}
function filterHistory() {
  var city = document.getElementById('historyCityFilter').value;
  var date = document.getElementById('historyDateFilter').value;
  var clearBtn = document.getElementById('clearHistoryFilterBtn');
  if (clearBtn) clearBtn.style.display = (city || date) ? '' : 'none';
  document.querySelectorAll('#historyTable tbody tr').forEach(function(row) {
    var okCity = !city || row.getAttribute('data-city') === city;
    var okDate = !date || row.getAttribute('data-date') === date;
    row.style.display = (okCity && okDate) ? '' : 'none';
  });
}
function clearHistoryFilters() {
  document.getElementById('historyCityFilter').value = '';
  document.getElementById('historyDateFilter').value = '';
  filterHistory();
}
function setLockStatus(marketId, text, color) {
  var el = document.getElementById("lockstatus-" + marketId);
  if (el) { el.textContent = text; el.style.color = color || "#9aa0a6"; }
  var elActive = document.getElementById("lockstatus-" + marketId + "-active");
  if (elActive) { elActive.textContent = text; elActive.style.color = color || "#9aa0a6"; }
}
function getGitHubRepo() {
  var fallback = "GITHUB_REPO_PLACEHOLDER";
  try {
    var m = window.location.hostname.match(/^([^.]+)\.github\.io$/i);
    var parts = window.location.pathname.split("/").filter(Boolean);
    if (m && parts.length > 0) {
      return m[1] + "/" + parts[0];
    }
  } catch (e) {}
  return fallback;
}

/* ============================================================================
   Interactive OrderConfigModal State & Smart Auto-Pilot Math
   ============================================================================ */
var modalCtx = null;
var modalState = {
  mode: 'create',
  side: 'YES',
  executionMode: 'SHADOW',
  entryOrderType: 'IMMEDIATE_MARKETABLE',
  smartAutopilot: true,
  entryPrice: 0.25,
  usdcAmount: 15,
  quantityShares: 60,
  tpCents: 35,
  tpPct: 40,
  slCents: 18,
  slPct: 28
};

function computeSmartAutoPilotJS(marketPrice, modelProb, beliefProb, ev, entryOrderType, tickSize) {
  var tick = (tickSize && tickSize > 0) ? tickSize : 0.01;
  function roundDownTick(val) {
    var steps = Math.floor((val + 1e-9) / tick);
    var res = Number((steps * tick).toFixed(4));
    return Math.max(tick, Math.min(1 - tick, res));
  }
  var ask = roundDownTick(Math.max(0.01, Math.min(0.99, marketPrice || 0.25)));
  var mProb = (modelProb != null && modelProb > 0) ? modelProb : ask;
  var bProb = (beliefProb != null && beliefProb > 0) ? beliefProb : mProb;
  var evVal = ev != null ? ev : 0;

  var entryPrice = ask;
  if (entryOrderType === 'CONDITIONAL_LIMIT_GTC') {
    var cand1 = ask - 0.02;
    var cand2 = mProb * 0.82;
    var rawLimit = mProb > 0.03 ? Math.min(cand1, cand2) : cand1;
    entryPrice = roundDownTick(Math.max(tick, rawLimit));
    if (entryPrice >= ask && ask > tick) {
      entryPrice = roundDownTick(ask - tick);
    }
  }

  var minTp = Math.min(0.99, entryPrice + Math.max(0.03, tick * 3));
  var rawTp = entryPrice * 1.3;
  if (evVal >= 0.15 && mProb >= minTp) {
    rawTp = Math.max(mProb, bProb, entryPrice * 1.25);
  } else if (mProb > entryPrice + 0.04) {
    rawTp = Math.max(mProb, entryPrice * 1.25);
  }
  var tpPrice = roundDownTick(Math.min(0.99, Math.max(minTp, rawTp)));
  if (tpPrice <= entryPrice) {
    tpPrice = Math.min(0.999, Number((entryPrice + tick).toFixed(4)));
  }

  var maxSl = Math.max(tick, entryPrice - tick);
  var rawSl = entryPrice * 0.72;
  if (entryPrice < 0.15) {
    rawSl = entryPrice * 0.62;
  } else if (entryPrice < 0.35) {
    rawSl = entryPrice * 0.72;
  } else {
    rawSl = entryPrice * 0.78;
  }
  var slPrice = roundDownTick(Math.min(maxSl, Math.max(tick, rawSl)));
  if (slPrice >= entryPrice && entryPrice > 0.001) {
    slPrice = Math.max(0.001, Number((entryPrice - 0.001).toFixed(4)));
  }

  var tpPct = entryPrice > 0 ? Number((((tpPrice - entryPrice) / entryPrice) * 100).toFixed(1)) : 30.0;
  var slPct = entryPrice > 0 ? Number((((entryPrice - slPrice) / entryPrice) * 100).toFixed(1)) : 28.0;
  return { entryPrice: entryPrice, tpPrice: tpPrice, tpPct: tpPct, slPrice: slPrice, slPct: slPct };
}

function openOrderModal(ctx) {
  modalCtx = ctx;
  modalState.mode = ctx.mode || 'create';
  modalState.side = ctx.side || 'YES';
  modalState.executionMode = ctx.executionMode || 'SHADOW';
  modalState.entryOrderType = ctx.entryOrderType || 'IMMEDIATE_MARKETABLE';
  modalState.smartAutopilot = (ctx.smartAutopilot !== undefined) ? Boolean(ctx.smartAutopilot) : true;
  modalState.usdcAmount = ctx.usdcAmount || 15;

  document.getElementById('modalCityName').textContent = ctx.cityName || ctx.city;
  document.getElementById('modalDate').textContent = ctx.date;
  document.getElementById('modalBucketLabel').textContent = ctx.label;
  document.getElementById('modalTempTypeBadge').textContent = (ctx.marketType === 'min') ? '❄️ MIN TEMP' : '🔥 MAX TEMP';
  document.getElementById('modalHeaderTitle').textContent = (modalState.mode === 'edit')
    ? '⚙️ تنظیمات پوزیشن فعال و مدیریت خودکار TP / SL'
    : '⚡ ثبت سفارش هوشمند و تنظیم اتوماتیک سیو سود / حد ضرر';

  var yesCents = Math.round((ctx.yesPrice || 0.25) * 100);
  var noCents = Math.round((1 - (ctx.yesPrice || 0.25)) * 100);
  document.getElementById('sideBtnYES').textContent = 'خرید YES (' + yesCents + '¢)';
  document.getElementById('sideBtnNO').textContent = 'خرید NO (' + noCents + '¢)';
  document.getElementById('modalUsdcAmount').value = modalState.usdcAmount;

  var fb = document.getElementById('modalFeedbackBox');
  if (fb) fb.style.display = 'none';
  var simSec = document.getElementById('modalTickSimulatorSection');
  var simRes = document.getElementById('simResultBox');
  if (simRes) simRes.style.display = 'none';

  if (modalState.mode === 'edit') {
    if (simSec) simSec.style.display = 'block';
    if (ctx.existingTpPrice && ctx.existingSlPrice) {
      modalState.entryPrice = ctx.entryPrice || ctx.yesPrice || 0.25;
      modalState.tpCents = Number((ctx.existingTpPrice * 100).toFixed(1));
      modalState.slCents = Number((ctx.existingSlPrice * 100).toFixed(1));
      var ec = modalState.entryPrice * 100;
      modalState.tpPct = ec > 0 ? Number((((modalState.tpCents - ec) / ec) * 100).toFixed(1)) : 35;
      modalState.slPct = ec > 0 ? Number((((ec - modalState.slCents) / ec) * 100).toFixed(1)) : 28;
      document.getElementById('modalEntryCents').value = Number(ec.toFixed(1));
      document.getElementById('modalTpCents').value = modalState.tpCents;
      document.getElementById('modalTpPct').value = modalState.tpPct;
      document.getElementById('modalSlCents').value = modalState.slCents;
      document.getElementById('modalSlPct').value = modalState.slPct;
      var simInput = document.getElementById('simBidCents');
      if (simInput) simInput.value = modalState.tpCents;
    }
  } else {
    if (simSec) simSec.style.display = 'none';
  }

  setModalExecMode(modalState.executionMode);
  setModalOrderType(modalState.entryOrderType);
  setModalSide(modalState.side);
  onToggleSmartAutopilot(modalState.smartAutopilot);

  document.getElementById('orderConfigModal').className = 'modal-backdrop open';
}

function closeOrderModal() {
  document.getElementById('orderConfigModal').className = 'modal-backdrop';
}

function getSidePrices() {
  if (!modalCtx) return { marketPrice: 0.25, modelProb: null, beliefProb: null, ev: null };
  var rawYes = Number(modalCtx.yesPrice || 0.25);
  var rawNo = Math.max(0.01, 1 - rawYes);
  var mp = modalState.side === 'YES' ? Math.max(0.01, Math.min(0.99, rawYes)) : Math.max(0.01, Math.min(0.99, rawNo));
  var modP = modalCtx.modelProb != null ? (modalState.side === 'YES' ? Number(modalCtx.modelProb) : 1 - Number(modalCtx.modelProb)) : null;
  var belP = modalCtx.beliefProb != null ? (modalState.side === 'YES' ? Number(modalCtx.beliefProb) : 1 - Number(modalCtx.beliefProb)) : modP;
  return { marketPrice: mp, modelProb: modP, beliefProb: belP, ev: modalCtx.ev };
}

function applyAutopilotValues() {
  var sp = getSidePrices();
  var smart = computeSmartAutoPilotJS(sp.marketPrice, sp.modelProb, sp.beliefProb, sp.ev, modalState.entryOrderType, 0.01);
  if (modalState.mode !== 'edit' || modalState.entryOrderType === 'CONDITIONAL_LIMIT_GTC') {
    modalState.entryPrice = smart.entryPrice;
  }
  modalState.tpCents = Number((smart.tpPrice * 100).toFixed(1));
  modalState.tpPct = smart.tpPct;
  modalState.slCents = Number((smart.slPrice * 100).toFixed(1));
  modalState.slPct = smart.slPct;

  document.getElementById('modalEntryCents').value = Number((modalState.entryPrice * 100).toFixed(1));
  document.getElementById('modalTpCents').value = modalState.tpCents;
  document.getElementById('modalTpPct').value = modalState.tpPct;
  document.getElementById('modalSlCents').value = modalState.slCents;
  document.getElementById('modalSlPct').value = modalState.slPct;
  recalcModalPreview();
}

function onToggleSmartAutopilot(checked) {
  modalState.smartAutopilot = Boolean(checked);
  document.getElementById('modalSmartAutopilot').checked = modalState.smartAutopilot;
  var card = document.getElementById('autopilotCard');
  var grid = document.getElementById('autopilotPreviewGrid');
  var btnMan = document.getElementById('btnSwitchManual');
  var manSec = document.getElementById('manualTpSlSection');
  if (modalState.smartAutopilot) {
    card.className = 'autopilot-box active';
    grid.style.display = '';
    btnMan.style.display = '';
    manSec.style.display = 'none';
    applyAutopilotValues();
  } else {
    card.className = 'autopilot-box';
    grid.style.display = 'none';
    btnMan.style.display = 'none';
    manSec.style.display = 'block';
    recalcModalPreview();
  }
}

function setModalExecMode(mode) {
  modalState.executionMode = mode;
  document.getElementById('execBtnPAPER').className = (mode === 'PAPER') ? 'mode-btn active-dark' : 'mode-btn';
  document.getElementById('execBtnSHADOW').className = (mode === 'SHADOW') ? 'mode-btn active-blue' : 'mode-btn';
  document.getElementById('execBtnLIVE').className = (mode === 'LIVE') ? 'mode-btn active-red' : 'mode-btn';
  var desc = document.getElementById('execModeDesc');
  if (mode === 'SHADOW') desc.textContent = 'در حالت SHADOW، ربات روی اوردربوک زنده پلی‌مارکت تصمیم می‌گیرد و شبیه‌سازی واقعی انجام می‌دهد (بدون کسر پول).';
  else if (mode === 'PAPER') desc.textContent = 'در حالت PAPER، سفارش و تست‌های تیک کاملاً در محیط شبیه‌ساز داخلی اجرا می‌شوند.';
  else desc.textContent = 'در حالت LIVE، سفارش‌ها مستقیماً با تنظیمات کلید کیف‌پول به صرافی پلی‌مارکت ارسال می‌شوند.';
}

function setModalOrderType(otype) {
  modalState.entryOrderType = otype;
  document.getElementById('orderTypeImmediate').className = (otype === 'IMMEDIATE_MARKETABLE') ? 'mode-btn active-blue' : 'mode-btn';
  document.getElementById('orderTypeConditional').className = (otype === 'CONDITIONAL_LIMIT_GTC') ? 'mode-btn active-purple' : 'mode-btn';
  document.getElementById('entryInputLabel').textContent = (otype === 'CONDITIONAL_LIMIT_GTC')
    ? '⏳ قیمت شرطی خرید (سنت ¢)'
    : 'قیمت مبنای ورود (سنت ¢)';
  document.getElementById('apEntryLabel').textContent = (otype === 'CONDITIONAL_LIMIT_GTC')
    ? 'قیمت خرید شرطی اتوماتیک:'
    : 'قیمت ورود بازار:';
  document.getElementById('orderTypeDesc').textContent = (otype === 'IMMEDIATE_MARKETABLE')
    ? 'همین لحظه در قیمت فعلی بازار خریداری شده و بلافاصله چتر TP/SL فعال می‌شود.'
    : 'سفارش خرید در قیمت پایین‌تر منتظر می‌ماند؛ به محض رسیدن قیمت و پر شدن، چتر TP/SL خودکار فعال می‌شود.';
  if (modalState.smartAutopilot) {
    applyAutopilotValues();
  } else {
    recalcModalPreview();
  }
}

function setModalSide(side) {
  modalState.side = side;
  document.getElementById('sideBtnYES').className = (side === 'YES') ? 'mode-btn active-green' : 'mode-btn';
  document.getElementById('sideBtnNO').className = (side === 'NO') ? 'mode-btn active-red' : 'mode-btn';
  document.getElementById('statSideLabel').textContent = side;
  var sp = getSidePrices();
  if (modalState.mode !== 'edit') {
    modalState.entryPrice = sp.marketPrice;
    document.getElementById('modalEntryCents').value = Number((sp.marketPrice * 100).toFixed(1));
  }
  if (modalState.smartAutopilot) {
    applyAutopilotValues();
  } else {
    recalcModalPreview();
  }
}

function onManualEntryCentsChange(val) {
  var c = Math.max(0.5, Math.min(99.5, parseFloat(val) || 25));
  modalState.entryPrice = Number((c / 100).toFixed(4));
  modalState.tpCents = Math.min(99.5, Number((c * (1 + modalState.tpPct / 100)).toFixed(1)));
  modalState.slCents = Math.max(0.5, Number((c * (1 - modalState.slPct / 100)).toFixed(1)));
  document.getElementById('modalTpCents').value = modalState.tpCents;
  document.getElementById('modalSlCents').value = modalState.slCents;
  onToggleSmartAutopilot(false);
}

function onModalUsdcChange(val) {
  var u = Math.max(1, parseFloat(val) || 15);
  modalState.usdcAmount = u;
  document.getElementById('modalUsdcAmount').value = u;
  recalcModalPreview();
}

function onManualTpCentsChange(val) {
  var c = Math.max(0.5, Math.min(99.9, parseFloat(val) || 35));
  modalState.tpCents = c;
  var ec = modalState.entryPrice * 100;
  if (ec > 0) {
    modalState.tpPct = Number((((c - ec) / ec) * 100).toFixed(1));
    document.getElementById('modalTpPct').value = modalState.tpPct;
  }
  recalcModalPreview();
}

function onManualTpPctChange(val) {
  var p = Math.max(1, parseFloat(val) || 35);
  modalState.tpPct = p;
  document.getElementById('modalTpPct').value = p;
  var ec = modalState.entryPrice * 100;
  modalState.tpCents = Math.min(99.5, Number((ec * (1 + p / 100)).toFixed(1)));
  document.getElementById('modalTpCents').value = modalState.tpCents;
  recalcModalPreview();
}

function onManualSlCentsChange(val) {
  var c = Math.max(0.1, Math.min(99.0, parseFloat(val) || 15));
  modalState.slCents = c;
  var ec = modalState.entryPrice * 100;
  if (ec > 0) {
    modalState.slPct = Number((((ec - c) / ec) * 100).toFixed(1));
    document.getElementById('modalSlPct').value = modalState.slPct;
  }
  recalcModalPreview();
}

function onManualSlPctChange(val) {
  var p = Math.max(1, Math.min(98, Math.abs(parseFloat(val) || 28)));
  modalState.slPct = p;
  document.getElementById('modalSlPct').value = p;
  var ec = modalState.entryPrice * 100;
  modalState.slCents = Math.max(0.1, Number((ec * (1 - p / 100)).toFixed(1)));
  document.getElementById('modalSlCents').value = modalState.slCents;
  recalcModalPreview();
}

function applyModelFairTp() {
  var sp = getSidePrices();
  if (sp.modelProb == null) return;
  var ec = modalState.entryPrice * 100;
  var fairC = Math.min(99.5, Math.max(ec + 2, Number((sp.modelProb * 100).toFixed(1))));
  modalState.tpCents = fairC;
  modalState.tpPct = ec > 0 ? Number((((fairC - ec) / ec) * 100).toFixed(1)) : 35;
  document.getElementById('modalTpCents').value = modalState.tpCents;
  document.getElementById('modalTpPct').value = modalState.tpPct;
  recalcModalPreview();
}

function recalcModalPreview() {
  var sp = getSidePrices();
  var mktCents = Number((sp.marketPrice * 100).toFixed(1));
  document.getElementById('statMarketPrice').innerHTML = mktCents + '¢ <span style="font-size:11px;font-weight:400;color:var(--text-dim)">($' + sp.marketPrice.toFixed(3) + ')</span>';
  document.getElementById('statModelFair').textContent = sp.modelProb != null ? (sp.modelProb * 100).toFixed(1) + '¢ (' + (sp.modelProb * 100).toFixed(1) + '%)' : '-';
  document.getElementById('statBelief').textContent = sp.beliefProb != null ? (sp.beliefProb * 100).toFixed(1) + '%' : '-';
  if (sp.ev != null) {
    var evP = sp.ev * 100;
    var evEl = document.getElementById('statEv');
    evEl.textContent = (evP > 0 ? '+' : '') + evP.toFixed(1) + '%';
    evEl.style.color = evP > 0 ? 'var(--green)' : (evP < 0 ? 'var(--red)' : 'var(--text-dim)');
  } else {
    document.getElementById('statEv').textContent = '-';
  }

  var ep = Math.max(0.005, modalState.entryPrice || 0.25);
  var ec = Number((ep * 100).toFixed(1));
  var shares = Number((modalState.usdcAmount / ep).toFixed(2));
  modalState.quantityShares = shares;

  document.getElementById('modalSharesDisplay').innerHTML = shares.toFixed(2) + ' Shares <span style="font-size:11px;color:var(--text-dim)">(ارزش در صورت برد ۱۰۰¢: <b style="color:var(--green)">$' + shares.toFixed(2) + '</b>)</span>';

  document.getElementById('apEntryVal').textContent = ec + '¢';
  document.getElementById('apTpVal').textContent = modalState.tpCents + '¢ (+' + modalState.tpPct + '%)';
  document.getElementById('apSlVal').textContent = modalState.slCents + '¢ (-' + modalState.slPct + '%)';

  var tpDec = modalState.tpCents / 100.0;
  var slDec = modalState.slCents / 100.0;
  var estProfit = Math.max(0, shares * (tpDec - ep));
  var estLoss = Math.max(0, shares * (ep - slDec));
  document.getElementById('modalTpProfitLabel').textContent = 'سود خالص: +$' + estProfit.toFixed(2);
  document.getElementById('modalSlLossLabel').textContent = 'حداکثر ریسک: -$' + estLoss.toFixed(2);

  document.getElementById('rrSlText').textContent = '🛑 SL: ' + modalState.slCents + '¢ (-$' + estLoss.toFixed(2) + ')';
  document.getElementById('rrEntryText').textContent = (modalState.entryOrderType === 'CONDITIONAL_LIMIT_GTC' ? '⏳ Limit Buy: ' : '⚡ Entry: ') + ec + '¢ ($' + modalState.usdcAmount.toFixed(2) + ')';
  document.getElementById('rrTpText').textContent = '🎯 TP: ' + modalState.tpCents + '¢ (+$' + estProfit.toFixed(2) + ')';
  var rr = (estLoss > 0 && estProfit > 0) ? (estProfit / estLoss).toFixed(2) : null;
  document.getElementById('rrRatioText').innerHTML = rr ? ('نسبت ریسک به ریوارد (Risk / Reward): <b class="ltr-inline" style="color:var(--text)">1 : ' + rr + '</b>') : '';

  var subBtn = document.getElementById('modalSubmitBtn');
  if (modalState.mode === 'edit') {
    subBtn.textContent = '💾 ذخیره تغییرات پوزیشن و بروزرسانی TP/SL';
    subBtn.className = 'submit-order-btn';
  } else if (modalState.entryOrderType === 'CONDITIONAL_LIMIT_GTC') {
    subBtn.textContent = '⏳ ثبت سفارش خرید شرطی در ' + ec + '¢ + مسلح‌سازی خودکار TP/SL';
    subBtn.className = 'submit-order-btn purple';
  } else {
    subBtn.textContent = '⚡ ثبت سفارش خرید فوری (' + ec + '¢) + فعال‌سازی TP/SL';
    subBtn.className = 'submit-order-btn';
  }
}

function submitOrderModal() {
  if (!modalCtx) return;
  var sp = getSidePrices();
  var tokenId = (modalState.side === 'NO' && modalCtx.noTokenId) ? modalCtx.noTokenId : modalCtx.tokenId;
  var tpDec = Number((modalState.tpCents / 100.0).toFixed(4));
  var slDec = Number((modalState.slCents / 100.0).toFixed(4));
  var autoExit = modalState.smartAutopilot ? true : document.getElementById('modalAutoExitEnabled').checked;
  var tpEn = modalState.smartAutopilot ? true : document.getElementById('modalTpEnabled').checked;
  var slEn = modalState.smartAutopilot ? true : document.getElementById('modalSlEnabled').checked;
  var metarStop = modalState.smartAutopilot ? true : document.getElementById('modalMetarStop').checked;
  var sell99 = modalState.smartAutopilot ? true : document.getElementById('modalAutoSell99').checked;
  var trailEn = modalState.smartAutopilot ? false : document.getElementById('modalTrailingEnabled').checked;
  var trailPct = parseFloat(document.getElementById('modalTrailingPct').value) || 8.0;
  var exitPol = document.getElementById('modalExitPolicy').value || 'MARKETABLE_FAK';
  var slipPct = parseFloat(document.getElementById('modalSlippagePct').value) || 2.0;
  var tickSz = parseFloat(document.getElementById('modalTickSize').value) || 0.01;

  var payload = {
    mode: modalState.mode,
    update_existing: (modalState.mode === 'edit'),
    city: modalCtx.city,
    date: modalCtx.date,
    market_id: modalCtx.marketId + (tokenId ? ("|" + tokenId) : ""),
    token_id: tokenId,
    side: modalState.side,
    price: sp.marketPrice,
    limit_buy_price: (modalState.entryOrderType === 'CONDITIONAL_LIMIT_GTC') ? modalState.entryPrice : null,
    entry_order_type: modalState.entryOrderType,
    smart_autopilot: modalState.smartAutopilot,
    market_type: (modalCtx.marketType || "max"),
    execution_mode: modalState.executionMode,
    exit_policy: exitPol,
    quantity_shares: modalState.quantityShares,
    usdc_amount: modalState.usdcAmount,
    auto_exit_enabled: autoExit,
    tp_enabled: tpEn,
    tp_price: tpEn ? tpDec : null,
    tp_pct: tpEn ? modalState.tpPct : null,
    sl_enabled: slEn,
    sl_price: slEn ? slDec : null,
    sl_pct: slEn ? modalState.slPct : null,
    max_slippage_pct: slipPct,
    tick_size: tickSz,
    trailing_stop_enabled: trailEn,
    trailing_offset_pct: trailPct,
    metar_auto_stop: metarStop,
    auto_sell_99c: sell99
  };

  var isGitHubPages = window.location.hostname.indexOf('github.io') !== -1;
  if (!isGitHubPages) {
    // Try direct API first (AI Studio / Local Server), fall back to GitHub Issue if offline
    var apiEndpoint = (modalState.mode === 'edit') ? '/api/locks/rules' : '/api/locks';
    var apiMethod = (modalState.mode === 'edit') ? 'PATCH' : 'POST';
    var apiPayload = Object.assign({}, payload, { market_id: modalCtx.marketId });
    fetch(apiEndpoint, {
      method: apiMethod,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(apiPayload)
    }).then(function(r) {
      if (r.ok) return r.json();
      throw new Error('fallback');
    }).then(function(res) {
      if (res && res.ok) {
        setLockStatus(modalCtx.marketId, "✔ سفارش با موفقیت ثبت شد!", "#16a34a");
        closeOrderModal();
        window.location.reload();
      } else {
        openGitHubIssueForOrder(payload);
      }
    }).catch(function() {
      openGitHubIssueForOrder(payload);
    });
  } else {
    openGitHubIssueForOrder(payload);
  }
}

function openGitHubIssueForOrder(payload) {
  var repo = getGitHubRepo();
  var actionPrefix = (payload.mode === 'edit') ? "LOCK " : "LOCK ";
  var priceTag = (payload.entry_order_type === 'CONDITIONAL_LIMIT_GTC')
    ? ("LIMIT@" + payload.limit_buy_price)
    : ("@" + payload.price);
  var title = encodeURIComponent(
    actionPrefix + (modalCtx.cityName || payload.city) + " " + payload.date + " " + (modalCtx.label || "") + " " + payload.side + " " + priceTag
  );
  var body = encodeURIComponent(JSON.stringify(payload));
  var url = "https://github.com/" + repo + "/issues/new?title=" + title + "&body=" + body + "&labels=lock-request";
  var win = window.open(url, "_blank");
  var fb = document.getElementById('modalFeedbackBox');
  if (win) {
    if (fb) {
      fb.style.display = 'block';
      fb.style.background = '#e8f8ee';
      fb.style.color = '#16a34a';
      fb.style.border = '1px solid #16a34a55';
      fb.textContent = '⚠️ در تب جدید گیت‌هاب حتماً روی دکمهٔ سبز «Submit new issue» کلیک کنید تا سفارش و تنظیمات TP/SL ثبت شود!';
    }
    setLockStatus(modalCtx.marketId, "⚠️ در تب جدید روی «Submit new issue» کلیک کنید!", "#f59e0b");
  } else {
    if (fb) {
      fb.style.display = 'block';
      fb.style.background = '#fdeceb';
      fb.style.color = '#dc2626';
      fb.style.border = '1px solid #dc262655';
      fb.textContent = '❌ مرورگر پاپ‌آپ را مسدود کرد -- اجازهٔ پاپ‌آپ بدهید و دوباره کلیک کنید.';
    }
  }
}

function runModalSimulateTick() {
  if (!modalCtx) return;
  var bidCents = parseFloat(document.getElementById('simBidCents').value || '0');
  var metarElim = document.getElementById('simMetarElim').checked;
  var resBox = document.getElementById('simResultBox');
  resBox.style.display = 'block';
  if (isNaN(bidCents) || bidCents <= 0 || bidCents >= 100) {
    resBox.style.background = '#fdeceb';
    resBox.style.color = '#dc2626';
    resBox.style.border = '1px solid #dc262655';
    resBox.textContent = '❌ قیمت شبیه‌سازی باید بین 0.5 و 99.9 سنت باشد.';
    return;
  }
  var isGitHubPages = window.location.hostname.indexOf('github.io') !== -1;
  if (!isGitHubPages) {
    fetch('/api/locks/simulate-tick', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        city: modalCtx.city,
        date: modalCtx.date,
        market_id: modalCtx.marketId,
        simulated_bid: Number((bidCents / 100.0).toFixed(4)),
        force_metar_eliminated: metarElim
      })
    }).then(function(r) {
      if (r.ok) return r.json();
      throw new Error('fallback');
    }).then(function(d) {
      if (d && d.ok) {
        resBox.style.background = '#e8f8ee';
        resBox.style.color = '#16a34a';
        resBox.style.border = '1px solid #16a34a55';
        resBox.textContent = d.message;
        return;
      }
      throw new Error('local');
    }).catch(function() {
      evaluateTickClientSide(bidCents, metarElim, resBox);
    });
  } else {
    evaluateTickClientSide(bidCents, metarElim, resBox);
  }
}

function evaluateTickClientSide(bidCents, metarElim, resBox) {
  var entryC = Number((modalState.entryPrice * 100).toFixed(1));
  var tpC = modalState.tpCents;
  var slC = modalState.slCents;
  var pnlPct = entryC > 0 ? (((bidCents - entryC) / entryC) * 100).toFixed(1) : '0.0';
  var shares = modalState.quantityShares || 60;
  var pnlUsdc = (shares * ((bidCents - entryC) / 100.0)).toFixed(2);
  if (metarElim) {
    resBox.style.background = '#e8f8ee';
    resBox.style.color = '#16a34a';
    resBox.style.border = '1px solid #16a34a55';
    resBox.textContent = '🌡️ [تست شبیه‌ساز] عبور دمای METAR از سقف باکت تشخیص داده شد! موتور Auto-Exit دستور خروج اضطراری را در قیمت ' + bidCents + '¢ (' + pnlPct + '%) صادر می‌کند.';
  } else if (bidCents >= 99.5) {
    resBox.style.background = '#e8f8ee';
    resBox.style.color = '#16a34a';
    resBox.style.border = '1px solid #16a34a55';
    resBox.textContent = '🎯 [تست شبیه‌ساز] قیمت به 99.5¢ رسید! فروش خودکار قبل از تسویه UMA فعال شد (+$' + pnlUsdc + ' / +' + pnlPct + '%).';
  } else if (bidCents >= tpC) {
    resBox.style.background = '#e8f8ee';
    resBox.style.color = '#16a34a';
    resBox.style.border = '1px solid #16a34a55';
    resBox.textContent = '🎯 [تست شبیه‌ساز] سیو سود خودکار (TP) در قیمت ' + bidCents + '¢ (≥ هدف ' + tpC + '¢) فعال شد! سود خالص: +$' + pnlUsdc + ' (+' + pnlPct + '%).';
  } else if (bidCents <= slC) {
    resBox.style.background = '#fdeceb';
    resBox.style.color = '#dc2626';
    resBox.style.border = '1px solid #dc262655';
    resBox.textContent = '🛑 [تست شبیه‌ساز] حد ضرر خودکار (SL) در قیمت ' + bidCents + '¢ (≤ استاپ ' + slC + '¢) فعال شد و جلوی ضرر بیشتر گرفته شد (' + pnlPct + '%).';
  } else {
    resBox.style.background = '#eff6ff';
    resBox.style.color = '#2563eb';
    resBox.style.border = '1px solid #2563eb44';
    resBox.textContent = '👁️ [تست شبیه‌ساز] در قیمت ' + bidCents + '¢ (' + (pnlPct >= 0 ? '+' : '') + pnlPct + '%) پوزیشن بین استاپ (' + slC + '¢) و هدف سود (' + tpC + '¢) قرار دارد و در حالت پایش (WATCHING) باقی می‌ماند.';
  }
}

function unlockBucket(city, cityName, date, marketId, label) {
  setLockStatus(marketId, "⏳ در حال باز شدن گیت‌هاب...", "#eab308");
  var isGitHubPages = window.location.hostname.indexOf('github.io') !== -1;
  if (!isGitHubPages) {
    fetch('/api/locks/unlock', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ city: city, cityName: cityName, date: date, market_id: marketId, label: label, force_exit: true })
    }).then(function(r) {
      if (r.ok) { window.location.reload(); return; }
      throw new Error('fallback');
    }).catch(function() {
      openGitHubIssueForUnlock(city, cityName, date, marketId, label);
    });
  } else {
    openGitHubIssueForUnlock(city, cityName, date, marketId, label);
  }
}

function openGitHubIssueForUnlock(city, cityName, date, marketId, label) {
  var repo = getGitHubRepo();
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


def _signal_badge_html(is_main, is_smart_main, ev_val):
    ev_val = ev_val if ev_val is not None else 0.0
    active_main = bool(is_smart_main or is_main)
    smart_tag = " (هوشمند)" if (is_smart_main and not is_main) else ""
    if active_main and ev_val >= EV_GATE_THRESHOLD:
        return f'<span class="signal-badge signal-green">\U0001F7E2 سیگنال نهایی{smart_tag}</span>'
    if active_main:
        return f'<span class="signal-badge signal-blue">\U0001F535 محتمل\u200cترین{smart_tag}</span>'
    if ev_val >= EV_GATE_THRESHOLD:
        return '<span class="signal-badge signal-purple">\U0001F7E3 ارزش بالا</span>'
    return ''


def _format_pct_paren(pct_val, pct_6h_val=None):
    """فرمت کردن درصد تغییرات در داخل پرانتز (هم تغییر نسبت به اسکن قبلی و هم اسکن ۶ ساعته اخیر)."""
    if pct_val is None and pct_6h_val is None:
        return ""
    v10 = float(pct_val if pct_val is not None else (pct_6h_val or 0.0))
    v6 = float(pct_6h_val) if pct_6h_val is not None else None
    if v6 is not None:
        c10 = "chg-zero" if abs(v10) < 0.05 else ("chg-pos" if v10 > 0 else "chg-neg")
        c6 = "chg-zero" if abs(v6) < 0.05 else ("chg-pos" if v6 > 0 else "chg-neg")
        s10 = "0.0%" if abs(v10) < 0.05 else f"{v10:+.1f}%"
        s6 = "0.0%" if abs(v6) < 0.05 else f"{v6:+.1f}%"
        return (
            f' <span class="{c10}" title="تغییر نسبت به اسکن قبلی">({s10})</span>'
            f'<div style="font-size:10.5px;color:var(--text-dim);line-height:1.2;margin-top:2px;direction:ltr" '
            f'title="تغییر نسبت به اسکن ۶ ساعته اخیر">(6h: <span class="{c6}">{s6}</span>)</div>'
        )
    val = v10
    if abs(val) < 0.05:
        return ' <span class="chg-zero">(0.0%)</span>'
    css = "chg-pos" if val > 0 else "chg-neg"
    return f' <span class="{css}">({val:+.1f}%)</span>'


def _bucket_table(city_slug, city_name, date, unit_sym, full_distribution, locked_keys, open_locks_map, main_signal_id, market_type="max"):
    """جدول باکت‌ها دقیقاً منطبق با پیش‌نمایش AI Studio شامل دکمهٔ «⚡ خرید و تنظیم اتوماتیک»
    و باز کردن مودال پیشرفتهٔ تنظیمات TP/SL/Limit Buy."""
    if not full_distribution:
        return '<div class="empty">داده‌ای موجود نیست.</div>'

    rows = [
        "<table><thead><tr><th>باکت</th><th>احتمال مدل (هوشمند)</th><th>احتمال بازار (YES)</th>"
        "<th>قیمت YES</th><th>باور نهایی</th><th>EV</th><th>عملیات</th></tr></thead><tbody>"
    ]
    has_visible = False
    for b in full_distribution:
        is_elim = bool(b.get("intraday_eliminated", False))
        is_plausible = max(b.get("smart_model_prob") or b.get("model_prob") or 0.0, b.get("yes_price") or 0.0) >= BUCKET_PLAUSIBILITY_MIN
        if is_plausible:
            has_visible = True
        low, high = b.get("range", [None, None])
        label = _label_for_range(low, high, unit_sym)
        model_prob = b.get("smart_model_prob") if b.get("smart_model_prob") is not None else b.get("model_prob")
        raw_model_prob = b.get("model_prob")
        yes_price = b.get("yes_price")

        m_6h_chg = b.get("model_prob_6h_change")
        if m_6h_chg is None or abs(float(m_6h_chg)) < 0.0005:
            m_6h_chg = b.get("model_prob_change")
        if (m_6h_chg is None or abs(float(m_6h_chg)) < 0.0005) and model_prob is not None and b.get("prev_scan6h_model_prob") is not None:
            m_6h_chg = round(float(model_prob) - float(b["prev_scan6h_model_prob"]), 4)
        if (m_6h_chg is None or abs(float(m_6h_chg)) < 0.0005) and model_prob is not None and raw_model_prob is not None:
            if abs(float(model_prob) - float(raw_model_prob)) >= 0.0005:
                m_6h_chg = round(float(model_prob) - float(raw_model_prob), 4)
        if m_6h_chg is None:
            m_6h_chg = 0.0

        m_10m_chg = b.get("model_prob_10m_change")
        if m_10m_chg is None:
            if model_prob is not None and raw_model_prob is not None and abs(float(model_prob) - float(raw_model_prob)) >= 0.0005:
                m_10m_chg = round(float(model_prob) - float(raw_model_prob), 4)
            elif b.get("model_prob_change") is not None:
                m_10m_chg = b.get("model_prob_change")
            else:
                m_10m_chg = m_6h_chg

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

        model_str_smart = f"{model_prob * 100:.1f}%" if model_prob is not None else "-"
        if model_prob is not None and (m_10m_chg is not None or m_6h_chg is not None):
            model_str_smart += _format_pct_paren(
                float(m_10m_chg) * 100.0 if m_10m_chg is not None else None,
                float(m_6h_chg) * 100.0 if m_6h_chg is not None else None,
            )
        model_str_leg = f"{raw_model_prob * 100:.1f}%" if raw_model_prob is not None else model_str_smart
        if raw_model_prob is not None and (m_10m_chg is not None or m_6h_chg is not None):
            model_str_leg += _format_pct_paren(
                float(m_10m_chg) * 100.0 if m_10m_chg is not None else None,
                float(m_6h_chg) * 100.0 if m_6h_chg is not None else None,
            )
        model_str = f'<span class="smart-val">{model_str_smart}</span><span class="legacy-val" style="display:none">{model_str_leg}</span>'

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
        raw_belief_val = b.get("belief_prob")
        belief_str_smart = f"{belief_val * 100:.1f}%" if belief_val is not None else "-"
        belief_str_leg = f"{raw_belief_val * 100:.1f}%" if raw_belief_val is not None else belief_str_smart
        belief_str = f'<span class="smart-val">{belief_str_smart}</span><span class="legacy-val" style="display:none">{belief_str_leg}</span>'

        ev_val = b.get("smart_ev") if b.get("smart_ev") is not None else b.get("ev")
        raw_ev_val = b.get("ev")
        def _fmt_ev_cell(v):
            if v is None:
                return "-"
            ep = v * 100
            ecss = "win" if ep > 0 else ("loss" if ep < 0 else "")
            esign = "+" if ep > 0 else ""
            return f'<span class="{ecss}">{esign}{ep:.1f}%</span>' if ecss else f"{ep:.1f}%"
        ev_str = f'<span class="smart-val">{_fmt_ev_cell(ev_val)}</span><span class="legacy-val" style="display:none">{_fmt_ev_cell(raw_ev_val if raw_ev_val is not None else ev_val)}</span>'

        market_id = str(b.get("market_id", ""))
        yes_token = str(b.get("yes_token_id", "") or "")
        no_token = str(b.get("no_token_id", "") or "")

        is_main = (not is_elim) and main_signal_id is not None and market_id == main_signal_id
        is_smart_main = bool(b.get("is_smart_main"))
        label_html = f'<span class="ltr-inline" style="font-weight:600">{label}</span> ' + (
            _signal_badge_html(is_main, is_smart_main, ev_val) if not is_elim else ""
        )

        is_locked = (city_slug, date, market_id) in locked_keys
        action_html = ""
        status_span = f'<span class="lock-status" id="lockstatus-{market_id}"></span>' if market_id else ""
        if yes_price is not None and market_id:
            mp_js = "null" if model_prob is None else f"{float(model_prob):.4f}"
            bp_js = "null" if belief_val is None else f"{float(belief_val):.4f}"
            ev_js = "null" if ev_val is None else f"{float(ev_val):.4f}"
            if is_locked:
                lock_obj = open_locks_map.get((city_slug, date, market_id), {})
                l_side = lock_obj.get("side", "YES")
                l_entry = float(lock_obj.get("entry_price") or yes_price)
                l_tp = float(lock_obj.get("tp_price") or min(0.99, l_entry * 1.35))
                l_sl = float(lock_obj.get("sl_price") or max(0.01, l_entry * 0.72))
                l_usdc = float(lock_obj.get("usdc_amount") or 15.0)
                l_exec = lock_obj.get("execution_mode") or "SHADOW"
                l_otype = lock_obj.get("entry_order_type") or "IMMEDIATE_MARKETABLE"
                l_auto = "true" if lock_obj.get("smart_autopilot", True) else "false"
                edit_call = (
                    f"openOrderModal({{mode:'edit',city:'{city_slug}',cityName:'{city_name}',date:'{date}',"
                    f"marketId:'{market_id}',tokenId:'{yes_token}',noTokenId:'{no_token}',side:'{l_side}',"
                    f"yesPrice:{float(yes_price):.4f},entryPrice:{l_entry:.4f},existingTpPrice:{l_tp:.4f},"
                    f"existingSlPrice:{l_sl:.4f},usdcAmount:{l_usdc},executionMode:'{l_exec}',"
                    f"entryOrderType:'{l_otype}',smartAutopilot:{l_auto},label:'{label}',"
                    f"marketType:'{market_type}',modelProb:{mp_js},beliefProb:{bp_js},ev:{ev_js}}})"
                )
                unlock_call = f"unlockBucket('{city_slug}','{city_name}','{date}','{market_id}','{label}')"
                action_html = (
                    '<span class="locked-badge">\U0001F512 پوزیشن فعال \u2714</span>'
                    '<div style="display:flex;align-items:center;justify-content:center;gap:4px">'
                    f'<button class="edit-btn" onclick="{edit_call}">⚙️ تنظیمات TP/SL</button>'
                    f'<button class="unlock-btn" onclick="{unlock_call}">فروش</button>'
                    '</div>'
                    f'{status_span}'
                )
            else:
                create_call = (
                    f"openOrderModal({{mode:'create',city:'{city_slug}',cityName:'{city_name}',date:'{date}',"
                    f"marketId:'{market_id}',tokenId:'{yes_token}',noTokenId:'{no_token}',side:'YES',"
                    f"yesPrice:{float(yes_price):.4f},label:'{label}',marketType:'{market_type}',"
                    f"modelProb:{mp_js},beliefProb:{bp_js},ev:{ev_js}}})"
                )
                action_html = (
                    f'<button class="lock-btn" onclick="{create_call}">⚡ خرید و تنظیم اتوماتیک</button>'
                    f'{status_span}'
                )

        row_classes = []
        if not is_plausible:
            row_classes.append("low-plausibility-row")
        if is_smart_main:
            row_classes.append("smart-main-row")
        cls_attr = f" class=\"{' '.join(row_classes)}\"" if row_classes else ""
        style_attr = " style='display:none'" if not is_plausible else ""
        rows.append(
            f"<tr{cls_attr}{style_attr}><td>{label_html}</td><td class='ltr-inline'>{model_str}</td><td class='ltr-inline'>{market_str}</td>"
            f"<td class='ltr-inline'>{price_str}</td><td class='ltr-inline' style='font-weight:600'>{belief_str}</td><td class='ltr-inline'>{ev_str}</td>"
            f"<td>{action_html}</td></tr>"
        )
    if not has_visible:
        return '<div class="empty">داده‌ای موجود نیست.</div>'
    rows.append("</tbody></table>")
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
    """جدول ۱۰ ستونهٔ پوزیشن‌ها و سیگنال‌های قفل‌شدهٔ فعال دقیقاً مشابه پیش‌نمایش AI Studio."""
    open_locks = [l for l in locks if l.get("status") == "open"]
    if not open_locks:
        return ""

    now = datetime.now(timezone.utc)
    visible_rows = []

    for l in sorted(open_locks, key=lambda x: (x.get("city", ""), x.get("date", ""))):
        city = l.get("city", "")
        date = l.get("date", "")
        is_min = l.get("market_type") == "min"
        side = l.get("side", "YES")

        mkt = _load_market_by_key_min(city, date) if is_min else _load_market_by_key(city, date)
        if mkt is not None and mkt.get("status") in RESOLVED_LIKE_STATUSES:
            continue

        time_str, _ = _local_day_time_label(city, date, now)
        unit_sym = mkt.get("unit", "") if mkt else ""
        bucket_label = _find_locked_bucket_label(mkt, l.get("market_id"), unit_sym)

        bucket_obj = None
        if mkt:
            for b in mkt.get("full_distribution", []) or []:
                if str(b.get("market_id")) == str(l.get("market_id")):
                    bucket_obj = b
                    break

        name = LOCATIONS.get(city, {}).get("name", city)
        link = _build_polymarket_url_min(city, date) if is_min else _build_polymarket_url(city, date)
        exec_mode = l.get("execution_mode") or "SHADOW"
        is_pending = (l.get("position_state") == "ENTRY_PENDING")
        smart_auto = bool(l.get("smart_autopilot", True))

        exec_badge_style = (
            "background:var(--red-bg);color:var(--red);border:1px solid #dc262644"
            if exec_mode == "LIVE"
            else (
                "background:var(--blue-bg);color:var(--blue);border:1px solid #2563eb44"
                if exec_mode == "SHADOW"
                else "background:var(--surface-2);color:var(--text-dim);border:1px solid var(--border)"
            )
        )
        auto_badge = (
            '<span style="display:inline-block;padding:1px 6px;border-radius:4px;font-size:9.5px;font-weight:700;background:var(--green-bg);color:var(--green);border:1px solid #16a34a44">🤖 اتوماتیک</span>'
            if smart_auto else ""
        )
        city_col_html = (
            f'<a href="{link}" target="_blank" rel="noopener" style="font-weight:700;display:block">{name}</a>'
            f'<div style="display:flex;align-items:center;justify-content:center;gap:4px;margin-top:2px;flex-wrap:wrap">'
            f'<span class="ltr-inline" style="padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700;{exec_badge_style}">{exec_mode}</span>'
            f'{auto_badge}</div>'
        )

        mtype_label = "❄️ کمینه" if is_min else "🔥 بیشینه"
        side_style = "background:#fef2f2;color:#dc2626" if side == "NO" else "background:#eff6ff;color:#2563eb"
        state_html = (
            '<span style="display:block;margin-top:2px;font-size:10px;font-weight:700;color:var(--purple)">⏳ خرید شرطی</span>'
            if is_pending
            else '<span style="display:block;margin-top:2px;font-size:10px;font-weight:700;color:var(--green)">👁️ فعال (WATCHING)</span>'
        )
        type_col_html = (
            f'<div style="font-size:12px">{mtype_label}</div>'
            f'<span style="display:inline-block;margin-top:2px;padding:1px 6px;border-radius:4px;font-size:10.5px;font-weight:700;{side_style}">{side}</span>'
            f'{state_html}'
        )

        usdc_amt = l.get("usdc_amount")
        qty_shares = l.get("quantity_shares")
        if usdc_amt is not None:
            size_col_html = (
                f'<div><b>${float(usdc_amt):.2f}</b>'
                f'<span style="display:block;font-size:10.5px;color:var(--text-dim)">{float(qty_shares or 0):.1f} سهم</span></div>'
            )
        else:
            size_col_html = '<span style="color:var(--text-dim)">1 واحد</span>'

        entry = l.get("entry_price")
        last = l.get("last_price")
        if last is None and bucket_obj is not None and bucket_obj.get("yes_price") is not None:
            yp_b = float(bucket_obj["yes_price"])
            last = (1.0 - yp_b) if side == "NO" else yp_b

        entry_c_str = f"{float(entry)*100:.1f}" if entry is not None else "-"
        curr_c_str = f"{float(last)*100:.1f}" if last is not None else "-"

        raw_mp = (bucket_obj.get("smart_model_prob") or bucket_obj.get("model_prob")) if bucket_obj else None
        if raw_mp is not None:
            fair_p = (1.0 - float(raw_mp)) if side == "NO" else float(raw_mp)
            fair_c = round(fair_p * 100)
        else:
            fair_c = l.get("model_fair_cents")

        fair_note = f'<span style="display:block;font-size:10.5px;color:var(--blue)">مدل: {fair_c}¢</span>' if fair_c is not None else ""
        if is_pending:
            price_col_html = (
                f'<div><span style="font-size:10.5px;color:var(--purple);font-weight:700;display:block">شرط خرید: ≤{entry_c_str}¢</span>'
                f'<span style="font-size:12px;color:var(--text-dim)">فعلی: {curr_c_str}¢</span>{fair_note}</div>'
            )
        else:
            price_col_html = (
                f'<span style="color:var(--text-dim)">{entry_c_str}¢</span>'
                f'<span style="margin:0 4px;color:#9aa0a6">→</span>'
                f'<span style="font-weight:700">{curr_c_str}¢</span>{fair_note}'
            )

        tp_price = l.get("tp_price")
        sl_price = l.get("sl_price")
        if l.get("auto_exit_enabled") is False:
            tpsl_col_html = '<span style="color:var(--text-dim);font-size:11px">غیرفعال (دستی)</span>'
        else:
            tp_c = round(float(tp_price) * 100) if tp_price is not None else (round(float(entry) * 135) if entry else None)
            sl_c = round(float(sl_price) * 100) if sl_price is not None else (round(float(entry) * 72) if entry else None)
            badges = []
            if l.get("tp_enabled") is not False and tp_c is not None:
                badges.append(f'<span style="background:var(--green-bg);color:var(--green);border:1px solid #16a34a44;padding:2px 8px;border-radius:4px;font-size:10.5px;font-weight:700">🎯 TP: {tp_c}¢</span>')
            if l.get("sl_enabled") is not False and sl_c is not None:
                badges.append(f'<span style="background:var(--red-bg);color:var(--red);border:1px solid #dc262644;padding:2px 8px;border-radius:4px;font-size:10.5px;font-weight:700">🛑 SL: {sl_c}¢</span>')
            if l.get("trailing_stop_enabled"):
                badges.append(f'<span style="font-size:10px;color:var(--purple);font-weight:600">📈 Trailing {l.get("trailing_offset_pct") or 8}%</span>')
            tpsl_col_html = f'<div style="display:flex;flex-direction:column;align-items:center;gap:3px">{"".join(badges) or "-"}</div>'

        pct = l.get("last_pct")
        if pct is None and entry and last is not None and float(entry) > 0:
            pct = round(((float(last) - float(entry)) / float(entry)) * 100.0, 1)
        if is_pending:
            pnl_col_html = '<span style="font-size:11px;font-weight:600;color:var(--purple)">در انتظار ورود</span>'
        elif pct is None:
            pnl_col_html = "-"
        else:
            css = "win" if pct >= 0 else "loss"
            pnl_col_html = f'<span class="{css}" style="font-size:13.5px">{pct:+.1f}%</span>'

        last_checked = l.get("last_checked_at")
        updated_html = f'<div class="relative-time" data-ts="{last_checked}" style="font-size:10px;opacity:0.8"></div>' if last_checked else ""
        time_col_html = f'<div class="ltr-inline" style="font-size:11px;color:var(--text-dim)"><div>{time_str or "active"}</div>{updated_html}</div>'

        market_id = str(l.get("market_id", ""))
        token_id = str(l.get("token_id") or "")
        yes_p_val = float(bucket_obj.get("yes_price") or entry or 0.25) if bucket_obj else float(entry or 0.25)
        l_entry_val = float(entry or 0.25)
        l_tp_val = float(tp_price or min(0.99, l_entry_val * 1.35))
        l_sl_val = float(sl_price or max(0.01, l_entry_val * 0.72))
        l_usdc_val = float(usdc_amt or 15.0)
        l_otype = l.get("entry_order_type") or "IMMEDIATE_MARKETABLE"
        l_auto_js = "true" if smart_auto else "false"
        mp_js = "null" if raw_mp is None else f"{float(raw_mp):.4f}"

        edit_call = (
            f"openOrderModal({{mode:'edit',city:'{city}',cityName:'{name}',date:'{date}',"
            f"marketId:'{market_id}',tokenId:'{token_id}',side:'{side}',"
            f"yesPrice:{yes_p_val:.4f},entryPrice:{l_entry_val:.4f},existingTpPrice:{l_tp_val:.4f},"
            f"existingSlPrice:{l_sl_val:.4f},usdcAmount:{l_usdc_val},executionMode:'{exec_mode}',"
            f"entryOrderType:'{l_otype}',smartAutopilot:{l_auto_js},label:'{bucket_label}',"
            f"marketType:'{'min' if is_min else 'max'}',modelProb:{mp_js},beliefProb:{mp_js},ev:null}})"
        )
        unlock_call = f"unlockBucket('{city}','{name}','{date}','{market_id}','{bucket_label}')"
        exit_btn_label = "✕ لغو خرید شرطی" if is_pending else "🚨 خروج اضطراری"

        action_html = (
            '<div style="display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:6px">'
            f'<button class="edit-btn" onclick="{edit_call}">⚙️ تنظیمات / تست TP-SL</button>'
            f'<button class="unlock-btn" onclick="{unlock_call}">{exit_btn_label}</button>'
            '</div>'
            f'<span class="lock-status" id="lockstatus-{market_id}-active"></span>'
        )

        visible_rows.append(
            f"<tr><td>{city_col_html}</td><td>{type_col_html}</td><td class='ltr-inline'>{date}</td>"
            f"<td class='ltr-inline' style='font-weight:700'>{bucket_label}</td><td class='ltr-inline'>{size_col_html}</td>"
            f"<td class='ltr-inline'>{price_col_html}</td><td class='ltr-inline'>{tpsl_col_html}</td>"
            f"<td class='ltr-inline'>{pnl_col_html}</td><td>{time_col_html}</td><td>{action_html}</td></tr>"
        )

    if not visible_rows:
        return ""

    rows = [
        "<table><thead><tr><th>شهر / حالت</th><th>نوع / سمت</th><th>تاریخ</th><th>باکت دما</th>"
        "<th>حجم پوزیشن</th><th>ورود ← Bid فعلی</th><th>اهداف خودکار (TP / SL)</th>"
        "<th>سود / زیان</th><th>زمان باقی‌مانده</th><th>مدیریت سفارش و خروج</th></tr></thead><tbody>"
    ] + visible_rows
    rows.append("</tbody></table>")
    table_html = "".join(rows)

    return (
        "<details class='city-block locked-section' open style='margin-bottom:16px'>"
        "<summary style='display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px'>"
        f"<span><b>\U0001F512 پوزیشن‌ها و سیگنال‌های قفل‌شدهٔ فعال ({len(visible_rows)})</b></span>"
        "<span style='font-size:11px;font-weight:600;background:#fff;color:var(--green);border:1px solid #16a34a55;padding:2px 10px;border-radius:6px'>🤖 موتور پایش خودکار TP / SL روی Best Bid فعال است</span>"
        "</summary>"
        f"<div class='table-scroll'>{table_html}</div>"
        "</details>"
    )


def _submodel_bar_html(m):
    models_raw = m.get("model_means_raw") or {}
    prof = m.get("city_form")
    unit_sym = m.get("unit", "")
    model_spread = m.get("model_spread")
    obs_temp = m.get("observed_intraday_temp")
    if not models_raw and not prof and obs_temp is None:
        return ""
    chips = []
    best_m = prof.get("best_submodel") if prof else None
    worst_m = prof.get("worst_submodel") if prof else None
    weights = (prof.get("submodel_weights") if prof else None) or {}
    for mname, mval in models_raw.items():
        if mval is None:
            continue
        cls = "sm-chip"
        if best_m == mname:
            cls += " sm-best"
        elif worst_m == mname and best_m != mname:
            cls += " sm-worst"
        w_pct = f" ({round(float(weights[mname])*100)}%)" if mname in weights else ""
        chips.append(f'<span class="{cls}">{mname.upper()}: {float(mval):.1f}{unit_sym}{w_pct}</span>')
    if model_spread is not None and float(model_spread) > 0:
        chips.append(f'<span class="sm-chip" style="border-color:#9333ea55;color:var(--purple)">پراکندگی مدل‌ها: ±{float(model_spread):.2f}{unit_sym}</span>')
    if obs_temp is not None:
        chips.append(f'<span class="sm-chip" style="background:var(--amber-bg);color:var(--amber);border-color:#b4530955;font-weight:700">🌡️ دمای ثبت‌شده امروز (METAR): {float(obs_temp):.1f}{unit_sym}</span>')
    left_html = f"<div><b style='color:var(--text)'>زیرمدل‌های هواشناسی:</b> {''.join(chips)}</div>" if chips else ""
    right_html = f"<div>💡 <b style='color:var(--text)'>وضعیت گیت:</b> {prof['gate_reason']}</div>" if prof else ""
    return f'<div class="submodel-bar">{left_html}{right_html}</div>'


def _date_block_common_html(m, city_slug, city_name, locked_keys, open_locks_map, market_type="max"):
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
            corr_html = f' <span class="ltr-inline smart-val" style="color:var(--purple);font-weight:600">(اصلاح از {float(f_legacy):.1f}{unit_sym})</span>'
        leg_val_str = f"{float(f_legacy):.1f}{unit_sym}" if f_legacy is not None else f"{float(f_smart):.1f}{unit_sym}"
        mean_html = (
            f' <span style="font-size:12px;color:var(--text-dim);background:#fff;border:1px solid var(--border);border-radius:6px;padding:2px 8px;margin-right:6px">'
            f'<span class="smart-val">پیش‌بینی هوشمند: <b class="ltr-inline" style="color:var(--text)">{float(f_smart):.1f}{unit_sym}</b></span>'
            f'<span class="legacy-val" style="display:none">پیش‌بینی مدل: <b class="ltr-inline" style="color:var(--text)">{leg_val_str}</b></span>'
            f'{corr_html}</span>'
        )

    time_badge = (
        f'<span class="time-note" style="background:#fff;border:1px solid var(--border);border-radius:6px;padding:2px 8px">⏱ {time_note}</span>'
        if time_note else ""
    )
    summary = (
        f'<div style="display:inline-flex;flex-wrap:wrap;align-items:center;justify-content:space-between;width:96%;gap:8px">'
        f'<div><a href="{link}" target="_blank" rel="noopener" style="font-weight:700">{city_name} ↗</a> — <b class="ltr-inline">{date}</b>{mean_html}</div>'
        f'{time_badge}</div>'
    )
    sm_bar = _submodel_bar_html(m)
    table = _bucket_table(
        city_slug, city_name, date, unit_sym,
        m.get("full_distribution"), locked_keys, open_locks_map, main_signal_id, market_type=market_type,
    )
    return f"<details class='date-block' open><summary>{summary}</summary>{sm_bar}<div class='table-scroll'>{table}</div></details>"


def _date_block_html(m, city_slug, city_name, locked_keys, open_locks_map):
    return _date_block_common_html(m, city_slug, city_name, locked_keys, open_locks_map, market_type="max")


def _date_block_html_min(m, city_slug, city_name, locked_keys, open_locks_map):
    return _date_block_common_html(m, city_slug, city_name, locked_keys, open_locks_map, market_type="min")


DAILY_PNL_VISIBLE_DAYS = 5


def _daily_pnl_section_html(locks):
    daily = {}
    for l in locks:
        if l.get("status") not in history_manager.CLOSED_STATUSES:
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
        reason_map = {
            "closed_resolved": "resolve خودکار",
            "closed_auto_tp": "🎯 سیو سود خودکار (TP)",
            "closed_auto_sl": "🛑 حد ضرر خودکار (SL)",
            "closed_auto_metar": "🌡️ حد ضرر METAR",
            "closed_auto_trailing": "📈 تریلینگ استاپ خودکار",
            "closed_manual": "حذف دستی",
        }
        for l in day_locks:
            entry = l.get("entry_price")
            exit_price = l.get("exit_price")
            _, _, pct = history_manager._calc_cent_consistent_pct(entry, exit_price)
            if pct is not None:
                pcts.append(pct)

            is_min = l.get("market_type") == "min"
            mkt = _load_market_by_key_min(l.get("city"), l.get("date")) if is_min else _load_market_by_key(l.get("city"), l.get("date"))
            unit_sym = mkt.get("unit", "") if mkt else ""
            label = _find_locked_bucket_label(mkt, l.get("market_id"), unit_sym)

            name = LOCATIONS.get(l.get("city"), {}).get("name", l.get("city"))
            reason = reason_map.get(l.get("status"), "حذف دستی")
            entry_str = f"{entry:.3f}" if entry is not None else "-"
            exit_str = f"{exit_price:.3f}" if exit_price is not None else "-"
            css = "win" if (pct or 0) >= 0 else "loss"
            pct_str = f"<span class='{css}'>{pct:+.1f}%</span>" if pct is not None else "-"

            rows.append(
                f"<tr><td>{name}</td><td class='ltr-inline'>{l.get('date')}</td><td class='ltr-inline'>{label}</td>"
                f"<td class='ltr-inline'>{entry_str}</td><td class='ltr-inline'>{exit_str}</td><td class='ltr-inline'>{pct_str}</td><td>{reason}</td></tr>"
            )

        avg_pct = sum(pcts) / len(pcts) if pcts else 0.0
        summary_css = "win" if avg_pct >= 0 else "loss"
        table_html = (
            "<table><thead><tr><th>شهر</th><th>تاریخ هدف</th><th>باکت</th><th>ورود</th>"
            "<th>خروج</th><th>درصد</th><th>دلیل</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        )
        return (
            f"<details class='date-block'{open_attr}>"
            f"<summary><b class='ltr-inline'>{date}</b> — <span class='{summary_css} ltr-inline'>{avg_pct:+.1f}%</span> <span style='font-size:12px;color:var(--text-dim);margin-right:6px'>({len(day_locks)} معامله بسته‌شده)</span></summary>"
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

    return "<h2 id='daily-section'>\U0001F4C5 گزارش روزانه (بر اساس تاریخ بسته‌شدن به وقت ایران)</h2>" + "".join(blocks)


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
            "<table id='historyTable'><thead><tr><th>تاریخ</th><th>شهر</th><th>دمای سیگنال</th>"
            "<th>دمای نهایی</th><th>خرید (سنت)</th><th>فروش (سنت)</th><th>برآیند</th></tr></thead><tbody>"
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
                f"<td>{r['date']}</td><td>{r['city_name']}</td><td class='ltr-inline'>{r['signal_label']}</td>"
                f"<td class='ltr-inline'>{r['final_temp']}</td><td>{r['buy_cents']}</td><td>{sell}</td><td>{pct_html}</td></tr>"
            )
        parts.append("</tbody></table>")
        table_html = "".join(parts)

    return table_html, city_options, date_options


def _gate_badge_html(gd):
    if gd == "revert_to_shadow":
        return '<span class="gate-badge">🛡️ سوئیچ به مدل Shadow (حذف اورکورکشن)</span>'
    if gd == "sign_flip_damped":
        return '<span class="gate-badge" style="background:var(--amber-bg);color:var(--amber);border-color:#b4530944">🍂 ترمز تغییر فصل فعال</span>'
    if gd == "smart_submodel_blend":
        return '<span class="gate-badge" style="background:var(--blue-bg);color:var(--blue);border-color:#2563eb44">⚖️ وزن‌دهی هوشمند زیرمدل‌ها</span>'
    return '<span class="gate-badge" style="background:var(--green-bg);color:var(--green);border-color:#16a34a44">✅ کالیبره سودمند</span>'


def _build_group_section(markets, date_block_fn, profiles, market_type="max"):
    locks = _load_locks()
    locked_keys = _load_locked_keys(locks)
    open_locks_map = {
        (l.get("city"), l.get("date"), str(l.get("market_id"))): l
        for l in locks
        if l.get("status") == "open"
    }

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
        return '<div class="empty">هیچ بازار بازی برای نمایش وجود ندارد.</div>', 0

    for slug in sorted_slugs:
        city_name = city_names[slug]
        mlist = groups[slug]
        has_signal = group_has_signal(mlist)
        prof = profiles.get((slug, market_type)) or profiles.get((slug, "max"))
        us_flag = " 🇺🇸" if slug in US_CITIES else ""
        badge = '<span class="main-badge">🟢 سیگنال فعال</span>' if has_signal else ""
        form_badge = ""
        gate_badge = ""
        hit_pct = 100
        if prof:
            hit_pct = prof["recent_hit_pct"]
            form_badge = f'<span class="form-badge form-{prof["form_status"]}">{prof["form_label"]}</span>'
            if prof["gate_decision"] != "use_calibrated":
                gate_badge = _gate_badge_html(prof["gate_decision"])

        inner = "".join(date_block_fn(m, slug, city_name, locked_keys, open_locks_map) for m in mlist)
        count_note = f' <span style="font-size:11.5px;color:var(--text-dim);background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:2px 8px">{len(mlist)} تاریخ</span>' if len(mlist) > 1 else ""
        summary = (
            f'<div style="display:inline-flex;flex-wrap:wrap;align-items:center;justify-content:space-between;width:96%;gap:8px">'
            f'<div><b style="font-size:14.5px">{city_name}</b>{us_flag}{count_note} {badge}</div>'
            f'<div>{form_badge} {gate_badge}</div></div>'
        )

        is_us_attr = "1" if slug in US_CITIES else "0"
        parts.append(
            f"<details class='city-block' data-city-name='{city_name}' data-city-slug='{slug}' data-hit-pct='{hit_pct}' data-is-us='{is_us_attr}'>"
            f"<summary>{summary}</summary>{inner}</details>"
        )

    return "".join(parts), len(sorted_slugs)


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
        old_str = f"{p['old_hit_pct']}%" if p["old_hit_pct"] is not None else "-"
        delta = p["hit_delta_pct"]
        if delta is None:
            delta_html = "-"
        elif delta > 0:
            delta_html = f'<span class="win">+{delta}% ↑</span>'
        elif delta < 0:
            delta_html = f'<span class="loss">{delta}% ↓</span>'
        else:
            delta_html = "0%"

        cal_vs_raw = "-"
        if p["calibrated_mae"] is not None and p["raw_shadow_mae"] is not None:
            cal_vs_raw = f"Cal: {p['calibrated_mae']} / Shd: <b>{p['raw_shadow_mae']}</b>"

        best_sm = f'<span class="win">✔ {p["best_submodel"].upper()}</span>' if p["best_submodel"] else "-"
        if p.get("worst_submodel") and p["worst_submodel"] != p.get("best_submodel"):
            best_sm += f' <span class="loss">(✖ {p["worst_submodel"].upper()})</span>'

        if p["form_status"] == "hot":
            advice = "🟢 اولویت بالا برای ورود"
        elif p["form_status"] == "stable":
            advice = "🔵 مناسب با رعایت EV"
        elif p["form_status"] == "cold_drop":
            advice = "🟠 احتیاط (چرخش فصلی)"
        else:
            advice = "🔴 فقط با حاشیه امنیت بالا"

        display_style = "" if mtype == "max" else " style='display:none'"
        rows.append(
            f"<tr data-city-name='{p['city_name']}' data-mtype='{mtype}' data-is-us='{is_us_attr}'{display_style}>"
            f"<td>{idx}</td>"
            f"<td><b>{p['city_name']}</b>{us_flag}</td>"
            f"<td>{mtype_label}</td>"
            f"<td><span class='form-badge form-{p['form_status']}'>{p['form_label']}</span></td>"
            f"<td><b>{p['recent_hit_pct']}%</b></td>"
            f"<td style='color:var(--blue);font-weight:600'>{p['recent_within1_pct']}%</td>"
            f"<td>{old_str}</td>"
            f"<td class='ltr-inline'>{delta_html}</td>"
            f"<td class='ltr-inline'>{p['recent_mae']}°{p['unit']}</td>"
            f"<td class='ltr-inline'>{cal_vs_raw}</td>"
            f"<td class='ltr-inline'>{best_sm}</td>"
            f"<td>{_gate_badge_html(p['gate_decision'])}</td>"
            f"<td>{advice}</td>"
            f"</tr>"
        )

    table_html = (
        "<div class='table-scroll'><table id='accuracyLeaderboardTable'>"
        "<thead><tr><th>#</th><th>شهر</th><th>نوع</th><th>وضعیت فرم</th><th>دقت اخیر (باکت دقیق)</th><th>دقت با ±1 باکت</th>"
        "<th>دقت قدیم</th><th>تغییر روند</th><th>خطای اخیر (MAE)</th>"
        "<th>کالیبره vs Shadow</th><th>بهترین / بدترین مدل</th><th>تصمیم گیت هوشمند</th><th>پیشنهاد معاملاتی</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table></div>"
    )

    # Raw daily station accuracy records table (matches Card 2 in AI Studio Preview)
    raw_rows = []
    total_acc_lines = 0
    if ACCURACY_CSV.exists():
        try:
            with ACCURACY_CSV.open("r", encoding="utf-8-sig") as f:
                reader = list(csv.reader(f))
                data_lines = reader[1:] if len(reader) > 1 else []
                total_acc_lines = len(data_lines)
                for cols in reversed(data_lines[-120:]):
                    if len(cols) < 9:
                        continue
                    c_name = cols[0].strip()
                    dt_str = cols[1].strip()
                    mt_str = cols[2].strip()
                    f_m = cols[4].strip() or "-"
                    sig_str = cols[5].strip() or "-"
                    act_str = cols[6].strip() or "-"
                    err_str = cols[7].strip() or "-"
                    bias_str = cols[8].strip() or "-"
                    out_raw = cols[12].strip() if len(cols) > 12 else "-"
                    pnl_raw = cols[13].strip() if len(cols) > 13 else "-"
                    out_html = (
                        '<span style="background:var(--green-bg);color:var(--green);padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600">برد</span>'
                        if out_raw == "win"
                        else (
                            '<span style="background:var(--red-bg);color:var(--red);padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600">باخت</span>'
                            if out_raw == "loss"
                            else out_raw
                        )
                    )
                    raw_rows.append(
                        f"<tr data-city-name='{c_name}'>"
                        f"<td style='font-weight:600'>{c_name}</td><td class='ltr-inline'>{dt_str}</td><td>{mt_str}</td>"
                        f"<td class='ltr-inline'>{f_m}</td><td class='ltr-inline'>{sig_str}</td>"
                        f"<td class='ltr-inline' style='font-weight:600'>{act_str}</td><td class='ltr-inline'>{err_str}</td>"
                        f"<td class='ltr-inline'>{bias_str}</td><td>{out_html}</td><td class='ltr-inline'>{pnl_raw}</td></tr>"
                    )
        except Exception:
            pass

    raw_card_html = (
        f'<div style="background:#fff;border:1px solid var(--border);border-radius:12px;padding:16px;margin-top:16px">'
        f'<h3 style="margin-top:0;font-size:14.5px">ریزِ رکوردهای روزانه دقت مدل در برابر دمای واقعی ایستگاه‌ها ({total_acc_lines} رکورد)</h3>'
        f'<div class="table-scroll"><table id="rawAccuracyTable">'
        f'<thead><tr><th>شهر</th><th>تاریخ</th><th>نوع بازار</th><th>پیش‌بینی مدل</th><th>سیگما (σ)</th>'
        f'<th>دمای واقعی</th><th>خطای مطلق</th><th>بایاس</th><th>نتیجه</th><th>سود/زیان (u)</th></tr></thead>'
        f'<tbody>{"".join(raw_rows)}</tbody></table></div></div>'
    ) if raw_rows else ""

    return f"""
    <div style="background:#fff;border:1px solid var(--border);border-radius:12px;padding:16px">
      <div style="display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px">
        <h2 style="margin:0;border:none;padding:0">🛡️ جدول رتبه‌بندی فرم اخیر شهرها و گیت کالیبراسیون هوشمند (Champion-Challenger 14d)</h2>
        <a class="download-btn" style="background:var(--blue);color:#fff;border-color:var(--blue)" href="data/accuracy_report.csv" download="accuracy_report.csv">⬇ دانلود CSV دقت</a>
      </div>
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
    </div>
    {raw_card_html}
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
    open_locks_count = sum(1 for l in locks if l.get("status") == "open")

    locked_section_html = _locked_signals_section_html(locks)
    body_html, max_cities_count = _build_group_section(markets, _date_block_html, profiles, market_type="max")
    min_body_html, min_cities_count = _build_group_section(min_markets, _date_block_html_min, profiles, market_type="min")

    history_html, history_city_options, history_date_options = _history_table_html(locks)
    daily_pnl_html = _daily_pnl_section_html(locks)
    accuracy_tab_html = _build_accuracy_tab_html(profiles)

    scan_iso, scan_kind = _latest_scan_info()
    reference_dt = datetime.fromisoformat(scan_iso) if scan_iso else datetime.now(timezone.utc)

    html = HTML_TEMPLATE
    html = html.replace("OPEN_MARKETS_COUNT", str(len(markets) + len(min_markets)))
    html = html.replace("OPEN_LOCKS_COUNT", str(open_locks_count))
    html = html.replace("MAX_CITIES_COUNT", str(max_cities_count))
    html = html.replace("MIN_CITIES_COUNT", str(min_cities_count))
    html = html.replace("LASTUPDATE", _iran_time_str(reference_dt))
    html = html.replace("LASTSCANISO", scan_iso or "")
    html = html.replace("LASTSCANKIND", scan_kind or "")
    html = html.replace("LASTSCANFALLBACK", "بدون سابقهٔ اسکن" if not scan_iso else "")
    html = html.replace("LOCKED_SECTION_HTML", locked_section_html)
    html = html.replace("MIN_BODYHTML", min_body_html)
    html = html.replace("BODYHTML", body_html)
    html = html.replace("DAILYPNLHTML", daily_pnl_html)
    html = html.replace("HISTORYHTML", history_html)
    html = html.replace("HISTORYCITYOPTIONS", history_city_options)
    html = html.replace("HISTORYDATEOPTIONS", history_date_options)
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

