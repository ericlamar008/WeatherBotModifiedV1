import express from 'express';
import cors from 'cors';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { execFileSync } from 'child_process';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();
const PORT = Number(process.env.PORT) || 3000;

app.use(cors());
app.use(express.json());

const DATA_DIR = path.join(__dirname, 'data');
const MARKETS_DIR = path.join(DATA_DIR, 'markets');
const LOCKS_FILE = path.join(DATA_DIR, 'locked_signals.json');
const LAST_SCAN_FILE = path.join(DATA_DIR, 'last_scan.json');
const LOCK_HISTORY_CSV = path.join(DATA_DIR, 'lock_history.csv');
const ACCURACY_CSV = path.join(DATA_DIR, 'accuracy_report.csv');
const CALIBRATION_FILE = path.join(DATA_DIR, 'calibration.json');
const CONFIG_FILE = path.join(__dirname, 'config.json');
const LOCK_REQUESTS_FILE = path.join(DATA_DIR, 'lock_requests.txt');
const PRICE_STATUS_FILE = path.join(DATA_DIR, 'last_price_status.txt');

const CITY_NAMES: Record<string, string> = {
  'ankara': 'Ankara',
  'atlanta': 'Atlanta',
  'austin': 'Austin',
  'buenos-aires': 'Buenos Aires',
  'cairo': 'Cairo',
  'cape-town': 'Cape Town',
  'chicago': 'Chicago',
  'dallas': 'Dallas',
  'denver': 'Denver',
  'dubai': 'Dubai',
  'frankfurt': 'Frankfurt',
  'hong-kong': 'Hong Kong',
  'houston': 'Houston',
  'istanbul': 'Istanbul',
  'jeddah': 'Jeddah',
  'kuala-lumpur': 'Kuala Lumpur',
  'lagos': 'Lagos',
  'london': 'London',
  'los-angeles': 'Los Angeles',
  'madrid': 'Madrid',
  'mexico-city': 'Mexico City',
  'miami': 'Miami',
  'milan': 'Milan',
  'mumbai': 'Mumbai',
  'munich': 'Munich',
  'nyc': 'New York',
  'paris': 'Paris',
  'riyadh': 'Riyadh',
  'rome': 'Rome',
  'sao-paulo': 'São Paulo',
  'seoul': 'Seoul',
  'shanghai': 'Shanghai',
  'singapore': 'Singapore',
  'sydney': 'Sydney',
  'taipei': 'Taipei',
  'tel-aviv': 'Tel Aviv',
  'tokyo': 'Tokyo',
  'toronto': 'Toronto',
  'warsaw': 'Warsaw',
};

const NAME_TO_SLUG: Record<string, string> = {};
for (const [slug, name] of Object.entries(CITY_NAMES)) {
  NAME_TO_SLUG[name.toLowerCase()] = slug;
}

const US_CITIES = new Set([
  'atlanta',
  'austin',
  'chicago',
  'dallas',
  'denver',
  'houston',
  'los-angeles',
  'miami',
  'nyc',
]);

function getCityName(slug: string, fallback?: string): string {
  if (fallback && fallback !== slug) return fallback;
  if (CITY_NAMES[slug]) return CITY_NAMES[slug];
  return slug
    .split('-')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

function getCitySlugFromName(name: string): string {
  const lower = name.trim().toLowerCase();
  if (NAME_TO_SLUG[lower]) return NAME_TO_SLUG[lower];
  return lower.replace(/\s+/g, '-');
}

function formatRangeLabel(range: [number, number] | undefined, unit: string): string {
  if (!range || range.length < 2) return '-';
  const [low, high] = range;
  if (low <= -998 && high >= 998) return `?${unit}`;
  if (low <= -998) return `≤${high}${unit}`;
  if (high >= 998) return `≥${low}${unit}`;
  if (low === high) return `${low}${unit}`;
  return `${low}-${high}${unit}`;
}

/**
 * Correct bucket hit test:
 * - For US Fahrenheit markets (2°F bucket width, actual_temp stored as midpoint X.5):
 *   Any forecast within ±1.0°F of midpoint (e.g. 68.0..69.0 around 68.5) hits the bucket.
 * - For Celsius markets (1°C bucket width, actual_temp stored as integer X.0):
 *   round(forecast) === round(actual) (or within ±0.5°C).
 */
function isExactBucketHit(forecast: number, actual: number, isFahrenheit: boolean): boolean {
  if (isFahrenheit) {
    return Math.abs(forecast - actual) <= 1.0;
  }
  return Math.round(forecast) === Math.round(actual);
}

function isWithinOneBucketHit(forecast: number, actual: number, isFahrenheit: boolean): boolean {
  if (isFahrenheit) {
    return Math.abs(forecast - actual) <= 2.5;
  }
  return Math.abs(forecast - actual) <= 1.25;
}

/**
 * Student-t CDF (df = 5) approximation for recomputing bucket probabilities
 * around the smart gated forecast mean.
 */
function studentTCdf5(t: number): number {
  const df = 5.0;
  const x = t / Math.sqrt(df);
  const u = 1 / (1 + x * x);
  const c =
    0.5 +
    (x *
      Math.sqrt(u) *
      (1 + 0.5 * u + 0.375 * u * u)) /
      (Math.PI / 2);
  // Exact closed form for df=5:
  // F(t) = 0.5 + (1 / pi) * [ atan(t / sqrt(5)) + (t * sqrt(5)) / (5 + t^2) * (1 + 2 / (3 * (1 + t^2 / 5))) ]
  const s5 = Math.sqrt(5);
  const z = t / s5;
  const denom = 1 + z * z;
  const exact =
    0.5 +
    (1 / Math.PI) *
      (Math.atan(z) + (z / denom) * (1 + 2 / (3 * denom)));
  if (isNaN(exact)) return c;
  return Math.max(0.0001, Math.min(0.9999, exact));
}

function parseCsvLine(line: string): string[] {
  const result: string[] = [];
  let current = '';
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') {
      inQuotes = !inQuotes;
    } else if (ch === ',' && !inQuotes) {
      result.push(current.trim());
      current = '';
    } else {
      current += ch;
    }
  }
  result.push(current.trim());
  return result;
}

interface MarketCache {
  all: any[];
  lastLoaded: number;
}

const marketCache: MarketCache = {
  all: [],
  lastLoaded: 0,
};

function loadAllMarkets(force = false): any[] {
  const now = Date.now();
  if (!force && marketCache.all.length > 0 && now - marketCache.lastLoaded < 30000) {
    return marketCache.all;
  }
  if (!fs.existsSync(MARKETS_DIR)) return [];
  const files = fs.readdirSync(MARKETS_DIR).filter((f) => f.endsWith('.json'));
  const results: any[] = [];
  for (const file of files) {
    try {
      const filePath = path.join(MARKETS_DIR, file);
      const raw = fs.readFileSync(filePath, 'utf-8');
      const data = JSON.parse(raw);
      const isMin = file.endsWith('_min.json');
      data.market_type = isMin ? 'min' : 'max';
      data.file_key = file.replace('.json', '');
      data.city_name = getCityName(data.city, data.city_name);
      results.push(data);
    } catch {}
  }
  marketCache.all = results;
  marketCache.lastLoaded = now;
  return results;
}

/**
 * SMART CALIBRATION & CITY FORM ANALYZER
 * Evaluates all 26 cities across recent 14 days vs older history,
 * compares calibrated vs shadow vs sub-models (ECMWF, GFS, GEM, ICON),
 * detects seasonal sign-flips, and decides the profit-only gate action.
 */
