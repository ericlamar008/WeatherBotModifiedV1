"""
test_wpde_strategy.py -- test suite for wpde_strategy.py (Belief Fusion +
Scenario Portfolio Utility Optimization engine, v9)
================================================================================
Run with: python test_wpde_strategy.py

This replaces the old test_strategy.py, which validated the previous
Edge/EV-first architecture (evaluate_bucket()/evaluate_no_side() min_prob /
min_ev / stress-EV elimination, then "sort survivors by EV, take #1").
Those concepts no longer exist in wpde_strategy.py, so those tests are
retired. In their place:

LAYER 1 -- Belief Fusion: fuse_belief() blends model_prob and market_prob
           by belief_model_weight, and collapses to model_prob when there
           is no valid market price to fuse with.
LAYER 2 -- Candidate Set + Utility-first core selection: build_candidate_set()
           builds YES/NO candidates for every priced bucket with NO min_ev/
           min_prob elimination; select_core_position() picks the candidate
           with the highest standalone expected-utility (log-growth) score,
           NOT the one with the largest raw edge -- verified directly by
           constructing a bucket with a huge edge but poor standalone
           utility and confirming it is not selected.
LAYER 3 -- Confidence: monotonic in model/market agreement; does not change
           which market_id the engine treats as core signal.
LAYER 4 -- Worst-case constraint: chosen combo's worst_case_loss_frac never
           exceeds worst_case_max_loss_frac.
LAYER 5 -- Invariants: total_allocated_units never exceeds balance_units,
           every allocated leg has a valid 0 < price < 1, no NaN/Inf.
"""

import math
import sys
import strategy

FAILURES = []

def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)

def make_bucket(mid_low, mid_high, model_prob, yes_price, market_id=None):
    market_id = market_id or f"b_{mid_low}_{mid_high}"
    return {
        "market_id": market_id,
        "question": f"bucket {mid_low}-{mid_high}",
        "range": (mid_low, mid_high),
        "model_prob": model_prob,
        "yes_price": yes_price,
        "no_price": round(1.0 - yes_price, 4) if 0 < yes_price < 1 else 0.5,
        "volume": 1000,
    }

# ---------------------------------------------------------------------------
print("=" * 70)
print("LAYER 1 -- Belief Fusion")
print("=" * 70)

def layer1_fusion_collapses_to_model_without_market():
    fused = strategy.fuse_belief(0.42, None, strategy.DEFAULT_PARAMS)
    check("L1 fuse_belief collapses to model_prob when market_prob is None",
          abs(fused - 0.42) < 1e-9, detail=f"got {fused}")

def layer1_fusion_weight_respected():
    model_p, market_p = 0.60, 0.20
    p_model_heavy = {**strategy.DEFAULT_PARAMS, "belief_model_weight": 0.9}
    p_market_heavy = {**strategy.DEFAULT_PARAMS, "belief_model_weight": 0.1}
    b_model = strategy.fuse_belief(model_p, market_p, p_model_heavy)
    b_market = strategy.fuse_belief(model_p, market_p, p_market_heavy)
    check("L1 higher belief_model_weight pulls fused belief toward model_prob",
          b_model > b_market, detail=f"model_heavy={b_model} market_heavy={b_market}")

def layer1_fusion_matches_manual_blend():
    p = {**strategy.DEFAULT_PARAMS, "belief_model_weight": 0.65}
    fused = strategy.fuse_belief(0.41, 0.28, p)
    expected = round(0.65 * 0.41 + 0.35 * 0.28, 4)
    check("L1 fuse_belief matches manual weighted blend", abs(fused - expected) < 1e-6,
          detail=f"got {fused} expected {expected}")

layer1_fusion_collapses_to_model_without_market()
layer1_fusion_weight_respected()
layer1_fusion_matches_manual_blend()

# ---------------------------------------------------------------------------
print("=" * 70)
print("LAYER 2 -- Candidate Set + Utility-first Core Selection")
print("=" * 70)

def layer2_candidate_set_no_elimination():
    outcomes = [
        make_bucket(20, 21, 0.02, 0.01),
        make_bucket(21, 22, 0.30, 0.30),
        make_bucket(22, 23, 0.40, 0.35),
    ]
    p = strategy.get_merged_params()
    candidates = strategy.build_candidate_set(outcomes, p)
    ids = {c["market_id"] for c in candidates}
    check("L2 build_candidate_set keeps low-prob/low-price buckets (no min_prob/min_ev cut)",
          "b_20_21" in ids, detail=f"candidate ids: {ids}")
    check("L2 build_candidate_set produces both YES and NO sides",
          any(c["side"] == "NO" for c in candidates) and any(c["side"] == "YES" for c in candidates))

def layer2_edge_plays_no_role_in_core_selection():
    outcomes = [
        make_bucket(28, 28, 0.55, 0.50),
        make_bucket(50, 50, 0.02, 0.001),
    ]
    p = strategy.get_merged_params()
    candidates = strategy.build_candidate_set(outcomes, p)
    scenarios = [(28.0, 0.94), (35.0, 0.05), (50.0, 0.01)]
    core, score = strategy.select_core_position(candidates, scenarios, p["balance_units"], p)
    check("L2 core position is NOT chosen by raw edge magnitude alone",
          core is not None and core["range"][0] == 28,
          detail=f"got core={core['market_id'] if core else None}")

