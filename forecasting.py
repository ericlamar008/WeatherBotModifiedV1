"""
forecasting.py -- Ensemble weather forecasting + calibration engine for WeatherBet v3
=====================================================================================
Replaces deterministic single-value forecasts with real ensemble distributions
(ECMWF ensemble + GEFS ensemble) and calibrates them against historical
forecast-vs-actual data (no need to wait 30 days of live paper trading).

FIX LOG (this version -- full_completeness_v2.py)

1. full_bucket_distribution() normalizes on UNROUNDED probabilities, rounds
LAST (see bucket_probability/full_bucket_distribution).
2. `distribution_sum(dist)` lets callers sanity-check completeness.
(Student's t fat-tailed CDF unchanged -- see TAIL_DF.)

--- PHASE 2 (WEATHERBOT_ROADMAP.md, F2) --------------------------------------
`fetch_historical_actual()` fetches ground truth from the SAME station feed
used for live resolution (NOAA METAR archive / Hong Kong Observatory), not a
gridded reanalysis product. Disk cache: `data/actuals_cache.json`.

--- PHASE 4 (F4) --------------------------------------------------------------
`_bias_cap()` is DYNAMIC: `max(flat default, legacy manual floor,
BIAS_CAP_SAFETY_MARGIN x observed |raw bias|)`, bounded by an absolute
ceiling (`ABSOLUTE_BIAS_CAP_F`/`_C`).

--- PHASE 5 (F5) --------------------------------------------------------------
`backfill_calibration()` uses a day-of-year seasonal weighting
(`_seasonal_weight`, 45-day half-life) instead of a flat 180-day average, so
the current season's bias isn't diluted by a milder/stronger season earlier
in the rolling window.

--- PHASE 6 FIX LOG (WEATHERBOT_ROADMAP.md, یافته F6) -------------------------
Sigma for horizons D1/D2/D3 used to be pure guesswork: `base_mae * sqrt(1 +
h*0.6)`. FIX: `fetch_historical_forecast_by_horizon()` pulls the archived
HOURLY forecast at each lead-time offset via Open-Meteo's Previous Model Runs
API, aggregates to a real daily max locally, and `backfill_calibration()`
now compares this REAL per-horizon forecast against actual, with the SAME
seasonal weighting as Phase 5. Explicit tested FALLBACK to the old synthetic
scaling if fewer than MIN_REAL_HORIZON_SAMPLES real samples come back.

=====================================================================================
نقشه‌راه بهبود (۱۶ سپتامبر ۲۰۲۶) -- خلاصهٔ افزودنی‌های این نسخه
=====================================================================================
فاز ۳+۴ (وزن‌دهی پویای مدل‌ها، جایگزین -- بدون هیچ اثر روی forecast_mean/
sigma زنده): build_combined_distribution_full() یک بار همان دو فراخوانی
ECMWF+GEFS موجود را انجام می‌دهد و هم لیست ترکیبی قدیمی (برای سازگاری
کامل) و هم دو (تا چهار) لیست جدا به‌ازای هر مدل را برمی‌گرداند. GEM
(همهٔ شهرها) و ICON-EU-EPS (فقط region=="eu") به‌عنوان مدل‌های اضافی، بدون
فراخوانی شبکهٔ اضافه‌شده به مسیر قدیمی، اضافه شده‌اند. HRRR (قطعی، نه
ensemble؛ فقط شهرهای region=="us") جدا ذخیره می‌شود، وارد هیچ میانگینی
نمی‌شود. build_shadow_mean() میانگین وزن‌دار (بر اساس مهارت واقعی
ثبت‌شده در data/model_skill.json، با fallback ایمن به میانگین مساوی) را
در forecast_mean_shadow ذخیره می‌کند -- کاملاً جدا از forecast_mean اصلی
که هیچ‌گاه تغییر نمی‌کند. update_model_skill_from_live() مشابه دقیق
update_calibration_from_live موجود است، فقط برای خطای هر مدل جدا.

⚠️ نکتهٔ صداقت فنی (بدهی فنی فاز ۷): شناسهٔ دقیق مدل "icon_eu_eps" از
الگوی نام‌گذاری مستندات Open-Meteo استنتاج شده، نه از رشتهٔ تأییدشدهٔ
مستقیم. اگر اشتباه باشد، طبق طراحی امن این ماژول فقط dict خالی برمی‌گردد
(هرگز crash نمی‌کند) و آن مدل بی‌اثر می‌ماند تا تأیید/اصلاح شود.

فاز ۶ (بازار دمای کمینه، کاملاً مستقل از حداکثر): تمام توابع با پسوند
"_min" -- کالیبراسیون با کلیدهای جدا ({city}_min_D{h}) در همان
calibration.json، بدون هیچ تداخل کلید با نسخهٔ حداکثر (تست شد).
=====================================================================================
"""

import json
import math
import time
try:
    import requests
except ImportError:
    requests = None
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
CALIBRATION_FILE = DATA_DIR / "calibration.json"
ACTUALS_CACHE_FILE = DATA_DIR / "actuals_cache.json"
MODEL_SKILL_FILE = DATA_DIR / "model_skill.json"

DEFAULT_SIGMA_F = 2.5
DEFAULT_SIGMA_C = 1.4
DEFAULT_BIAS_F = 0.0
DEFAULT_BIAS_C = 0.0
BIAS_CAP_C = 2.0
BIAS_CAP_F = 3.6
ABSOLUTE_BIAS_CAP_F = 10.0
ABSOLUTE_BIAS_CAP_C = 5.5
BIAS_CAP_SAFETY_MARGIN = 1.3
SEASONAL_HALF_LIFE_DAYS = 45
# --- FIX (Phase 6 / F6): minimum real per-horizon samples required before
# trusting the new real-lead-time measurement over the old synthetic scaling.
MIN_REAL_HORIZON_SAMPLES = 20
MIN_MODEL_SKILL_SAMPLES = 15  # (فاز ۳+۴) هماهنگ با آستانهٔ update_calibration_from_live
TAIL_DF = 5.0
MAX_RETRIES = 3
RETRY_DELAY_S = 3

BIAS_CAP_OVERRIDES = {
    "los-angeles": 7.5,
    "shanghai": 3.0,
    "seoul": 2.5,
    "kuala-lumpur": 2.5,
    "munich": 2.5,
    "tel-aviv": 2.5,
    "dallas": 3.6,
    "singapore": 2.5,
    "miami": 3.6,
    "mexico-city": 2.0,
    "hong-kong": 2.5,
    "houston": 3.6,
}

# (فاز ۴) کدام مدل‌های اضافی برای کدام شهرها فعال‌اند -- بر اساس فیلد
# region موجود در locations.py؛ None یعنی «همهٔ شهرها».
EXTRA_MODEL_REGIONS = {
    "gem": None,
    "icon_eu": "eu",
}


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _betacf(a, b, x, maxit=200, eps=3e-7, fpmin=1e-30):
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, maxit + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def _betai(a, b, x):
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    else:
        return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(t, df=TAIL_DF):
    x = df / (df + t * t)
    p = 0.5 * _betai(df / 2.0, 0.5, x)
    return 1.0 - p if t > 0 else p


def _bias_cap(unit, city_slug=None, observed_raw_bias=None):
    """(Phase 4 / F4: dynamic cap -- see module docstring.)"""
    flat_default = BIAS_CAP_F if unit == "F" else BIAS_CAP_C
    absolute_ceiling = ABSOLUTE_BIAS_CAP_F if unit == "F" else ABSOLUTE_BIAS_CAP_C
    floor = flat_default
    if city_slug is not None and city_slug in BIAS_CAP_OVERRIDES:
        floor = max(floor, BIAS_CAP_OVERRIDES[city_slug])
    if observed_raw_bias is not None:
        dynamic = min(absolute_ceiling, abs(observed_raw_bias) * BIAS_CAP_SAFETY_MARGIN)
        floor = max(floor, dynamic)
    return round(floor, 3)


def _day_of_year_distance(d1, d2):
    """(Phase 5 / F5.) Circular day-of-year distance, handles year wrap."""
    doy1 = d1.timetuple().tm_yday
    doy2 = d2.timetuple().tm_yday
    diff = abs(doy1 - doy2)
    return min(diff, 365 - diff)


