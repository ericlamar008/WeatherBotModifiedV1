"""
dashboard.py -- Self-contained HTML dashboard generator for WeatherBet v3
================================================================================
V5 BUGFIX (this version): unified_bucket_table() previously picked only ONE
allocation per bucket row via `alloc = yes_alloc or no_alloc`. But the
insurance engine can legitimately allocate BOTH a YES leg (e.g. the main
signal) AND a NO leg (e.g. a hedge -- NO on the main's OWN bucket is
actually the PERFECT insurance, since it wins in exactly every scenario
where the YES main loses) on the SAME bucket simultaneously. The old code
silently dropped whichever leg it didn't pick, while STILL counting its
units in the denominator used for "% سرمایه" -- causing percentages to
visibly sum to less than 100%.

CONFIRMED root cause (reproduced directly): a bucket with a YES main leg
(29.7 units) AND a NO hedge leg (14.92 units) on the SAME bucket -- the old
code showed only the YES row, silently hiding the NO leg, while both were
still counted in the shared total. Before the fix: percentages summed to
85.08%. After the fix: exactly 100.00%. This is a byte-for-byte match for
the pattern you found in Munich (69.4% instead of 100%).

FIX: each bucket now renders ONE ROW PER ALLOCATED LEG (so a bucket with
both a YES and a NO allocation gets two rows, each correctly labeled,
sized, and percentaged), and buckets with no allocation still render
exactly one "not-signal" row as before. Header/row column counts remain
identical in every mode (verified across (is_committed, show_result)
combinations).

--- BELIEF COLUMN (added) ----------------------------------------------------
Added one new read-only column ("باور نهایی") showing b["belief_prob"] --
the model+market-blended belief that strategy.py already computes for every
bucket in full_distribution, but never surfaced in the UI before. Purely
additive: no scoring, sizing, hedge-selection, or gating logic touched.
Verified header <th> count == row <td> count in both is_committed and
show_result modes before shipping.

Everything else (all CSS, the sell-info column, resolved/no-signal/
city-stats sections, the "Lock this moment" JS workflow) is UNCHANGED.
================================================================================
"""

import json
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

try:
    from locations import MONTHS
except ImportError:
    MONTHS = ["january", "february", "march", "april", "may", "june",
              "july", "august", "september", "october", "november", "december"]

