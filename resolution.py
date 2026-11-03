"""
resolution.py -- Resolution (ground truth) fetcher for WeatherBet v3
=====================================================================================
FIX LOG (this version -- resolution with network retry)

*** NEW FIX: TRANSIENT NETWORK FAILURES BLOCKING RESOLUTION ***
get_polymarket_settlement() used to make exactly ONE attempt to reach
gamma-api.polymarket.com. Any transient DNS hiccup or brief connection drop
(even with otherwise-healthy internet) caused an immediate "fetch error" and
a `None` return, which the caller (resolve_expired_markets in
weatherbot_v3.py) treats identically to "not settled yet" -- so the market
silently stayed unresolved until the NEXT full scan cycle happened to catch
it on a lucky attempt. This is what caused Jul 8-9 markets to resolve fine
while Jul 10 got stuck, even on a run where the internet connection was
confirmed healthy overall.

THE FIX: both get_polymarket_settlement() and is_event_fully_closed() now
retry up to MAX_RETRIES times with a short delay between attempts, using the
exact same retry pattern already used by forecasting.py's _fetch_ensemble()
(3 attempts, 3s delay) -- kept consistent rather than inventing a second
retry style. Only after all retries are exhausted does the function give up
and return None (still correctly distinguished from "event found but
nothing settled yet", which returns an empty dict).

--- PATCH (فاز ۶ نقشه‌راه -- بازار دمای کمینه) -------------------------------
افزودن ۳ تابع موازی برای دمای کمینه:
  - get_polymarket_settlement_min(): معادل کامل get_polymarket_settlement
    ولی با اسلاگ "lowest-temperature-in-...".
  - get_actual_noaa_min() / get_actual_hko_min(): معادل نسخهٔ حداکثر ولی
    min(temps) به‌جای max(temps)، و dataType=CLMMINT به‌جای CLMMAXT برای
    HKO (تأییدشده از مستندات رسمی HKO Open Data API).
  - get_display_temp_min(): دیسپچر معادل get_display_temp برای کمینه.
توجه: get_actual_temp_from_settlement() هیچ تغییری نکرده -- کاملاً عمومی
است و بدون فرض حداکثر/کمینه، مستقیماً روی full_distribution/settlement کار
می‌کند، پس برای هر دو نوع بازار به‌طور یکسان REUSE می‌شود.
=====================================================================================
"""

import json
import math
import re
import time
try:
    import requests
except ImportError:
    import urllib.request

    class _UrllibResponse:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text

        def json(self):
            return json.loads(self.text)

    class _UrllibRequestsShim:
        @staticmethod
        def get(url, timeout=10, headers=None):
            t = timeout[1] if isinstance(timeout, (tuple, list)) and len(timeout) > 1 else (timeout[0] if isinstance(timeout, (tuple, list)) else timeout)
            hdrs = {"User-Agent": "WeatherBot/3.0"}
            if headers:
                hdrs.update(headers)
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=t) as resp:
                return _UrllibResponse(resp.status, resp.read().decode("utf-8", errors="replace"))

    requests = _UrllibRequestsShim()
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

MAX_RETRIES = 3
RETRY_DELAY_S = 3

# =============================================================================
# AUTHORITATIVE WIN/LOSS SOURCE -- Polymarket's own settled outcomePrices
# =============================================================================

def get_polymarket_settlement(city_slug, month, day, year):
    """
    Fetches the event fresh from Polymarket and returns a dict:
    { market_id (str): won_yes (bool) }
    for every sub-market (bucket) that Polymarket has ALREADY settled.

    Retries up to MAX_RETRIES times on any request/connection error (DNS
    hiccup, transient timeout, etc.) before giving up -- a single transient
    network error must never be mistaken for "not settled yet".

    Returns None only if EVERY attempt failed to reach the API at all, so
    the caller can distinguish "network failure" from "event found but
    nothing settled yet" (which returns an empty dict, not None).
    """
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    data = None
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=(5, 8))
            data = r.json()
            last_err = None
            break
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)

    if last_err is not None:
        print(f"  [RESOLVE-PM] {city_slug} {year}-{month}-{day}: fetch error after {MAX_RETRIES} attempts: {last_err}")
        return None

    if not data or not isinstance(data, list) or len(data) == 0:
        return None

    event = data[0]
    settled = {}
    TOL = 0.02  # float tolerance around 0.0 / 1.0

    for market in event.get("markets", []):
        mid = str(market.get("id", ""))
        try:
            prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
            yes_price = float(prices[0])
        except Exception:
            continue

        if yes_price >= (1.0 - TOL):
            settled[mid] = True   # YES won
        elif yes_price <= TOL:
            settled[mid] = False  # NO won
        # else: still unsettled (e.g. 0.5/0.5 or in-between) -- omit entirely

    return settled


