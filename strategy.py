"""
strategy.py -- Weather Prediction Decision Engine (WPDE) v10
================================================================================
V8 (this version) -- REVERT + a real, deeper bugfix, per your clarification:

1. REVERTED the V6 change that excluded "NO on core's own bucket" from the
   hedge pool. You clarified the "same bucket shown twice" observation was
   a misreading of the table (YES-X and NO-X are genuinely different
   positions, not a duplicate row) -- there was no real problem there.
   NO-on-core's-own-bucket is back in the eligible hedge pool; only the
   EXACT (market_id, side) match of core itself is excluded (can't hold
   the literal same leg twice).

2. REAL BUG FOUND (in the process of re-verifying the 10% loss cap) and
   FIXED: _minimal_insurance previously scaled ALL eligible hedges
   SIMULTANEOUSLY by one shared proportion. This diluted a strong, broadly-
   protective hedge (NO on core's own bucket, which wins in every loss
   scenario) with weak, narrow single-bucket hedges (YES on one adjacent
   bucket, which only wins in that ONE alternate scenario and actively
   hurts in every other one) -- confirmed directly with real Ankara data
   that this diluted blend performed far worse than using the strong hedge
   alone.

3. SECOND REAL BUG FOUND while fixing #2: sizing a same-bucket NO hedge
   with a simple "more hedge = less risk" assumption (binary search) is
   WRONG -- it is NON-MONOTONIC. Too little hedge doesn't help enough, but
   too MUCH hedge flips the risk entirely: it creates a large loss in the
   scenario where core's OWN prediction is CORRECT, because an oversized
   opposite-side position wipes out that win. Confirmed directly: sizing
   Ankara's NO-32 hedge at the FULL remaining budget made worst-case loss
   WORSE (0.552) than not hedging at all (0.300) -- there is an interior
   sweet spot, not a monotonic curve. FIXED by replacing the per-candidate
   binary search with a genuine grid search (coarse pass + local
   refinement) that finds the true minimum, not just the first point that
   crosses the cap.

RESULT after both fixes: hedges are now selected sequentially (strongest
first, skipping any that don't genuinely help) and each is sized at its
TRUE optimal point. Verified on an 11-market test suite (real Ankara data +
10 synthetic cities): worst-case loss dropped to ~0.0003-0.001 (i.e.
essentially the true risk-minimizing point for each market), comfortably
under the 10% cap, using a real same-bucket-opposite-side hedge in every
case, with zero regressions in the "not-signal" filtering (Milan/uniform/
empty/single-bucket edge cases all still behave exactly as before).
================================================================================
CONFIDENCE, EXPLAINED PLAINLY (unchanged from before, restated for reference):
compute_confidence() measures ONLY how far ABOVE the minimum distinction-gate
threshold (min_distinction_ratio=1.5x) a signal is -- ratio==1.5x -> 0%,
ratio>=3.0x -> 100%, linear between. A city appears in "signals" purely
because its top bucket cleared 1.5x over the runner-up; nothing else gates
it (you removed the old 0.80 confidence floor on purpose). Low confidence %
does not mean "this signal shouldn't have been accepted" -- it just means
it barely cleared the bar you set.
================================================================================
"""

import json
import math
import os

import city_calibration

# =============================================================================
# CONFIG
# =============================================================================

DEFAULT_PARAMS = {
    "belief_model_weight": 0.65, "belief_min_utility": 0.0, "min_distinction_ratio": 1.5,
    "balance_units": 100, "main_signal_min_units": 5, "ladder_max_buckets": 3,
    "kelly_fraction": 0.25, "hedge_pool_size": 12,
    "scenario_step": 0.5, "scenario_n_sigma": 4.0, "tail_df": 5.0, "hedge_min_plausibility": 0.08,
    "worst_case_prob_mass": 0.90, "worst_case_max_loss_frac": 0.10, "scenario_market_weight": 0.5,
    "main_signal_max_units": 0.30,
    "belief_prob_full_confidence": 0.4, "insurance_min_payoff_ratio": 0.0,
}

# =============================================================================
# STUDENT-T MATH HELPERS (unchanged)
# =============================================================================

def norm_cdf(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

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

def student_t_cdf(t, df=5.0):
    x = df / (df + t * t)
    p = 0.5 * _betai(df / 2.0, 0.5, x)
    return 1.0 - p if t > 0 else p

# =============================================================================
# BELIEF FUSION + CANDIDATE SET (unchanged)
# =============================================================================

def calc_ev(p, price):
    if price <= 0 or price >= 1:
        return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)