def _seasonal_weight(date_obj, reference_date, half_life_days=SEASONAL_HALF_LIFE_DAYS):
    """(Phase 5 / F5.) Weight decaying by day-of-year distance from "today"."""
    dist = _day_of_year_distance(date_obj, reference_date)
    return 0.5 ** (dist / half_life_days)


def _weighted_mean(values, weights):
    total_w = sum(weights) or 1.0
    return sum(v * w for v, w in zip(values, weights)) / total_w


# =============================================================================
# ENSEMBLE FETCH (LIVE FORECASTING)
# =============================================================================

def _fetch_ensemble_uncached(lat, lon, tz, unit, models, forecast_days=7, variable="temperature_2m_max"):
    """(مرحلهٔ ۲: نام این تابع از _fetch_ensemble به _fetch_ensemble_uncached عوض شد؛ بدنه‌اش
    دقیقاً همان قبلی است.)
    (فاز ۶) پارامتر variable اضافه شد؛ مقدار پیش‌فرض دقیقاً رفتار
    قبلی (temperature_2m_max) را حفظ می‌کند -- هیچ فراخوانی موجودی نیازی
    به تغییر ندارد."""
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    url = (
        f"https://ensemble-api.open-meteo.com/v1/ensemble"
        f"?latitude={lat}&longitude={lon}"
        f"&daily={variable}&temperature_unit={temp_unit}"
        f"&forecast_days={forecast_days}&timezone={tz}"
        f"&models={models}"
    )
    for attempt in range(MAX_RETRIES):
        try:
            data = requests.get(url, timeout=(5, 12)).json()
            if "error" in data:
                return {}
            daily = data.get("daily", {})
            dates = daily.get("time", [])
            result = {d: [] for d in dates}
            for key, series in daily.items():
                if key == "time" or not key.startswith(variable):
                    continue
                for d, v in zip(dates, series):
                    if v is not None:
                        result[d].append(v)
            return result
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)
            else:
                return {}
    return {}


# =============================================================================
# (مرحلهٔ ۲) حافظهٔ موقت + پیش‌گرفتن موازی پاسخ‌های Open-Meteo
# -----------------------------------------------------------------------------
# تا قبل از این، برای هر (شهر، تاریخ) کل پیش‌بینی ۷ روزهٔ همان شهر دوباره از
# اینترنت گرفته می‌شد؛ در حالی که آدرس درخواست برای همهٔ تاریخ‌های یک شهر یکی
# است. حالا:
#  - هر آدرس یکتا فقط یک بار در طول یک اسکن گرفته می‌شود (حافظهٔ موقت).
#  - فقط پاسخ موفق و دارای داده ذخیره می‌شود؛ شکست/خالی هرگز ذخیره نمی‌شود و
#    مثل قبل برای هر مارکت دوباره تلاش می‌شود (رفتار خطا دست‌نخورده).
#  - هر بار که حافظه می‌خواند، یک کپی برمی‌گرداند تا هیچ کدی نتواند آن را خراب کند.
#  - prefetch_ensembles() همین آدرس‌ها را قبل از حلقهٔ اصلی با چند کار همزمان
#    می‌گیرد؛ حلقه‌های اسکن هیچ تغییری نکرده‌اند و فقط از حافظه می‌خوانند.
#  - با set_ensemble_cache_enabled(False) رفتار دقیقاً مثل قبل است.
# =============================================================================
_ENSEMBLE_CACHE = {}
_ENSEMBLE_CACHE_LOCK = threading.Lock()
_ENSEMBLE_CACHE_ENABLED = True

def set_ensemble_cache_enabled(enabled):
    global _ENSEMBLE_CACHE_ENABLED
    _ENSEMBLE_CACHE_ENABLED = bool(enabled)

def clear_ensemble_cache():
    with _ENSEMBLE_CACHE_LOCK:
        _ENSEMBLE_CACHE.clear()

def _copy_ensemble(result):
    return {d: list(v) for d, v in result.items()}

def _ensemble_has_data(result):
    return bool(result) and any(result.values())

def _fetch_ensemble(lat, lon, tz, unit, models, forecast_days=7, variable="temperature_2m_max"):
    """همان امضا و همان خروجی قبلی؛ فقط اگر همین آدرس قبلاً در همین اسکن با
    موفقیت گرفته شده باشد، از حافظه می‌خواند."""
    if not _ENSEMBLE_CACHE_ENABLED:
        return _fetch_ensemble_uncached(lat, lon, tz, unit, models, forecast_days, variable)
    key = (lat, lon, tz, unit, models, forecast_days, variable)
    with _ENSEMBLE_CACHE_LOCK:
        hit = _ENSEMBLE_CACHE.get(key)
    if hit is not None:
        return _copy_ensemble(hit)
    result = _fetch_ensemble_uncached(lat, lon, tz, unit, models, forecast_days, variable)
    if _ensemble_has_data(result):
        with _ENSEMBLE_CACHE_LOCK:
            _ENSEMBLE_CACHE[key] = _copy_ensemble(result)
    return result

def prefetch_ensembles(locations, max_cities=None, min_cities=None, workers=4):
    """آدرس‌های یکتای مورد نیاز اسکن (Open-Meteo + ایستگاه‌های METAR روز جاری D0)
    را با چند کار همزمان می‌گیرد و در حافظه می‌گذارد تا هیچ تاخیری به حلقهٔ اسکن اضافه نشود."""
    tasks = []
    now_utc = datetime.now(timezone.utc)
    for slug, loc in locations.items():
        args = (loc["lat"], loc["lon"], loc["tz"], loc["unit"])
        need_max = (max_cities is None or slug in max_cities)
        need_min = (min_cities is None or slug in min_cities)
        if need_max:
            tasks.append((get_ecmwf_ensemble, args))
            tasks.append((get_gefs_ensemble, args))
            for extra in _active_extra_models(loc):
                if extra == "gem":
                    tasks.append((get_gem_ensemble, args))
                elif extra == "icon_eu":
                    tasks.append((get_icon_eu_ensemble, args))
        if need_min:
            tasks.append((get_ecmwf_ensemble_min, args))
            tasks.append((get_gefs_ensemble_min, args))
        if (need_max or need_min) and loc.get("station"):
            try:
                local_today = now_utc.astimezone(ZoneInfo(loc["tz"])).strftime("%Y-%m-%d")
            except Exception:
                local_today = now_utc.strftime("%Y-%m-%d")
            tasks.append((get_intraday_observed_bound, (loc, local_today, False)))

    def _run(task):
        fn, args = task
        try:
            fn(*args)
        except Exception:
            pass

    t0 = time.perf_counter()
    if tasks:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
            list(pool.map(_run, tasks))
    with _ENSEMBLE_CACHE_LOCK:
        cached = len(_ENSEMBLE_CACHE)
    return {"tasks": len(tasks), "cached": cached, "seconds": round(time.perf_counter() - t0, 1)}

