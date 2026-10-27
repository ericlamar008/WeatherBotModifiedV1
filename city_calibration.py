"""city_calibration.py -- Per-city model-calibration tracking for WeatherBet v3"""
import json, os
from statistics import mean

HISTORY_PATH = os.environ.get("CITY_CALIBRATION_PATH", "city_calibration_history.json")
ROLLING_WINDOW = 8
MIN_SAMPLES_FOR_PENALTY = 3
FULL_STRENGTH_DISTANCE = 0.5
BIG_MISS_THRESHOLD = 1.5
PENALTY_SATURATION_DISTANCE = 3.5
MAX_PENALTY = 0.5

def _load_history():
    if not os.path.exists(HISTORY_PATH): return {}
    try:
        with open(HISTORY_PATH, "r") as f: return json.load(f)
    except Exception: return {}

def _save_history(history):
    try:
        with open(HISTORY_PATH, "w") as f: json.dump(history, f, indent=2)
    except Exception as e:
        print(f"[city_calibration] warning: could not save history: {e}")

def bucket_distance(main_range, actual_range, all_ranges_sorted):
    try:
        idx_main = all_ranges_sorted.index(tuple(main_range))
        idx_actual = all_ranges_sorted.index(tuple(actual_range))
        return abs(idx_main - idx_actual)
    except ValueError:
        return None

def record_resolution(city_slug, main_range, actual_range, all_ranges_sorted):
    dist = bucket_distance(main_range, actual_range, all_ranges_sorted)
    if dist is None: return None
    history = _load_history()
    entries = history.get(city_slug, [])
    entries.append(dist)
    entries = entries[-ROLLING_WINDOW:]
    history[city_slug] = entries
    _save_history(history)
    return dist

def get_avg_distance(city_slug):
    history = _load_history()
    entries = history.get(city_slug, [])
    if len(entries) < MIN_SAMPLES_FOR_PENALTY: return None
    return mean(entries)

def get_size_multiplier(city_slug):
    avg = get_avg_distance(city_slug)
    if avg is None or avg <= FULL_STRENGTH_DISTANCE: return 1.0
    if avg >= PENALTY_SATURATION_DISTANCE: return MAX_PENALTY
    span = PENALTY_SATURATION_DISTANCE - FULL_STRENGTH_DISTANCE
    frac = (avg - FULL_STRENGTH_DISTANCE) / span
    return round(1.0 - frac*(1.0-MAX_PENALTY), 3)

def city_calibration_report():
    history = _load_history()
    report = {}
    for city, entries in history.items():
        report[city] = {"avg_distance": round(mean(entries),2) if entries else None,
                         "samples": len(entries), "multiplier": get_size_multiplier(city)}
    return report