def get_polymarket_settlement_min(city_slug, month, day, year):
    """(فاز ۶) معادل دقیق get_polymarket_settlement، فقط با اسلاگ lowest-
    به‌جای highest- (همان الگوی retry/۳-تلاش را عیناً حفظ می‌کند)."""
    slug = f"lowest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    data = None
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=(5, 8))
            data = r.json()
            last_err = None
            break
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)

    if last_err is not None:
        print(f"  [RESOLVE-PM-MIN] {city_slug} {year}-{month}-{day}: fetch error after {MAX_RETRIES} attempts: {last_err}")
        return None

    if not data or not isinstance(data, list) or len(data) == 0:
        return None

    event = data[0]
    settled = {}
    TOL = 0.02
    for market in event.get("markets", []):
        mid = str(market.get("id", ""))
        try:
            prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
            yes_price = float(prices[0])
        except Exception:
            continue
        if yes_price >= (1.0 - TOL):
            settled[mid] = True
        elif yes_price <= TOL:
            settled[mid] = False
    return settled


def is_event_fully_closed(city_slug, month, day, year):
    """
    Best-effort check for whether Polymarket itself considers the event
    closed. Same retry pattern as get_polymarket_settlement() -- a transient
    network error should not be mistaken for "event not closed yet".
    """
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=(5, 8))
            data = r.json()
            if data and isinstance(data, list) and len(data) > 0:
                return bool(data[0].get("closed", False))
            return False
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)
    return False

# =============================================================================
# DISPLAY TEMPERATURE DERIVED FROM POLYMARKET'S OWN SETTLEMENT
# =============================================================================

def get_actual_temp_from_settlement(mkt, settlement):
    """
    Derives the display temperature directly from Polymarket's OWN settled
    outcomePrices -- finds the bucket whose YES side actually won and
    returns its midpoint (or lower/upper bound for open-ended buckets).

    REUSE کامل برای فاز ۶: این تابع هیچ فرض حداکثر/کمینه‌ای ندارد -- فقط
    روی full_distribution بازار (mkt) و دیکشنری settlement کار می‌کند، پس
    برای بازار کمینه هم بدون هیچ تغییری صحیح است.
    """
    full_dist = mkt.get("full_distribution", [])
    for b in full_dist:
        mid = str(b.get("market_id", ""))
        if mid in settlement and settlement[mid] is True:
            rng = b.get("range")
            if not rng:
                continue
            low, high = rng
            if low is None or high is None:
                continue
            if low <= -998:
                return round(high, 2)
            if high >= 998:
                return round(low, 2)
            return round((low + high) / 2, 2)
    return None

# =============================================================================
# COSMETIC FALLBACK-ONLY DISPLAY TEMPERATURE (used only if settlement-derived
# value above is unavailable -- never used to decide win/loss or PnL)
# =============================================================================

def get_actual_wunderground_proxy(station, unit, date_str, vc_key):
    """Visual Crossing timeline API -- FALLBACK DISPLAY ONLY, not authoritative."""
    if not vc_key:
        return None
    vc_unit = "us" if unit == "F" else "metric"
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
        f"/{station}/{date_str}/{date_str}"
        f"?unitGroup={vc_unit}&key={vc_key}&include=days&elements=tempmax"
    )
    for attempt in range(MAX_RETRIES):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            days = data.get("days", [])
            if days and days[0].get("tempmax") is not None:
                return round(float(days[0]["tempmax"]), 1)
            return None
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)
            else:
                print(f"  [DISPLAY-WU] {station} {date_str}: {e}")
    return None

_T_REMARK_RE = re.compile(r"\bT([01])(\d{3})(?:[01]\d{3}|\d{4})\b")


def _parse_routine_metar_temp_c(row):
    """Extracts routine METAR temperature in Celsius per Polymarket official METAR rules:
    - Excludes SPECI (special observations).
    - Uses 8-digit T-group remark (tenths of C) if present in rawOb, otherwise row['temp']."""
    raw = str(row.get("rawOb") or "").strip()
    if raw.startswith("SPECI ") or str(row.get("metarType") or "").upper() == "SPECI":
        return None
    if raw:
        m = _T_REMARK_RE.search(raw)
        if m:
            sign = -1.0 if m.group(1) == "1" else 1.0
            return sign * (int(m.group(2)) / 10.0)
    if row.get("temp") is not None:
        try:
            return float(row["temp"])
        except Exception:
            return None
    return None


def _pm_round_whole_degree(val):
    """Polymarket official rounding rule: round to nearest whole degree, with exact .5 rounded UP
    to the next higher whole degree (e.g. 16.5 -> 17, -8.5 -> -8)."""
    return math.floor(float(val) + 0.5)