function buildCityFormProfiles(allMarkets: any[]): Record<string, any> {
  const profiles: Record<string, any> = {};

  // 1. Parse accuracy_report.csv
  interface AccRecord {
    city: string;
    city_name: string;
    date: string;
    market_type: 'max' | 'min';
    forecast_mean: number;
    actual_temp: number;
    abs_error: number;
    signed_bias: number; // actual - forecast (or csv signed bias)
  }

  const accRecords: AccRecord[] = [];
  if (fs.existsSync(ACCURACY_CSV)) {
    try {
      const raw = fs.readFileSync(ACCURACY_CSV, 'utf-8').replace(/^\uFEFF/, '');
      const lines = raw.split(/\r?\n/).filter((l) => l.trim().length > 0);
      for (let i = 1; i < lines.length; i++) {
        const cols = parseCsvLine(lines[i]);
        if (cols.length >= 9 && cols[4] !== '' && cols[6] !== '') {
          const cityName = cols[0];
          const slug = getCitySlugFromName(cityName);
          const mtype: 'max' | 'min' = cols[2] === 'کمینه' ? 'min' : 'max';
          const fVal = Number(cols[4]);
          const aVal = Number(cols[6]);
          const absErr = cols[7] !== '' ? Number(cols[7]) : Math.abs(fVal - aVal);
          const signedBias = aVal - fVal; // positive means actual was warmer than forecast
          if (!isNaN(fVal) && !isNaN(aVal)) {
            accRecords.push({
              city: slug,
              city_name: getCityName(slug, cityName),
              date: cols[1],
              market_type: mtype,
              forecast_mean: fVal,
              actual_temp: aVal,
              abs_error: absErr,
              signed_bias: signedBias,
            });
          }
        }
      }
    } catch {}
  }

  // 2. Collect shadow & sub-model comparisons from resolved market JSON files
  const shadowByKey: Record<
    string,
    Array<{
      date: string;
      f_cal: number;
      f_shadow: number | null;
      actual: number;
      models: Record<string, number>;
    }>
  > = {};

  for (const m of allMarkets) {
    if (m.actual_temp == null || m.forecast_mean == null) continue;
    const key = `${m.city}|${m.market_type || 'max'}`;
    if (!shadowByKey[key]) shadowByKey[key] = [];
    shadowByKey[key].push({
      date: m.date || '',
      f_cal: Number(m.forecast_mean_legacy ?? m.forecast_mean),
      f_shadow: m.forecast_mean_shadow != null ? Number(m.forecast_mean_shadow) : null,
      actual: Number(m.actual_temp),
      models: m.model_means_raw || {},
    });
  }

  // Load calibration.json to blend verified 14d/55d stats for cities with < 10 local market JSONs
  let calData: Record<string, any> = {};
  const calPath = path.join(DATA_DIR, 'calibration.json');
  if (fs.existsSync(calPath)) {
    try {
      calData = JSON.parse(fs.readFileSync(calPath, 'utf-8'));
    } catch {}
  }

  // Group accuracy records by (city|market_type)
  const groupedAcc: Record<string, AccRecord[]> = {};
  for (const rec of accRecords) {
    const key = `${rec.city}|${rec.market_type}`;
    if (!groupedAcc[key]) groupedAcc[key] = [];
    groupedAcc[key].push(rec);
  }

  for (const [key, records] of Object.entries(groupedAcc)) {
    const [city, mtype] = key.split('|') as [string, 'max' | 'min'];
    const isUs = US_CITIES.has(city);
    const unit: 'F' | 'C' = isUs ? 'F' : 'C';
    const sorted = [...records].sort((a, b) => a.date.localeCompare(b.date));

    // Recent window: >= 2026-10-10 (last 15 days)
    const recentRecs = sorted.filter((r) => r.date >= '2026-10-10');
    const oldRecs = sorted.filter((r) => r.date < '2026-10-05');
    const last5Recs = sorted.slice(-5);
    const prev30Recs = sorted.slice(-35, -5);

    const calcMetrics = (arr: AccRecord[]) => {
      if (arr.length === 0) return null;
      const hits = arr.filter((r) => isExactBucketHit(r.forecast_mean, r.actual_temp, isUs)).length;
      const within1 = arr.filter((r) => isWithinOneBucketHit(r.forecast_mean, r.actual_temp, isUs)).length;
      const mae = arr.reduce((s, r) => s + r.abs_error, 0) / arr.length;
      const bias = arr.reduce((s, r) => s + r.signed_bias, 0) / arr.length;
      return {
        n: arr.length,
        hits,
        within1,
        hit_pct: Number(((hits / arr.length) * 100).toFixed(1)),
        within1_pct: Number(((within1 / arr.length) * 100).toFixed(1)),
        mae: Number(mae.toFixed(2)),
        bias: Number(bias.toFixed(2)),
      };
    };

    const activeRecentArr = recentRecs.length >= 3 ? recentRecs : sorted.slice(-10);
    const recStats = calcMetrics(activeRecentArr)!;
    let oldStats = calcMetrics(oldRecs);

    // Blend with verified 14-day & 55-day statistics in data/calibration.json when local market JSONs are < 10
    const calKey = mtype === 'min' ? `${city}_min_D0` : `${city}_D0`;
    const calEntry = calData[calKey];
    if (calEntry && recStats.n < 10) {
      const calMae = Number(calEntry.live_mae ?? calEntry.base_mae ?? recStats.mae);
      const calNTotal = Number(calEntry.n_live ?? calEntry.n_samples ?? 30);
      const calNRec = Math.max(Number(calEntry.recent_14d_n ?? 14), 10);
      const sEff = Math.max(0.28, calMae / 0.9488);
      const w0 = isUs ? 1.0 : 0.52;
      const w1 = isUs ? 3.0 : 1.52;
      const calHitProb = Math.max(0.15, Math.min(0.92, 2.0 * studentTCdf5(w0 / sEff) - 1.0));
      const calW1Prob = Math.max(calHitProb, Math.min(0.99, 2.0 * studentTCdf5(w1 / sEff) - 1.0));

      const priorRecN = Math.max(0, calNRec - recStats.n);
      const totalRecN = recStats.n + priorRecN;
      if (totalRecN > 0) {
        const sumErr = activeRecentArr.reduce((s, r) => s + r.abs_error, 0) + priorRecN * calMae;
        const calRecBias = -Number(calEntry.recent_14d_bias ?? calEntry.live_bias_raw ?? 0);
        const sumBias = activeRecentArr.reduce((s, r) => s + r.signed_bias, 0) + priorRecN * calRecBias;
        recStats.mae = Number((sumErr / totalRecN).toFixed(2));
        recStats.bias = Number((sumBias / totalRecN).toFixed(2));
        recStats.hit_pct = Number((((recStats.hits + priorRecN * calHitProb) / totalRecN) * 100).toFixed(1));
        recStats.within1_pct = Number((((recStats.within1 + priorRecN * calW1Prob) / totalRecN) * 100).toFixed(1));
        recStats.n = totalRecN;
      }

      if (!oldStats && calNTotal > totalRecN) {
        const oldN = calNTotal - activeRecentArr.length;
        const oldMae = Number(Number(calEntry.base_mae ?? calMae).toFixed(2));
        const sOld = Math.max(0.28, oldMae / 0.9488);
        const oldHitPct = Number((Math.max(0.15, Math.min(0.92, 2.0 * studentTCdf5(w0 / sOld) - 1.0)) * 100).toFixed(1));
        const oldBias = Number((-Number(calEntry.live_bias_raw ?? calEntry.base_bias_raw ?? 0)).toFixed(2));
        oldStats = {
          n: oldN,
          hits: Math.round((oldHitPct / 100) * oldN),
          within1: Math.round(0.9 * oldN),
          hit_pct: oldHitPct,
          within1_pct: 90.0,
          mae: oldMae,
          bias: oldBias,
        };
      }
    }

    // Check seasonal sign flip (5d bias vs previous 30d bias)
    const bias5d =
      last5Recs.length > 0
        ? last5Recs.reduce((s, r) => s + r.signed_bias, 0) / last5Recs.length
        : 0;
    const bias30d =
      prev30Recs.length > 0
        ? prev30Recs.reduce((s, r) => s + r.signed_bias, 0) / prev30Recs.length
        : 0;
    const signFlipDetected =
      prev30Recs.length >= 8 &&
      Math.abs(bias5d) >= (isUs ? 0.6 : 0.35) &&
      Math.abs(bias30d) >= (isUs ? 0.4 : 0.25) &&
      bias5d * bias30d < 0;

    // Compare Calibrated vs Shadow & Sub-models
    const mktItems = (shadowByKey[key] || []).filter((x) => x.date >= '2026-09-25');
    const shadowItems = mktItems.filter((x) => x.f_shadow != null);

    let rawShadowMae: number | null = null;
    let calibratedMae: number | null = null;
    let rawShadowHitPct: number | null = null;
    let calibratedHitPct: number | null = null;

    if (shadowItems.length >= 2) {
      rawShadowMae = Number(
        (shadowItems.reduce((s, x) => s + Math.abs((x.f_shadow as number) - x.actual), 0) / shadowItems.length).toFixed(2)
      );
      calibratedMae = Number(
        (shadowItems.reduce((s, x) => s + Math.abs(x.f_cal - x.actual), 0) / shadowItems.length).toFixed(2)
      );
      rawShadowHitPct = Number(
        (
          (shadowItems.filter((x) => isExactBucketHit(x.f_shadow as number, x.actual, isUs)).length /
            shadowItems.length) *
          100
        ).toFixed(1)
      );
      calibratedHitPct = Number(
        (
          (shadowItems.filter((x) => isExactBucketHit(x.f_cal, x.actual, isUs)).length / shadowItems.length) *
          100
        ).toFixed(1)
      );
    }

    // Sub-model errors & inverse-MAE^2 weights
    const submodelErrs: Record<string, number[]> = {};
    for (const item of mktItems) {
      for (const [mname, val] of Object.entries(item.models || {})) {
        if (val != null && !isNaN(Number(val))) {
          if (!submodelErrs[mname]) submodelErrs[mname] = [];
          submodelErrs[mname].push(Math.abs(Number(val) - item.actual));
        }
      }
    }

    const submodelMaes: Record<string, number> = {};
    for (const [mname, errs] of Object.entries(submodelErrs)) {
      if (errs.length >= 2) {
        submodelMaes[mname] = Number((errs.reduce((a, b) => a + b, 0) / errs.length).toFixed(2));
      }
    }

    let bestSubmodel: string | null = null;
    let worstSubmodel: string | null = null;
    const submodelWeights: Record<string, number> = {};

    const subEntries = Object.entries(submodelMaes).sort((a, b) => a[1] - b[1]);
    if (subEntries.length > 0) {
      bestSubmodel = subEntries[0][0];
      worstSubmodel = subEntries[subEntries.length - 1][0];
      const minMae = Math.max(0.25, subEntries[0][1]);
      let weightSum = 0;
      for (const [mname, mae] of subEntries) {
        // Outlier pruning: if a sub-model's MAE is > 1.35x the best model, heavily penalize it
        const ratio = mae / minMae;
        const penalty = ratio > 1.35 ? 0.15 : ratio > 1.18 ? 0.55 : 1.0;
        const w = (1 / (mae * mae + 0.12)) * penalty;
        submodelWeights[mname] = w;
        weightSum += w;
      }
      if (weightSum > 0) {
        for (const mname of Object.keys(submodelWeights)) {
          submodelWeights[mname] = Number((submodelWeights[mname] / weightSum).toFixed(3));
        }
      }
    }

    // Decide Gate Action ("Profit-Only Calibration Gate")
    let gateDecision: 'use_calibrated' | 'revert_to_shadow' | 'smart_submodel_blend' | 'sign_flip_damped' =
      'use_calibrated';
    let gateReason = 'کالیبراسیون فعلی خطا را کاهش داده و سودمند است';

    if (rawShadowMae != null && calibratedMae != null) {
      if (rawShadowMae < calibratedMae * 0.96 || (rawShadowHitPct != null && calibratedHitPct != null && rawShadowHitPct > calibratedHitPct + 8)) {
        gateDecision = 'revert_to_shadow';
        gateReason = `مدل Shadow دقیق‌تر از کالیبره است (MAE ${rawShadowMae} در برابر ${calibratedMae} — حذف اورکورکشن)`;
      } else if (bestSubmodel && worstSubmodel && submodelMaes[worstSubmodel] > submodelMaes[bestSubmodel] * 1.4) {
        gateDecision = 'smart_submodel_blend';
        gateReason = `حذف اثر مخرب مدل ${worstSubmodel.toUpperCase()} و وزن‌دهی بالا به ${bestSubmodel.toUpperCase()}`;
      }
    } else if (signFlipDetected) {
      gateDecision = 'sign_flip_damped';
      gateReason = `ترمز تغییر فصل فعال شد (چرخش بایاس از ${bias30d > 0 ? '+' : ''}${bias30d.toFixed(2)} به ${bias5d > 0 ? '+' : ''}${bias5d.toFixed(2)})`;
    }

    // Determine City Form Status
    const hitDelta = oldStats ? Number((recStats.hit_pct - oldStats.hit_pct).toFixed(1)) : null;
    let formStatus: 'hot' | 'stable' | 'cold_drop' | 'weak' = 'stable';
    let formLabel = '⚖️ متعادل';
    let tradingAdvice = 'قابل بررسی با رعایت EV';

    if (
      recStats.hit_pct >= 50 ||
      (recStats.hit_pct >= 45 && recStats.mae <= (isUs ? 1.15 : 0.65)) ||
      (gateDecision === 'revert_to_shadow' && (rawShadowHitPct || 0) >= 55)
    ) {
      formStatus = 'hot';
      const effectiveHit =
        gateDecision === 'revert_to_shadow' && rawShadowHitPct != null
          ? Math.max(recStats.hit_pct, rawShadowHitPct)
          : recStats.hit_pct;
      formLabel = `🔥 روی فرم (${effectiveHit}%)`;
      tradingAdvice = 'اولویت بالا برای قفل سیگنال ✅';
    } else if (oldStats && oldStats.hit_pct >= 38 && recStats.hit_pct <= oldStats.hit_pct - 14) {
      formStatus = 'cold_drop';
      formLabel = `⚠️ افت اخیر (${oldStats.hit_pct}% ← ${recStats.hit_pct}%)`;
      tradingAdvice =
        gateDecision === 'revert_to_shadow'
          ? 'اصلاح‌شده با گیت Shadow 🛡️'
          : 'احتیاط — تغییر فصل / نوسان مدل ⚠️';
    } else if (recStats.hit_pct < 28) {
      formStatus = 'weak';
      formLabel = `🔴 دقت پایین (${recStats.hit_pct}%)`;
      tradingAdvice = 'قرنطینه موقت — عدم ورود ⛔';
    } else {
      formStatus = 'stable';
      formLabel = `⚖️ متعادل (${recStats.hit_pct}%)`;
      tradingAdvice = recStats.within1_pct >= 80 ? 'مناسب استراتژی ۲ باکتی (نردبانی)' : 'معمولی';
    }

    profiles[key] = {
      city,
      city_name: records[0]?.city_name || getCityName(city),
      market_type: mtype,
      unit,
      is_us: isUs,
      recent_n: recStats.n,
      recent_hit_pct: recStats.hit_pct,
      recent_within1_pct: recStats.within1_pct,
      recent_mae: recStats.mae,
      recent_bias: recStats.bias,
      old_n: oldStats?.n || 0,
      old_hit_pct: oldStats?.hit_pct ?? null,
      old_mae: oldStats?.mae ?? null,
      old_bias: oldStats?.bias ?? null,
      hit_delta_pct: hitDelta,
      form_status: formStatus,
      form_label: formLabel,
      raw_shadow_mae: rawShadowMae,
      calibrated_mae: calibratedMae,
      raw_shadow_hit_pct: rawShadowHitPct,
      calibrated_hit_pct: calibratedHitPct,
      gate_decision: gateDecision,
      gate_reason: gateReason,
      sign_flip_detected: signFlipDetected,
      best_submodel: bestSubmodel,
      worst_submodel: worstSubmodel,
      submodel_maes: submodelMaes,
      submodel_weights: submodelWeights,
      trading_advice: tradingAdvice,
    };
  }

  return profiles;
}

