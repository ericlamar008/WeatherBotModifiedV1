# WeatherBet Dashboard (React + TypeScript + Express)

Interactive Polymarket weather prediction trading and signal intelligence application rewritten in React, TypeScript, Vite, and Express.

## Features

- **Simple Dashboard Mode (`Simple`)**:
  - Real-time active locked signals panel (`سیگنال‌های قفل‌شدهٔ فعال`) with entry price, live sell price, drift percentage, local day countdown, and one-click unlock.
  - Maximum Temperature (`دمای حداکثر`) and Minimum Temperature (`دمای حداقل`) markets grouped by city and target date.
  - Plausibility filtering (`>= 5%` with toggle to show all buckets), model probability, market probability, blended belief (`باور نهایی`), EV (`ارزش مورد انتظار`), and color-coded signal badges (`🟢 سیگنال نهایی`, `🔵 محتمل‌ترین`, `🟣 ارزش بالا`).
  - Interactive Lock (`قفل کن`) and Unlock (`حذف قفل`) actions persisted to `data/locked_signals.json`.
  - Daily PnL Report (`گزارش روزانه`) grouped by closing date (Iran timezone) with average daily return and trade breakdown.
  - Full Trade History (`تاریخچهٔ معاملات`) with city/date filters and CSV export (`lock_history.csv`).
- **Pro Terminal Mode (`v3 Pro`)**:
  - Strategy summary cards (PnL balance, win rate, total trades, open markets, city count).
  - V5 Unified Bucket Table supporting simultaneous YES and NO allocations on the same bucket (`main_signal`, `hedge`, `emergency_hedge`).
  - Scenario statistics (best-case PnL, worst-case PnL, success probability, confidence) and `lock_requests.txt` commit support.
  - Resolved markets grouped by date and per-city performance statistics.
- **Accuracy & Calibration View (`Accuracy`)**:
  - Model forecast vs. actual station temperature tracking (`accuracy_report.csv`), absolute error, signed bias, and CSV download.

## Development

```bash
npm install
npm run dev
```

Runs the Express API and Vite frontend on `http://0.0.0.0:3000`.