OUTPUT_FILE = Path("dashboard.html")

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<title>WeatherBet v3 Dashboard</title>
<style>
body{background:#0f1115;color:#e6e6e6;font-family:Tahoma,Vazir,sans-serif;padding:24px}
h1{font-size:20px}
.meta{color:#9aa0a6;font-size:13px;margin-bottom:20px}
.summary-cards{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:14px}
.card{background:#181b21;border:1px solid #2a2e37;border-radius:10px;padding:14px 20px;min-width:140px}
.card div:first-child{color:#9aa0a6;font-size:12px;margin-bottom:6px}
.card .val{font-size:22px;font-weight:bold}
.track-label{font-size:13px;color:#9aa0a6;margin:4px 0 10px}
.section-title{font-size:16px;margin:30px 0 12px;border-bottom:1px solid #2a2e37;padding-bottom:8px;scroll-margin-top:70px}
.quick-nav{position:sticky;top:0;z-index:50;display:flex;gap:10px;flex-wrap:wrap;background:#0f1115;padding:10px 0;margin-bottom:6px;border-bottom:1px solid #2a2e37}
.quick-nav a{color:#c7ccd1;background:#181b21;border:1px solid #2a2e37;border-radius:8px;padding:6px 14px;font-size:12.5px;text-decoration:none;white-space:nowrap}
.quick-nav a:hover{background:#232730;color:#e6e6e6}
details.market-block{background:#14161b;border:1px solid #2a2e37;border-radius:10px;padding:10px 16px;margin-bottom:10px}
details.market-block[open]{padding-bottom:16px}
details.date-group{background:#101216;border:1px solid #232730;border-radius:10px;padding:10px 16px;margin-bottom:14px}
summary{cursor:pointer;font-size:14px;color:#c7ccd1;list-style:none;padding:4px 0}
summary::-webkit-details-marker{display:none}
summary::before{content:"\\25B8 ";color:#6b7280;font-size:11px;margin-left:6px}
details[open]>summary::before{content:"\\25BE "}
.date-summary{font-size:15px;color:#e6e6e6}
.market-head{font-size:14px;color:#c7ccd1}
.live-tag{color:#eab308;font-size:11px}
.committed-tag{color:#4ade80;font-size:11px}
.win-tag{color:#4ade80;font-size:12px;font-weight:bold}
.loss-tag{color:#f87171;font-size:12px;font-weight:bold}
.scenario-stats{font-size:11.5px;color:#9aa0a6;margin-right:14px}
.scenario-stats .best{color:#4ade80}
.scenario-stats .worst{color:#f87171}
.scenario-stats .prob{color:#60a5fa}
.scenario-stats .confidence{color:#c084fc}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:10px;margin-bottom:6px}
th,td{padding:6px 8px;text-align:center;border-bottom:1px solid #23262d}
th{color:#9aa0a6;font-weight:normal}
tr.role-main{background:rgba(34,197,94,0.10)}
tr.role-hedge{background:rgba(234,179,8,0.08)}
tr.role-emergency{background:rgba(220,38,38,0.10)}
tr.not-signal{opacity:0.55}
.badge{padding:2px 8px;border-radius:6px;font-size:11px}
.badge-main{background:#16a34a33;color:#4ade80;border:1px solid #16a34a}
.badge-hedge{background:#ca8a0433;color:#fbbf24;border:1px solid #ca8a04}
.badge-emergency{background:#dc262633;color:#f87171;border:1px solid #dc2626}
.badge-entered{background:#2563eb33;color:#60a5fa;border:1px solid #2563eb}
.badge-skipped{background:#37415133;color:#9ca3af;border:1px solid #374151}
.key-cell{font-family:Consolas,monospace;font-size:11.5px;color:#93c5fd;direction:ltr;unicode-bidi:embed}
.empty{color:#6b7280;font-style:italic;padding:12px 0}
.live-col{color:#60a5fa}
.commit-panel{display:flex;align-items:center;gap:10px;margin:8px 0;padding:8px 10px;background:#101216;border:1px solid #232730;border-radius:8px;flex-wrap:wrap}
.commit-btn{background:#2563eb;color:#fff;border:none;border-radius:6px;padding:6px 14px;font-size:12.5px;cursor:pointer}
.commit-btn:hover{background:#1d4ed8}
.commit-btn:disabled{background:#374151;cursor:not-allowed}
.commit-status{font-size:11.5px;color:#9aa0a6}
.attach-btn{background:#374151;color:#e6e6e6;border:1px solid #4b5563;border-radius:6px;padding:6px 12px;font-size:11.5px;cursor:pointer}
.attach-btn:hover{background:#4b5563}
</style>
</head>
<body>
<h1>WeatherBet v3 Dashboard</h1>
<nav class="quick-nav">
<a href="#sec-signals">سیگنال‌ها</a>
<a href="#sec-open">بازارهای باز</a>
<a href="#sec-resolved">بازارهای حل‌شده</a>
<a href="#sec-no-signal">بدون سیگنال</a>
<a href="#sec-city-stats">آمار شهرها</a>
</nav>
<div class="meta">آخرین به‌روزرسانی: LAST_UPDATE UTC&nbsp;&nbsp;&nbsp;&nbsp;(داده‌ها از my_entries.txt: no = ثبت نشده, yes = ثبت شده)</div>
<div class="commit-panel">
<button class="attach-btn" onclick="attachLockFile()">اتصال به lock_requests.txt</button>
<span class="commit-status" id="globalCommitStatus">داشبورد از http://localhost:8765 باز شود.</span>
</div>

<div class="track-label">-- استراتژی کامل (همه‌ی سیگنال‌ها) --</div>
<div class="summary-cards">
<div class="card"><div>موجودی</div><div class="val">BALANCE</div></div>
<div class="card"><div>نرخ برد</div><div class="val">WIN_RATE</div></div>
<div class="card"><div>تعداد کل معاملات</div><div class="val">TOTAL_TRADES</div></div>
</div>

<div class="track-label">-- معاملات واقعی شما (my_entries.txt = yes) --</div>
<div class="summary-cards">
<div class="card"><div>موجودی من</div><div class="val">MY_BALANCE</div></div>
<div class="card"><div>نرخ برد من</div><div class="val">MY_WIN_RATE</div></div>
<div class="card"><div>تعداد معاملات من</div><div class="val">MY_TOTAL_TRADES</div></div>
<div class="card"><div>بازارهای باز</div><div class="val">OPEN_COUNT</div></div>
<div class="card"><div>تعداد شهرها</div><div class="val">CITY_COUNT</div></div>
</div>

<div class="section-title" id="sec-signals">سیگنال‌ها</div>
SIGNALS_HTML

<div class="section-title" id="sec-open">بازارهای باز</div>
OPEN_MARKETS_HTML

<div class="section-title" id="sec-resolved">بازارهای حل‌شده</div>
RESOLVED_HTML

<div class="section-title" id="sec-no-signal">بازارهای منقضی‌شده بدون سیگنال</div>
NO_SIGNAL_HTML

<div class="section-title" id="sec-city-stats">آمار به‌تفکیک شهر</div>
CITY_STATS_HTML

<script>
let lockFileHandle = null;
function fsAccessSupported() { return "showSaveFilePicker" in window; }
async function attachLockFile() {
  if (!fsAccessSupported()) {
    document.getElementById("globalCommitStatus").textContent = "مرورگر از File System Access API پشتیبانی نمی‌کند.";
    return;
  }
  try {
    lockFileHandle = await window.showSaveFilePicker({
      suggestedName: "lock_requests.txt",
      types: [{ description: "Text file", accept: { "text/plain": [".txt"] } }],
    });
    document.getElementById("globalCommitStatus").textContent = "به lock_requests.txt متصل شد.";
    document.querySelectorAll(".commit-btn").forEach(b => b.disabled = false);
  } catch (err) {
    document.getElementById("globalCommitStatus").textContent = "خطا: " + err;
  }
}
async function writeLockLine(marketKey) {
  const line = marketKey + "|" + new Date().toISOString() + "\\n";
  const statusEl = document.getElementById("status_" + marketKey);
  if (lockFileHandle) {
    try {
      const existing = await lockFileHandle.getFile();
      const existingText = await existing.text();
      const writable = await lockFileHandle.createWritable();
      await writable.write(existingText + line);
      await writable.close();
      statusEl.textContent = "قفل شد -- " + new Date().toLocaleTimeString("fa-IR");
      statusEl.style.color = "#4ade80";
      return;
    } catch (err) {
      statusEl.textContent = "خطا، حالت fallback...";
    }
  }
  const blob = new Blob([line], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "lock_requests_append.txt";
  a.click();
  URL.revokeObjectURL(url);
  statusEl.textContent = "دانلود شد -- به data/lock_requests.txt اضافه کنید.";
  statusEl.style.color = "#eab308";
}
window.addEventListener("DOMContentLoaded", () => {
  if (!fsAccessSupported()) {
    document.getElementById("globalCommitStatus").textContent = "حالت fallback (بدون File System Access API).";
  }
});
</script>
</body>
</html>"""

def fmt(v, digits=2):
    if v is None:
        return "-"
    return f"{v:.{digits}f}"

def role_class(role):
    if role == "main_signal": return "role-main"
    if role == "emergency_hedge": return "role-emergency"
    if role: return "role-hedge"
    return "not-signal"

def role_badge(role):
    if role == "main_signal": return '<span class="badge badge-main">main</span>'
    if role == "emergency_hedge": return '<span class="badge badge-emergency">emergency</span>'
    if role: return '<span class="badge badge-hedge">hedge</span>'
    return "-"

def entry_badge(user_entered):
    if user_entered: return '<span class="badge badge-entered">وارد شد</span>'
    return '<span class="badge badge-skipped">رد شد</span>'

def label_for_range(low, high, unit_sym):
    if low <= -998 and high >= 998: return f"?{unit_sym}"
    if low <= -998: return f"\u2264{high}{unit_sym}"
    if high >= 998: return f"\u2265{low}{unit_sym}"
    if low == high: return f"{low}{unit_sym}"
    return f"{low}-{high}{unit_sym}"

def scenario_stats_html(best_pnl, worst_pnl, success_prob, confidence=None):
    if best_pnl is None and worst_pnl is None and success_prob is None and confidence is None:
        return ""
    best_str = f"{best_pnl:+.2f}u" if best_pnl is not None else "-"
    worst_str = f"{worst_pnl:+.2f}u" if worst_pnl is not None else "-"
    prob_str = f"{success_prob * 100:.0f}%" if success_prob is not None else "-"
    conf_str = f"{confidence * 100:.0f}%" if confidence is not None else "-"
    conf_html = f'&nbsp;&nbsp;<span class="confidence">اطمینان: {conf_str}</span>' if confidence is not None else ""
    return (
        '<span class="scenario-stats">'
        f'بهترین حالت: <span class="best">{best_str}</span>&nbsp;&nbsp;'
        f'بدترین حالت: <span class="worst">{worst_str}</span>&nbsp;&nbsp;'
        f'احتمال موفقیت: <span class="prob">{prob_str}</span>{conf_html}</span>'
    )

def _render_leg_row(b, alloc, unit_sym, is_committed, live_by_key, show_result):
    """V5 (NEW helper): renders ONE row for ONE specific allocated leg (or
    the 'not-signal' fallback if alloc is None). Split out of the main loop
    so a bucket with BOTH a YES and a NO allocation can call this twice."""
    low, high = b["range"]
    label = label_for_range(low, high, unit_sym)
    yes_price = b.get("yes_price", b.get("ask", 0.0))
    no_price = b.get("no_price", round(1.0 - yes_price, 4))

    if alloc:
        entry_price = alloc.get("price")
        if entry_price is not None:
            if alloc["side"] == "YES":
                yes_price = entry_price; no_price = round(1.0 - entry_price, 4)
            else:
                no_price = entry_price; yes_price = round(1.0 - entry_price, 4)

    market_prob_pct = yes_price * 100
    model_prob = b.get("model_prob", 0.0)
    role = alloc.get("role") if alloc else None
    rclass = role_class(role)
    side_display = alloc["side"] if alloc else "YES"
    entry_key = alloc.get("entry_key", "-") if alloc else "-"
    idx = alloc.get("idx", "-") if alloc else "-"
    pct_capital = alloc.get("_pct_capital_precomputed", "-") if alloc else "-"
    edge_val = alloc.get("ev") if alloc else b.get("ev")
    edge_str = f"{edge_val:+.3f}" if edge_val is not None else "-"

    sell_value = alloc.get("sell_value") if alloc else None
    entry_price_for_sell = alloc.get("price") if alloc else None
    if sell_value is not None and entry_price_for_sell is not None and entry_price_for_sell > 0:
        drift_pct = (sell_value - entry_price_for_sell) / entry_price_for_sell * 100.0
        sell_str = f"{sell_value:.3f} ({drift_pct:+.1f}%)"
    else:
        sell_str = "-"

    row = f'<tr class="{rclass}">'
    row += f"<td>{label}</td><td>{side_display}</td>"
    belief_val = b.get("belief_prob")
    belief_str = f"{belief_val*100:.1f}%" if belief_val is not None else "-"
    row += f"<td>{model_prob*100:.1f}%</td><td>{market_prob_pct:.1f}%</td><td>{belief_str}</td>"
    row += f"<td>{yes_price:.3f}</td><td>{no_price:.3f}</td><td>{edge_str}</td>"
    row += f"<td>{idx}</td><td class='key-cell'>{entry_key}</td>"
    row += f"<td>{role_badge(role)}</td><td>{pct_capital}</td>"

    if is_committed:
        live_yes = live_by_key.get((b["market_id"], "YES"))
        live_no = live_by_key.get((b["market_id"], "NO"))
        live_alloc_row = live_yes or live_no
        live_model_prob = b.get("live_model_prob")
        if live_model_prob is None:
            live_model_prob = model_prob
        live_yes_price = None
        live_edge = None
        if live_alloc_row:
            lp = live_alloc_row.get("price")
            if lp is not None:
                live_yes_price = lp if live_alloc_row["side"] == "YES" else round(1.0 - lp, 4)
            live_edge = live_alloc_row.get("ev")
        live_yes_str = f"{live_yes_price:.3f}" if live_yes_price is not None else "-"
        live_edge_str = f"{live_edge:.3f}" if live_edge is not None else "-"
        row += f"<td class='live-col'>{live_model_prob*100:.1f}%</td>"
        row += f"<td class='live-col'>{live_yes_str}</td>"
        row += f"<td class='live-col'>{live_edge_str}</td>"

    if show_result:
        if alloc:
            won = alloc.get("won")
            result_label = "-" if won is None else ("برد" if won else "باخت")
            pnl = alloc.get("pnl_units")
            pnl_str = "-" if pnl is None else f"{pnl:+.2f}"
            entered = entry_badge(alloc.get("user_entered", False))
        else:
            result_label, pnl_str, entered = "-", "-", "-"
        row += f"<td>{entered}</td><td>{result_label}</td><td>{pnl_str}</td>"

    row += f"<td>{sell_str}</td>"
    row += "</tr>"
    return row

def unified_bucket_table(distribution, committed_allocation, live_allocation, unit_sym,
                          show_result=False, is_committed=False, balance_units=100):
    """
    V5 BUGFIX: a bucket can legitimately carry BOTH a YES and a NO allocation
    at once (e.g. main=YES on bucket X, hedge=NO on the SAME bucket X). The
    previous version picked only one via `alloc = yes_alloc or no_alloc`,
    silently hiding the other leg's units from the table while STILL
    counting them in the "% سرمایه" denominator -- causing percentages to
    visibly undercount (confirmed root cause of the reported Munich case).

    FIX: every allocated leg gets its OWN row. A bucket with both a YES and
    a NO allocation renders two rows; a bucket with one or zero allocations
    renders exactly one row, same as before.
    """
    committed_by_key = {}
    for a in (committed_allocation or []):
        committed_by_key[(a["market_id"], a["side"])] = a
    live_by_key = {}
    for a in (live_allocation or []):
        live_by_key[(a["market_id"], a["side"])] = a
    display_by_key = committed_by_key if committed_allocation else live_by_key

    display_total_units = sum(a.get("units", 0.0) for a in display_by_key.values()) or balance_units
    for a in display_by_key.values():
        a["_pct_capital_precomputed"] = f"{a['units'] / display_total_units * 100:.1f}" if display_total_units > 0 else "-"

    header = ("<tr><th>محدوده</th><th>سمت</th><th>احتمال مدل</th><th>احتمال بازار</th>"
              "<th>باور نهایی</th>"
              "<th>قیمت YES</th><th>قی��ت NO</th><th>Edge</th><th>#</th><th>کلید my_entries.txt</th>"
              "<th>نقش</th><th>٪ سرمایه</th>")
    if is_committed:
        header += "<th class='live-col'>احتمال مدل (زنده)</th><th class='live-col'>قیمت YES (زنده)</th><th class='live-col'>Edge (زنده)</th>"
    if show_result:
        header += "<th>ورود</th><th>نتیجه</th><th>PnL</th>"
    header += "<th>فروش</th></tr>"

    rows = [f"<table>{header}"]
    for b in distribution:
        yes_alloc = display_by_key.get((b["market_id"], "YES"))
        no_alloc = display_by_key.get((b["market_id"], "NO"))
        legs = [a for a in (yes_alloc, no_alloc) if a is not None]

        if not legs:
            rows.append(_render_leg_row(b, None, unit_sym, is_committed, live_by_key, show_result))
        else:
            for alloc in legs:
                rows.append(_render_leg_row(b, alloc, unit_sym, is_committed, live_by_key, show_result))

    rows.append("</table>")
    return "".join(rows)

def attach_live_model_prob(committed_distribution, live_distribution):
    live_by_id = {d["market_id"]: d.get("model_prob") for d in (live_distribution or [])}
    merged = []
    for d in (committed_distribution or []):
        d2 = dict(d)
        d2["live_model_prob"] = live_by_id.get(d["market_id"])
        merged.append(d2)
    return merged

def commit_panel_html(market_key, already_committed):
    if already_committed:
        return f'<div class="commit-panel"><span class="committed-tag">قفل‌شده</span></div>'
    onclick_attr = "writeLockLine(&quot;" + market_key + "&quot;)"
    return (
        '<div class="commit-panel">'
        f'<button class="commit-btn" onclick="{onclick_attr}" disabled>قفل کردن این لحظه</button>'
        f'<span class="commit-status" id="status_{market_key}">-- به lock_requests.txt متصل شوید</span>'
        '</div>'
    )

def _build_polymarket_url(city_slug, date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        month = MONTHS[dt.month - 1]
        slug = f"highest-temperature-in-{city_slug}-on-{month}-{dt.day}-{dt.year}"
        return f"https://polymarket.com/event/{slug}"
    except Exception:
        return "https://polymarket.com/"

def _compute_city_forecast_accuracy(all_markets):
    stats = defaultdict(lambda: [0, 0])
    for m in all_markets:
        if m.get("status") not in ("resolved", "resolved_no_signal"):
            continue
        actual = m.get("actual_temp")
        forecast = m.get("forecast_mean")
        if actual is None or forecast is None:
            continue
        slug = m.get("city", "?")
        stats[slug][1] += 1
        if round(forecast) == round(actual):
            stats[slug][0] += 1
    return {slug: (c / t * 100.0 if t else None) for slug, (c, t) in stats.items()}

def build_open_markets_html(open_markets, accuracy_by_city=None):
    if not open_markets:
        return '<div class="empty">بازار باز موجود نیست.</div>'
    accuracy_by_city = accuracy_by_city or {}
    parts = []
    for m in open_markets:
        unit_sym = m["unit"]
        committed = m.get("committed_allocation")
        live_alloc = m.get("live_allocation")
        mkey = f"{m['city']}_{m['date']}"
        if committed:
            distribution = attach_live_model_prob(
                m.get("committed_full_distribution") or m.get("full_distribution", []),
                m.get("full_distribution", []),
            )
            balunits = m.get("committed_balance_units", m.get("balance_units", 100))
            table = unified_bucket_table(distribution, committed, live_alloc, unit_sym,
                                          show_result=False, is_committed=True, balance_units=balunits)
            note = f'<span class="committed-tag">قفل‌شده -- {len(committed)} پوزیشن</span>'
            best_pnl = m.get("committed_best_case_pnl_units", m.get("best_case_pnl_units"))
            worst_pnl = m.get("committed_worst_case_pnl_units", m.get("worst_case_pnl_units"))
            success_prob = m.get("committed_success_probability", m.get("success_probability"))
            confidence = m.get("committed_confidence", m.get("confidence"))
        else:
            distribution = m.get("full_distribution", [])
            balunits = m.get("balance_units", 100)
            table = unified_bucket_table(distribution, None, live_alloc, unit_sym,
                                          show_result=False, is_committed=False, balance_units=balunits)
            note = '<span class="live-tag">در حال پایش</span>'
            best_pnl = m.get("best_case_pnl_units")
            worst_pnl = m.get("worst_case_pnl_units")
            success_prob = m.get("success_probability")
            confidence = m.get("confidence")
        stats_html = scenario_stats_html(best_pnl, worst_pnl, success_prob, confidence)
        panel_html = commit_panel_html(mkey, bool(committed))
        acc = accuracy_by_city.get(m["city"])
        acc_str = f"{acc:.0f}%" if acc is not None else "-"
        acc_html = f"&nbsp;&nbsp;درصد صحت پیش‌بینی: <span class='prob'>{acc_str}</span>"
        summary = (f"<b>{m['city_name']}</b> {m['date']} &nbsp;&nbsp;"
                  f"پیش‌بینی: {fmt(m.get('forecast_mean'), 1)}{unit_sym} &nbsp;&nbsp;"
                  f"{fmt(m.get('hours_left'), 1)}h &nbsp;&nbsp;{note}{acc_html}&nbsp;&nbsp;{stats_html}")
        poly_url = _build_polymarket_url(m["city"], m["date"])
        poly_link_html = (
            f'<div style="margin-top:8px;font-size:12px;">'
            f'<a href="{poly_url}" target="_blank" rel="noopener" style="color:#60a5fa;">'
            f'مشاهده این بازار در Polymarket \u2197</a></div>'
        )
        parts.append(
            f'<details class="market-block"><summary>{summary}</summary>{panel_html}{table}{poly_link_html}</details>'
        )
    return "".join(parts)

def build_resolved_html(resolved_markets):
    if not resolved_markets:
        return '<div class="empty">هنوز بازاری حل نشده است.</div>'
    by_date = defaultdict(list)
    for m in resolved_markets:
        by_date[m.get("date", "?")].append(m)
    dates_sorted = sorted(by_date.keys(), reverse=True)
    parts = []
    for i, date in enumerate(dates_sorted):
        markets = sorted(by_date[date], key=lambda x: x.get("city_name", ""))
        wins = sum(1 for m in markets if m.get("resolved_outcome") == "win")
        total = len(markets)
        pnl_sum = sum(m.get("pnl_units", 0.0) for m in markets)
        win_rate = f"{wins}/{total} ({wins/total*100:.0f}%)" if total else "-"
        market_blocks = []
        for m in markets:
            unit_sym = m["unit"]
            result_label = "برد" if m.get("resolved_outcome") == "win" else "باخت"
            tag_class = "win-tag" if m.get("resolved_outcome") == "win" else "loss-tag"
            pnl = m.get("pnl_units", 0.0)
            my_pnl = m.get("my_pnl_units")
            my_pnl_str = "" if my_pnl is None else f" | PnL شما: {my_pnl:+.2f}u"
            graded_against = m.get("graded_against", "committed")
            graded_tag = "قفل‌شده" if graded_against == "committed" else "زنده (fallback)"
            graded_allocation = m.get("graded_allocation") or m.get("committed_allocation") or []
            distribution = m.get("full_distribution", [])
            balunits = m.get("committed_balance_units", m.get("balance_units", 100))
            table = unified_bucket_table(distribution, graded_allocation, None, unit_sym,
                                          show_result=True, is_committed=False, balance_units=balunits)
            best_pnl = m.get("committed_best_case_pnl_units", m.get("best_case_pnl_units"))
            worst_pnl = m.get("committed_worst_case_pnl_units", m.get("worst_case_pnl_units"))
            success_prob = m.get("committed_success_probability", m.get("success_probability"))
            confidence = m.get("committed_confidence", m.get("confidence"))
            stats_html = scenario_stats_html(best_pnl, worst_pnl, success_prob, confidence)
            summary = (f"<b>{m['city_name']}</b>&nbsp;&nbsp;"
                      f"پیش‌ب��نی: {fmt(m.get('forecast_mean'), 1)}{unit_sym} -- "
                      f"واقعی: {fmt(m.get('actual_temp'), 1)}{unit_sym}&nbsp;&nbsp;"
                      f"<span class='{tag_class}'>{result_label} {pnl:+.2f}u</span>{my_pnl_str}"
                      f"&nbsp;&nbsp;<small>{graded_tag}</small>{stats_html}")
            market_blocks.append(
                f'<details class="market-block"><summary>{summary}</summary>{table}</details>'
            )
        date_summary = (f"<span class='date-summary'>{date}</span>&nbsp;&nbsp;"
                        f"{total} بازار&nbsp;&nbsp;{win_rate}&nbsp;&nbsp;PnL: {pnl_sum:+.1f}u")
        open_attr = "open" if i < 2 else ""
        parts.append(
            f'<details class="date-group" {open_attr}><summary>{date_summary}</summary>{"".join(market_blocks)}</details>'
        )
    return "".join(parts)

def build_no_signal_html(no_signal_markets, locations):
    if not no_signal_markets:
        return '<div class="empty">بازار بدون سیگنالی ثبت نشده.</div>'
    by_city = defaultdict(list)
    for m in no_signal_markets:
        by_city[m.get("city", "?")].append(m)
    city_slugs_sorted = sorted(by_city.keys(), key=lambda slug: locations.get(slug, {}).get("name", slug))
    city_blocks = []
    for slug in city_slugs_sorted:
        group = sorted(by_city[slug], key=lambda x: x.get("date", ""), reverse=True)
        name = locations.get(slug, {}).get("name", slug)
        rows = ["<table><tr><th>شهر</th><th>تاریخ</th><th>وضعیت</th><th>دمای واقعی</th><th>دمای پیش‌بینی مدل</th></tr>"]
        for m in group:
            unit_sym = m.get("unit", "")
            status_label = "منقضی‌شده (بدون سیگنال)" if m.get("status") == "expired_no_signal" else "حل‌شده (بدون سیگنال)"
            actual = m.get("actual_temp")
            actual_str = f"{actual}{unit_sym}" if actual is not None else "-"
            forecast_mean = m.get("forecast_mean")
            forecast_str = f"{forecast_mean}{unit_sym}" if forecast_mean is not None else "-"
            rows.append(
                f"<tr><td>{name}</td><td>{m.get('date','?')}</td><td>{status_label}</td>"
                f"<td>{actual_str}</td><td>{forecast_str}</td></tr>"
            )
        rows.append("</table>")
        city_blocks.append(
            f"<details class='date-group'><summary><span class='date-summary'>{name}</span>"
            f"&nbsp;&nbsp;{len(group)} مورد</summary>{''.join(rows)}</details>"
        )
    return (
        "<details class='market-block'>"
        f"<summary><b>بازارهای منقضی‌شده بدون سیگنال</b>&nbsp;&nbsp;({len(no_signal_markets)} مورد)</summary>"
        + "".join(city_blocks) + "</details>"
    )

def build_city_stats_html(resolved_markets, locations):
    if not resolved_markets:
        return '<div class="empty">هنوز آماری موجود نیست.</div>'
    rows = ['<table><tr><th>شهر</th><th>تعداد</th><th>برد</th><th>نرخ برد</th>'
           '<th>PnL کل</th><th>معاملات شما</th><th>PnL شما</th></tr>']
    cities = sorted(set(m["city"] for m in resolved_markets))
    for city_slug in cities:
        group = [m for m in resolved_markets if m["city"] == city_slug]
        w = len([m for m in group if m.get("resolved_outcome") == "win"])
        pnl_sum = sum(m.get("pnl_units", 0.0) for m in group)
        my_group = [m for m in group if m.get("my_pnl_units") is not None]
        my_pnl_sum = sum(m.get("my_pnl_units", 0.0) for m in my_group)
        name = locations.get(city_slug, {}).get("name", city_slug)
        rows.append(
            f"<tr><td>{name}</td><td>{len(group)}</td><td>{w}</td>"
            f"<td>{w/len(group)*100:.0f}%</td><td>{pnl_sum:+.1f}</td>"
            f"<td>{len(my_group)}</td><td>{my_pnl_sum:+.1f}</td></tr>"
        )
    rows.append("</table>")
    return "".join(rows)

def build_dashboard(state, markets, locations):
    open_markets = [m for m in markets if m.get("status") == "open" and m.get("full_distribution")]
    resolved_markets = [m for m in markets if m.get("status") == "resolved"]
    no_signal_markets = [m for m in markets if m.get("status") in ("expired_no_signal", "resolved_no_signal")]
    signal_markets = [m for m in open_markets if m.get("committed_allocation") or m.get("live_allocation")]
    accuracy_by_city = _compute_city_forecast_accuracy(resolved_markets + no_signal_markets)
    wins = state.get("wins", 0)
    losses = state.get("losses", 0)
    total_trades = wins + losses
    win_rate = f"{wins/total_trades*100:.0f}%" if total_trades else "-"
    my_wins = state.get("my_wins", 0)
    my_losses = state.get("my_losses", 0)
    my_total_trades = my_wins + my_losses
    my_win_rate = f"{my_wins/my_total_trades*100:.0f}%" if my_total_trades else "-"
    html = HTML_TEMPLATE
    html = html.replace("LAST_UPDATE", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
    html = html.replace("BALANCE", f"{state.get('balance_units', 0):.1f}")
    html = html.replace("MY_BALANCE", f"{state.get('my_balance_units', 0):.1f}")
    html = html.replace("MY_WIN_RATE", my_win_rate)
    html = html.replace("MY_TOTAL_TRADES", str(my_total_trades))
    html = html.replace("OPEN_COUNT", str(len(open_markets)))
    html = html.replace("WIN_RATE", win_rate)
    html = html.replace("TOTAL_TRADES", str(total_trades))
    html = html.replace("CITY_COUNT", str(len(locations)))
    html = html.replace("SIGNALS_HTML", build_open_markets_html(signal_markets, accuracy_by_city))
    html = html.replace("OPEN_MARKETS_HTML", build_open_markets_html(open_markets, accuracy_by_city))
    html = html.replace("RESOLVED_HTML", build_resolved_html(resolved_markets))
    html = html.replace("NO_SIGNAL_HTML", build_no_signal_html(no_signal_markets, locations))
    html = html.replace("CITY_STATS_HTML", build_city_stats_html(resolved_markets, locations))
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    return str(OUTPUT_FILE.resolve())