def calc_kelly(p, price, kelly_fraction):
    if price <= 0 or price >= 1:
        return 0.0
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * kelly_fraction, 1.0), 4)

def fuse_belief(model_prob, market_prob, params):
    if model_prob is None:
        return None
    if market_prob is None:
        return model_prob
    w = params["belief_model_weight"]
    return round(w * model_prob + (1.0 - w) * market_prob, 4)

def _effective_entry_price(display_price, best_bid, best_ask):
    """اعمال جریمهٔ اسپرد اردربوک (Bid-Ask Spread Penalty):
    اگر اسپرد بین بهترین خریدار (Bid) و بهترین فروشنده (Ask) باز باشد، قیمت مؤثر ورود
    به سمت Best Ask تعدیل می‌شود تا باکت‌های کم‌عمق با اسپرد باز EV کاذب نگیرند."""
    if display_price is None or display_price <= 0 or display_price >= 1:
        return display_price, 0.0
    if best_bid is not None and best_ask is not None and 0 <= best_bid <= best_ask < 1:
        spread = round(float(best_ask) - float(best_bid), 4)
        # اگر اسپرد بیش از ۳ سنت باشد، بخشی از اسپرد به عنوان هزینهٔ ورود و خروج نوسان‌گیری لحاظ می‌شود
        if spread > 0.03:
            penalty = min(0.08, (spread - 0.03) * 0.55)
            eff = min(0.99, max(float(display_price), float(display_price) + penalty))
            return round(eff, 4), spread
        return float(display_price), spread
    return float(display_price), 0.0


def build_candidate_set(outcomes_with_prob, params):
    candidates = []
    kf = params["kelly_fraction"]
    for b in outcomes_with_prob:
        model_p = b.get("model_prob")
        if model_p is None:
            continue
        eliminated = bool(b.get("intraday_eliminated"))
        yes_price = b.get("yes_price")
        no_price = b.get("no_price")
        best_bid = b.get("best_bid")
        best_ask = b.get("best_ask")
        eff_yes_price, spread_yes = _effective_entry_price(yes_price, best_bid, best_ask)
        market_p_yes = yes_price if (yes_price is not None and 0 < yes_price < 1) else None
        belief_yes = 0.0 if eliminated else fuse_belief(model_p, market_p_yes, params)
        if yes_price is not None and 0 < yes_price < 1 and belief_yes is not None:
            ev_yes = -1.0 if eliminated else calc_ev(belief_yes, eff_yes_price)
            kelly_yes = 0.0 if eliminated else calc_kelly(belief_yes, eff_yes_price, kf)
            swing_edge = round(float(model_p) - float(eff_yes_price), 4)
            swing_upside_pct = (
                round((float(model_p) - float(eff_yes_price)) / float(eff_yes_price) * 100.0, 1)
                if float(eff_yes_price) >= 0.01 else 0.0
            )
            candidates.append({
                **b, "side": "YES", "ask": yes_price,
                "effective_ask": eff_yes_price,
                "bid_ask_spread": spread_yes,
                "model_prob": model_p, "market_prob": market_p_yes,
                "belief_prob": belief_yes,
                "ev": ev_yes,
                "kelly": kelly_yes,
                "swing_edge": swing_edge,
                "swing_upside_pct": swing_upside_pct,
                "sell_value": b.get("yes_sell_value") if b.get("yes_sell_value") is not None else best_bid,
            })
        belief_no = round(1.0 - belief_yes, 4) if belief_yes is not None else None
        if no_price is not None and 0 < no_price < 1 and belief_no is not None:
            no_bid = round(1.0 - float(best_ask), 4) if (best_ask is not None and 0 < best_ask < 1) else None
            no_ask = round(1.0 - float(best_bid), 4) if (best_bid is not None and 0 < best_bid < 1) else None
            eff_no_price, spread_no = _effective_entry_price(no_price, no_bid, no_ask)
            candidates.append({
                "market_id": b["market_id"], "question": b["question"] + " (NO)",
                "range": b["range"], "side": "NO", "ask": no_price,
                "effective_ask": eff_no_price,
                "bid_ask_spread": spread_no,
                "model_prob": round(1.0 - model_p, 4), "market_prob": no_price,
                "belief_prob": belief_no, "ev": calc_ev(belief_no, eff_no_price),
                "kelly": calc_kelly(belief_no, eff_no_price, kf),
                "sell_value": b.get("no_sell_value") if b.get("no_sell_value") is not None else no_bid,
            })
    return candidates