def verify_ensemble_cache(sample=5):
    """(فقط برای بررسی) چند آدرس حافظه را دوباره مستقیم می‌گیرد و برابری را می‌سنجد.
    برمی‌گرداند (تعداد برابر, تعداد بررسی‌شده)."""
    with _ENSEMBLE_CACHE_LOCK:
        keys = list(_ENSEMBLE_CACHE.keys())
    if not keys:
        return 0, 0
    step = max(1, len(keys) // sample)
    picked = keys[::step][:sample]
    same = 0
    for key in picked:
        fresh = _fetch_ensemble_uncached(*key)
        with _ENSEMBLE_CACHE_LOCK:
            cached = _ENSEMBLE_CACHE.get(key)
        if fresh == cached:
            same += 1
    return same, len(picked)

def get_ecmwf_ensemble(lat, lon, tz, unit):
    return _fetch_ensemble(lat, lon, tz, unit, "ecmwf_ifs025", forecast_days=7)


def get_gefs_ensemble(lat, lon, tz, unit):
    return _fetch_ensemble(lat, lon, tz, unit, "gfs_seamless", forecast_days=7)


def get_gem_ensemble(lat, lon, tz, unit):
    """(فاز ۴) GEM (سرویس هواشناسی کانادا) -- ۲۱ عضو، جهانی."""
    return _fetch_ensemble(lat, lon, tz, unit, "gem_global", forecast_days=7)


def get_icon_eu_ensemble(lat, lon, tz, unit):
    """(فاز ۴) ICON-EU-EPS (DWD آلمان) -- ۴۰ عضو، ۱۳ کیلومتر، فقط اروپا.
    ⚠️ رشتهٔ مدل استنتاجی -- نگاه کنید به یادداشت صداقت فنی بالای فایل."""
    return _fetch_ensemble(lat, lon, tz, unit, "icon_eu_eps", forecast_days=7)


def get_hrrr_forecast(lat, lon, tz, unit):
    """(فاز ۴) HRRR -- برخلاف بقیه، قطعی است نه ensemble، و از endpoint
    متفاوتی (v1/gfs) می‌آید. فقط برای شهرهای آمریکایی معنا دارد. خروجی:
    dict[date_str] -> float (یا {} در صورت هر خطایی)."""
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    url = (
        f"https://api.open-meteo.com/v1/gfs"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
        f"&timezone={tz}&forecast_days=7&models=ncep_hrrr_conus"
    )
    for attempt in range(MAX_RETRIES):
        try:
            data = requests.get(url, timeout=(5, 12)).json()
            daily = data.get("daily", {})
            return dict(zip(daily.get("time", []), daily.get("temperature_2m_max", [])))
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)
            else:
                return {}
    return {}


def build_combined_distribution(city_slug, loc, date_str):
    """UNCHANGED -- همچنان جای دیگری (برای سازگاری قدیمی) قابل‌فراخوانی
    است، ولی weatherbot_v3.py از این پس build_combined_distribution_full
    را صدا می‌زند."""
    members = []
    ecmwf = get_ecmwf_ensemble(loc["lat"], loc["lon"], loc["tz"], loc["unit"])
    gefs = get_gefs_ensemble(loc["lat"], loc["lon"], loc["tz"], loc["unit"])
    members.extend(ecmwf.get(date_str, []))
    members.extend(gefs.get(date_str, []))
    return members


def _active_extra_models(loc):
    """(فاز ۴) کدام مدل‌های اضافی برای این شهر معنا دارند، بر اساس فیلد
    region موجود در locations.py -- هیچ تغییری در locations.py لازم نیست."""
    active = []
    for model_name, required_region in EXTRA_MODEL_REGIONS.items():
        if required_region is None or loc.get("region") == required_region:
            active.append(model_name)
    return active


def _fetch_extra_model(model_name, lat, lon, tz, unit, date_str):
    if model_name == "gem":
        return get_gem_ensemble(lat, lon, tz, unit).get(date_str, [])
    if model_name == "icon_eu":
        return get_icon_eu_ensemble(lat, lon, tz, unit).get(date_str, [])
    return []


def build_combined_distribution_full(city_slug, loc, date_str):
    """(فاز ��+۴) جایگزین نقطهٔ فراخوانی build_combined_distribution --
    همان دو فراخوانی ECMWF+GEFS را فقط یک‌بار انجام می‌دهد و هم لیست
    ترکیبی (دقیقاً همان‌طور که تا الان forecast_mean/sigma زنده را
    می‌ساخت -- بدون تغییر) و هم دیکشنری اعضای هر مدل جدا (برای محاسبهٔ
    سایه) را برمی‌گرداند. مدل‌های اضافی (GEM/ICON-EU) فقط در دیکشنری دوم
    ظاهر می‌شوند، هرگز در members_combined -- یعنی سیگنال واقعی دست‌نخورده
    می‌ماند.
    خروجی: (members_combined, model_members_dict)"""
    ecmwf = get_ecmwf_ensemble(loc["lat"], loc["lon"], loc["tz"], loc["unit"])
    gefs = get_gefs_ensemble(loc["lat"], loc["lon"], loc["tz"], loc["unit"])
    ecmwf_members = ecmwf.get(date_str, [])
    gfs_members = gefs.get(date_str, [])
    members_combined = list(ecmwf_members) + list(gfs_members)

    model_members = {"ecmwf": ecmwf_members, "gfs": gfs_members}
    for extra in _active_extra_models(loc):
        model_members[extra] = _fetch_extra_model(
            extra, loc["lat"], loc["lon"], loc["tz"], loc["unit"], date_str
        )
    return members_combined, model_members


# =============================================================================
# فاز ۳+۴: مهارت هرمدل + میانگین وزن‌دار «سایه» (بدون اثر روی سیگنال زنده)
# =============================================================================

_MODEL_SKILL_MEM_CACHE = None


def load_model_skill():
    global _MODEL_SKILL_MEM_CACHE
    if _MODEL_SKILL_MEM_CACHE is not None:
        return _MODEL_SKILL_MEM_CACHE
    if MODEL_SKILL_FILE.exists():
        try:
            _MODEL_SKILL_MEM_CACHE = json.loads(MODEL_SKILL_FILE.read_text(encoding="utf-8"))
            return _MODEL_SKILL_MEM_CACHE
        except Exception:
            return {}
    return {}


def save_model_skill(skill):
    global _MODEL_SKILL_MEM_CACHE
    _MODEL_SKILL_MEM_CACHE = skill
    MODEL_SKILL_FILE.write_text(json.dumps(skill, indent=2, ensure_ascii=False), encoding="utf-8")


_DYNAMIC_BMA_CACHE = None


def _compute_bma_skill_from_markets():
    """محاسبهٔ پویا و کش‌شدهٔ خطای زیرمدل‌ها (EWMA RMSE/MAE در ۳۰ روز اخیر)
    از روی فایل‌های data/markets/*.json برای زمانی که model_skill.json کامل نیست."""
    global _DYNAMIC_BMA_CACHE
    if _DYNAMIC_BMA_CACHE is not None:
        return _DYNAMIC_BMA_CACHE

    result = {}
    markets_dir = DATA_DIR / "markets"
    if not markets_dir.exists():
        _DYNAMIC_BMA_CACHE = result
        return result

    by_city = {}
    for p in markets_dir.glob("*.json"):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if m.get("status") not in ("resolved", "resolved_no_signal"):
            continue
        if m.get("actual_temp") is None or not isinstance(m.get("model_means_raw"), dict):
            continue
        c_key = f"{m.get('city')}_min" if m.get("market_type") == "min" else str(m.get("city"))
        by_city.setdefault(c_key, []).append(m)

    for c_key, items in by_city.items():
        items.sort(key=lambda x: str(x.get("date", "")))
        recent = items[-30:]
        model_names = set()
        for m in recent:
            model_names.update(m["model_means_raw"].keys())
        c_skill = {}
        for mname in model_names:
            errs_sq, errs_abs, w_list = [], [], []
            valid = [m for m in recent if m["model_means_raw"].get(mname) is not None]
            if len(valid) < 3:
                continue
            n_val = len(valid)
            for idx, m in enumerate(valid):
                age = (n_val - 1) - idx
                w = 0.90 ** age
                err = abs(float(m["model_means_raw"][mname]) - float(m["actual_temp"]))
                errs_abs.append(err * w)
                errs_sq.append((err ** 2) * w)
                w_list.append(w)
            w_sum = sum(w_list) or 1.0
            ewma_mae = sum(errs_abs) / w_sum
            ewma_rmse = math.sqrt(sum(errs_sq) / w_sum)
            c_skill[mname] = {
                "mae": round(ewma_mae, 3),
                "rmse": round(ewma_rmse, 3),
                "n": n_val,
            }
        if c_skill:
            result[c_key] = c_skill

    _DYNAMIC_BMA_CACHE = result
    return result


