"""
locations.py -- 26 city configuration for WeatherBet v3
Coordinates point to the EXACT station Polymarket uses for resolution
(not city center), matching the official resolve source per city.

--- PHASE 1 FIX LOG (WEATHERBOT_ROADMAP.md, یافته F1) -----------------------
Paris was pointing at Paris-Charles de Gaulle Airport (LFPG, 49.0097, 2.5479).
Direct verification against multiple live Polymarket "Highest temperature in
Paris" market pages (resolution source field) confirms the actual resolve
station is Paris-Le Bourget Airport (LFPB), ~9km away, with its own Wunderground
history page: wunderground.com/history/daily/fr/bonneuil-en-france/LFPB.
This mismatch matched a persistent cold-bias pattern (~0.5-3.4C) seen in
the bot's own dashboard history for Paris. Fixed below -- ONLY the Paris
entry changed (lat/lon/station); every other field/key/city is untouched
per the zero-disruption principle in WEATHERBOT_ROADMAP.md section 0.5.

Full 24-city audit performed against live Polymarket resolution-source text
for this fix (2026-08-26): all other 23 cities' station/coordinates were
verified to match the CURRENT live resolution source exactly. No other
mismatch was found. (Hong Kong intentionally uses the Hong Kong Observatory
coordinates, not the airport, matching its "hko" resolve_source dispatch in
resolution.py -- this was double-checked and is correct, not a bug.)

--- فاز ۵ نقشه‌راه (۱۶ سپتامبر ۲۰۲۶) -----------------------------------------
افزوده شدن دو شهر جدید بعد از تحقیق مستقل روی صفحهٔ زندهٔ Polymarket:
  - cape-town: تأیید شده با ۴ نمونهٔ بازار مستقل (۲۵ آگوست، ۳۱ آگوست،
    ۱۱ سپتامبر، ۱۴ سپتامبر ۲۰۲۶) -- ایستگاه FACT (Cape Town Intl)، منبع
    NOAA/Wunderground استاندارد.
  - denver: تأیید شده با ۳ نمونهٔ بازار مستقل (۲۹ آگوست، ۳۱ آگوست،
    ۱۷ سپتامبر ۲۰۲۶) + یک منبع مستقل ثالث -- ایستگاه واقعی Buckley Space
    Force Base (KBKF)، **نه** فرودگاه بین‌المللی دنور (KDEN).
شش شهر پیشنهادی اولیه (Phoenix, Las Vegas, Riyadh, Cairo, Athens, Perth) و
San Francisco (به‌خاطر مشکل شدید marine layer، مثل Los Angeles) عمداً
اضافه نشدند -- جزئیات کامل در 03_DECISIONS.md / نقشه‌راه پروژه.
------------------------------------------------------------------------------
"""