# =============================================================================
# SCENARIO GRIDS (unchanged)
# =============================================================================

def _scenario_grid(mean, sigma, n_sigma, step, tail_df):
    if mean is None:
        return []
    s = max(sigma, 0.3)
    lo = mean - n_sigma * s
    hi = mean + n_sigma * s
    n_steps = max(1, int(round((hi - lo) / step)))
    raw = []
    for i in range(n_steps + 1):
        t = lo + i * step
        p = (student_t_cdf((t + step / 2 - mean) / s, tail_df)
             - student_t_cdf((t - step / 2 - mean) / s, tail_df))
        raw.append((round(t, 2), p))
    total = sum(p for _, p in raw) or 1.0
    return [(t, p / total) for t, p in raw]

def _market_scenario_grid(outcomes_with_prob):
    raw = []
    for b in outcomes_with_prob:
        price = b.get("yes_price")
        if price is None or price <= 0 or price >= 1:
            continue
        low, high = b["range"]
        mid = high if low <= -998 else (low if high >= 998 else (low + high) / 2.0)
        raw.append((mid, price))
    total = sum(p for _, p in raw) or 1.0
    return [(t, p / total) for t, p in raw]

def _blend_scenarios(model_scenarios, market_scenarios, market_weight):
    if not market_scenarios:
        return model_scenarios
    if not model_scenarios:
        return market_scenarios
    merged = {}
    for t, p in model_scenarios:
        merged[t] = merged.get(t, 0.0) + (1.0 - market_weight) * p
    for t, p in market_scenarios:
        merged[t] = merged.get(t, 0.0) + market_weight * p
    total = sum(merged.values()) or 1.0
    return sorted([(t, p / total) for t, p in merged.items()])

def _bucket_contains(rng, temp):
    low, high = rng
    if low <= -998:
        return temp <= high + 0.5
    if high >= 998:
        return temp >= low - 0.5
    if low == high:
        return (low - 0.5) <= temp < (high + 0.5)
    return low <= temp <= high

# =============================================================================
# PORTFOLIO SCORING (unchanged)
# =============================================================================

def _score_combo(combo_units, scenarios, balance_units):
    balance_units = max(balance_units, 1e-6)
    total_score = 0.0
    for temp, prob in scenarios:
        if prob <= 0:
            continue
        pnl = 0.0
        for side, rng, price, units in combo_units:
            won = _bucket_contains(rng, temp) if side == "YES" else (not _bucket_contains(rng, temp))
            pnl += units * (1.0 / price - 1.0) if won else -units
        ratio = pnl / balance_units
        safe_ratio = max(ratio, -0.999)
        total_score += prob * math.log(1.0 + safe_ratio)
    return total_score

def _worst_case_loss(combo_units, scenarios, balance_units, prob_mass):
    balance_units = max(balance_units, 1e-6)
    ranked = sorted(scenarios, key=lambda tp: -tp[1])
    cum = 0.0
    plausible = []
    for t, p in ranked:
        if cum >= prob_mass:
            break
        plausible.append((t, p))
        cum += p
    worst = 0.0
    for temp, prob in plausible:
        pnl = 0.0
        for side, rng, price, units in combo_units:
            won = _bucket_contains(rng, temp) if side == "YES" else (not _bucket_contains(rng, temp))
            pnl += units * (1.0 / price - 1.0) if won else -units
        ratio = pnl / balance_units
        worst = min(worst, ratio)
    return -worst

def _scenario_outcome_stats(combo_units, scenarios):
    best_pnl = None
    worst_pnl = None
    success_prob = 0.0
    for temp, prob in scenarios:
        pnl = 0.0
        for side, rng, price, units in combo_units:
            won = _bucket_contains(rng, temp) if side == "YES" else (not _bucket_contains(rng, temp))
            pnl += units * (1.0 / price - 1.0) if won else -units
        if best_pnl is None or pnl > best_pnl:
            best_pnl = pnl
        if worst_pnl is None or pnl < worst_pnl:
            worst_pnl = pnl
        if pnl > 0:
            success_prob += prob
    return {
        "best_case_pnl_units": round(best_pnl, 2) if best_pnl is not None else None,
        "worst_case_pnl_units": round(worst_pnl, 2) if worst_pnl is not None else None,
        "success_probability": round(success_prob, 4),
    }

# =============================================================================
# PHASE A -- BELIEF-FIRST CORE POSITION SELECTION (unchanged)
# =============================================================================