def get_model_weights(city_slug, model_names, min_n=3, is_min=False):
    """وزن‌دهی پویا به روش Bayesian / Inverse-RMSE² (BMA) به تفکیک هر شهر.
    اگر برخی زیرمدل‌ها دادهٔ تاریخی کمتری داشته باشند، به جای حذف کامل وزن‌دهی،
    میانگین خطای شهر به عنوان prior برای آن زیرمدل لحاظ می‌شود تا همیشه مدل‌های
    دقیق‌تر آن شهر وزن بالاتری بگیرند."""
    if not model_names:
        return None
    lookup_key = f"{city_slug}_min" if is_min else city_slug
    file_skill = load_model_skill()
    dyn_skill = _compute_bma_skill_from_markets()
    skill = {**dyn_skill.get(city_slug, {}), **file_skill.get(city_slug, {}), **dyn_skill.get(lookup_key, {}), **file_skill.get(lookup_key, {})}

    scores = {}
    for m in model_names:
        entry = skill.get(m, {})
        err_metric = entry.get("rmse") or entry.get("mae")
        n = entry.get("n", 0)
        if err_metric and n >= min_n:
            # Inverse-variance (1 / (RMSE^2 + 0.15)) gives sharper BMA separation
            scores[m] = 1.0 / ((float(err_metric) ** 2) + 0.15)

    if not scores:
        return None

    # اگر زیرمدل جدیدی (مثل gem یا icon_eu) هنوز به حد نصاب نرسیده، وزن میانه بگیرد
    vals = sorted(scores.values())
    median_score = vals[len(vals) // 2]
    for m in model_names:
        if m not in scores:
            scores[m] = median_score * 0.85

    total = sum(scores.values()) or 1.0
    return {m: round(scores[m] / total, 4) for m in model_names}


def compute_model_spread(raw_means, weights=None):
    """محاسبهٔ واگرایی (Spread) بین زیرمدل‌ها برای کالیبراسیون پویای Sigma و تشخیص دوقله‌ای."""
    if not raw_means or len(raw_means) < 2:
        return 0.0
    vals = [float(v) for v in raw_means.values() if v is not None]
    if len(vals) < 2:
        return 0.0
    if weights:
        w_vals = [float(weights.get(k, 1.0 / len(vals))) for k in raw_means if raw_means[k] is not None]
        w_sum = sum(w_vals) or 1.0
        mean_v = sum(v * w for v, w in zip(vals, w_vals)) / w_sum
        var = sum(w * ((v - mean_v) ** 2) for v, w in zip(vals, w_vals)) / w_sum
        std_spread = math.sqrt(max(0.0, var))
    else:
        mean_v = sum(vals) / len(vals)
        var = sum((v - mean_v) ** 2 for v in vals) / len(vals)
        std_spread = math.sqrt(max(0.0, var))
    range_spread = (max(vals) - min(vals)) * 0.5
    return round(max(std_spread, range_spread), 3)


def build_shadow_mean(model_members, city_slug, horizon_days, unit, is_min=False):
    """میانگین وزن‌دار پویا (BMA) روی زیرمدل‌های هواشناسی (ECMWF, GFS, GEM, ICON-EU)."""
    raw_means = {}
    for model_name, members in (model_members or {}).items():
        if members:
            raw_means[model_name] = sum(members) / len(members)

    if not raw_means:
        return None, {}

    weights = get_model_weights(city_slug, list(raw_means.keys()), is_min=is_min)
    if weights is None:
        raw_shadow_mean = sum(raw_means.values()) / len(raw_means)
    else:
        raw_shadow_mean = sum(weights[m] * raw_means[m] for m in raw_means)

    bias = get_bias_min(city_slug, horizon_days, unit) if is_min else get_bias(city_slug, horizon_days, unit)
    shadow_mean = round(raw_shadow_mean + bias, 2)
    raw_means_rounded = {m: round(v, 2) for m, v in raw_means.items()}
    return shadow_mean, raw_means_rounded


def update_model_skill_from_live(markets, locations):
    """به‌روزرسانی مهارت هر زیرمدل با وزن‌دهی نمایی به روزهای اخیر (EWMA RMSE & MAE)."""
    skill = load_model_skill()
    resolved = [
        m for m in markets
        if m.get("status") in ("resolved", "resolved_no_signal")
        and m.get("actual_temp") is not None
        and m.get("model_means_raw")
    ]
    resolved.sort(key=lambda x: str(x.get("date", "")))
    all_model_names = set()
    for m in resolved:
        all_model_names.update(m["model_means_raw"].keys())

    for city_slug in locations:
        group = [m for m in resolved if m["city"] == city_slug][-30:]
        for model_name in all_model_names:
            samples = [m for m in group if m["model_means_raw"].get(model_name) is not None]
            if len(samples) < 3:
                continue
            n_s = len(samples)
            errs_abs, errs_sq, weights = [], [], []
            for idx, m in enumerate(samples):
                w = 0.90 ** ((n_s - 1) - idx)
                err = abs(float(m["model_means_raw"][model_name]) - float(m["actual_temp"]))
                errs_abs.append(err * w)
                errs_sq.append((err ** 2) * w)
                weights.append(w)
            w_sum = sum(weights) or 1.0
            mae = sum(errs_abs) / w_sum
            rmse = math.sqrt(sum(errs_sq) / w_sum)
            entry = skill.setdefault(city_slug, {})
            entry[model_name] = {
                "mae": round(mae, 3),
                "rmse": round(rmse, 3),
                "n": n_s,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
    save_model_skill(skill)
    return skill


# =============================================================================
# CALIBRATION (HISTORICAL BACKFILL) -- حداکثر دما
# =============================================================================

def fetch_historical_forecast(lat, lon, tz, unit, start_date, end_date):
    """Unchanged. Used as the D0 baseline AND as the fallback source for any
    horizon that doesn't get enough real per-horizon samples (Phase 6)."""
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    url = (
        f"https://historical-forecast-api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
        f"&timezone={tz}&start_date={start_date}&end_date={end_date}"
        f"&models=ecmwf_ifs025"
    )
    for attempt in range(MAX_RETRIES):
        try:
            data = requests.get(url, timeout=(5, 15)).json()
            daily = data.get("daily", {})
            return dict(zip(daily.get("time", []), daily.get("temperature_2m_max", [])))
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)
            else:
                return {}
    return {}


def _daily_max_from_hourly_previous_day(hourly_data, var_key):
    """(Phase 6 / F6.) The Previous Runs API only exposes HOURLY variables
    (no daily-max variant), so we aggregate to a daily max ourselves."""
    times = hourly_data.get("time", [])
    series = hourly_data.get(var_key, [])
    daily_max = {}
    for t_str, v in zip(times, series):
        if v is None:
            continue
        date_part = t_str[:10]
        if date_part not in daily_max or v > daily_max[date_part]:
            daily_max[date_part] = v
    return daily_max


def fetch_historical_forecast_by_horizon(lat, lon, tz, unit, start_date, end_date, horizon_days=(0, 1, 2, 3)):
    """
    FIX (Phase 6 / F6). Real per-horizon forecast series via Open-Meteo's
    Previous Model Runs API (fixed lead-time offsets), aggregated to daily
    max locally. Returns dict[h] -> dict[date_str] -> daily_max_float.
    """
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    var_names = [f"temperature_2m_previous_day{h}" for h in horizon_days]
    url = (
        f"https://previous-runs-api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly={','.join(var_names)}"
        f"&temperature_unit={temp_unit}&timezone={tz}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&models=ecmwf_ifs025"
    )
    result = {h: {} for h in horizon_days}
    for attempt in range(MAX_RETRIES):
        try:
            data = requests.get(url, timeout=(5, 20)).json()
            hourly = data.get("hourly", {})
            if not hourly:
                return result
            for h in horizon_days:
                key = f"temperature_2m_previous_day{h}"
                if key in hourly:
                    result[h] = _daily_max_from_hourly_previous_day(hourly, key)
            return result
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)
            else:
                return result
    return result


def _load_actuals_cache():
    if ACTUALS_CACHE_FILE.exists():
        try:
            return json.loads(ACTUALS_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_actuals_cache(cache):
    ACTUALS_CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def _fetch_metar_max_for_date(station, unit, date_str):
    url = (
        f"https://aviationweather.gov/api/data/metar"
        f"?ids={station}&format=json&date={date_str.replace('-', '')}"
    )
    for attempt in range(MAX_RETRIES):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if not data:
                return None
            temps = [float(d.get("temp")) for d in data if d.get("temp") is not None]
            if not temps:
                return None
            max_c = max(temps)
            return round(max_c * 9 / 5 + 32, 1) if unit == "F" else round(max_c, 1)
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)
            else:
                return None
    return None