/**
 * Applies the Smart Champion-Challenger Gate to an open market:
 * Computes smart_forecast_mean, recomputes bucket probabilities if the gate overrides legacy forecast_mean,
 * and identifies smart_main_market_id.
 */
function enrichMarketWithSmartGate(market: any, profile: any | undefined): any {
  const m = { ...market, city_form: profile || null };
  const fCal = m.forecast_mean;
  const fShadow = m.forecast_mean_shadow;
  const modelsRaw = m.model_means_raw || {};
  const sigma = Math.max(0.55, Number(m.sigma || (m.unit === 'F' ? 1.5 : 0.85)));

  let smartMean = fCal;
  let gateDecision = profile?.gate_decision || 'use_calibrated';
  let gateNote = profile?.gate_reason || 'کالیبراسیون استاندارد';

  if (fCal != null && profile) {
    if (profile.gate_decision === 'revert_to_shadow' && fShadow != null) {
      // Also blend with inverse-MAE weighted sub-models if available
      let subWeighted: number | null = null;
      if (profile.submodel_weights && Object.keys(modelsRaw).length > 0) {
        let wSum = 0;
        let vSum = 0;
        for (const [mk, mv] of Object.entries(modelsRaw)) {
          const w = profile.submodel_weights[mk];
          if (w && mv != null) {
            vSum += Number(mv) * w;
            wSum += w;
          }
        }
        if (wSum > 0) subWeighted = vSum / wSum;
      }
      smartMean = subWeighted != null ? Number((0.65 * fShadow + 0.35 * subWeighted).toFixed(2)) : Number(fShadow.toFixed(2));
    } else if (profile.gate_decision === 'smart_submodel_blend' && Object.keys(modelsRaw).length > 0) {
      let wSum = 0;
      let vSum = 0;
      for (const [mk, mv] of Object.entries(modelsRaw)) {
        const w = profile.submodel_weights?.[mk] ?? 0.25;
        if (mv != null) {
          vSum += Number(mv) * w;
          wSum += w;
        }
      }
      if (wSum > 0) {
        const weightedRaw = vSum / wSum;
        smartMean = Number((0.5 * fCal + 0.5 * weightedRaw).toFixed(2));
      }
    } else if (profile.gate_decision === 'sign_flip_damped') {
      // Damp the recent bias correction by 60%
      if (fShadow != null) {
        smartMean = Number((0.5 * fCal + 0.5 * fShadow).toFixed(2));
      } else {
        smartMean = Number((fCal + profile.recent_bias * 0.4).toFixed(2));
      }
    }
  }

  m.smart_forecast_mean = smartMean;
  m.smart_sigma = sigma;
  m.smart_gate_decision = gateDecision;
  m.smart_gate_note = gateNote;

  // Build baseline lookup from committed_full_distribution or live_allocation if scan-over-scan deltas are missing
  const committedDistById = new Map<string, any>();
  if (Array.isArray(m.committed_full_distribution)) {
    for (const cb of m.committed_full_distribution) {
      if (cb && cb.market_id != null) committedDistById.set(String(cb.market_id), cb);
    }
  }
  const allocPriceById = new Map<string, number>();
  const allocList = Array.isArray(m.committed_allocation) && m.committed_allocation.length > 0
    ? m.committed_allocation
    : Array.isArray(m.live_allocation)
    ? m.live_allocation
    : [];
  for (const leg of allocList) {
    if (leg && leg.market_id != null && leg.price != null) {
      const pYes = leg.side === 'NO' ? Number((1 - Number(leg.price)).toFixed(4)) : Number(leg.price);
      allocPriceById.set(String(leg.market_id), pYes);
    }
  }

  // Recompute bucket smart probabilities and percentage changes if smartMean is available
  if (Array.isArray(m.full_distribution) && smartMean != null) {
    const rawProbs: number[] = [];
    const shadowRawProbs: number[] = [];
    const eliminatedFlags: boolean[] = [];
    const shadowRef = fShadow != null ? Number(fShadow) : Number(fCal ?? smartMean);
    const isMin = m.market_type === 'min';
    const obsBound = m.observed_intraday_temp != null ? Number(m.observed_intraday_temp) : null;
    const unit = m.unit || 'C';
    const compSigma = Math.max(unit === 'C' ? 0.55 : 0.95, sigma * 0.78);
    const validModels = Object.entries(modelsRaw).filter(([, v]) => v != null && !isNaN(Number(v))) as [string, number][];
    const rawConsensus =
      validModels.length > 0 ? validModels.reduce((s, [, v]) => s + Number(v), 0) / validModels.length : null;
    const calShift = rawConsensus != null ? smartMean - rawConsensus : 0.0;
    const bmaWeights = profile?.submodel_weights || {};

    for (const b of m.full_distribution) {
      const [low, high] = b.range || [-999, 999];
      let alreadyElim = Boolean(b.intraday_eliminated);
      if (!alreadyElim && obsBound != null && !isNaN(obsBound)) {
        if (!isMin && high < 998 && high + 0.49 < obsBound) alreadyElim = true;
        else if (isMin && low > -998 && low - 0.49 > obsBound) alreadyElim = true;
      }

      if (alreadyElim) {
        eliminatedFlags.push(true);
        rawProbs.push(0.0);
        shadowRawProbs.push(0.0);
        continue;
      }

      eliminatedFlags.push(false);
      const lowerBound = low <= -998 ? -Infinity : low - 0.5;
      const upperBound = high >= 998 ? Infinity : high + 0.5;
      const cdfHigh = upperBound === Infinity ? 1.0 : studentTCdf5((upperBound - smartMean) / sigma);
      const cdfLow = lowerBound === -Infinity ? 0.0 : studentTCdf5((lowerBound - smartMean) / sigma);
      const pCons = Math.max(0.0005, cdfHigh - cdfLow);

      let pFinal = pCons;
      if (validModels.length >= 2) {
        let pMixSub = 0.0;
        let wTot = 0.0;
        for (const [mk, mv] of validModels) {
          const w = Number(bmaWeights[mk] ?? 1.0 / validModels.length);
          const mLoc = Number(mv) + calShift;
          const chM = upperBound === Infinity ? 1.0 : studentTCdf5((upperBound - mLoc) / compSigma);
          const clM = lowerBound === -Infinity ? 0.0 : studentTCdf5((lowerBound - mLoc) / compSigma);
          pMixSub += w * Math.max(0.0005, chM - clM);
          wTot += w;
        }
        if (wTot > 0) {
          pMixSub /= wTot;
          pFinal = 0.55 * pCons + 0.45 * pMixSub;
        }
      }
      rawProbs.push(Math.max(0.0005, pFinal));

      const sHigh = upperBound === Infinity ? 1.0 : studentTCdf5((upperBound - shadowRef) / sigma);
      const sLow = lowerBound === -Infinity ? 0.0 : studentTCdf5((lowerBound - shadowRef) / sigma);
      shadowRawProbs.push(Math.max(0.0005, sHigh - sLow));
    }
    const probSum = rawProbs.reduce((a, b) => a + b, 0) || 1.0;
    const shadowSum = shadowRawProbs.reduce((a, b) => a + b, 0) || 1.0;

    let bestBucketId: string | null = null;
    let bestBelief = -1;

    m.full_distribution = m.full_distribution.map((b: any, idx: number) => {
      const midStr = String(b.market_id);
      const isElim = eliminatedFlags[idx];
      const smartModelProb = isElim ? 0.0 : Number((rawProbs[idx] / probSum).toFixed(4));
      const shadowModelProb = isElim ? 0.0 : Number((shadowRawProbs[idx] / shadowSum).toFixed(4));
      const legacyModelProb = Number(b.model_prob ?? smartModelProb);
      const yesPrice = Number(b.yes_price ?? b.ask ?? 0.001);
      const smartBeliefProb = isElim ? 0.0 : Number((0.5 * smartModelProb + 0.5 * yesPrice).toFixed(4));
      const smartEv = isElim ? -1.0 : yesPrice > 0.001 ? Number(((smartBeliefProb - yesPrice) / yesPrice).toFixed(4)) : b.ev;

      if (!isElim && smartBeliefProb > bestBelief) {
        bestBelief = smartBeliefProb;
        bestBucketId = midStr;
      }

      // 1. Model probability changes: both previous scan (10m/smart) and 6-hour scan (6h)
      let modelProb6hChange: number | null =
        b.model_prob_6h_change != null && Math.abs(Number(b.model_prob_6h_change)) >= 0.0005
          ? Number(b.model_prob_6h_change)
          : b.model_prob_change != null
          ? Number(b.model_prob_change)
          : null;
      const committedBucket = committedDistById.get(midStr);
      if ((modelProb6hChange == null || Math.abs(modelProb6hChange) < 0.0005) && b.prev_scan6h_model_prob != null) {
        modelProb6hChange = Number((legacyModelProb - Number(b.prev_scan6h_model_prob)).toFixed(4));
      }
      if ((modelProb6hChange == null || Math.abs(modelProb6hChange) < 0.0005) && committedBucket?.model_prob != null) {
        modelProb6hChange = Number((legacyModelProb - Number(committedBucket.model_prob)).toFixed(4));
      }
      if ((modelProb6hChange == null || Math.abs(modelProb6hChange) < 0.0005) && fShadow != null && Math.abs(legacyModelProb - shadowModelProb) >= 0.0005) {
        modelProb6hChange = Number((legacyModelProb - shadowModelProb).toFixed(4));
      }
      if (modelProb6hChange == null) {
        modelProb6hChange = 0.0;
      }

      const modelProb10mChange: number =
        b.model_prob_10m_change != null
          ? Number(b.model_prob_10m_change)
          : b.model_prob_change != null
          ? Number(b.model_prob_change)
          : modelProb6hChange;

      let smartModelProb6hChange: number = modelProb6hChange;
      if (b.prev_scan6h_model_prob != null && Math.abs(smartModelProb - Number(b.prev_scan6h_model_prob)) >= 0.0005) {
        smartModelProb6hChange = Number((smartModelProb - Number(b.prev_scan6h_model_prob)).toFixed(4));
      } else if (Math.abs(smartModelProb6hChange) < 0.0005 && Math.abs(smartModelProb - legacyModelProb) >= 0.0005) {
        smartModelProb6hChange = Number((smartModelProb - legacyModelProb).toFixed(4));
      }

      let smartModelProbChange: number = modelProb10mChange;
      if (Math.abs(smartModelProb - legacyModelProb) >= 0.0005) {
        smartModelProbChange = Number((smartModelProb - legacyModelProb).toFixed(4));
      }

      // 2. Price & Market probability change (from previous 6-hour scan scan6h_yes_price/ask, or 30m refresh, or committed/allocation baseline)
      let yesPriceChange: number | null =
        b.yes_price_change != null && Math.abs(Number(b.yes_price_change)) >= 0.0005
          ? Number(b.yes_price_change)
          : b.yes_price_6h_change != null && Math.abs(Number(b.yes_price_6h_change)) >= 0.0005
          ? Number(b.yes_price_6h_change)
          : null;

      // Fallback to 6-hour scan snapshot stored in scan6h_yes_price or ask
      if (yesPriceChange == null || Math.abs(yesPriceChange) < 0.0005) {
        const ref6hPrice = b.scan6h_yes_price != null ? Number(b.scan6h_yes_price) : b.ask != null ? Number(b.ask) : null;
        if (ref6hPrice != null && Math.abs(yesPrice - ref6hPrice) >= 0.0005) {
          yesPriceChange = Number((yesPrice - ref6hPrice).toFixed(4));
        }
      }
      if ((yesPriceChange == null || Math.abs(yesPriceChange) < 0.0005) && committedBucket?.yes_price != null) {
        yesPriceChange = Number((yesPrice - Number(committedBucket.yes_price)).toFixed(4));
      }
      if ((yesPriceChange == null || Math.abs(yesPriceChange) < 0.0005) && allocPriceById.has(midStr)) {
        const entryP = allocPriceById.get(midStr)!;
        yesPriceChange = Number((yesPrice - entryP).toFixed(4));
      }
      if (yesPriceChange == null) {
        yesPriceChange = 0.0;
      }

      // Relative percentage change in YES price (e.g. 0.20 -> 0.25 = +25.0%)
      let yesPricePctChange: number | null =
        b.yes_price_pct_change != null && Math.abs(Number(b.yes_price_pct_change)) >= 0.05
          ? Number(b.yes_price_pct_change)
          : b.yes_price_6h_pct_change != null && Math.abs(Number(b.yes_price_6h_pct_change)) >= 0.05
          ? Number(b.yes_price_6h_pct_change)
          : null;
      if (yesPricePctChange == null && yesPriceChange != null) {
        const prevPrice =
          b.prev_scan6h_yes_price != null
            ? Number(b.prev_scan6h_yes_price)
            : b.prev_yes_price != null
            ? Number(b.prev_yes_price)
            : yesPrice - yesPriceChange;
        if (prevPrice > 0.001 && Math.abs(yesPriceChange) >= 0.0005) {
          yesPricePctChange = Number(((yesPriceChange / prevPrice) * 100).toFixed(1));
        } else if (Math.abs(yesPriceChange) >= 0.0005) {
          yesPricePctChange = Number((yesPriceChange * 100).toFixed(1));
        } else {
          yesPricePctChange = 0.0;
        }
      }

      // Also compute explicit 10m and 6h price/model changes so the UI can show both in 6h scans
      const yesPrice10mChange =
        b.yes_price_10m_change != null ? Number(b.yes_price_10m_change) : yesPriceChange;
      const yesPrice10mPctChange =
        b.yes_price_10m_pct_change != null ? Number(b.yes_price_10m_pct_change) : yesPricePctChange;
      const yesPrice6hChange =
        b.yes_price_6h_change != null ? Number(b.yes_price_6h_change) : yesPriceChange;
      const yesPrice6hPctChange =
        b.yes_price_6h_pct_change != null ? Number(b.yes_price_6h_pct_change) : yesPricePctChange;

      return {
        ...b,
        model_prob_change: modelProb10mChange,
        model_prob_10m_change: modelProb10mChange,
        model_prob_6h_change: modelProb6hChange,
        smart_model_prob: smartModelProb,
        smart_model_prob_change: smartModelProbChange,
        smart_model_prob_6h_change: smartModelProb6hChange,
        smart_belief_prob: smartBeliefProb,
        smart_ev: smartEv,
        yes_price_change: yesPriceChange,
        yes_price_pct_change: yesPricePctChange,
        yes_price_10m_change: yesPrice10mChange,
        yes_price_10m_pct_change: yesPrice10mPctChange,
        yes_price_6h_change: yesPrice6hChange,
        yes_price_6h_pct_change: yesPrice6hPctChange,
      };
    });

    m.smart_main_market_id = bestBucketId;
    m.full_distribution = m.full_distribution.map((b: any) => ({
      ...b,
      is_smart_main: String(b.market_id) === bestBucketId,
    }));
  }

  return m;
}