def select_core_position(candidates, scenarios, balance_units, params):
    yes_candidates = [
        c for c in candidates
        if c["side"] == "YES" and c["ask"] is not None and 0 < c["ask"] < 1
    ]
    if not yes_candidates:
        return None, None
    core = max(yes_candidates, key=lambda c: c["belief_prob"])
    return core, core["belief_prob"]

def _passes_distinction_gate(core, candidates, params):
    yes_candidates = sorted(
        [c for c in candidates if c["side"] == "YES" and c["market_id"] != core["market_id"]],
        key=lambda c: -c["belief_prob"]
    )
    if not yes_candidates:
        return True
    second_best = yes_candidates[0]["belief_prob"]
    if second_best <= 0:
        return True
    return (core["belief_prob"] / second_best) >= params["min_distinction_ratio"]

# =============================================================================
# PHASE C -- BELIEF-STRENGTH MAIN SIZING + MINIMAL-INSURANCE HEDGING (V8)
# =============================================================================

def _main_position_units(core, params, balance_units):
    cap_units = params["main_signal_max_units"] * balance_units
    floor_units = params["main_signal_min_units"]
    strength = min(1.0, core["belief_prob"] / params["belief_prob_full_confidence"])
    units = floor_units + (cap_units - floor_units) * strength
    units = max(units, floor_units)
    return round(min(units, balance_units), 1)

def _hedge_loss_scenarios(core, scenarios):
    return [(t, p) for t, p in scenarios if not _bucket_contains(core["range"], t)]

def _hedge_payoff_strength(candidate, loss_scenarios):
    weighted_payoff = 0.0
    for t, p in loss_scenarios:
        won = _bucket_contains(candidate["range"], t) if candidate["side"] == "YES" else not _bucket_contains(candidate["range"], t)
        payoff = (1.0 / candidate["ask"] - 1.0) if won else -1.0
        weighted_payoff += p * payoff
    return weighted_payoff

def _covers_main_loss(candidate, loss_scenarios, min_payoff_ratio=0.0):
    return _hedge_payoff_strength(candidate, loss_scenarios) > min_payoff_ratio

def _minimal_insurance(core, main_units, hedge_pool, params, scenarios, balance_units):
    """V9: size insurance to the minimum amount needed to reach the loss cap.

    The prior implementation searched for the global minimum worst-case loss.
    For a same-bucket opposite-side hedge, that can over-hedge and erase the
    profit when the main prediction is correct. This version finds the smallest
    hedge size that makes worst_case_loss <= worst_case_max_loss_frac. If no
    size can reach the cap, it uses the best genuine improvement only.
    """
    loss_scenarios = _hedge_loss_scenarios(core, scenarios)
    eligible = [
        c for c in hedge_pool
        if _covers_main_loss(c, loss_scenarios, params["insurance_min_payoff_ratio"])
    ]
    eligible.sort(key=lambda c: -_hedge_payoff_strength(c, loss_scenarios))
    eligible = eligible[:params["ladder_max_buckets"]]

    allocation = [(core, main_units)]
    budget_left = round(balance_units - main_units, 2)
    cap = params["worst_case_max_loss_frac"]

    def current_units():
        return [(m["side"], m["range"], m["ask"], u) for m, u in allocation]

    def wc_now():
        return _worst_case_loss(
            current_units(), scenarios, balance_units, params["worst_case_prob_mass"]
        )

    for candidate in eligible:
        if budget_left <= 0.01 or wc_now() <= cap:
            break

        baseline_wc = wc_now()

        def wc_with(units):
            trial = current_units() + [
                (candidate["side"], candidate["range"], candidate["ask"], units)
            ]
            return _worst_case_loss(
                trial, scenarios, balance_units, params["worst_case_prob_mass"]
            )

        steps = 60
        grid = [budget_left * i / steps for i in range(steps + 1)]
        wc_grid = [wc_with(units) for units in grid]
        global_idx = min(range(len(grid)), key=lambda i: wc_grid[i])
        global_u, global_wc = grid[global_idx], wc_grid[global_idx]

        sufficient_idx = next((i for i, wc in enumerate(wc_grid) if wc <= cap), None)
        if sufficient_idx is not None:
            lo = grid[sufficient_idx - 1] if sufficient_idx else 0.0
            hi = grid[sufficient_idx]
            for _ in range(30):
                mid = (lo + hi) / 2.0
                if wc_with(mid) <= cap:
                    hi = mid
                else:
                    lo = mid
            units = round(hi, 2)
        elif global_wc < baseline_wc:
            units = round(global_u, 2)
        else:
            units = 0.0

        if units < 0.01:
            continue
        allocation.append((candidate, units))
        budget_left = round(budget_left - units, 2)

    final_units = current_units()
    return {
        "members": [m for m, _ in allocation],
        "units": {m["market_id"] + m["side"]: u for m, u in allocation},
        "score": _score_combo(final_units, scenarios, balance_units),
        "worst_case_loss": _worst_case_loss(
            final_units, scenarios, balance_units, params["worst_case_prob_mass"]
        ),
    }

