"""resolution_patch.py -- Records per-city bucket-distance calibration data (compat with new strategy.py)"""
import city_calibration

def _find_winning_range(full_distribution, settlement):
    for b in full_distribution:
        mid = str(b.get("market_id", ""))
        if mid in settlement and settlement[mid] is True:
            rng = b.get("range")
            if rng: return tuple(rng)
    return None

def _find_main_range(mkt):
    locked = mkt.get("locked_allocation") or []
    if locked:
        main_legs = [a for a in locked if a.get("role") == "main_signal"]
        if not main_legs:
            main_legs = sorted(locked, key=lambda a: a.get("units", 0), reverse=True)[:1]
        if main_legs and main_legs[0].get("range"):
            return tuple(main_legs[0]["range"])
    legacy_main = mkt.get("main_signal")
    if legacy_main and legacy_main.get("range"):
        return tuple(legacy_main["range"])
    return None

def record_city_calibration(mkt, settlement, city_slug):
    full_dist = mkt.get("full_distribution") or []
    if not full_dist: return None
    main_range = _find_main_range(mkt)
    if main_range is None: return None
    winning_range = _find_winning_range(full_dist, settlement)
    if winning_range is None: return None
    all_ranges_sorted = sorted({tuple(b["range"]) for b in full_dist if b.get("range")}, key=lambda r: r[0])
    return city_calibration.record_resolution(city_slug, main_range=main_range,
                                               actual_range=winning_range, all_ranges_sorted=all_ranges_sorted)