def _fetch_metar_min_for_date(station, unit, date_str):
    """(فاز ۶) معادل _fetch_metar_max_for_date -- min(temps) به‌جای max(temps)."""
    url = (
        f"https://aviationweather.gov/api/data/metar"
        f"?ids={station}&format=json&date={date_str.replace('-', '')}"
    )
    for attempt in range(MAX_RETRIES):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if not data:
                return None
            temps = [float(d.get("temp")) for d in data if d.get("temp") is not None]
            if not temps:
                return None
            min_c = min(temps)
            return round(min_c * 9 / 5 + 32, 1) if unit == "F" else round(min_c, 1)
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)
            else:
                return None
    return None


def _fetch_historical_actual_noaa_range(station, unit, start_date, end_date, cache):
    station_cache = cache.setdefault(station, {})
    results = {}
    d = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    while d <= end:
        date_str = d.isoformat()
        if date_str in station_cache:
            val = station_cache[date_str]
        else:
            val = _fetch_metar_max_for_date(station, unit, date_str)
            station_cache[date_str] = val
            time.sleep(0.2)
        if val is not None:
            results[date_str] = val
        d += timedelta(days=1)
    return results


def _fetch_historical_actual_min_noaa_range(station, unit, start_date, end_date, cache):
    """(فاز ۶) کش جدا از نسخهٔ حداکثر (کلید فرعی "_min" در همان cache dict)."""
    station_cache = cache.setdefault(station + "_min", {})
    results = {}
    d = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    while d <= end:
        date_str = d.isoformat()
        if date_str in station_cache:
            val = station_cache[date_str]
        else:
            val = _fetch_metar_min_for_date(station, unit, date_str)
            station_cache[date_str] = val
            time.sleep(0.2)
        if val is not None:
            results[date_str] = val
        d += timedelta(days=1)
    return results


def _fetch_historical_actual_hko_range(start_date, end_date, cache):
    station_cache = cache.setdefault("HKO", {})
    d = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    needed_dates = set()
    while d <= end:
        needed_dates.add(d.isoformat())
        d += timedelta(days=1)

    if not needed_dates.issubset(station_cache.keys()):
        try:
            url = (
                "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php"
                "?dataType=CLMMAXT&lang=en&rformat=json&station=HKO"
            )
            data = requests.get(url, timeout=(5, 15)).json()
            for row in data.get("data", []):
                raw_date = str(row.get("date", ""))
                if len(raw_date) == 8 and row.get("value") is not None:
                    iso = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                    station_cache[iso] = round(float(row["value"]), 1)
        except Exception:
            pass

    return {dt: station_cache[dt] for dt in needed_dates if dt in station_cache and station_cache[dt] is not None}


def _fetch_historical_actual_min_hko_range(start_date, end_date, cache):
    """(فاز ۶) معادل نسخهٔ حداکثر ولی با dataType=CLMMINT."""
    station_cache = cache.setdefault("HKO_min", {})
    d = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    needed_dates = set()
    while d <= end:
        needed_dates.add(d.isoformat())
        d += timedelta(days=1)

    if not needed_dates.issubset(station_cache.keys()):
        try:
            url = (
                "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php"
                "?dataType=CLMMINT&lang=en&rformat=json&station=HKO"
            )
            data = requests.get(url, timeout=(5, 15)).json()
            for row in data.get("data", []):
                raw_date = str(row.get("date", ""))
                if len(raw_date) == 8 and row.get("value") is not None:
                    iso = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                    station_cache[iso] = round(float(row["value"]), 1)
        except Exception:
            pass

    return {dt: station_cache[dt] for dt in needed_dates if dt in station_cache and station_cache[dt] is not None}


def fetch_historical_actual(loc, start_date, end_date):
    """(Phase 2 / F2.) Ground truth from the SAME station feed used for live
    resolution. Output shape: dict[date_str] -> float."""
    cache = _load_actuals_cache()
    unit = loc["unit"]
    if loc.get("resolve_source") == "hko":
        result = _fetch_historical_actual_hko_range(start_date, end_date, cache)
    else:
        result = _fetch_historical_actual_noaa_range(loc["station"], unit, start_date, end_date, cache)
    _save_actuals_cache(cache)
    return result


def fetch_historical_actual_min(loc, start_date, end_date):
    """(فاز ۶) معادل fetch_historical_actual برای کمینه."""
    cache = _load_actuals_cache()
    unit = loc["unit"]
    if loc.get("resolve_source") == "hko":
        result = _fetch_historical_actual_min_hko_range(start_date, end_date, cache)
    else:
        result = _fetch_historical_actual_min_noaa_range(loc["station"], unit, start_date, end_date, cache)
    _save_actuals_cache(cache)
    return result