def _fetch_routine_metar_temps_for_local_date(station, date_str, tz_name=None):
    """Fetches all routine METAR temperatures (in Celsius) for `station` on `date_str`
    in the station's local timezone (`tz_name`), using `&hours=28` on aviationweather.gov."""
    tz = None
    if tz_name and ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None

    now_utc = datetime.now(timezone.utc)
    try:
        y, m, d = map(int, date_str.split("-"))
        if tz is not None:
            local_end = datetime(y, m, d, 23, 59, 59, tzinfo=tz) + timedelta(hours=2)
            end_utc = min(now_utc, local_end.astimezone(timezone.utc))
        else:
            local_end = datetime(y, m, d, 23, 59, 59, tzinfo=timezone.utc) + timedelta(hours=6)
            end_utc = min(now_utc, local_end)
        date_param = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (
            f"https://aviationweather.gov/api/data/metar"
            f"?ids={station}&format=json&date={date_param}&hours=28"
        )
    except Exception:
        url = (
            f"https://aviationweather.gov/api/data/metar"
            f"?ids={station}&format=json&hours=28"
        )

    data = requests.get(url, timeout=(5, 10)).json()
    if not data or not isinstance(data, list):
        return []

    temps = []
    for row in data:
        if tz is not None and row.get("obsTime") is not None:
            try:
                local_dt = datetime.fromtimestamp(int(row["obsTime"]), tz=timezone.utc).astimezone(tz)
                if local_dt.strftime("%Y-%m-%d") != date_str:
                    continue
            except Exception:
                pass
        tc = _parse_routine_metar_temp_c(row)
        if tc is not None:
            temps.append(tc)
    return temps


def get_actual_noaa(station, unit, date_str, tz_name=None):
    """NOAA Aviation Weather METAR archive -- FALLBACK DISPLAY ONLY, not authoritative."""
    try:
        temps = _fetch_routine_metar_temps_for_local_date(station, date_str, tz_name=tz_name)
        if not temps:
            return None
        max_c = max(temps)
        val = (max_c * 9.0 / 5.0 + 32.0) if unit == "F" else max_c
        return round(val, 1)
    except Exception as e:
        print(f"  [DISPLAY-NOAA] {station} {date_str}: {e}")
        return None


def get_actual_noaa_min(station, unit, date_str, tz_name=None):
    """(فاز ۶) معادل get_actual_noaa (فقط نمایشی/fallback، نه مرجع تعیین
    برد/باخت) -- min(temps) به‌جای max(temps)."""
    try:
        temps = _fetch_routine_metar_temps_for_local_date(station, date_str, tz_name=tz_name)
        if not temps:
            return None
        min_c = min(temps)
        val = (min_c * 9.0 / 5.0 + 32.0) if unit == "F" else min_c
        return round(val, 1)
    except Exception as e:
        print(f"  [DISPLAY-NOAA-MIN] {station} {date_str}: {e}")
        return None


def get_actual_hko(date_str):
    """Hong Kong Observatory open data API -- FALLBACK DISPLAY ONLY, not authoritative."""
    try:
        url = (
            "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php"
            "?dataType=CLMMAXT&lang=en&rformat=json&station=HKO"
        )
        data = requests.get(url, timeout=(5, 10)).json()
        for row in data.get("data", []):
            if row.get("date") == date_str.replace("-", ""):
                return round(float(row["value"]), 1)
    except Exception as e:
        print(f"  [DISPLAY-HKO] {date_str}: {e}")
    return None


def get_actual_hko_min(date_str):
    """(فاز ۶) معادل get_actual_hko با dataType=CLMMINT به‌جای CLMMAXT
    (تأییدشده از مستندات رسمی HKO Open Data API)."""
    try:
        url = (
            "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php"
            "?dataType=CLMMINT&lang=en&rformat=json&station=HKO"
        )
        data = requests.get(url, timeout=(5, 10)).json()
        for row in data.get("data", []):
            if row.get("date") == date_str.replace("-", ""):
                return round(float(row["value"]), 1)
    except Exception as e:
        print(f"  [DISPLAY-HKO-MIN] {date_str}: {e}")
    return None


def get_display_temp(loc, date_str, vc_key=""):
    """Dispatches to the correct FALLBACK-ONLY source per city config."""
    source = loc.get("resolve_source", "wunderground")
    if source == "noaa":
        return get_actual_noaa(loc["station"], loc["unit"], date_str, tz_name=loc.get("tz"))
    if source == "hko":
        return get_actual_hko(date_str)
    return get_actual_wunderground_proxy(loc["station"], loc["unit"], date_str, vc_key)


def get_display_temp_min(loc, date_str, vc_key=""):
    """(فاز ۶) معادل get_display_temp برای کمینه."""
    source = loc.get("resolve_source", "wunderground")
    if source == "noaa":
        return get_actual_noaa_min(loc["station"], loc["unit"], date_str, tz_name=loc.get("tz"))
    if source == "hko":
        return get_actual_hko_min(date_str)
    return get_actual_wunderground_proxy(loc["station"], loc["unit"], date_str, vc_key)


# Backward-compat alias
get_actual_temp = get_display_temp