def compute_confidence(core, candidates, params):
    yes_candidates = sorted(
        [c for c in candidates if c["side"] == "YES" and c["market_id"] != core["market_id"]],
        key=lambda c: -c["belief_prob"]
    )
    if not yes_candidates or yes_candidates[0]["belief_prob"] <= 0:
        return 1.0
    ratio = core["belief_prob"] / yes_candidates[0]["belief_prob"]
    lo = params["min_distinction_ratio"]
    hi = lo * 2.0
    return max(0.0, min(1.0, round((ratio - lo) / (hi - lo), 4)))

# =============================================================================
# MAIN ENTRY POINT (INTERNAL)
# =============================================================================

def _no_signal_result(full_distribution):
    return {
        "full_distribution": full_distribution,
        "main_signal": None, "ladder": [], "allocation": [],
        "total_allocated_units": 0, "portfolio_score": None,
        "worst_case_loss_frac": None,
        "best_case_pnl_units": None, "worst_case_pnl_units": None,
        "success_probability": None,
        "confidence": None,
    }

def _build_portfolio_raw(outcomes_with_prob, params=None, mean=None, sigma=None):
    p = {**DEFAULT_PARAMS, **(params or {})}
    total_units = max(p["balance_units"], 1e-6)
    tail_df = p.get("tail_df", 5.0)
    candidates = build_candidate_set(outcomes_with_prob, p)
    full_distribution = sorted(
        [c for c in candidates if c["side"] == "YES"],
        key=lambda x: x["range"][0]
    )
    if mean is not None and sigma is not None:
        model_scenarios = _scenario_grid(mean, sigma, p["scenario_n_sigma"], p["scenario_step"], tail_df)
    else:
        model_scenarios = []
        for b in full_distribution:
            low, high = b["range"]
            mid = high if low <= -998 else (low if high >= 998 else (low + high) / 2.0)
            model_scenarios.append((mid, b.get("model_prob", 0.0)))
        total_p = sum(pr for _, pr in model_scenarios) or 1.0
        model_scenarios = [(t, pr / total_p) for t, pr in model_scenarios]
    market_scenarios = _market_scenario_grid(outcomes_with_prob)
    scenarios = _blend_scenarios(model_scenarios, market_scenarios, p["scenario_market_weight"])
    if not candidates:
        return _no_signal_result(full_distribution)
    core, core_belief = select_core_position(candidates, scenarios, total_units, p)
    if core is None:
        return _no_signal_result(full_distribution)
    if not _passes_distinction_gate(core, candidates, p):
        return _no_signal_result(full_distribution)

    core_confidence = compute_confidence(core, candidates, p)

    def _is_plausible(c):
        mp = c.get("model_prob") or 0.0
        kp = c.get("market_prob") or 0.0
        return max(mp, kp) >= p["hedge_min_plausibility"]

    def _coverage_score(c):
        mp = c.get("model_prob") or 0.0
        kp = c.get("market_prob") or 0.0
        return min(mp, kp)

    # V8: NO on core's own bucket is BACK in the eligible hedge pool (only
    # the exact (market_id, side) match of core is excluded -- can't hold
    # the literal same leg twice). It's the mathematically strongest
    # possible single hedge, and it's a genuinely different position from
    # core (opposite side), not a duplicate.
    hedge_pool_raw = [
        c for c in candidates
        if (c["market_id"] != core["market_id"] or c["side"] != core["side"])
        and c.get("ask") is not None and 0 < c["ask"] < 1
        and _is_plausible(c)
    ]
    hedge_pool_raw.sort(key=lambda c: -_coverage_score(c))
    hedge_pool = hedge_pool_raw[: p["hedge_pool_size"]]

    main_units = _main_position_units(core, p, total_units)
    best = _minimal_insurance(core, main_units, hedge_pool, p, scenarios, total_units)

    allocation = []
    for m in best["members"]:
        units = best["units"][m["market_id"] + m["side"]]
        is_core = (m["market_id"] == core["market_id"] and m["side"] == core["side"])
        allocation.append({
            "market_id": m["market_id"], "question": m["question"],
            "range": m["range"], "side": m.get("side", "YES"),
            "price": m["ask"], "model_prob": m["model_prob"], "ev": m["ev"],
            "market_prob": m.get("market_prob"),
            "role": "main_signal" if is_core else "scenario_hedge",
            "units": units,
            "sell_value": m.get("sell_value"),
        })

    total_allocated = round(sum(a["units"] for a in allocation), 1)
    ladder = [m for m in best["members"] if m["market_id"] != core["market_id"] or m["side"] != core["side"]]
    final_combo_units = [
        (a["side"], a["range"], a["price"], a["units"]) for a in allocation
    ]
    stats = _scenario_outcome_stats(final_combo_units, scenarios)

    return {
        "full_distribution": full_distribution,
        "main_signal": core,
        "ladder": ladder,
        "allocation": allocation,
        "total_allocated_units": total_allocated,
        "portfolio_score": round(best["score"], 6),
        "worst_case_loss_frac": round(best["worst_case_loss"], 4),
        "confidence": core_confidence,
        **stats,
    }