/**
 * Syncs and updates data/calibration.json so that:
 * 1) US cities stuck on 'historical_backfill' (austin_D1, dallas_D1, houston_D1, atlanta_D0, miami_D0, nyc_D0)
 *    are synced with live 14-day blended calibration.
 * 2) Cities with seasonal sign-flip or calibration degradation have their bias damped so future scans never over-correct.
 */
function applySmartCalibrationToFile(profiles: Record<string, any>): { updated_keys: string[]; total_keys: number } {
  if (!fs.existsSync(CALIBRATION_FILE)) return { updated_keys: [], total_keys: 0 };
  try {
    const cal = JSON.parse(fs.readFileSync(CALIBRATION_FILE, 'utf-8'));
    const updatedKeys: string[] = [];
    const nowIso = new Date().toISOString();

    for (const [key, entry] of Object.entries<any>(cal)) {
      const match = key.match(/^(.+)_D([0-3])$/);
      if (!match) continue;
      const baseSlug = match[1];
      const isMin = baseSlug.endsWith('_min');
      const citySlug = isMin ? baseSlug.slice(0, -4) : baseSlug;
      const mtype = isMin ? 'min' : 'max';
      const prof = profiles[`${citySlug}|${mtype}`];
      if (!prof) continue;

      let modified = false;

      // Fix 1: Upgrade stale historical_backfill entries on D0/D1 using recent 14d metrics
      if (entry.source === 'historical_backfill' && (key.endsWith('_D0') || key.endsWith('_D1'))) {
        entry.source = 'smart_gated_14d';
        entry.n_live = prof.recent_n;
        entry.live_mae = prof.recent_mae;
        entry.sigma = Math.max(0.55, prof.recent_mae);
        entry.bias = prof.sign_flip_detected ? 0.0 : Number((-prof.recent_bias * 0.5).toFixed(3));
        entry.updated_at = nowIso;
        modified = true;
      }

      // Fix 2: If gate detected over-correction or sign-flip, damp the bias so it cannot hurt accuracy
      if (prof.gate_decision === 'revert_to_shadow' || prof.sign_flip_detected) {
        const oldBias = Number(entry.bias || 0);
        const dampedBias = prof.sign_flip_detected ? 0.0 : Number((oldBias * 0.35).toFixed(3));
        if (Math.abs(oldBias - dampedBias) > 0.05) {
          entry.bias_legacy = oldBias;
          entry.bias = dampedBias;
          entry.gate_status = prof.gate_decision;
          entry.updated_at = nowIso;
          modified = true;
        }
      }

      if (modified) {
        updatedKeys.push(key);
      }
    }

    if (updatedKeys.length > 0) {
      fs.writeFileSync(CALIBRATION_FILE, JSON.stringify(cal, null, 2), 'utf-8');
    }
    return { updated_keys: updatedKeys, total_keys: Object.keys(cal).length };
  } catch (e) {
    console.error('Error updating calibration.json:', e);
    return { updated_keys: [], total_keys: 0 };
  }
}

function loadLocks(): any[] {
  if (!fs.existsSync(LOCKS_FILE)) return [];
  try {
    return JSON.parse(fs.readFileSync(LOCKS_FILE, 'utf-8'));
  } catch {
    return [];
  }
}

function saveLocks(locks: any[]): void {
  try {
    fs.writeFileSync(LOCKS_FILE, JSON.stringify(locks, null, 2), 'utf-8');
  } catch (e) {
    console.error('Failed to save locks:', e);
  }
}

function toIranDateStr(isoStr: string): string {
  try {
    const dt = new Date(isoStr);
    if (isNaN(dt.getTime())) return isoStr.slice(0, 10);
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Tehran',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(dt);
  } catch {
    return isoStr.slice(0, 10);
  }
}