def backfill_calibration(locations, lookback_days=180, horizon_days=(0, 1, 2, 3)):
    cal = load_calibration()
    end = datetime.now(timezone.utc).date() - timedelta(days=6)
    start = end - timedelta(days=lookback_days)
    reference_date = end  # Phase 5: "today" for seasonal weighting

    for city_slug, loc in locations.items():
        forecasts = fetch_historical_forecast(
            loc["lat"], loc["lon"], loc["tz"], loc["unit"],
            start.isoformat(), end.isoformat()
        )
        actuals = fetch_historical_actual(loc, start.isoformat(), end.isoformat())

        if not forecasts or not actuals:
            print(f"  [CAL-SKIP] {loc['name']}: no historical data")
            continue

        abs_errors = []
        signed_errors = []
        weights = []
        for date_str, fval in forecasts.items():
            actual = actuals.get(date_str)
            if fval is not None and actual is not None:
                abs_errors.append(abs(fval - actual))
                signed_errors.append(actual - fval)
                d_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                weights.append(_seasonal_weight(d_obj, reference_date))

        if len(abs_errors) < 20:
            print(f"  [CAL-SKIP] {loc['name']}: only {len(abs_errors)} samples")
            continue

        base_mae = _weighted_mean(abs_errors, weights)
        base_bias_raw = _weighted_mean(signed_errors, weights)

        per_horizon_forecast = fetch_historical_forecast_by_horizon(
            loc["lat"], loc["lon"], loc["tz"], loc["unit"],
            start.isoformat(), end.isoformat(), horizon_days
        )

        horizon_summary = []
        for h in horizon_days:
            horizon_forecasts = per_horizon_forecast.get(h, {})
            abs_errors_h, signed_errors_h, weights_h = [], [], []
            for date_str, fval in horizon_forecasts.items():
                actual = actuals.get(date_str)
                if fval is not None and actual is not None:
                    abs_errors_h.append(abs(fval - actual))
                    signed_errors_h.append(actual - fval)
                    d_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                    weights_h.append(_seasonal_weight(d_obj, reference_date))

            if len(abs_errors_h) >= MIN_REAL_HORIZON_SAMPLES:
                mae_h = _weighted_mean(abs_errors_h, weights_h)
                bias_raw_h = _weighted_mean(signed_errors_h, weights_h)
                n_h = len(abs_errors_h)
                source_h = "historical_backfill_real_horizon"
            else:
                g = math.sqrt(1 + h * 0.6)
                mae_h = base_mae * g
                bias_raw_h = base_bias_raw
                n_h = len(abs_errors)
                source_h = "historical_backfill_synthetic_fallback"

            cap = _bias_cap(loc["unit"], city_slug, observed_raw_bias=bias_raw_h)
            bias_h = max(-cap, min(cap, bias_raw_h))
            key = f"{city_slug}_D{h}"
            cal[key] = {
                "sigma": round(mae_h, 3), "bias": round(bias_h, 3),
                "base_mae": round(mae_h, 3), "base_bias_raw": round(bias_raw_h, 3),
                "n": n_h, "source": source_h,
                "seasonal_half_life_days": SEASONAL_HALF_LIFE_DAYS,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            horizon_summary.append(f"D{h}:{source_h.split('_')[-1]}(n={n_h})")

        print(f"  [CAL] {loc['name']}: base_mae={base_mae:.2f} bias={base_bias_raw:+.2f} "
              f"n={len(abs_errors)} | " + " ".join(horizon_summary))
        time.sleep(0.5)

    save_calibration(cal)
    return cal


def backfill_calibration_min(locations, lookback_days=180):
    """(فاز ۶) معادل backfill_calibration برای کمینه -- ساده‌تر از نسخهٔ
    حداکثر (بدون per-horizon واقعی فاز ۶ قدیمی؛ همان فرمول synthetic
    sqrt(1+0.6h) برای D1-D3 کافی است چون این یک ویژگی کاملاً جدید است).
    کلیدهای کالیبراسیون: {city}_min_D{h}."""
    cal = load_calibration()
    end = datetime.now(timezone.utc).date() - timedelta(days=6)
    start = end - timedelta(days=lookback_days)
    reference_date = end

    for city_slug, loc in locations.items():
        forecasts = fetch_historical_forecast_min(
            loc["lat"], loc["lon"], loc["tz"], loc["unit"], start.isoformat(), end.isoformat()
        )
        actuals = fetch_historical_actual_min(loc, start.isoformat(), end.isoformat())
        if not forecasts or not actuals:
            print(f"  [CAL-MIN-SKIP] {loc['name']}: no historical data")
            continue

        abs_errors, signed_errors, weights = [], [], []
        for date_str, fval in forecasts.items():
            actual = actuals.get(date_str)
            if fval is not None and actual is not None:
                abs_errors.append(abs(fval - actual))
                signed_errors.append(actual - fval)
                d_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                weights.append(_seasonal_weight(d_obj, reference_date))

        if len(abs_errors) < 20:
            print(f"  [CAL-MIN-SKIP] {loc['name']}: only {len(abs_errors)} samples")
            continue

        base_mae = _weighted_mean(abs_errors, weights)
        base_bias_raw = _weighted_mean(signed_errors, weights)

        for h in (0, 1, 2, 3):
            g = math.sqrt(1 + h * 0.6)
            mae_h = base_mae * g
            cap = _bias_cap(loc["unit"], city_slug, observed_raw_bias=base_bias_raw)
            bias_h = max(-cap, min(cap, base_bias_raw))
            key = f"{city_slug}_min_D{h}"
            cal[key] = {
                "sigma": round(mae_h, 3), "bias": round(bias_h, 3),
                "base_mae": round(mae_h, 3), "base_bias_raw": round(base_bias_raw, 3),
                "n": len(abs_errors), "source": "historical_backfill_synthetic_fallback",
                "seasonal_half_life_days": SEASONAL_HALF_LIFE_DAYS,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        print(f"  [CAL-MIN] {loc['name']}: base_mae={base_mae:.2f} bias={base_bias_raw:+.2f} n={len(abs_errors)}")
        time.sleep(0.5)

    save_calibration(cal)
    return cal


_CALIBRATION_MEM_CACHE = None


def load_calibration():
    global _CALIBRATION_MEM_CACHE
    if _CALIBRATION_MEM_CACHE is not None:
        return _CALIBRATION_MEM_CACHE
    if CALIBRATION_FILE.exists():
        try:
            _CALIBRATION_MEM_CACHE = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
            return _CALIBRATION_MEM_CACHE
        except Exception:
            return {}
    return {}


def save_calibration(cal):
    global _CALIBRATION_MEM_CACHE
    _CALIBRATION_MEM_CACHE = cal
    CALIBRATION_FILE.write_text(json.dumps(cal, indent=2, ensure_ascii=False), encoding="utf-8")


def _compute_smart_live_calibration_entry(group, old, unit, city_slug):
    """گیت کالیبراسیون هوشمند ۱۴ روزه (Champion-Challenger + ترمز تغییر فصل)."""
    sorted_group = sorted(
        [m for m in group if m.get("forecast_mean") is not None and m.get("actual_temp") is not None],
        key=lambda m: str(m.get("date", "")),
    )
    if len(sorted_group) < 5:
        return None

    recent = sorted_group[-14:]
    abs_errors = [abs(m["forecast_mean"] - m["actual_temp"]) for m in recent]
    # اگر forecast_mean_shadow موجود باشد بایاس خام واقعی (actual - shadow) است، وگرنه باقیمانده
    signed_errors = [
        (m["actual_temp"] - m["forecast_mean_shadow"])
        if m.get("forecast_mean_shadow") is not None
        else (m["actual_temp"] - m["forecast_mean"])
        for m in recent
    ]

    live_mae = sum(abs_errors) / len(abs_errors)
    live_bias_raw = sum(signed_errors) / len(signed_errors)
    n_live = len(recent)
    cap = _bias_cap(unit, city_slug, observed_raw_bias=live_bias_raw)

    # بررسی چرخش فصل (۵ روز اخیر در برابر ۳۰ روز قبلی)
    last5 = sorted_group[-5:]
    prev30 = sorted_group[-35:-5]
    sign_flip = False
    if len(last5) >= 3 and len(prev30) >= 6:
        b5 = sum(m["actual_temp"] - m["forecast_mean"] for m in last5) / len(last5)
        b30 = sum(m["actual_temp"] - m["forecast_mean"] for m in prev30) / len(prev30)
        thresh = 0.55 if unit == "F" else 0.30
        if abs(b5) >= thresh and abs(b30) >= thresh and (b5 * b30 < 0):
            sign_flip = True

    # بررسی Champion-Challenger (مقایسه MAE کالیبره با MAE مدل Shadow)
    shadow_samples = [m for m in recent if m.get("forecast_mean_shadow") is not None]
    revert_to_shadow = False
    if len(shadow_samples) >= 5:
        cal_mae = sum(abs(m["forecast_mean"] - m["actual_temp"]) for m in shadow_samples) / len(shadow_samples)
        sh_mae = sum(abs(m["forecast_mean_shadow"] - m["actual_temp"]) for m in shadow_samples) / len(shadow_samples)
        if sh_mae < cal_mae * 0.96:
            revert_to_shadow = True

    default_sigma = DEFAULT_SIGMA_F if unit == "F" else DEFAULT_SIGMA_C
    default_bias = DEFAULT_BIAS_F if unit == "F" else DEFAULT_BIAS_C
    old_sigma = old.get("sigma", default_sigma)
    old_bias = old.get("bias", default_bias)

    w_live = min(0.75, n_live / (n_live + 10))
    new_sigma = round(max(0.55, old_sigma * (1 - w_live) + live_mae * w_live), 3)
    new_bias_raw = old_bias * (1 - w_live) + live_bias_raw * w_live
    if sign_flip or revert_to_shadow:
        new_bias_raw *= 0.25
    new_bias = round(max(-cap, min(cap, new_bias_raw)), 3)

    return {
        "sigma": new_sigma,
        "bias": new_bias,
        "n_live": len(sorted_group),
        "recent_14d_n": n_live,
        "live_mae": round(live_mae, 3),
        "live_bias_raw": round(live_bias_raw, 3),
        "gate_status": "revert_to_shadow" if revert_to_shadow else ("sign_flip_damped" if sign_flip else "use_calibrated"),
        "source": "smart_gated_14d",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def update_calibration_from_live(markets, locations):
    cal = load_calibration()
    resolved = [
        m for m in markets
        if m.get("status") in ("resolved", "resolved_no_signal")
        and m.get("actual_temp") is not None
    ]
    for city_slug in locations:
        unit = locations[city_slug]["unit"]
        city_all = [m for m in resolved if m["city"] == city_slug]
        for h in range(4):
            group = [m for m in city_all if m.get("horizon_days") == h]
            if len(group) < 5 and h in (0, 1) and len(city_all) >= 5:
                group = city_all
            key = f"{city_slug}_D{h}"
            old = cal.get(key, {})
            entry = _compute_smart_live_calibration_entry(group, old, unit, city_slug)
            if entry:
                cal[key] = entry
    save_calibration(cal)
    return cal


def update_calibration_from_live_min(markets, locations):
    """(فاز ۶) معادل دقیق update_calibration_from_live -- فقط برای
    مارکت‌های market_type=="min"، با کلید {city}_min_D{h}."""
    cal = load_calibration()
    resolved = [
        m for m in markets
        if m.get("status") in ("resolved", "resolved_no_signal")
        and m.get("actual_temp") is not None
        and m.get("market_type") == "min"
    ]
    for city_slug in locations:
        unit = locations[city_slug]["unit"]
        city_all = [m for m in resolved if m["city"] == city_slug]
        for h in range(4):
            group = [m for m in city_all if m.get("horizon_days") == h]
            if len(group) < 5 and h in (0, 1) and len(city_all) >= 5:
                group = city_all
            key = f"{city_slug}_min_D{h}"
            old = cal.get(key, {})
            entry = _compute_smart_live_calibration_entry(group, old, unit, city_slug)
            if entry:
                cal[key] = entry
    save_calibration(cal)
    return cal


def get_sigma(city_slug, horizon_days, unit):
    cal = load_calibration()
    h = min(horizon_days, 3)
    key = f"{city_slug}_D{h}"
    if key in cal:
        return cal[key]["sigma"]
    return DEFAULT_SIGMA_F if unit == "F" else DEFAULT_SIGMA_C


def get_gate_status(city_slug, horizon_days=0, is_min=False):
    cal = load_calibration()
    h = min(horizon_days, 3)
    key = f"{city_slug}_min_D{h}" if is_min else f"{city_slug}_D{h}"
    if key in cal:
        return cal[key].get("gate_status", "use_calibrated")
    return "use_calibrated"


def get_bias(city_slug, horizon_days, unit):
    cal = load_calibration()
    h = min(horizon_days, 3)
    key = f"{city_slug}_D{h}"
    if key in cal:
        if cal[key].get("gate_status") == "revert_to_shadow":
            return 0.0
        raw = cal[key].get("live_bias_raw", cal[key].get("base_bias_raw"))
        cap = _bias_cap(unit, city_slug, observed_raw_bias=raw)
        return max(-cap, min(cap, cal[key].get("bias", 0.0)))
    return DEFAULT_BIAS_F if unit == "F" else DEFAULT_BIAS_C


def get_sigma_min(city_slug, horizon_days, unit):
    """(فاز ۶) معادل get_sigma برای کمینه."""
    cal = load_calibration()
    h = min(horizon_days, 3)
    key = f"{city_slug}_min_D{h}"
    if key in cal:
        return cal[key]["sigma"]
    return DEFAULT_SIGMA_F if unit == "F" else DEFAULT_SIGMA_C


def get_bias_min(city_slug, horizon_days, unit):
    """(فاز ۶) معادل get_bias برای کمینه."""
    cal = load_calibration()
    h = min(horizon_days, 3)
    key = f"{city_slug}_min_D{h}"
    if key in cal:
        if cal[key].get("gate_status") == "revert_to_shadow":
            return 0.0
        raw = cal[key].get("live_bias_raw", cal[key].get("base_bias_raw"))
        cap = _bias_cap(unit, city_slug, observed_raw_bias=raw)
        return max(-cap, min(cap, cal[key].get("bias", 0.0)))
    return DEFAULT_BIAS_F if unit == "F" else DEFAULT_BIAS_C


# =============================================================================
# PROBABILITY DISTRIBUTION FROM ENSEMBLE + CALIBRATED SIGMA/BIAS
# =============================================================================

_INTRADAY_METAR_CACHE = {}
_INTRADAY_METAR_LOCK = threading.Lock()


def get_intraday_observed_bound(loc, date_str, is_min=False):
    """فیلتر دمای لحظه‌ای روز بازار (D0 Intraday METAR Floor/Ceiling):
    برای بازارهای روز جاری (D0)، دمای ثبت‌شده تا این لحظه در ایستگاه فرودگاهی
    (METAR) را با تایم‌اوت کوتاه می‌گیرد و هر دو کران (بیشینه و کمینه) را با هم در
    کش مشترک ذخیره می‌کند تا بازارهای Max و Min فقط یک بار ایستگاه را بخوانند."""
    station = (loc or {}).get("station")
    unit = (loc or {}).get("unit", "C")
    if not station or not date_str:
        return None

    cache_key = (station, date_str)
    now_ts = time.time()
    with _INTRADAY_METAR_LOCK:
        cached = _INTRADAY_METAR_CACHE.get(cache_key)
    if cached and (now_ts - cached[0] < 900):
        max_c, min_c = cached[1], cached[2]
        raw_c = min_c if is_min else max_c
        if raw_c is None:
            return None
        return round(raw_c * 9.0 / 5.0 + 32.0, 1) if unit == "F" else round(raw_c, 1)

    url = (
        f"https://aviationweather.gov/api/data/metar"
        f"?ids={station}&format=json&date={date_str.replace('-', '')}"
    )
    max_c, min_c = None, None
    try:
        r = requests.get(url, timeout=(2, 3))
        data = r.json()
        if isinstance(data, list) and data:
            temps = [float(d.get("temp")) for d in data if d.get("temp") is not None]
            if temps:
                max_c = max(temps)
                min_c = min(temps)
    except Exception:
        max_c, min_c = None, None

    with _INTRADAY_METAR_LOCK:
        _INTRADAY_METAR_CACHE[cache_key] = (now_ts, max_c, min_c)

    raw_c = min_c if is_min else max_c
    if raw_c is None:
        return None
    return round(raw_c * 9.0 / 5.0 + 32.0, 1) if unit == "F" else round(raw_c, 1)


def build_calibrated_distribution(members, city_slug, horizon_days, unit, model_members=None):
    calibrated_sigma = get_sigma(city_slug, horizon_days, unit)
    bias = get_bias(city_slug, horizon_days, unit)

    if not members:
        return None, calibrated_sigma

    raw_mean = sum(members) / len(members)

    # اگر زیرمدل‌ها (ECMWF, GFS, GEM, ICON) موجود باشند، میانگین BMA وزن‌دار را با میانگین انسمبل ترکیب می‌کنیم
    model_spread = 0.0
    if model_members and isinstance(model_members, dict):
        raw_means = {k: (sum(v) / len(v)) for k, v in model_members.items() if v}
        if raw_means:
            weights = get_model_weights(city_slug, list(raw_means.keys()), is_min=False)
            if weights:
                bma_raw = sum(weights[m] * raw_means[m] for m in raw_means)
                raw_mean = 0.45 * raw_mean + 0.55 * bma_raw
            model_spread = compute_model_spread(raw_means, weights)

    corrected_mean = raw_mean + bias

    if len(members) > 1:
        raw_sigma = (sum((x - raw_mean) ** 2 for x in members) / (len(members) - 1)) ** 0.5
    else:
        raw_sigma = calibrated_sigma

    base_sigma = max(raw_sigma, calibrated_sigma)

    # کالیبراسیون پویای Sigma بر اساس واگرایی زیرمدل‌ها (Dynamic Spread-Skill Sigma)
    if model_spread > 0:
        tight_thresh = 1.0 if unit == "F" else 0.55
        wide_thresh = 2.0 if unit == "F" else 1.10
        if model_spread <= tight_thresh:
            final_sigma = max(0.55 if unit == "C" else 0.95, base_sigma * 0.93)
        elif model_spread >= wide_thresh:
            final_sigma = math.sqrt(base_sigma ** 2 + (0.55 * model_spread) ** 2)
        else:
            final_sigma = base_sigma
    else:
        final_sigma = base_sigma

    return round(corrected_mean, 2), round(final_sigma, 3)


def build_calibrated_distribution_min(members, city_slug, horizon_days, unit, model_members=None):
    """(فاز ۶) معادل build_calibrated_distribution برای کمینه همراه با BMA و Dynamic Sigma."""
    calibrated_sigma = get_sigma_min(city_slug, horizon_days, unit)
    bias = get_bias_min(city_slug, horizon_days, unit)
    if not members:
        return None, calibrated_sigma
    raw_mean = sum(members) / len(members)

    model_spread = 0.0
    if model_members and isinstance(model_members, dict):
        raw_means = {k: (sum(v) / len(v)) for k, v in model_members.items() if v}
        if raw_means:
            weights = get_model_weights(city_slug, list(raw_means.keys()), is_min=True)
            if weights:
                bma_raw = sum(weights[m] * raw_means[m] for m in raw_means)
                raw_mean = 0.45 * raw_mean + 0.55 * bma_raw
            model_spread = compute_model_spread(raw_means, weights)

    if len(members) > 1:
        raw_sigma = (sum((x - raw_mean) ** 2 for x in members) / (len(members) - 1)) ** 0.5
    else:
        raw_sigma = calibrated_sigma
    base_sigma = max(raw_sigma, calibrated_sigma)

    if model_spread > 0:
        tight_thresh = 1.0 if unit == "F" else 0.55
        wide_thresh = 2.0 if unit == "F" else 1.10
        if model_spread <= tight_thresh:
            final_sigma = max(0.55 if unit == "C" else 0.95, base_sigma * 0.93)
        elif model_spread >= wide_thresh:
            final_sigma = math.sqrt(base_sigma ** 2 + (0.55 * model_spread) ** 2)
        else:
            final_sigma = base_sigma
    else:
        final_sigma = base_sigma

    corrected_mean = raw_mean + bias
    return round(corrected_mean, 2), round(final_sigma, 3)


def bucket_probability(mean, sigma, t_low, t_high):
    if mean is None:
        return 0.0
    s = max(sigma, 0.3)
    if t_low <= -998:
        return student_t_cdf((t_high + 0.5 - mean) / s)
    if t_high >= 998:
        return 1.0 - student_t_cdf((t_low - 0.5 - mean) / s)
    lo = t_low - 0.5 if t_low == t_high else t_low
    hi = t_high + 0.5 if t_low == t_high else t_high
    return student_t_cdf((hi - mean) / s) - student_t_cdf((lo - mean) / s)


def full_bucket_distribution(
    mean,
    sigma,
    outcomes,
    model_means_raw=None,
    city_slug=None,
    horizon_days=0,
    unit="C",
    is_min=False,
    observed_bound=None,
):
    """محاسبهٔ توزیع احتمال باکت‌ها با دو ارتقای کلیدی برای نوسان‌گیری:
    ۱) توزیع مخلوط چندمدلی (Multi-Model Mixture / GMM) با وزن‌های BMA:
       وقتی زیرمدل‌ها (ECMWF, GFS, GEM, ICON) اختلاف نظر دارند، به جای ایجاد یک قلهٔ کاذب
       در وسط، ۴۵٪ وزن احتمال مستقیماً از ترکیب منحنی‌های مستقل هر زیرمدل ساخته می‌شود.
    ۲) فیلتر دمای لحظه‌ای روز بازار (Intraday METAR Floor/Ceiling برای افق D0):
       در بازارهای امروز، باکت‌هایی که دمای ثبت‌شدهٔ فعلی ایستگاه قبلاً از آن‌ها عبور کرده
       prob=0.0 و برچسب intraday_eliminated=True می‌گیرند و احتمال بین باکت‌های زنده بازتوزیع می‌شود."""
    # آماده‌سازی اجزای مخلوط چندمدلی (GMM) در صورت وجود زیرمدل‌ها
    gmm_components = []
    if model_means_raw and isinstance(model_means_raw, dict) and len(model_means_raw) >= 2:
        valid_models = {k: float(v) for k, v in model_means_raw.items() if v is not None}
        if len(valid_models) >= 2:
            weights = get_model_weights(city_slug, list(valid_models.keys()), is_min=is_min) if city_slug else None
            if not weights:
                weights = {k: 1.0 / len(valid_models) for k in valid_models}
            bias = (
                (get_bias_min(city_slug, horizon_days, unit) if is_min else get_bias(city_slug, horizon_days, unit))
                if city_slug else 0.0
            )
            comp_sigma = max(0.45 if unit == "C" else 0.85, sigma * 0.82)
            for m_name, raw_v in valid_models.items():
                gmm_components.append((raw_v + bias, comp_sigma, float(weights.get(m_name, 0.25))))

    raw = []
    eliminated_flags = []
    for o in outcomes:
        t_low, t_high = o["range"]

        # بررسی حذف باکت با دمای ثبت‌شدهٔ لحظه‌ای در روز بازار (D0)
        eliminated = False
        if observed_bound is not None:
            # در Polymarket دما به عدد صحیح گرد می‌شود؛ حاشیهٔ امن ۰.۳ درجه برای گزارش‌های ساعتی METAR
            if not is_min and t_high < 998:
                if float(t_high) < math.floor(float(observed_bound) - 0.3 + 0.5):
                    eliminated = True
            elif is_min and t_low > -998:
                if float(t_low) > math.ceil(float(observed_bound) + 0.3 - 0.5):
                    eliminated = True

        if eliminated:
            raw.append(0.0)
            eliminated_flags.append(True)
            continue

        eliminated_flags.append(False)
        p_consensus = bucket_probability(mean, sigma, t_low, t_high)
        if gmm_components:
            p_gmm = sum(w * bucket_probability(m_mu, m_sig, t_low, t_high) for (m_mu, m_sig, w) in gmm_components)
            p_blended = 0.55 * p_consensus + 0.45 * p_gmm
        else:
            p_blended = p_consensus
        raw.append(p_blended)

    # اگر همهٔ باکت‌ها به هر دلیلی صفر شدند، بدون حذف محاسبه کن تا تقسیم بر صفر نشود
    total = sum(raw)
    if total <= 1e-9:
        raw = [bucket_probability(mean, sigma, o["range"][0], o["range"][1]) for o in outcomes]
        eliminated_flags = [False for _ in outcomes]
        total = sum(raw) or 1.0

    dist = []
    for o, p, elim in zip(outcomes, raw, eliminated_flags):
        item = {
            **o,
            "model_prob": round(p / total, 4),
            "intraday_eliminated": bool(elim),
        }
        dist.append(item)
    return dist


def distribution_sum(dist):
    return round(sum(d.get("model_prob", 0.0) for d in dist), 4)


def scenario_grid(mean, sigma, n_sigma=4.0, step=0.5):
    if mean is None:
        return []
    s = max(sigma, 0.3)
    lo = mean - n_sigma * s
    hi = mean + n_sigma * s
    n_steps = max(1, int(round((hi - lo) / step)))
    points = [lo + i * step for i in range(n_steps + 1)]
    raw = []
    for t in points:
        p = student_t_cdf((t + step / 2 - mean) / s) - student_t_cdf((t - step / 2 - mean) / s)
        raw.append((round(t, 2), p))
    total = sum(p for _, p in raw) or 1.0
    return [(t, p / total) for t, p in raw]


# =============================================================================
# فاز ۶: مسیر ensemble کاملاً مستقل برای دمای کمینه
# =============================================================================

def get_ecmwf_ensemble_min(lat, lon, tz, unit):
    return _fetch_ensemble(lat, lon, tz, unit, "ecmwf_ifs025", forecast_days=7, variable="temperature_2m_min")


def get_gefs_ensemble_min(lat, lon, tz, unit):
    return _fetch_ensemble(lat, lon, tz, unit, "gfs_seamless", forecast_days=7, variable="temperature_2m_min")


def build_combined_distribution_min(city_slug, loc, date_str):
    """معادل کامل build_combined_distribution ولی برای دمای کمینه --
    کاملاً مستقل، هیچ داده‌ای با مسیر حداکثر به اشتراک نمی‌گذارد."""
    members = []
    ecmwf = get_ecmwf_ensemble_min(loc["lat"], loc["lon"], loc["tz"], loc["unit"])
    gefs = get_gefs_ensemble_min(loc["lat"], loc["lon"], loc["tz"], loc["unit"])
    members.extend(ecmwf.get(date_str, []))
    members.extend(gefs.get(date_str, []))
    return members


def fetch_historical_forecast_min(lat, lon, tz, unit, start_date, end_date):
    """معادل fetch_historical_forecast برای کمینه (برای backfill)."""
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    url = (
        f"https://historical-forecast-api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_min&temperature_unit={temp_unit}"
        f"&timezone={tz}&start_date={start_date}&end_date={end_date}"
        f"&models=ecmwf_ifs025"
    )
    for attempt in range(MAX_RETRIES):
        try:
            data = requests.get(url, timeout=(5, 15)).json()
            daily = data.get("daily", {})
            return dict(zip(daily.get("time", []), daily.get("temperature_2m_min", [])))
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)
            else:
                return {}
    return {}