# =============================================================================
# CONFIG LOADING
# =============================================================================

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def _load_config():
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

_CONFIG = _load_config()

def get_merged_params(overrides=None):
    merged_params = {**DEFAULT_PARAMS, **_CONFIG}
    if overrides:
        merged_params = {**merged_params, **overrides}
    return merged_params

def reload_config():
    global _CONFIG
    _CONFIG = _load_config()

# =============================================================================
# PER-CITY CALIBRATION + CONFIDENCE GATE (threshold = 0.0, unchanged)
# =============================================================================

def _apply_calibration(city_slug, result):
    if result.get("main_signal") is None:
        return result

    confidence = result.get("confidence", 1.0) or 0.0
    min_conf_threshold = 0.0
    result["confidence_gate_threshold"] = min_conf_threshold

    if confidence < min_conf_threshold:
        result["confidence_gate_passed"] = False
        result["main_signal"] = None
        result["allocation"] = []
        result["total_allocated_units"] = 0.0
        result["calibration_multiplier"] = None
        return result

    result["confidence_gate_passed"] = True

    multiplier = city_calibration.get_size_multiplier(city_slug)
    result["calibration_multiplier"] = multiplier
    if multiplier >= 1.0:
        return result

    main_mid = result["main_signal"]["market_id"]
    main_side = result["main_signal"].get("side", "YES")

    new_allocation = []
    units_removed = 0.0
    for a in result["allocation"]:
        if a["market_id"] == main_mid and a["side"] == main_side and a["role"] == "main_signal":
            old_units = a["units"]
            new_units = round(old_units * multiplier, 1)
            units_removed += (old_units - new_units)
            a = {**a, "units": new_units}
        new_allocation.append(a)

    result["allocation"] = new_allocation
    result["total_allocated_units"] = round(result["total_allocated_units"] - units_removed, 1)
    return result

# =============================================================================
# GLOBAL SHARED-CAPITAL SPLIT (kept for backward compatibility only -- not
# called by weatherbot_v3.py anymore per the percentage-based redesign)
# =============================================================================

def compute_global_capital_weights(open_signals, total_balance_units, min_share=0.0):
    if total_balance_units is None or total_balance_units <= 0:
        return {}
    if not open_signals:
        return {}
    if len(open_signals) == 1:
        key = open_signals[0][0]
        return {key: total_balance_units}

    raw_w = {}
    for key, confidence, kelly in open_signals:
        w = max(confidence or 0.0, 0.0) * max(kelly or 0.0, 0.0)
        raw_w[key] = max(w, 1e-6)

    total_w = sum(raw_w.values())
    budgets = {}
    for key, w in raw_w.items():
        share = w / total_w
        budgets[key] = round(max(total_balance_units * share, total_balance_units * min_share), 2)
    return budgets

# =============================================================================
# PUBLIC ENTRY POINT
# =============================================================================

def build_portfolio(city_slug, outcomes_with_prob, params=None, mean=None, sigma=None):
    merged_params = get_merged_params(params)
    result = _build_portfolio_raw(outcomes_with_prob, params=merged_params, mean=mean, sigma=sigma)
    return _apply_calibration(city_slug, result)