LOCATIONS = {
    "nyc": {"lat": 40.7772, "lon": -73.8726, "name": "New York", "station": "KLGA", "unit": "F", "region": "us", "resolve_source": "wunderground", "tz": "America/New_York"},
    "miami": {"lat": 25.7959, "lon": -80.2870, "name": "Miami", "station": "KMIA", "unit": "F", "region": "us", "resolve_source": "wunderground", "tz": "America/New_York"},
    "chicago": {"lat": 41.9742, "lon": -87.9073, "name": "Chicago", "station": "KORD", "unit": "F", "region": "us", "resolve_source": "wunderground", "tz": "America/Chicago"},
    "dallas": {"lat": 32.8471, "lon": -96.8518, "name": "Dallas", "station": "KDAL", "unit": "F", "region": "us", "resolve_source": "wunderground", "tz": "America/Chicago"},
    "houston": {"lat": 29.6454, "lon": -95.2789, "name": "Houston", "station": "KHOU", "unit": "F", "region": "us", "resolve_source": "wunderground", "tz": "America/Chicago"},
    "atlanta": {"lat": 33.6407, "lon": -84.4277, "name": "Atlanta", "station": "KATL", "unit": "F", "region": "us", "resolve_source": "wunderground", "tz": "America/New_York"},
    "los-angeles": {"lat": 33.9416, "lon": -118.4085, "name": "Los Angeles", "station": "KLAX", "unit": "F", "region": "us", "resolve_source": "wunderground", "tz": "America/Los_Angeles"},
    "austin": {"lat": 30.1975, "lon": -97.6664, "name": "Austin", "station": "KAUS", "unit": "F", "region": "us", "resolve_source": "wunderground", "tz": "America/Chicago"},

    "madrid": {"lat": 40.4983, "lon": -3.5676, "name": "Madrid", "station": "LEMD", "unit": "C", "region": "eu", "resolve_source": "wunderground", "tz": "Europe/Madrid"},
    # --- FIXED (Phase 1 / F1): was {"lat": 49.0097, "lon": 2.5479, "station": "LFPG"} (Charles de Gaulle).
    # Live Polymarket resolution source confirms Paris-Le Bourget (LFPB) is the actual resolve station.
    "paris": {"lat": 48.9694, "lon": 2.4414, "name": "Paris", "station": "LFPB", "unit": "C", "region": "eu", "resolve_source": "wunderground", "tz": "Europe/Paris"},
    "munich": {"lat": 48.3537, "lon": 11.7750, "name": "Munich", "station": "EDDM", "unit": "C", "region": "eu", "resolve_source": "wunderground", "tz": "Europe/Berlin"},
    "milan": {"lat": 45.6306, "lon": 8.7281, "name": "Milan", "station": "LIMC", "unit": "C", "region": "eu", "resolve_source": "wunderground", "tz": "Europe/Rome"},
    "istanbul": {"lat": 41.2753, "lon": 28.7519, "name": "Istanbul", "station": "LTFM", "unit": "C", "region": "eu", "resolve_source": "noaa", "tz": "Europe/Istanbul"},
    "jeddah": {"lat": 21.6796, "lon": 39.1565, "name": "Jeddah", "station": "OEJN", "unit": "C", "region": "asia", "resolve_source": "wunderground", "tz": "Asia/Riyadh"},
    "tel-aviv": {"lat": 32.0114, "lon": 34.8867, "name": "Tel Aviv", "station": "LLBG", "unit": "C", "region": "asia", "resolve_source": "noaa", "tz": "Asia/Jerusalem"},
    "ankara": {"lat": 40.1281, "lon": 32.9951, "name": "Ankara", "station": "LTAC", "unit": "C", "region": "eu", "resolve_source": "wunderground", "tz": "Europe/Istanbul"},
    "tokyo": {"lat": 35.5494, "lon": 139.7798, "name": "Tokyo", "station": "RJTT", "unit": "C", "region": "asia", "resolve_source": "wunderground", "tz": "Asia/Tokyo"},
    "seoul": {"lat": 37.4691, "lon": 126.4505, "name": "Seoul", "station": "RKSI", "unit": "C", "region": "asia", "resolve_source": "wunderground", "tz": "Asia/Seoul"},
    "singapore": {"lat": 1.3502, "lon": 103.9940, "name": "Singapore", "station": "WSSS", "unit": "C", "region": "asia", "resolve_source": "wunderground", "tz": "Asia/Singapore"},
    "hong-kong": {"lat": 22.3022, "lon": 114.1746, "name": "Hong Kong", "station": "HKO", "unit": "C", "region": "asia", "resolve_source": "hko", "tz": "Asia/Hong_Kong"},
    "kuala-lumpur": {"lat": 2.7456, "lon": 101.7099, "name": "Kuala Lumpur", "station": "WMKK", "unit": "C", "region": "asia", "resolve_source": "wunderground", "tz": "Asia/Kuala_Lumpur"},
    "shanghai": {"lat": 31.1443, "lon": 121.8083, "name": "Shanghai", "station": "ZSPD", "unit": "C", "region": "asia", "resolve_source": "wunderground", "tz": "Asia/Shanghai"},
    "toronto": {"lat": 43.6772, "lon": -79.6306, "name": "Toronto", "station": "CYYZ", "unit": "C", "region": "ca", "resolve_source": "wunderground", "tz": "America/Toronto"},
    "mexico-city": {"lat": 19.4363, "lon": -99.0721, "name": "Mexico City", "station": "MMMX", "unit": "C", "region": "sa", "resolve_source": "wunderground", "tz": "America/Mexico_City"},

    # --- فاز ۵ نقشه‌راه: افزوده‌شدهٔ جدید (۱۶ سپتامبر ۲۰۲۶) -----------------
    "cape-town": {"lat": -33.9648, "lon": 18.6017, "name": "Cape Town", "station": "FACT", "unit": "C", "region": "af", "resolve_source": "wunderground", "tz": "Africa/Johannesburg"},
    "denver": {"lat": 39.7017, "lon": -104.7527, "name": "Denver", "station": "KBKF", "unit": "F", "region": "us", "resolve_source": "wunderground", "tz": "America/Denver"},
}

MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]