function toIranDateTimeStr(isoStr: string): string {
  try {
    const dt = new Date(isoStr);
    if (isNaN(dt.getTime())) return isoStr;
    const datePart = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Tehran',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(dt);
    const timePart = new Intl.DateTimeFormat('en-GB', {
      timeZone: 'Asia/Tehran',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(dt);
    return `${datePart} ${timePart}`;
  } catch {
    return isoStr;
  }
}

// GET /api/dashboard-data
app.get('/api/dashboard-data', (_req, res) => {
  try {
    const allMarkets = loadAllMarkets();
    const locks = loadLocks();
    const cityFormProfiles = buildCityFormProfiles(allMarkets);

    // Scan info
    let lastScan: { lite?: string; full?: string } = {};
    if (fs.existsSync(LAST_SCAN_FILE)) {
      try {
        lastScan = JSON.parse(fs.readFileSync(LAST_SCAN_FILE, 'utf-8'));
      } catch {}
    }

    let latestScanIso = '';
    let latestScanKind = '';
    const liteTime = lastScan.lite ? new Date(lastScan.lite).getTime() : 0;
    const fullTime = lastScan.full ? new Date(lastScan.full).getTime() : 0;
    if (liteTime >= fullTime && lastScan.lite) {
      latestScanIso = lastScan.lite;
      latestScanKind = 'اسکن سبک';
    } else if (lastScan.full) {
      latestScanIso = lastScan.full;
      latestScanKind = 'اسکن کامل';
    }

    const lastUpdateIran = latestScanIso
      ? toIranDateTimeStr(latestScanIso)
      : toIranDateTimeStr(new Date().toISOString());

    // Open markets enriched with Smart Calibration Gate
    const openMaxMarkets = allMarkets
      .filter(
        (m) =>
          m.market_type === 'max' &&
          m.status === 'open' &&
          Array.isArray(m.full_distribution) &&
          m.full_distribution.length > 0
      )
      .map((m) => enrichMarketWithSmartGate(m, cityFormProfiles[`${m.city}|max`]));

    const openMinMarkets = allMarkets
      .filter(
        (m) =>
          m.market_type === 'min' &&
          m.status === 'open' &&
          Array.isArray(m.full_distribution) &&
          m.full_distribution.length > 0
      )
      .map((m) => enrichMarketWithSmartGate(m, cityFormProfiles[`${m.city}|min`]));

    // Market index by key for quick lookup
    const marketByKey = new Map<string, any>();
    for (const m of allMarkets) {
      const suffix = m.market_type === 'min' ? '_min' : '';
      marketByKey.set(`${m.city}_${m.date}${suffix}`, m);
    }

    // Compute per-city forecast accuracy using the FIXED 2°F / 1°C bucket rule
    const cityAccCounts: Record<string, [number, number]> = {};
    for (const m of allMarkets) {
      if (m.status !== 'resolved' && m.status !== 'resolved_no_signal') continue;
      if (m.actual_temp == null || m.forecast_mean == null) continue;
      const slug = m.city || '?';
      const isF = m.unit === 'F' || US_CITIES.has(slug);
      if (!cityAccCounts[slug]) cityAccCounts[slug] = [0, 0];
      cityAccCounts[slug][1] += 1;
      if (isExactBucketHit(Number(m.forecast_mean), Number(m.actual_temp), isF)) {
        cityAccCounts[slug][0] += 1;
      }
    }
    const accuracyByCity: Record<string, number | null> = {};
    for (const [slug, [correct, total]] of Object.entries(cityAccCounts)) {
      const profMax = cityFormProfiles[`${slug}|max`];
      // Prefer recent 14d accuracy when available, else overall fixed bucket accuracy
      accuracyByCity[slug] =
        profMax?.recent_hit_pct != null
          ? Math.round(profMax.recent_hit_pct)
          : total > 0
          ? Math.round((correct / total) * 100)
          : null;
    }

    const roundHalfUp = (val: number): number =>
      val >= 0 ? Math.floor(val + 0.5) : -Math.floor(Math.abs(val) + 0.5);
    const toCents = (p: number | null | undefined): number | null =>
      p == null ? null : roundHalfUp(Number(p) * 100);
    const calcCentConsistentPct = (entryP: number | null | undefined, currP: number | null | undefined): number | null => {
      const ec = toCents(entryP);
      const cc = toCents(currP);
      if (ec == null || cc == null || ec <= 0) return null;
      const diff = cc - ec;
      if (diff === 0) return 0.0;
      return Number(((diff / ec) * 100).toFixed(1));
    };

    // Enrich open locks with bucket label & market info
    const enrichedLocks = locks.map((l) => {
      const isMin = l.market_type === 'min';
      const suffix = isMin ? '_min' : '';
      const mkt = marketByKey.get(`${l.city}_${l.date}${suffix}`) || marketByKey.get(`${l.city}_${l.date}`);
      const unitSym = mkt?.unit || '';
      let bucketLabel = '-';
      let bucketObj: any = null;
      if (mkt && Array.isArray(mkt.full_distribution)) {
        bucketObj = mkt.full_distribution.find((item: any) => String(item.market_id) === String(l.market_id));
        if (bucketObj && bucketObj.range) {
          bucketLabel = formatRangeLabel(bucketObj.range, unitSym);
        }
      }

      // Zero-movement guard: if last_mid equals entry_price, don't let bid-ask spread report a fake -2%
      let effectiveLastPrice = l.last_price;
      if (
        l.status === 'open' &&
        l.entry_price != null &&
        l.last_mid != null &&
        Math.abs(Number(l.last_mid) - Number(l.entry_price)) < 0.0025 &&
        l.entry_bid == null
      ) {
        effectiveLastPrice = Number(l.entry_price);
      }

      const consistentLastPct =
        effectiveLastPrice != null && l.entry_price != null
          ? calcCentConsistentPct(Number(l.entry_price), Number(effectiveLastPrice))
          : l.last_pct ?? null;

      const rawMp = bucketObj?.smart_model_prob ?? bucketObj?.model_prob;
      const fairProb = rawMp != null ? (l.side === 'NO' ? 1 - Number(rawMp) : Number(rawMp)) : null;
      const fairCents = fairProb != null ? roundHalfUp(fairProb * 100) : l.model_fair_cents ?? null;

      return {
        ...l,
        last_price: effectiveLastPrice,
        last_pct: consistentLastPct,
        city_name: getCityName(l.city, mkt?.city_name),
        bucket_label: bucketLabel,
        unit: unitSym,
        hours_left: mkt?.hours_left ?? null,
        time_status: mkt?.time_status ?? null,
        market_status: mkt?.status ?? 'open',
        forecast_mean: mkt?.forecast_mean ?? null,
        model_fair_cents: fairCents,
        best_bid: bucketObj?.best_bid ?? effectiveLastPrice ?? null,
        best_ask: bucketObj?.best_ask ?? null,
      };
    });

    const closedStatuses = new Set([
      'closed_manual',
      'closed_resolved',
      'closed_auto_tp',
      'closed_auto_sl',
      'closed_auto_metar',
      'closed_auto_trailing',
    ]);

    // Daily PnL grouped by Iran closing date
    const dailyMap: Record<string, any[]> = {};
    for (const l of enrichedLocks) {
      if (!closedStatuses.has(l.status)) continue;
      if (!l.closed_at || l.entry_price == null || l.exit_price == null) continue;
      const iranDate = toIranDateStr(l.closed_at);
      if (!dailyMap[iranDate]) dailyMap[iranDate] = [];
      const pct = calcCentConsistentPct(Number(l.entry_price), Number(l.exit_price)) ?? 0;
      dailyMap[iranDate].push({
        ...l,
        outcome_pct: pct,
      });
    }

    const dailyPnl = Object.keys(dailyMap)
      .sort((a, b) => b.localeCompare(a))
      .map((date) => {
        const items = dailyMap[date];
        const avgPct =
          items.length > 0 ? items.reduce((acc, it) => acc + (it.outcome_pct || 0), 0) / items.length : 0;
        return {
          date,
          avg_pct: avgPct,
          count: items.length,
          items,
        };
      });

    // Resolved markets
    const resolvedMarketsAll = allMarkets.filter((m) => m.status === 'resolved');
    const noSignalMarketsAll = allMarkets.filter(
      (m) => m.status === 'expired_no_signal' || m.status === 'resolved_no_signal'
    );

    let totalWins = 0;
    let totalLosses = 0;
    let totalPnlUnits = 0;
    let myWins = 0;
    let myLosses = 0;
    let myPnlUnits = 0;

    const cityStatsMap: Record<
      string,
      {
        city: string;
        city_name: string;
        total: number;
        wins: number;
        pnl_sum: number;
        my_trades: number;
        my_pnl_sum: number;
        accuracy_pct: number | null;
        recent_accuracy_pct: number | null;
        form_status: string;
      }
    > = {};

    for (const m of resolvedMarketsAll) {
      const isWin = m.resolved_outcome === 'win';
      if (isWin) totalWins++;
      else totalLosses++;
      const pnl = Number(m.pnl_units || 0);
      totalPnlUnits += pnl;

      const slug = m.city || 'unknown';
      const profMax = cityFormProfiles[`${slug}|max`];
      if (!cityStatsMap[slug]) {
        cityStatsMap[slug] = {
          city: slug,
          city_name: getCityName(slug, m.city_name),
          total: 0,
          wins: 0,
          pnl_sum: 0,
          my_trades: 0,
          my_pnl_sum: 0,
          accuracy_pct: accuracyByCity[slug] ?? null,
          recent_accuracy_pct: profMax?.recent_hit_pct ?? null,
          form_status: profMax?.form_status ?? 'stable',
        };
      }
      cityStatsMap[slug].total += 1;
      if (isWin) cityStatsMap[slug].wins += 1;
      cityStatsMap[slug].pnl_sum += pnl;

      if (m.my_pnl_units != null) {
        const myPnl = Number(m.my_pnl_units);
        myPnlUnits += myPnl;
        if (myPnl >= 0) myWins++;
        else myLosses++;
        cityStatsMap[slug].my_trades += 1;
        cityStatsMap[slug].my_pnl_sum += myPnl;
      }
    }

    const cityStats = Object.values(cityStatsMap).sort((a, b) => a.city_name.localeCompare(b.city_name));
    const sortedResolved = [...resolvedMarketsAll].sort((a, b) =>
      String(b.date || '').localeCompare(String(a.date || ''))
    );

    let config = {};
    if (fs.existsSync(CONFIG_FILE)) {
      try {
        config = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf-8'));
      } catch {}
    }

    let lastPriceStatus = '';
    if (fs.existsSync(PRICE_STATUS_FILE)) {
      try {
        lastPriceStatus = fs.readFileSync(PRICE_STATUS_FILE, 'utf-8');
      } catch {}
    }

    res.json({
      meta: {
        last_update_iran: lastUpdateIran,
        latest_scan_iso: latestScanIso,
        latest_scan_kind: latestScanKind,
        last_scan: lastScan,
        total_markets_files: allMarkets.length,
        open_max_count: openMaxMarkets.length,
        open_min_count: openMinMarkets.length,
        city_count: Object.keys(CITY_NAMES).length,
      },
      summary: {
        balance_units: Number((100 + totalPnlUnits).toFixed(1)),
        pnl_units: Number(totalPnlUnits.toFixed(1)),
        wins: totalWins,
        losses: totalLosses,
        total_trades: totalWins + totalLosses,
        win_rate: totalWins + totalLosses > 0 ? Math.round((totalWins / (totalWins + totalLosses)) * 100) : 0,
        my_balance_units: Number((100 + myPnlUnits).toFixed(1)),
        my_wins: myWins,
        my_losses: myLosses,
        my_total_trades: myWins + myLosses,
        open_count: openMaxMarkets.length + openMinMarkets.length,
      },
      open_max_markets: openMaxMarkets,
      open_min_markets: openMinMarkets,
      locks: enrichedLocks,
      daily_pnl: dailyPnl,
      resolved_markets: sortedResolved.slice(0, 120),
      no_signal_markets: noSignalMarketsAll
        .sort((a, b) => String(b.date || '').localeCompare(String(a.date || '')))
        .slice(0, 150)
        .map((m) => ({
          city: m.city,
          city_name: m.city_name,
          date: m.date,
          unit: m.unit,
          status: m.status,
          actual_temp: m.actual_temp,
          forecast_mean: m.forecast_mean,
          market_type: m.market_type,
        })),
      city_stats: cityStats,
      accuracy_by_city: accuracyByCity,
      city_form_profiles: cityFormProfiles,
      config,
      last_price_status: lastPriceStatus,
    });
  } catch (err: any) {
    console.error('Error in /api/dashboard-data:', err);
    res.status(500).json({ error: err.message || 'Internal Server Error' });
  }
});

// POST /api/recalibrate - Run Smart Calibration Gate & update calibration.json
app.post('/api/recalibrate', (_req, res) => {
  try {
    const allMarkets = loadAllMarkets(true);
    const profiles = buildCityFormProfiles(allMarkets);
    const result = applySmartCalibrationToFile(profiles);
    res.json({
      ok: true,
      message: `کالیبراسیون هوشمند ۱۴ روزه و ترمز تغییر فصل روی ${result.updated_keys.length} کلید اعمال شد.`,
      ...result,
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// POST /api/refresh-prices - Reload market cache & rebuild static simple.html
app.post('/api/refresh-prices', (_req, res) => {
  try {
    loadAllMarkets(true);
    res.json({ ok: true, refreshed_at: new Date().toISOString() });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// Serve simple.html directly when requested
app.get('/simple.html', (_req, res) => {
  const simplePath = path.join(__dirname, 'simple.html');
  if (fs.existsSync(simplePath)) {
    res.sendFile(simplePath);
  } else {
    res.redirect('/');
  }
});

// GET /api/history
app.get('/api/history', (_req, res) => {
  try {
    if (!fs.existsSync(LOCK_HISTORY_CSV)) {
      res.json({ rows: [], cities: [], dates: [] });
      return;
    }
    const raw = fs.readFileSync(LOCK_HISTORY_CSV, 'utf-8').replace(/^\uFEFF/, '');
    const lines = raw.split(/\r?\n/).filter((l) => l.trim().length > 0);
    const rows: any[] = [];
    for (let i = 1; i < lines.length; i++) {
      const cols = parseCsvLine(lines[i]);
      if (cols.length >= 7) {
        rows.push({
          date: cols[0],
          city_name: cols[1],
          signal_label: cols[2],
          final_temp: cols[3] || '-',
          buy_cents: cols[4] ? Number(cols[4]) : null,
          sell_cents: cols[5] ? Number(cols[5]) : null,
          outcome_pct: cols[6] !== '' ? Number(cols[6]) : null,
        });
      }
    }
    const cities = Array.from(new Set(rows.map((r) => r.city_name))).sort();
    const dates = Array.from(new Set(rows.map((r) => r.date))).sort((a, b) => b.localeCompare(a));
    res.json({ rows, cities, dates });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/accuracy
app.get('/api/accuracy', (_req, res) => {
  try {
    if (!fs.existsSync(ACCURACY_CSV)) {
      res.json({ rows: [], calibration: {} });
      return;
    }
    const raw = fs.readFileSync(ACCURACY_CSV, 'utf-8').replace(/^\uFEFF/, '');
    const lines = raw.split(/\r?\n/).filter((l) => l.trim().length > 0);
    const rows: any[] = [];
    for (let i = 1; i < Math.min(lines.length, 450); i++) {
      const cols = parseCsvLine(lines[i]);
      if (cols.length >= 10) {
        rows.push({
          city_name: cols[0],
          date: cols[1],
          market_type: cols[2],
          horizon_days: cols[3],
          forecast_mean: cols[4] ? Number(cols[4]) : null,
          sigma: cols[5] ? Number(cols[5]) : null,
          actual_temp: cols[6] ? Number(cols[6]) : null,
          abs_error: cols[7] ? Number(cols[7]) : null,
          signed_bias: cols[8] ? Number(cols[8]) : null,
          outcome: cols[9],
          pnl_units: cols[10] ? Number(cols[10]) : null,
          confidence: cols[11] ? Number(cols[11]) : null,
          success_prob: cols[12] ? Number(cols[12]) : null,
        });
      }
    }

    let calibration = {};
    if (fs.existsSync(CALIBRATION_FILE)) {
      try {
        calibration = JSON.parse(fs.readFileSync(CALIBRATION_FILE, 'utf-8'));
      } catch {}
    }

    res.json({ rows, calibration, total_lines: lines.length - 1 });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// Helper to sync lock events to the transactional SQLite WAL Audit DB (data/auto_exit_audit.db)
function syncLockToAuditDb(lockObj: any, eventType: string) {
  try {
    const pyCode = `
import sys, json
from auto_exit_bot import AutoExitAuditDB
lock_data = json.loads(sys.argv[1])
ev_type = sys.argv[2]
AutoExitAuditDB().sync_lock_record(lock_data, ev_type)
`;
    execFile('python3', ['-c', pyCode, JSON.stringify(lockObj), eventType], { cwd: __dirname, timeout: 5000 }, () => {});
  } catch {}
}

// GET /api/polymarket/account-status - Check MetaMask L1/L2 & Proxy Wallet Readiness
app.get('/api/polymarket/account-status', (_req, res) => {
  try {
    const pk = (process.env.POLY_PRIVATE_KEY || '').trim();
    const funder = (process.env.POLY_FUNDER_ADDRESS || '').trim();
    const sigType = Number(process.env.POLY_SIGNATURE_TYPE ?? (funder ? 2 : 0));
    const chainId = Number(process.env.POLY_CHAIN_ID || 137);
    const hasPk = pk.length >= 64;
    const hasFunder = funder.startsWith('0x') && funder.length >= 40;
    const sigLabel =
      sigType === 2
        ? 'MetaMask Gnosis Safe Proxy (Type 2)'
        : sigType === 0
        ? 'MetaMask Direct EOA (Type 0)'
        : 'Magic/Email Proxy (Type 1)';
    const funderPreview = hasFunder
      ? `${funder.slice(0, 6)}...${funder.slice(-4)}`
      : 'تنظیم‌نشده (در فایل .env سرور قرار می‌گیرد)';
    const liveReady = hasPk;

    res.json({
      live_ready: liveReady,
      has_private_key: hasPk,
      has_funder_address: hasFunder,
      funder_preview: funderPreview,
      signature_type: sigType,
      signature_label: sigLabel,
      chain_id: chainId,
      py_clob_installed: true,
      recommended_mode: liveReady ? 'LIVE' : 'SHADOW',
      status_text: liveReady
        ? `✅ متصل به حساب متامسک (${sigLabel})`
        : '🛡️ حالت ایمن SHADOW / PAPER فعال است (کلید خصوصی متامسک پس از انتقال به سرور در .env قرار می‌گیرد)',
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// POST /api/locks - Register an Order (Immediate or Conditional Limit Buy) & Auto-Exit Position
app.post('/api/locks', (req, res) => {
  try {
    const {
      city,
      date,
      market_id,
      token_id,
      side = 'YES',
      price,
      limit_buy_price,
      entry_order_type = 'IMMEDIATE_MARKETABLE',
      smart_autopilot = true,
      market_type = 'max',
      execution_mode = 'SHADOW',
      exit_policy = 'MARKETABLE_FAK',
      quantity_shares = 100,
      usdc_amount,
      auto_exit_enabled = true,
      tp_enabled = true,
      tp_price,
      tp_pct,
      sl_enabled = true,
      sl_price,
      sl_pct,
      max_slippage_pct = 2.0,
      tick_size = 0.01,
      trailing_stop_enabled = false,
      trailing_offset_pct = 8.0,
      metar_auto_stop = true,
      auto_sell_99c = true,
    } = req.body;

    if (!city || !date || !market_id) {
      res.status(400).json({ error: 'فیلدهای الزامی (city, date, market_id) ارسال نشده‌اند.' });
      return;
    }

    const isConditionalLimit = entry_order_type === 'CONDITIONAL_LIMIT_GTC';
    const effectiveEntryPrice = isConditionalLimit && limit_buy_price != null ? Number(limit_buy_price) : Number(price);

    if (!effectiveEntryPrice || effectiveEntryPrice <= 0 || effectiveEntryPrice >= 1) {
      res.status(400).json({ error: 'قیمت ورود باید بین 0.001 و 0.999 دلار (۱ تا ۹۹.۹ سنت) باشد.' });
      return;
    }

    const qty = Number(quantity_shares) || 0;
    if (qty <= 0) {
      res.status(400).json({ error: 'تعداد سهم (Quantity) باید بزرگ‌تر از صفر باشد.' });
      return;
    }

    const resolvedTpPrice =
      tp_enabled && tp_price != null
        ? Number(tp_price)
        : tp_enabled && tp_pct != null
        ? Math.min(0.995, Number((effectiveEntryPrice * (1 + Number(tp_pct) / 100)).toFixed(4)))
        : null;

    const resolvedSlPrice =
      sl_enabled && sl_price != null
        ? Number(sl_price)
        : sl_enabled && sl_pct != null
        ? Math.max(0.005, Number((effectiveEntryPrice * (1 - Math.abs(Number(sl_pct)) / 100)).toFixed(4)))
        : null;

    // Master Spec Safety Invariant #2: 0 < SL < Entry < TP < 1
    if (auto_exit_enabled) {
      if (sl_enabled && resolvedSlPrice != null) {
        if (resolvedSlPrice <= 0 || resolvedSlPrice >= effectiveEntryPrice) {
          res.status(400).json({
            error: `حد ضرر (${(resolvedSlPrice * 100).toFixed(1)}¢) باید کمتر از قیمت ورود (${(effectiveEntryPrice * 100).toFixed(1)}¢) و بزرگ‌تر از صفر باشد.`,
          });
          return;
        }
      }
      if (tp_enabled && resolvedTpPrice != null) {
        if (resolvedTpPrice >= 1 || resolvedTpPrice <= effectiveEntryPrice) {
          res.status(400).json({
            error: `حد سود (${(resolvedTpPrice * 100).toFixed(1)}¢) باید بالاتر از قیمت ورود (${(effectiveEntryPrice * 100).toFixed(1)}¢) و کمتر از ۱۰۰¢ باشد.`,
          });
          return;
        }
      }
    }

    const locks = loadLocks();
    const existingOpen = locks.find(
      (l) =>
        l.status === 'open' &&
        l.city === city &&
        l.date === date &&
        String(l.market_id) === String(market_id) &&
        (l.market_type || 'max') === market_type
    );

    if (existingOpen) {
      res.json({ ok: true, message: 'این باکت قبلاً در لیست پوزیشن‌های فعال قرار دارد.', lock: existingOpen });
      return;
    }

    const maxIssue = locks.reduce((max, l) => Math.max(max, Number(l.issue_number || 0)), 0);
    const nowIso = new Date().toISOString();
    const computedUsdc = usdc_amount != null ? Number(usdc_amount) : Number((qty * effectiveEntryPrice).toFixed(2));
    const computedTpPct =
      resolvedTpPrice != null
        ? Number((((resolvedTpPrice - effectiveEntryPrice) / effectiveEntryPrice) * 100).toFixed(1))
        : null;
    const computedSlPct =
      resolvedSlPrice != null
        ? Number((((effectiveEntryPrice - resolvedSlPrice) / effectiveEntryPrice) * 100).toFixed(1))
        : null;

    const initialState = isConditionalLimit ? 'ENTRY_PENDING' : 'WATCHING';

    const newLock = {
      issue_number: maxIssue + 1,
      position_id: `pos_${city}_${date}_${String(market_id)}_${maxIssue + 1}`,
      city,
      date,
      market_id: String(market_id),
      market_type: market_type || 'max',
      token_id: token_id || '',
      side,
      entry_price: effectiveEntryPrice,
      limit_buy_price: isConditionalLimit ? effectiveEntryPrice : null,
      entry_order_type: isConditionalLimit ? 'CONDITIONAL_LIMIT_GTC' : 'IMMEDIATE_MARKETABLE',
      smart_autopilot: Boolean(smart_autopilot),
      neg_risk: true,
      locked_at: nowIso,
      status: 'open',
      last_price: Number(price) || effectiveEntryPrice,
      last_pct: 0.0,
      exit_price: null,
      closed_at: null,
      close_reason: null,
      actual_temp: null,
      last_price_source: 'dashboard',
      last_mid: Number(price) || effectiveEntryPrice,
      last_checked_at: nowIso,
      // Auto-Exit Bot v3.0 Master Spec fields
      execution_mode,
      position_state: initialState,
      exit_policy,
      quantity_shares: qty,
      filled_shares: isConditionalLimit ? 0 : qty,
      usdc_amount: computedUsdc,
      auto_exit_enabled: Boolean(auto_exit_enabled),
      tp_enabled: Boolean(tp_enabled),
      tp_price: resolvedTpPrice,
      tp_pct: computedTpPct,
      sl_enabled: Boolean(sl_enabled),
      sl_price: resolvedSlPrice,
      sl_pct: computedSlPct,
      max_slippage_pct: Number(max_slippage_pct) || 2.0,
      tick_size: Number(tick_size) || 0.01,
      trailing_stop_enabled: Boolean(trailing_stop_enabled),
      trailing_offset_pct: Number(trailing_offset_pct) || 8.0,
      peak_bid: effectiveEntryPrice,
      metar_auto_stop: Boolean(metar_auto_stop),
      auto_sell_99c: Boolean(auto_sell_99c),
      rule_version: 1,
      latched_exit_reason: null,
    };

    locks.push(newLock);
    saveLocks(locks);
    syncLockToAuditDb(newLock, isConditionalLimit ? 'CREATE_LIMIT_BUY_ORDER' : 'CREATE_MARKETABLE_POSITION');

    try {
      const reqLine = `${city}_${date}|${nowIso}\n`;
      fs.appendFileSync(LOCK_REQUESTS_FILE, reqLine, 'utf-8');
    } catch {}

    res.json({
      ok: true,
      message: isConditionalLimit
        ? `⏳ سفارش خرید شرطی [${execution_mode}] در قیمت ${(effectiveEntryPrice * 100).toFixed(1)}¢ ثبت شد (وضعیت: ENTRY_PENDING — به محض پر شدن، TP/SL خودکار مسلح می‌شود).`
        : `⚡ خرید فوری [${execution_mode}] انجام شد و چتر خروج خودکار TP/SL فعال گردید (وضعیت: WATCHING).`,
      lock: newLock,
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// PATCH /api/locks/rules - Versioned Update of Entry/Exit Rules (Master Spec Safety Invariant #5)
app.patch('/api/locks/rules', (req, res) => {
  try {
    const {
      city,
      date,
      market_id,
      source_version,
      execution_mode,
      entry_order_type,
      limit_buy_price,
      smart_autopilot,
      exit_policy,
      quantity_shares,
      usdc_amount,
      auto_exit_enabled,
      tp_enabled,
      tp_price,
      sl_enabled,
      sl_price,
      max_slippage_pct,
      trailing_stop_enabled,
      trailing_offset_pct,
      metar_auto_stop,
      auto_sell_99c,
    } = req.body;

    const locks = loadLocks();
    const target = locks.find(
      (l) =>
        l.status === 'open' &&
        l.city === city &&
        l.date === date &&
        String(l.market_id) === String(market_id)
    );

    if (!target) {
      res.status(404).json({ error: 'پوزیشن باز یافت نشد.' });
      return;
    }

    const currentVer = Number(target.rule_version || 1);
    if (source_version != null && Number(source_version) < currentVer) {
      res.status(409).json({
        error: `خطای همزمانی (Rule Version Race): نسخه ارسالی (${source_version}) قدیمی‌تر از نسخه فعلی (${currentVer}) است.`,
      });
      return;
    }

    if (target.position_state === 'ENTRY_PENDING' && limit_buy_price != null && Number(limit_buy_price) > 0) {
      target.limit_buy_price = Number(limit_buy_price);
      target.entry_price = Number(limit_buy_price);
    }

    const entryPrice = Number(target.entry_price);
    const nextTpPrice = tp_enabled && tp_price != null ? Number(tp_price) : null;
    const nextSlPrice = sl_enabled && sl_price != null ? Number(sl_price) : null;

    if (auto_exit_enabled) {
      if (sl_enabled && nextSlPrice != null && (nextSlPrice <= 0 || nextSlPrice >= entryPrice)) {
        res.status(400).json({ error: 'حد ضرر (SL) باید کمتر از قیمت ورود و بزرگ‌تر از صفر باشد.' });
        return;
      }
      if (tp_enabled && nextTpPrice != null && (nextTpPrice >= 1 || nextTpPrice <= entryPrice)) {
        res.status(400).json({ error: 'حد سود (TP) باید بالاتر از قیمت ورود و کمتر از ۱.۰۰ باشد.' });
        return;
      }
    }

    target.execution_mode = execution_mode ?? target.execution_mode ?? 'SHADOW';
    if (entry_order_type != null) target.entry_order_type = entry_order_type;
    if (smart_autopilot != null) target.smart_autopilot = Boolean(smart_autopilot);
    target.exit_policy = exit_policy ?? target.exit_policy ?? 'MARKETABLE_FAK';
    if (quantity_shares != null && Number(quantity_shares) > 0) {
      target.quantity_shares = Number(quantity_shares);
      target.usdc_amount =
        usdc_amount != null ? Number(usdc_amount) : Number((Number(quantity_shares) * entryPrice).toFixed(2));
    }
    target.auto_exit_enabled = Boolean(auto_exit_enabled);
    target.tp_enabled = Boolean(tp_enabled);
    target.tp_price = nextTpPrice;
    target.tp_pct =
      nextTpPrice != null ? Number((((nextTpPrice - entryPrice) / entryPrice) * 100).toFixed(1)) : null;
    target.sl_enabled = Boolean(sl_enabled);
    target.sl_price = nextSlPrice;
    target.sl_pct =
      nextSlPrice != null ? Number((((entryPrice - nextSlPrice) / entryPrice) * 100).toFixed(1)) : null;
    if (max_slippage_pct != null) target.max_slippage_pct = Number(max_slippage_pct);
    if (trailing_stop_enabled != null) target.trailing_stop_enabled = Boolean(trailing_stop_enabled);
    if (trailing_offset_pct != null) target.trailing_offset_pct = Number(trailing_offset_pct);
    if (metar_auto_stop != null) target.metar_auto_stop = Boolean(metar_auto_stop);
    if (auto_sell_99c != null) target.auto_sell_99c = Boolean(auto_sell_99c);
    target.rule_version = currentVer + 1;
    target.last_checked_at = new Date().toISOString();

    saveLocks(locks);
    syncLockToAuditDb(target, `UPDATE_RULES_V${target.rule_version}`);
    res.json({
      ok: true,
      message: `قوانین پوزیشن با موفقیت به نسخه v${target.rule_version} ارتقا یافت.`,
      lock: target,
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// POST /api/locks/simulate-tick - Evaluate Market Tick against EntryTriggerEngine & TriggerEngine
app.post('/api/locks/simulate-tick', (req, res) => {
  try {
    const { city, date, market_id, simulated_bid, force_metar_eliminated = false } = req.body;
    const locks = loadLocks();
    const target = locks.find(
      (l) =>
        l.status === 'open' &&
        l.city === city &&
        l.date === date &&
        String(l.market_id) === String(market_id)
    );

    if (!target) {
      res.status(404).json({ error: 'پوزیشن فعال یافت نشد.' });
      return;
    }

    const bid = Number(simulated_bid);
    if (isNaN(bid) || bid <= 0 || bid >= 1) {
      res.status(400).json({ error: 'قیمت شبیه‌سازی‌شده باید بین 0.001 و 0.999 باشد.' });
      return;
    }

    const nowIso = new Date().toISOString();

    // Case A: Position is in ENTRY_PENDING (Conditional Limit Buy waiting to fill)
    if (target.position_state === 'ENTRY_PENDING') {
      if (force_metar_eliminated && target.metar_auto_stop !== false && (target.side || 'YES') === 'YES') {
        target.status = 'cancelled_entry';
        target.position_state = 'ENTRY_CANCELLED';
        target.latched_exit_reason = 'ENTRY_METAR_CANCELLED';
        target.closed_at = nowIso;
        target.close_reason = 'entry_metar_cancelled';
        saveLocks(locks);
        syncLockToAuditDb(target, 'ENTRY_METAR_CANCELLED');
        res.json({
          ok: true,
          triggered: true,
          blocked: false,
          reason: 'ENTRY_METAR_CANCELLED',
          message: `🌡️ دمای واقعی فرودگاه (METAR) از سقف این باکت عبور کرد — سفارش خرید شرطی شما به‌طور خودکار لغو (CANCEL) شد تا وارد باکت سوخته نشوید!`,
          lock: target,
        });
        return;
      }

      const limitBuyPrice = Number(target.limit_buy_price ?? target.entry_price);
      if (bid <= limitBuyPrice) {
        target.entry_price = bid;
        target.last_price = bid;
        target.last_mid = bid;
        target.last_pct = 0.0;
        target.peak_bid = bid;
        target.filled_shares = target.quantity_shares;
        target.position_state = 'WATCHING';
        target.last_checked_at = nowIso;
        saveLocks(locks);
        syncLockToAuditDb(target, 'ENTRY_LIMIT_FILLED_ARMED');
        res.json({
          ok: true,
          triggered: false,
          entry_filled: true,
          reason: 'ENTRY_LIMIT_FILLED',
          message: `✅ قیمت بازار (${(bid * 100).toFixed(1)}¢) به قیمت خرید شرطی شما (${(limitBuyPrice * 100).toFixed(1)}¢) رسید! سفارش خرید پر شد و وضعیت پوزیشن به WATCHING (مسلح به TP/SL) تغییر یافت.`,
          lock: target,
        });
        return;
      }

      target.last_price = bid;
      target.last_checked_at = nowIso;
      saveLocks(locks);
      res.json({
        ok: true,
        triggered: false,
        message: `⏳ قیمت فعلی (${(bid * 100).toFixed(1)}¢) هنوز بالاتر از قیمت خرید شرطی شما (${(limitBuyPrice * 100).toFixed(1)}¢) است — سفارش خرید همچنان در انتظار (ENTRY_PENDING) است.`,
        lock: target,
      });
      return;
    }

    // Case B: Active Position (WATCHING / ARMED / PARTIALLY_EXITED)
    const entry = Number(target.entry_price);
    target.last_price = bid;
    target.last_mid = bid;
    target.last_checked_at = nowIso;
    target.last_pct = Number((((bid - entry) / entry) * 100).toFixed(1));

    if (target.peak_bid == null || bid > Number(target.peak_bid)) {
      target.peak_bid = bid;
    }

    let triggeredReason: string | null = null;
    let newStatus = 'open';

    if (force_metar_eliminated && target.metar_auto_stop !== false && (target.side || 'YES') === 'YES') {
      triggeredReason = 'METAR_ELIMINATED';
      newStatus = 'closed_auto_metar';
    } else if (target.auto_sell_99c !== false && bid >= 0.995) {
      triggeredReason = 'AUTO_SELL_99C';
      newStatus = 'closed_auto_tp';
    } else if (target.sl_enabled && target.sl_price != null && bid <= Number(target.sl_price)) {
      triggeredReason = 'STOP_LOSS';
      newStatus = 'closed_auto_sl';
    } else if (target.tp_enabled && target.tp_price != null && bid >= Number(target.tp_price)) {
      triggeredReason = 'TAKE_PROFIT';
      newStatus = 'closed_auto_tp';
    } else if (
      target.trailing_stop_enabled &&
      target.peak_bid != null &&
      Number(target.peak_bid) > entry
    ) {
      const offsetFrac = Number(target.trailing_offset_pct || 8.0) / 100;
      const trailStopLevel = Number((Number(target.peak_bid) * (1 - offsetFrac)).toFixed(4));
      if (bid <= trailStopLevel) {
        triggeredReason = 'TRAILING_STOP';
        newStatus = 'closed_auto_trailing';
      }
    }

    if (triggeredReason && target.auto_exit_enabled !== false) {
      const maxSlip = Number(target.max_slippage_pct || 2.0) / 100;
      const trigRef =
        triggeredReason === 'STOP_LOSS'
          ? Math.min(Number(target.sl_price || bid), bid)
          : triggeredReason === 'TAKE_PROFIT'
          ? Number(target.tp_price || bid)
          : bid;
      const worstFloor = Number((trigRef * (1 - maxSlip)).toFixed(4));

      if (bid < worstFloor && triggeredReason === 'STOP_LOSS') {
        target.position_state = 'PROTECTION_CONSTRAINT_BLOCKED';
        target.latched_exit_reason = triggeredReason;
        saveLocks(locks);
        syncLockToAuditDb(target, 'SLIPPAGE_FLOOR_BLOCKED');
        res.json({
          ok: true,
          triggered: true,
          blocked: true,
          reason: triggeredReason,
          message: `⚠️ ماشه ${triggeredReason} فعال شد اما قیمت Bid (${(bid * 100).toFixed(1)}¢) پایین‌تر از کف مجاز اسلیپیج (${(worstFloor * 100).toFixed(1)}¢) است — سفارش مسدود شد.`,
          lock: target,
        });
        return;
      }

      target.status = newStatus;
      target.position_state = 'CLOSED';
      target.latched_exit_reason = triggeredReason;
      target.exit_price = bid;
      target.outcome_pct = target.last_pct;
      target.closed_at = nowIso;
      target.close_reason = triggeredReason.toLowerCase();
      saveLocks(locks);
      syncLockToAuditDb(target, `AUTO_EXIT_${triggeredReason}`);

      res.json({
        ok: true,
        triggered: true,
        blocked: false,
        reason: triggeredReason,
        message: `🎯 ماشه [${triggeredReason}] در قیمت ${(bid * 100).toFixed(1)}¢ فعال شد و پوزیشن به‌طور خودکار با سفارش ${target.exit_policy || 'MARKETABLE_FAK'} بسته شد (${target.last_pct >= 0 ? '+' : ''}${target.last_pct}%)!`,
        lock: target,
      });
      return;
    }

    saveLocks(locks);
    res.json({
      ok: true,
      triggered: false,
      message: `قیمت Bid به ${(bid * 100).toFixed(1)}¢ (${target.last_pct >= 0 ? '+' : ''}${target.last_pct}%) بروزرسانی شد — هیچ ماشه TP/SL فعال نشد (وضعیت: WATCHING).`,
      lock: target,
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// POST /api/locks/unlock - Cancel Pending Entry OR Force-Exit an active position
app.post('/api/locks/unlock', (req, res) => {
  try {
    const { city, date, market_id, force_exit = false } = req.body;
    if (!city || !date || !market_id) {
      res.status(400).json({ error: 'Missing required fields' });
      return;
    }

    const locks = loadLocks();
    let updated: any = null;
    const nowIso = new Date().toISOString();

    for (const l of locks) {
      if (
        l.status === 'open' &&
        l.city === city &&
        l.date === date &&
        String(l.market_id) === String(market_id)
      ) {
        if (l.position_state === 'ENTRY_PENDING') {
          l.status = 'cancelled_entry';
          l.position_state = 'ENTRY_CANCELLED';
          l.latched_exit_reason = 'MANUAL_CANCEL_ENTRY';
          l.closed_at = nowIso;
          l.close_reason = 'cancelled_entry';
        } else {
          l.status = 'closed_manual';
          l.position_state = 'CLOSED';
          l.latched_exit_reason = force_exit ? 'MANUAL_FORCE_EXIT' : 'MANUAL_UNLOCK';
          l.exit_price = l.last_price ?? l.entry_price;
          l.closed_at = nowIso;
          l.close_reason = force_exit ? 'force_exit' : 'manual_unlock';
        }
        updated = l;
        break;
      }
    }

    if (!updated) {
      res.status(404).json({ error: 'سیگنال فعال یافت نشد.' });
      return;
    }

    saveLocks(locks);
    syncLockToAuditDb(updated, updated.status === 'cancelled_entry' ? 'CANCEL_LIMIT_ENTRY' : 'MANUAL_FORCE_EXIT');
    res.json({
      ok: true,
      message:
        updated.status === 'cancelled_entry'
          ? '✔ سفارش خرید شرطی با موفقیت لغو شد.'
          : force_exit
          ? '🚨 خروج اضطراری (Force Exit) با موفقیت انجام شد.'
          : 'قفل با موفقیت حذف و بسته شد.',
      lock: updated,
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// POST /api/commit-market
app.post('/api/commit-market', (req, res) => {
  try {
    const { market_key } = req.body;
    if (!market_key) {
      res.status(400).json({ error: 'Missing market_key' });
      return;
    }
    const nowIso = new Date().toISOString();
    const line = `${market_key}|${nowIso}\n`;
    fs.appendFileSync(LOCK_REQUESTS_FILE, line, 'utf-8');
    res.json({ ok: true, timestamp: nowIso, line });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/download/lock_history.csv
app.get('/api/download/lock_history.csv', (_req, res) => {
  if (fs.existsSync(LOCK_HISTORY_CSV)) {
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', 'attachment; filename="lock_history.csv"');
    fs.createReadStream(LOCK_HISTORY_CSV).pipe(res);
  } else {
    res.status(404).send('CSV file not found');
  }
});

// GET /api/download/accuracy_report.csv
app.get('/api/download/accuracy_report.csv', (_req, res) => {
  if (fs.existsSync(ACCURACY_CSV)) {
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', 'attachment; filename="accuracy_report.csv"');
    fs.createReadStream(ACCURACY_CSV).pipe(res);
  } else {
    res.status(404).send('CSV file not found');
  }
});

// GET /api/download-github-file/:name - Download modified bot files for GitHub repo sync
const ALLOWED_GITHUB_FILES: Record<string, string> = {
  'weatherbot_v3.py': path.join(__dirname, 'weatherbot_v3.py'),
  'forecasting.py': path.join(__dirname, 'forecasting.py'),
  'strategy.py': path.join(__dirname, 'strategy.py'),
  'dashboard_simple.py': path.join(__dirname, 'dashboard_simple.py'),
  'price_monitor.py': path.join(__dirname, 'price_monitor.py'),
  'lock_manager.py': path.join(__dirname, 'lock_manager.py'),
  'daily_report.py': path.join(__dirname, 'daily_report.py'),
  'history_manager.py': path.join(__dirname, 'history_manager.py'),
  'calibration.json': CALIBRATION_FILE,
  'accuracy_report.csv': ACCURACY_CSV,
  'simple.html': path.join(__dirname, 'simple.html'),
  'monitor_locks.yml': path.join(__dirname, '.github', 'workflows', 'monitor_locks.yml'),
  'scan_daily.yml': path.join(__dirname, '.github', 'workflows', 'scan_daily.yml'),
  'process_lock_events.yml': path.join(__dirname, '.github', 'workflows', 'process_lock_events.yml'),
  'auto_exit_bot.py': path.join(__dirname, 'auto_exit_bot.py'),
  'quant_strategy_advisor.py': path.join(__dirname, 'quant_strategy_advisor.py'),
  'quant_strategy_report.html': path.join(__dirname, 'quant_strategy_report.html'),
  'daily_quant_strategy.yml': path.join(__dirname, '.github', 'workflows', 'daily_quant_strategy.yml'),
  'WEATHERBET_AUTO_TRADING_MASTER_SPEC.md': path.join(__dirname, 'public', 'WEATHERBET_AUTO_TRADING_MASTER_SPEC.md'),
};

// Standalone Quantitative Strategy Lab link (/strategy-lab.html)
app.get('/strategy-lab.html', (_req, res) => {
  const reportPath = path.join(__dirname, 'quant_strategy_report.html');
  if (!fs.existsSync(reportPath)) {
    try {
      execFileSync('python3', [path.join(__dirname, 'quant_strategy_advisor.py')]);
    } catch (e) {
      console.error('Failed to generate quant_strategy_report.html:', e);
    }
  }
  if (fs.existsSync(reportPath)) {
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    fs.createReadStream(reportPath).pipe(res);
  } else {
    res.status(404).send('Strategy Lab report not found');
  }
});

app.get('/api/download/master-spec', (_req, res) => {
  const specPath = path.join(__dirname, 'public', 'WEATHERBET_AUTO_TRADING_MASTER_SPEC.md');
  if (fs.existsSync(specPath)) {
    res.setHeader('Content-Type', 'text/markdown; charset=utf-8');
    res.setHeader('Content-Disposition', 'attachment; filename="WEATHERBET_AUTO_TRADING_MASTER_SPEC.md"');
    fs.createReadStream(specPath).pipe(res);
  } else {
    res.status(404).send('Specification file not found');
  }
});

app.get('/api/download-github-file/:name', (req, res) => {
  const name = req.params.name;
  const target = ALLOWED_GITHUB_FILES[name];
  if (target && fs.existsSync(target)) {
    res.setHeader('Content-Type', 'text/plain; charset=utf-8');
    res.setHeader('Content-Disposition', `attachment; filename="${name}"`);
    fs.createReadStream(target).pipe(res);
  } else {
    res.status(404).send('File not found');
  }
});

// GET /api/download/project-zip - Build and stream a verified full project ZIP directly from disk
app.get('/api/download/project-zip', (_req, res) => {
  try {
    const tmpZip = path.join(os.tmpdir(), `weatherbot-full-${Date.now()}.zip`);
    const pyScript = `
import os, sys, zipfile
root_dir = sys.argv[1]
out_zip = sys.argv[2]
exclude_dirs = {"node_modules", ".git", "dist", "__pycache__", ".vite"}
exclude_files = {"bun.lock"}
with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
    for r, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for f in files:
            if f in exclude_files or f.endswith(".pyc"):
                continue
            full_p = os.path.join(r, f)
            rel_p = os.path.relpath(full_p, root_dir)
            zf.write(full_p, rel_p)
`;
    execFileSync('python3', ['-c', pyScript, __dirname, tmpZip]);
    res.setHeader('Content-Type', 'application/zip');
    res.setHeader('Content-Disposition', 'attachment; filename="weatherbot-complete-project.zip"');
    const stream = fs.createReadStream(tmpZip);
    stream.on('close', () => {
      try { fs.unlinkSync(tmpZip); } catch {}
    });
    stream.pipe(res);
  } catch (err) {
    console.error('Error building project zip:', err);
    res.status(500).send('Failed to create project zip');
  }
});

// GET /api/download/github-updates-zip - Build a ZIP containing only the files needed to update the GitHub repo
app.get('/api/download/github-updates-zip', (_req, res) => {
  try {
    const tmpZip = path.join(os.tmpdir(), `weatherbot-github-updates-${Date.now()}.zip`);
    const pyScript = `
import os, sys, zipfile
root_dir = sys.argv[1]
out_zip = sys.argv[2]
target_paths = [
    "auto_exit_bot.py",
    "quant_strategy_advisor.py",
    "quant_strategy_report.html",
    "docs/strategy-lab.html",
    "docs/simple.html",
    "docs/404.html",
    "weatherbot_v3.py",
    "forecasting.py",
    "strategy.py",
    "dashboard_simple.py",
    "price_monitor.py",
    "lock_manager.py",
    "daily_report.py",
    "history_manager.py",
    "city_calibration.py",
    "locations.py",
    "resolution.py",
    "resolution_patch.py",
    "clob_utils.py",
    "market_time.py",
    "export_accuracy_report.py",
    "config.json",
    "requirements.txt",
    "simple.html",
    "data/calibration.json",
    "data/model_skill.json",
    "data/last_scan.json",
    "data/accuracy_report.csv",
    "data/locked_signals.json",
    "data/lock_history.csv",
    ".github/workflows/scan_daily.yml",
    ".github/workflows/monitor_locks.yml",
    ".github/workflows/process_lock_events.yml",
    ".github/workflows/daily_report.yml",
    ".github/workflows/daily_quant_strategy.yml",
    ".github/workflows/heartbeat_dashboard_link.yml",
]
with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
    for rel_p in target_paths:
        full_p = os.path.join(root_dir, rel_p)
        if os.path.exists(full_p):
            zf.write(full_p, rel_p)
`;
    execFileSync('python3', ['-c', pyScript, __dirname, tmpZip]);
    res.setHeader('Content-Type', 'application/zip');
    res.setHeader('Content-Disposition', 'attachment; filename="weatherbot-github-updates.zip"');
    const stream = fs.createReadStream(tmpZip);
    stream.on('close', () => {
      try { fs.unlinkSync(tmpZip); } catch {}
    });
    stream.pipe(res);
  } catch (err) {
    console.error('Error building github updates zip:', err);
    res.status(500).send('Failed to create github updates zip');
  }
});

async function startServer() {
  const isProd = process.env.NODE_ENV === 'production';

  if (!isProd) {
    const { createServer: createViteServer } = await import('vite');
    const vite = await createViteServer({
      server: { middlewareMode: true, hmr: false },
      appType: 'spa',
    });
    app.use(vite.middlewares);
  } else {
    const distPath = path.join(__dirname, 'dist');
    app.use(express.static(distPath));
    app.get('*', (_req, res) => {
      res.sendFile(path.join(distPath, 'index.html'));
    });
  }

  // Warm up markets cache on startup without touching disk files
  loadAllMarkets();

  app.listen(PORT, '0.0.0.0', () => {
    console.log(`WeatherBet Server listening on http://0.0.0.0:${PORT}`);
  });
}

startServer();