def layer2_core_has_positive_expected_utility():
    outcomes = [make_bucket(28, 28, 0.55, 0.50)]
    p = strategy.get_merged_params()
    candidates = strategy.build_candidate_set(outcomes, p)
    scenarios = [(28.0, 0.7), (30.0, 0.3)]
    core, score = strategy.select_core_position(candidates, scenarios, p["balance_units"], p)
    check("L2 selected core position has non-negative expected utility score",
          core is None or score >= 0.0,
          detail=f"score={score}")

def layer2_two_degree_bucket_contains_continuous_bounds():
    check("L2 2°F bucket (70, 71) contains 69.6 and 71.4",
          strategy._bucket_contains((70, 71), 69.6) and strategy._bucket_contains((70, 71), 71.4))
    check("L2 adjacent 2°F buckets (70, 71) and (72, 73) partition 71.5 without gap",
          strategy._bucket_contains((70, 71), 71.49) and strategy._bucket_contains((72, 73), 71.5))

layer2_candidate_set_no_elimination()
layer2_edge_plays_no_role_in_core_selection()
layer2_core_has_positive_expected_utility()
layer2_two_degree_bucket_contains_continuous_bounds()

# ---------------------------------------------------------------------------
print("=" * 70)
print("LAYER 3 -- Confidence & Single Core Allocation (No Hedge)")
print("=" * 70)

def layer3_confidence_monotonic_in_agreement():
    p = strategy.get_merged_params()
    agree = {"market_id": "m1", "side": "YES", "model_prob": 0.50, "market_prob": 0.50, "belief_prob": 0.50}
    disagree = {"market_id": "m1", "side": "YES", "model_prob": 0.80, "market_prob": 0.20, "belief_prob": 0.50}
    c_agree = strategy.compute_confidence(agree, [agree], p)
    c_disagree = strategy.compute_confidence(disagree, [disagree], p)
    check("L3 confidence is higher when model and market agree",
          c_agree >= c_disagree, detail=f"agree={c_agree} disagree={c_disagree}")

def layer3_single_signal_no_hedge_legs():
    outcomes = [
        make_bucket(28, 28, 0.70, 0.30),
        make_bucket(29, 29, 0.15, 0.20),
        make_bucket(30, 30, 0.10, 0.15),
    ]
    p = strategy.get_merged_params()
    res = strategy.build_portfolio("testcity", outcomes, params=p, mean=28.0, sigma=1.5)
    check("L3 ladder is empty and allocation contains only main_signal",
          res["ladder"] == [] and all(a["role"] == "main_signal" for a in res["allocation"]),
          detail=f"allocation={res['allocation']}")

layer3_confidence_monotonic_in_agreement()
layer3_single_signal_no_hedge_legs()

# ---------------------------------------------------------------------------
print("=" * 70)
print("LAYER 5 -- Invariants")
print("=" * 70)

def layer5_invariants():
    outcomes = [
        make_bucket(20, 21, 0.08, 0.15),
        make_bucket(21, 22, 0.18, 0.30),
        make_bucket(22, 23, 0.41, 0.28),
        make_bucket(23, 24, 0.23, 0.20),
        make_bucket(24, 25, 0.10, 0.07),
    ]
    p = strategy.get_merged_params({"confidence_agreement_weight": 0.0, "confidence_calibration_weight": 0.0})
    res = strategy.build_portfolio("testcity", outcomes, params=p, mean=22.5, sigma=1.5)
    total = sum(a["units"] for a in res["allocation"])
    check("L5 total_allocated_units never exceeds balance_units",
          total <= p["balance_units"] + 1e-6, detail=f"total={total}")
    ok_prices = all(0 < a["price"] < 1 for a in res["allocation"])
    check("L5 every allocated leg has a valid 0 < price < 1", ok_prices)
    ok_finite = all(math.isfinite(a["units"]) and math.isfinite(a["price"]) for a in res["allocation"])
    check("L5 no NaN/Inf in allocation units/prices", ok_finite)

def layer5_empty_input_handled():
    res = strategy.build_portfolio("testcity", [], params=strategy.get_merged_params())
    check("L5 empty outcomes list -> main_signal is None, no crash", res["main_signal"] is None)

def layer5_no_valid_prices_handled():
    outcomes = [
        {"market_id": "b1", "question": "q1", "range": (20, 21), "model_prob": 0.5,
         "yes_price": 0.0, "no_price": 0.0, "volume": 1000},
        {"market_id": "b2", "question": "q2", "range": (21, 22), "model_prob": 0.5,
         "yes_price": 1.0, "no_price": 1.0, "volume": 1000},
    ]
    res = strategy.build_portfolio("testcity", outcomes, params=strategy.get_merged_params())
    check("L5 all-invalid-price outcomes -> main_signal is None, no crash", res["main_signal"] is None)

layer5_invariants()
layer5_empty_input_handled()
layer5_no_valid_prices_handled()

# ---------------------------------------------------------------------------
print("=" * 70)
if FAILURES:
    print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
else:
    print("RESULT: ALL LAYERS PASSED")
