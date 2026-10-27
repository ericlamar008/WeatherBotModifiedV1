import React, { useState, useEffect, useMemo } from 'react';
import {
  DashboardDataResponse,
  WeatherMarket,
  BucketDistribution,
  LockedSignal,
  HistoryRow,
  AccuracyRow,
  AllocationLeg,
  CityFormProfile,
} from './types';

const MONTHS = [
  'january', 'february', 'march', 'april', 'may', 'june',
  'july', 'august', 'september', 'october', 'november', 'december',
];

const BUCKET_PLAUSIBILITY_MIN = 0.05;
const EV_GATE_THRESHOLD = 0.10;
const GITHUB_REPO = 'ericlamar008/weatherbot';

function formatRangeLabel(range: [number, number] | undefined, unitSym: string): string {
  if (!range || range.length < 2) return '-';
  const [low, high] = range;
  if (low <= -998 && high >= 998) return `?${unitSym}`;
  if (low <= -998) return `≤${high}${unitSym}`;
  if (high >= 998) return `≥${low}${unitSym}`;
  if (low === high) return `${low}${unitSym}`;
  return `${low}-${high}${unitSym}`;
}

function buildPolymarketUrl(city: string, dateStr: string, marketType: 'max' | 'min' = 'max'): string {
  try {
    const [year, monthStr, dayStr] = dateStr.split('-');
    const monthIdx = parseInt(monthStr, 10) - 1;
    const day = parseInt(dayStr, 10);
    const month = MONTHS[monthIdx] || 'october';
    const prefix = marketType === 'min' ? 'lowest' : 'highest';
    return `https://polymarket.com/event/${prefix}-temperature-in-${city}-on-${month}-${day}-${year}`;
  } catch {
    return 'https://polymarket.com';
  }
}

function formatHoursLeft(hours: number | null | undefined, timeStatus?: string): string {
  if (timeStatus === 'awaiting_settlement') return 'awaiting official settlement';
  if (hours == null) return '';
  const totalMinutes = Math.max(0, Math.round(hours * 60));
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return `${h}h ${m}m remaining`;
}

function formatRelativeTime(isoTs: string | null | undefined, kindLabel?: string): string {
  if (!isoTs) return '-';
  const then = new Date(isoTs);
  if (isNaN(then.getTime())) return '-';
  let diffMin = Math.round((Date.now() - then.getTime()) / 60000);
  if (diffMin < 0) diffMin = 0;
  const h = Math.floor(diffMin / 60);
  const m = diffMin % 60;
  return `${h}h ${m}m ago${kindLabel ? ` -- ${kindLabel}` : ''}`;
}

function getMainSignalMarketId(market: WeatherMarket, useSmartGate = true): string | null {
  if (useSmartGate && market.smart_main_market_id) {
    return String(market.smart_main_market_id);
  }
  const allocation = market.committed_allocation || market.live_allocation || [];
  for (const a of allocation) {
    if (a.role === 'main_signal') {
      return String(a.market_id);
    }
  }
  return null;
}

function getFormBadgeClasses(status?: string): string {
  switch (status) {
    case 'hot':
      return 'bg-[#e8f8ee] text-[#16a34a] border-[#16a34a55]';
    case 'cold_drop':
      return 'bg-[#fef3e0] text-[#b45309] border-[#b4530955]';
    case 'weak':
      return 'bg-[#fdeceb] text-[#dc2626] border-[#dc262655]';
    default:
      return 'bg-[#eaf1ff] text-[#2563eb] border-[#2563eb55]';
  }
}

function getGateDecisionBadge(decision?: string) {
  if (!decision || decision === 'use_calibrated') {
    return (
      <span className="inline-block rounded-md px-2 py-0.5 text-[10.5px] font-semibold bg-[#e8f8ee] text-[#16a34a] border border-[#16a34a44]">
        ✅ کالیبره سودمند
      </span>
    );
  }
  if (decision === 'revert_to_shadow') {
    return (
      <span className="inline-block rounded-md px-2 py-0.5 text-[10.5px] font-semibold bg-[#f3e8ff] text-[#9333ea] border border-[#9333ea44]">
        🛡️ سوئیچ به مدل Shadow (حذف اورکورکشن)
      </span>
    );
  }
  if (decision === 'smart_submodel_blend') {
    return (
      <span className="inline-block rounded-md px-2 py-0.5 text-[10.5px] font-semibold bg-[#eaf1ff] text-[#2563eb] border border-[#2563eb44]">
        ⚖️ وزن‌دهی هوشمند زیرمدل‌ها
      </span>
    );
  }
  if (decision === 'sign_flip_damped') {
    return (
      <span className="inline-block rounded-md px-2 py-0.5 text-[10.5px] font-semibold bg-[#fef3e0] text-[#b45309] border border-[#b4530944]">
        🍂 ترمز تغییر فصل فعال
      </span>
    );
  }
  return null;
}

export default function App() {
  const [viewMode, setViewMode] = useState<'simple' | 'pro' | 'accuracy'>('simple');
  const [data, setData] = useState<DashboardDataResponse | null>(null);
  const [historyData, setHistoryData] = useState<{ rows: HistoryRow[]; cities: string[]; dates: string[] }>({
    rows: [],
    cities: [],
    dates: [],
  });
  const [accuracyData, setAccuracyData] = useState<{ rows: AccuracyRow[]; calibration: Record<string, any>; total_lines: number }>({
    rows: [],
    calibration: {},
    total_lines: 0,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Smart Gate & Filters
  const [useSmartGate, setUseSmartGate] = useState(true);
  const [onlyHotCities, setOnlyHotCities] = useState(false);
  const [onlyUsCities, setOnlyUsCities] = useState(false);
  const [citySearch, setCitySearch] = useState('');
  const [historyCityFilter, setHistoryCityFilter] = useState('');
  const [historyDateFilter, setHistoryDateFilter] = useState('');
  const [accuracyCityFilter, setAccuracyCityFilter] = useState('');
  const [accuracyTypeFilter, setAccuracyTypeFilter] = useState<'all' | 'max' | 'min'>('max');
  const [accuracyRegionFilter, setAccuracyRegionFilter] = useState<'all' | 'us' | 'non_us'>('all');
  const [statusMessages, setStatusMessages] = useState<Record<string, { text: string; color: string }>>({});
  const [commitMessages, setCommitMessages] = useState<Record<string, string>>({});
  const [showAllBucketsInSimple, setShowAllBucketsInSimple] = useState(false);
  const [recalibrating, setRecalibrating] = useState(false);
  const [recalMsg, setRecalMsg] = useState<string | null>(null);

  const fetchAllData = async () => {
    try {
      const [dashRes, histRes] = await Promise.all([
        fetch('/api/dashboard-data'),
        fetch('/api/history'),
      ]);
      if (!dashRes.ok) throw new Error('خطا در دریافت اطلاعات داشبورد');
      const dashJson: DashboardDataResponse = await dashRes.json();
      setData(dashJson);

      if (histRes.ok) {
        const histJson = await histRes.json();
        setHistoryData(histJson);
      }
      setError(null);
    } catch (err: any) {
      setError(err.message || 'خطا در برقراری ارتباط با سرور');
    } finally {
      setLoading(false);
    }
  };

  const fetchAccuracyData = async () => {
    try {
      const res = await fetch('/api/accuracy');
      if (res.ok) {
        const json = await res.json();
        setAccuracyData(json);
      }
    } catch {}
  };

  useEffect(() => {
    fetchAllData();
  }, []);

  useEffect(() => {
    if (viewMode === 'accuracy' && accuracyData.rows.length === 0) {
      fetchAccuracyData();
    }
  }, [viewMode]);

  const handleRecalibrateNow = async () => {
    setRecalibrating(true);
    setRecalMsg(null);
    try {
      const res = await fetch('/api/recalibrate', { method: 'POST' });
      const json = await res.json();
      if (res.ok && json.ok) {
        setRecalMsg(`✔ ${json.message}`);
        await fetchAllData();
        if (viewMode === 'accuracy') await fetchAccuracyData();
      } else {
        setRecalMsg(`❌ خطا: ${json.error || 'نامشخص'}`);
      }
    } catch (e: any) {
      setRecalMsg(`❌ خطا: ${e.message}`);
    } finally {
      setRecalibrating(false);
    }
  };

  const setLockStatus = (key: string, text: string, color: string) => {
    setStatusMessages((prev) => ({ ...prev, [key]: { text, color } }));
  };

  const handleLockBucket = async (
    city: string,
    cityName: string,
    date: string,
    marketId: string,
    tokenId: string,
    side: string,
    price: number,
    label: string,
    marketType: 'max' | 'min'
  ) => {
    setLockStatus(marketId, '⏳ در حال ثبت قفل...', '#b45309');
    try {
      const res = await fetch('/api/locks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          city,
          date,
          market_id: marketId,
          token_id: tokenId,
          side,
          price,
          market_type: marketType,
        }),
      });
      const json = await res.json();
      if (res.ok && json.ok) {
        setLockStatus(marketId, '✔ قفل با موفقیت ثبت شد!', '#16a34a');
        await fetchAllData();
      } else {
        setLockStatus(marketId, `❌ خطا: ${json.error || 'نامشخص'}`, '#dc2626');
      }
    } catch (e: any) {
      setLockStatus(marketId, `❌ خطا: ${e.message}`, '#dc2626');
    }
  };

  const handleUnlockBucket = async (
    city: string,
    cityName: string,
    date: string,
    marketId: string,
    label: string
  ) => {
    setLockStatus(marketId, '⏳ در حال حذف قفل...', '#b45309');
    try {
      const res = await fetch('/api/locks/unlock', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ city, date, market_id: marketId, label, cityName }),
      });
      const json = await res.json();
      if (res.ok && json.ok) {
        setLockStatus(marketId, '✔ قفل حذف و بسته شد.', '#16a34a');
        await fetchAllData();
      } else {
        setLockStatus(marketId, `❌ خطا: ${json.error || 'نامشخص'}`, '#dc2626');
      }
    } catch (e: any) {
      setLockStatus(marketId, `❌ خطا: ${e.message}`, '#dc2626');
    }
  };

  const handleCommitMoment = async (marketKey: string) => {
    try {
      const res = await fetch('/api/commit-market', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ market_key: marketKey }),
      });
      if (res.ok) {
        const timeStr = new Date().toLocaleTimeString('fa-IR');
        setCommitMessages((prev) => ({
          ...prev,
          [marketKey]: `قفل شد در lock_requests.txt -- ${timeStr}`,
        }));
      }
    } catch {
      setCommitMessages((prev) => ({
        ...prev,
        [marketKey]: 'خطا در ثبت لحظه',
      }));
    }
  };

  const openLocks = useMemo(() => {
    if (!data) return [];
    const resolvedStatuses = new Set(['resolved', 'resolved_no_signal', 'expired_no_signal']);
    return data.locks
      .filter((l) => l.status === 'open' && !resolvedStatuses.has(l.market_status || ''))
      .sort((a, b) => {
        const c = (a.city || '').localeCompare(b.city || '');
        if (c !== 0) return c;
        return (a.date || '').localeCompare(b.date || '');
      });
  }, [data]);

  const lockedKeysSet = useMemo(() => {
    const set = new Set<string>();
    if (!data) return set;
    for (const l of data.locks) {
      if (l.status === 'open') {
        set.add(`${l.city}|${l.date}|${l.market_id}`);
      }
    }
    return set;
  }, [data]);

  // Group open markets by city (sorted: hot form & active signals first)
  const groupOpenMarketsByCity = (markets: WeatherMarket[], mtype: 'max' | 'min') => {
    const groups: Record<string, WeatherMarket[]> = {};
    for (const m of markets) {
      if (!groups[m.city]) groups[m.city] = [];
      groups[m.city].push(m);
    }
    for (const slug of Object.keys(groups)) {
      groups[slug].sort((a, b) => a.date.localeCompare(b.date));
    }

    const hasSignal = (mlist: WeatherMarket[]) =>
      mlist.some((m) => {
        const legacyMain = (m.committed_allocation || m.live_allocation || []).some((a) => a.role === 'main_signal');
        return legacyMain;
      });

    const getFormRank = (slug: string) => {
      const prof = data?.city_form_profiles?.[`${slug}|${mtype}`];
      if (!prof) return 2;
      if (prof.form_status === 'hot') return 0;
      if (prof.form_status === 'stable') return 1;
      if (prof.form_status === 'cold_drop') return 2;
      return 3;
    };

    const sortedSlugs = Object.keys(groups).sort((a, b) => {
      const sigA = hasSignal(groups[a]) ? 0 : 1;
      const sigB = hasSignal(groups[b]) ? 0 : 1;
      if (sigA !== sigB) return sigA - sigB;
      const rankA = getFormRank(a);
      const rankB = getFormRank(b);
      if (rankA !== rankB) return rankA - rankB;
      const nameA = groups[a][0]?.city_name || a;
      const nameB = groups[b][0]?.city_name || b;
      return nameA.localeCompare(nameB);
    });

    return sortedSlugs
      .map((slug) => ({
        slug,
        city_name: groups[slug][0]?.city_name || slug,
        markets: groups[slug],
        has_signal: hasSignal(groups[slug]),
        form_profile: data?.city_form_profiles?.[`${slug}|${mtype}`] || null,
      }))
      .filter((g) => {
        if (onlyHotCities && g.form_profile?.form_status !== 'hot') return false;
        if (onlyUsCities && !g.form_profile?.is_us) return false;
        if (!citySearch.trim()) return true;
        const q = citySearch.trim().toLowerCase();
        return g.city_name.toLowerCase().includes(q) || g.slug.toLowerCase().includes(q);
      });
  };

  const maxCityGroups = useMemo(
    () => (data ? groupOpenMarketsByCity(data.open_max_markets, 'max') : []),
    [data, citySearch, onlyHotCities, onlyUsCities]
  );

  const minCityGroups = useMemo(
    () => (data ? groupOpenMarketsByCity(data.open_min_markets, 'min') : []),
    [data, citySearch, onlyHotCities, onlyUsCities]
  );

  const filteredHistoryRows = useMemo(() => {
    return historyData.rows.filter((r) => {
      const okCity = !historyCityFilter || r.city_name === historyCityFilter;
      const okDate = !historyDateFilter || r.date === historyDateFilter;
      return okCity && okDate;
    });
  }, [historyData.rows, historyCityFilter, historyDateFilter]);

  const sortedFormProfiles = useMemo(() => {
    if (!data?.city_form_profiles) return [];
    return Object.values(data.city_form_profiles)
      .filter((p) => {
        if (accuracyTypeFilter !== 'all' && p.market_type !== accuracyTypeFilter) return false;
        if (accuracyRegionFilter === 'us' && !p.is_us) return false;
        if (accuracyRegionFilter === 'non_us' && p.is_us) return false;
        if (accuracyCityFilter.trim()) {
          const q = accuracyCityFilter.trim().toLowerCase();
          if (!p.city_name.toLowerCase().includes(q) && !p.city.toLowerCase().includes(q)) return false;
        }
        return true;
      })
      .sort((a, b) => {
        if (b.recent_hit_pct !== a.recent_hit_pct) return b.recent_hit_pct - a.recent_hit_pct;
        return a.recent_mae - b.recent_mae;
      });
  }, [data, accuracyTypeFilter, accuracyRegionFilter, accuracyCityFilter]);

  if (loading) {
    return (
      <div className="min-h-screen bg-[#eef1f6] text-[#1f2430] flex items-center justify-center p-6" dir="rtl">
        <div className="bg-white border border-[#dde3ec] rounded-xl p-8 shadow-sm text-center max-w-md w-full">
          <div className="w-8 h-8 border-3 border-[#2563eb] border-t-transparent rounded-full animate-spin mx-auto mb-4" />
          <div className="font-bold text-base mb-1">در حال بارگذاری WeatherBet...</div>
          <div className="text-xs text-[#6b7684]">دریافت بازارهای فعال و اجرای موتور کالیبراسیون هوشمند ۱۴ روزه</div>
        </div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="min-h-screen bg-[#eef1f6] text-[#1f2430] flex items-center justify-center p-6" dir="rtl">
        <div className="bg-white border border-red-200 rounded-xl p-6 shadow-sm max-w-md w-full text-center">
          <div className="text-red-600 font-bold mb-2">خطا در بارگذاری داده‌ها</div>
          <div className="text-sm text-gray-600 mb-4">{error}</div>
          <button
            onClick={fetchAllData}
            className="bg-[#2563eb] text-white px-4 py-2 rounded-lg text-sm font-semibold cursor-pointer"
          >
            تلاش مجدد
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      className={
        viewMode === 'pro'
          ? 'min-h-screen bg-[#0f1115] text-[#e6e6e6] p-4 md:p-6 transition-colors'
          : 'min-h-screen bg-[#eef1f6] text-[#1f2430] p-3.5 md:p-6 transition-colors'
      }
      dir="rtl"
    >
      {/* Top Mode Switcher Bar */}
      <div
        className={`flex flex-wrap items-center justify-between gap-3 pb-3 mb-4 border-b ${
          viewMode === 'pro' ? 'border-[#2a2e37]' : 'border-[#dde3ec]'
        }`}
      >
        <div className="flex items-center gap-3 flex-wrap">
          <h1 className="text-lg md:text-xl font-bold m-0">
            {viewMode === 'pro'
              ? 'WeatherBet v3 Dashboard'
              : viewMode === 'accuracy'
              ? 'WeatherBet -- کالیبراسیون هوشمند و رتبه‌بندی دقت شهرها'
              : 'WeatherBet -- داشبورد ساده'}
          </h1>
          <span
            className={`text-xs px-2.5 py-1 rounded-md font-medium ${
              viewMode === 'pro'
                ? 'bg-[#181b21] text-[#9aa0a6] border border-[#2a2e37]'
                : 'bg-white text-[#6b7684] border border-[#dde3ec]'
            }`}
          >
            {data.meta.open_max_count + data.meta.open_min_count} بازار باز • {openLocks.length} قفل فعال
          </span>
          <span className="text-xs px-2.5 py-1 rounded-md font-semibold bg-[#e8f8ee] text-[#16a34a] border border-[#16a34a55]">
            🛡️ گیت کالیبراسیون هوشمند (Champion-Challenger 14d) فعال
          </span>
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          <button
            onClick={() => setViewMode('simple')}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold cursor-pointer transition-all ${
              viewMode === 'simple'
                ? 'bg-[#2563eb] text-white shadow-sm'
                : viewMode === 'pro'
                ? 'bg-[#181b21] text-[#c7ccd1] border border-[#2a2e37] hover:bg-[#232730]'
                : 'bg-white text-[#1f2430] border border-[#dde3ec] hover:bg-gray-50'
            }`}
          >
            داشبورد ساده (Simple)
          </button>
          <button
            onClick={() => setViewMode('pro')}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold cursor-pointer transition-all ${
              viewMode === 'pro'
                ? 'bg-[#2563eb] text-white shadow-sm'
                : 'bg-white text-[#1f2430] border border-[#dde3ec] hover:bg-gray-50'
            }`}
          >
            داشبورد پیشرفته (v3 Pro)
          </button>
          <button
            onClick={() => setViewMode('accuracy')}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold cursor-pointer transition-all ${
              viewMode === 'accuracy'
                ? 'bg-[#2563eb] text-white shadow-sm'
                : viewMode === 'pro'
                ? 'bg-[#181b21] text-[#c7ccd1] border border-[#2a2e37] hover:bg-[#232730]'
                : 'bg-white text-[#1f2430] border border-[#dde3ec] hover:bg-gray-50'
            }`}
          >
            📊 دقت شهرها و کالیبراسیون هوشمند
          </button>
          <button
            onClick={async () => {
              try {
                await fetch('/api/refresh-prices', { method: 'POST' });
              } catch {}
              await fetchAllData();
            }}
            title="به‌روزرسانی داده‌ها"
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold cursor-pointer ${
              viewMode === 'pro'
                ? 'bg-[#181b21] text-[#60a5fa] border border-[#2a2e37] hover:bg-[#232730]'
                : 'bg-white text-[#2563eb] border border-[#dde3ec] hover:bg-gray-50'
            }`}
          >
            ↻ بروزرسانی
          </button>
        </div>
      </div>

      {/* =====================================================================
          MODE 1: SIMPLE DASHBOARD
         ===================================================================== */}
      {viewMode === 'simple' && (
        <div>
          <div className="text-[12.5px] text-[#6b7684] mb-3.5 leading-relaxed">
            آخرین به‌روزرسانی: <span className="font-semibold text-[#1f2430]">{data.meta.last_update_iran}</span> (به وقت ایران){' '}
            <span className="ltr-inline text-[11.5px] text-[#6b7684] mr-1">
              {formatRelativeTime(data.meta.latest_scan_iso, data.meta.latest_scan_kind)}
            </span>
            <br />
            مجهز به <b>ردیابی تغییرات اسکن‌های ۶ ساعته نسبت به اسکن ۶ ساعتهٔ قبلی</b> + <b>گیت کالیبراسیون هوشمند ۱۴ روزه</b> (حذف اورکورکشن فصل پاییز، وزن‌دهی زیرمدل‌ها و تصحیح باکت‌های ۲ درجه‌ای آمریکا)
          </div>

          {/* Toolbar */}
          <div className="flex gap-2 flex-wrap mb-4 items-center">
            <input
              type="text"
              value={citySearch}
              onChange={(e) => setCitySearch(e.target.value)}
              placeholder="جستجوی شهر (مثلاً Atlanta, Chicago, Cape Town, Seoul)..."
              className="flex-1 min-w-[180px] bg-white border border-[#dde3ec] text-[#1f2430] rounded-[10px] px-3.5 py-2.5 text-[13.5px] shadow-xs focus:outline-2 focus:outline-[#2563eb]"
            />
            <a
              href="#max-section"
              className="bg-[#2563eb] text-white rounded-[10px] px-3.5 py-2.5 text-[12.5px] font-semibold no-underline whitespace-nowrap shadow-xs hover:bg-[#1d4ed8]"
            >
              🔥 حداکثر ({maxCityGroups.length})
            </a>
            <a
              href="#min-section"
              className="bg-[#2563eb] text-white rounded-[10px] px-3.5 py-2.5 text-[12.5px] font-semibold no-underline whitespace-nowrap shadow-xs hover:bg-[#1d4ed8]"
            >
              ❄️ حداقل ({minCityGroups.length})
            </a>
            <a
              href="#daily-section"
              className="bg-[#2563eb] text-white rounded-[10px] px-3.5 py-2.5 text-[12.5px] font-semibold no-underline whitespace-nowrap shadow-xs hover:bg-[#1d4ed8]"
            >
              📅 آمار روزانه
            </a>
            <a
              href="#history-section"
              className="bg-[#2563eb] text-white rounded-[10px] px-3.5 py-2.5 text-[12.5px] font-semibold no-underline whitespace-nowrap shadow-xs hover:bg-[#1d4ed8]"
            >
              مشاهدهٔ نتایج ↓
            </a>
          </div>

          {/* Smart Filter Chips */}
          <div className="flex gap-2 flex-wrap mb-4 items-center">
            <label className="flex items-center gap-1.5 bg-white border border-[#16a34a66] rounded-[10px] px-3 py-2 text-xs text-[#1f2430] font-semibold cursor-pointer select-none shadow-2xs">
              <input
                type="checkbox"
                checked={useSmartGate}
                onChange={(e) => setUseSmartGate(e.target.checked)}
              />
              🛡️ اعمال گیت کالیبراسیون هوشمند (پیشنهادی)
            </label>
            <label className="flex items-center gap-1.5 bg-white border border-[#dde3ec] rounded-[10px] px-3 py-2 text-xs text-[#1f2430] font-medium cursor-pointer select-none">
              <input
                type="checkbox"
                checked={onlyHotCities}
                onChange={(e) => setOnlyHotCities(e.target.checked)}
              />
              🔥 فقط شهرهای روی فرم (دقت اخیر بالای ۵۰٪)
            </label>
            <label className="flex items-center gap-1.5 bg-white border border-[#dde3ec] rounded-[10px] px-3 py-2 text-xs text-[#1f2430] font-medium cursor-pointer select-none">
              <input
                type="checkbox"
                checked={onlyUsCities}
                onChange={(e) => setOnlyUsCities(e.target.checked)}
              />
              🇺🇸 فقط شهرهای آمریکا
            </label>
            <label className="flex items-center gap-1.5 bg-white border border-[#dde3ec] rounded-[10px] px-3 py-2 text-xs text-[#6b7684] cursor-pointer select-none">
              <input
                type="checkbox"
                checked={showAllBucketsInSimple}
                onChange={(e) => setShowAllBucketsInSimple(e.target.checked)}
              />
              نمایش باکت‌های زیر ۵٪
            </label>
          </div>

          {/* Active Locked Signals Section */}
          {openLocks.length > 0 && (
            <details
              open
              className="bg-[#e8f8ee] border border-[#16a34a55] rounded-xl px-3 py-2.5 mb-4 shadow-xs"
            >
              <summary className="cursor-pointer text-sm font-bold text-[#1f2430] py-1.5 select-none">
                🔒 سیگنال‌های قفل‌شدهٔ فعال ({openLocks.length})
              </summary>
              <div className="overflow-x-auto mt-2.5 rounded-lg">
                <table className="w-full min-w-[600px] border-collapse text-[13px] bg-white tabular-nums">
                  <thead>
                    <tr className="bg-[#f6f8fb] text-[#6b7684] text-[11.5px] font-semibold">
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">شهر</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">نوع</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">تاریخ</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">دمای سیگنال قفل‌شده</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">قیمت قفل‌شده</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">قیمت فروش فعلی</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">درصد تغییر</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">تا پایان روز محلی</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">آخرین به‌روزرسانی</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">عملیات</th>
                    </tr>
                  </thead>
                  <tbody>
                    {openLocks.map((l) => {
                      const isMin = l.market_type === 'min';
                      const polyUrl = buildPolymarketUrl(l.city, l.date, isMin ? 'min' : 'max');
                      const pct = l.last_pct;
                      const statusObj = statusMessages[l.market_id];
                      return (
                        <tr key={`${l.city}-${l.date}-${l.market_id}`} className="border-b border-[#dde3ec] last:border-b-0 text-center">
                          <td className="py-2.5 px-2 font-medium">
                            <a href={polyUrl} target="_blank" rel="noopener noreferrer" className="text-[#2563eb] hover:underline">
                              {l.city_name}
                            </a>
                          </td>
                          <td className="py-2.5 px-2 text-xs">
                            {isMin ? '❄️ کمینه' : '🔥 بیشینه'}
                          </td>
                          <td className="py-2.5 px-2">{l.date}</td>
                          <td className="py-2.5 px-2 font-semibold ltr-inline">{l.bucket_label || '-'}</td>
                          <td className="py-2.5 px-2">{l.entry_price != null ? l.entry_price.toFixed(3) : '-'}</td>
                          <td className="py-2.5 px-2">{l.last_price != null ? l.last_price.toFixed(3) : '-'}</td>
                          <td className="py-2.5 px-2">
                            {pct == null ? (
                              '-'
                            ) : (
                              <span className={pct >= 0 ? 'text-[#16a34a] font-semibold' : 'text-[#dc2626] font-semibold'}>
                                {pct >= 0 ? `+${pct.toFixed(1)}%` : `${pct.toFixed(1)}%`}
                              </span>
                            )}
                          </td>
                          <td className="py-2.5 px-2 ltr-inline text-xs text-[#6b7684]">
                            {formatHoursLeft(l.hours_left, l.time_status || undefined) || 'active'}
                          </td>
                          <td className="py-2.5 px-2 ltr-inline text-xs text-[#6b7684]">
                            {formatRelativeTime(l.last_checked_at)}
                          </td>
                          <td className="py-2.5 px-2">
                            <button
                              onClick={() => handleUnlockBucket(l.city, l.city_name, l.date, l.market_id, l.bucket_label || '')}
                              className="bg-[#dc2626] hover:bg-[#b91c1c] text-white border-none rounded-[9px] px-3.5 py-2 text-xs font-semibold cursor-pointer"
                            >
                              حذف قفل
                            </button>
                            {statusObj && (
                              <span className="block text-[10.5px] mt-1" style={{ color: statusObj.color }}>
                                {statusObj.text}
                              </span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </details>
          )}

          {/* Section: Maximum Temperature */}
          <h2 id="max-section" className="text-[15.5px] font-bold mt-6 mb-3 pb-2 border-b border-[#dde3ec]">
            🔥 دمای حداکثر ({maxCityGroups.length} شهر)
          </h2>
          {maxCityGroups.length === 0 ? (
            <div className="text-[#6b7684] italic py-3">هیچ بازار بازی با این فیلتر وجود ندارد.</div>
          ) : (
            maxCityGroups.map((group) => (
              <SimpleCityBlock
                key={`max-${group.slug}`}
                group={group}
                marketType="max"
                useSmartGate={useSmartGate}
                lockedKeysSet={lockedKeysSet}
                showAllBuckets={showAllBucketsInSimple}
                statusMessages={statusMessages}
                onLock={handleLockBucket}
                onUnlock={handleUnlockBucket}
              />
            ))
          )}

          {/* Section: Minimum Temperature */}
          <h2 id="min-section" className="text-[15.5px] font-bold mt-7 mb-3 pb-2 border-b border-[#dde3ec]">
            ❄️ دمای حداقل ({minCityGroups.length} شهر)
          </h2>
          {minCityGroups.length === 0 ? (
            <div className="text-[#6b7684] italic py-3">هیچ بازار بازی با این فیلتر وجود ندارد.</div>
          ) : (
            minCityGroups.map((group) => (
              <SimpleCityBlock
                key={`min-${group.slug}`}
                group={group}
                marketType="min"
                useSmartGate={useSmartGate}
                lockedKeysSet={lockedKeysSet}
                showAllBuckets={showAllBucketsInSimple}
                statusMessages={statusMessages}
                onLock={handleLockBucket}
                onUnlock={handleUnlockBucket}
              />
            ))
          )}

          {/* Section: Daily PnL Report */}
          {data.daily_pnl.length > 0 && (
            <div className="mt-8">
              <h2 id="daily-section" className="text-[15.5px] font-bold mb-3 pb-2 border-b border-[#dde3ec]">
                📅 گزارش روزانه (بر اساس تاریخ بسته‌شدن به وقت ایران)
              </h2>
              {data.daily_pnl.slice(0, 5).map((dayGroup) => (
                <DailyPnlBlock key={dayGroup.date} dayGroup={dayGroup} defaultOpen={true} />
              ))}
              {data.daily_pnl.length > 5 && (
                <details className="bg-white border border-[#dde3ec] rounded-xl px-3 py-2 mb-2.5 shadow-xs">
                  <summary className="cursor-pointer text-sm font-medium py-2">
                    نمایش روزهای قدیمی‌تر ({data.daily_pnl.length - 5} روز)
                  </summary>
                  <div className="mt-2">
                    {data.daily_pnl.slice(5).map((dayGroup) => (
                      <DailyPnlBlock key={dayGroup.date} dayGroup={dayGroup} defaultOpen={false} />
                    ))}
                  </div>
                </details>
              )}
            </div>
          )}

          {/* Section: Trade History */}
          <div className="mt-8">
            <h2 id="history-section" className="text-[15.5px] font-bold mb-3 pb-2 border-b border-[#dde3ec]">
              تاریخچهٔ معاملات ({filteredHistoryRows.length} مورد)
            </h2>
            <div className="flex flex-wrap items-center justify-between gap-2 my-2.5">
              <div className="flex gap-2 flex-wrap">
                <select
                  value={historyCityFilter}
                  onChange={(e) => setHistoryCityFilter(e.target.value)}
                  className="bg-white border border-[#dde3ec] text-[#1f2430] rounded-[10px] px-3 py-2 text-[12.5px]"
                >
                  <option value="">همهٔ شهرها ({historyData.cities.length})</option>
                  {historyData.cities.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
                <select
                  value={historyDateFilter}
                  onChange={(e) => setHistoryDateFilter(e.target.value)}
                  className="bg-white border border-[#dde3ec] text-[#1f2430] rounded-[10px] px-3 py-2 text-[12.5px]"
                >
                  <option value="">همهٔ تاریخ‌ها ({historyData.dates.length})</option>
                  {historyData.dates.map((d) => (
                    <option key={d} value={d}>
                      {d}
                    </option>
                  ))}
                </select>
                {(historyCityFilter || historyDateFilter) && (
                  <button
                    onClick={() => {
                      setHistoryCityFilter('');
                      setHistoryDateFilter('');
                    }}
                    className="bg-white border border-[#dde3ec] text-[#6b7684] rounded-[10px] px-3 py-2 text-xs cursor-pointer hover:text-[#1f2430]"
                  >
                    پاک کردن فیلتر
                  </button>
                )}
              </div>

              <a
                href="/api/download/lock_history.csv"
                download="lock_history.csv"
                className="inline-block bg-white text-[#1f2430] border border-[#dde3ec] rounded-[10px] px-4 py-2 text-[12.5px] font-semibold no-underline shadow-xs hover:border-[#2563eb]"
              >
                ⬇ دانلود CSV کامل
              </a>
            </div>

            <div className="overflow-x-auto rounded-lg border border-[#dde3ec] bg-white">
              {filteredHistoryRows.length === 0 ? (
                <div className="text-[#6b7684] italic p-4">هنوز هیچ معامله‌ای با این فیلتر ثبت نشده.</div>
              ) : (
                <table className="w-full min-w-[520px] border-collapse text-[13px] bg-white tabular-nums">
                  <thead>
                    <tr className="bg-[#f6f8fb] text-[#6b7684] text-[11.5px] font-semibold">
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">تاریخ</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">شهر</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">دمای سیگنال</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">دمای نهایی</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">خرید (سنت)</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">فروش (سنت)</th>
                      <th className="py-2.5 px-2 border-b border-[#dde3ec]">برآیند</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredHistoryRows.map((r, idx) => {
                      const pct = r.outcome_pct;
                      return (
                        <tr key={`${r.date}-${r.city_name}-${idx}`} className="border-b border-[#dde3ec] last:border-b-0 text-center">
                          <td className="py-2.5 px-2">{r.date}</td>
                          <td className="py-2.5 px-2 font-medium">{r.city_name}</td>
                          <td className="py-2.5 px-2 ltr-inline">{r.signal_label}</td>
                          <td className="py-2.5 px-2 ltr-inline">{r.final_temp || '-'}</td>
                          <td className="py-2.5 px-2">{r.buy_cents != null ? r.buy_cents : '-'}</td>
                          <td className="py-2.5 px-2">{r.sell_cents != null ? r.sell_cents : '-'}</td>
                          <td className="py-2.5 px-2">
                            {pct == null ? (
                              '-'
                            ) : (
                              <span className={pct >= 0 ? 'text-[#16a34a] font-semibold' : 'text-[#dc2626] font-semibold'}>
                                {pct >= 0 ? `+${pct.toFixed(1)}%` : `${pct.toFixed(1)}%`}
                              </span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </div>
      )}

      {/* =====================================================================
          MODE 2: PRO TERMINAL DASHBOARD
         ===================================================================== */}
      {viewMode === 'pro' && (
        <ProDashboardView
          data={data}
          citySearch={citySearch}
          setCitySearch={setCitySearch}
          commitMessages={commitMessages}
          onCommitMoment={handleCommitMoment}
        />
      )}

      {/* =====================================================================
          MODE 3: SMART CALIBRATION & CITY ACCURACY ANALYTICS
         ===================================================================== */}
      {viewMode === 'accuracy' && (
        <div className="space-y-6">
          {/* Card 1: Smart Calibration Gate & City Form Leaderboard */}
          <div className="bg-white border border-[#dde3ec] rounded-xl p-4 shadow-xs">
            <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
              <div>
                <h2 className="text-base font-bold m-0">
                  🛡️ جدول رتبه‌بندی فرم اخیر شهرها و گیت کالیبراسیون هوشمند (Champion-Challenger 14d)
                </h2>
                <p className="text-xs text-[#6b7684] mt-1">
                  مقایسه عملکرد اخیر (۱۴ روز گذشته) با دوره قدیم، تشخیص شهرهای دچار افت فصلی، انتخاب بهترین زیرمدل هواشناسی (GFS / ECMWF / GEM / ICON) و جلوگیری خودکار از کالیبراسیون مضر
                </p>
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <button
                  onClick={handleRecalibrateNow}
                  disabled={recalibrating}
                  className="bg-[#16a34a] hover:bg-[#15803d] text-white px-3.5 py-2 rounded-lg text-xs font-semibold cursor-pointer border-none"
                >
                  {recalibrating ? '⏳ در حال به‌روزرسانی...' : '⚡ اعمال کالیبراسیون هوشمند روی calibration.json'}
                </button>
                <a
                  href="/api/download/accuracy_report.csv"
                  download="accuracy_report.csv"
                  className="bg-[#2563eb] text-white px-3.5 py-2 rounded-lg text-xs font-semibold no-underline"
                >
                  ⬇ دانلود CSV دقت
                </a>
              </div>
            </div>

            {recalMsg && (
              <div className="mb-3 px-3 py-2 rounded-lg bg-[#e8f8ee] border border-[#16a34a55] text-xs font-semibold text-[#16a34a]">
                {recalMsg}
              </div>
            )}

            {/* Filters for City Leaderboard */}
            <div className="flex flex-wrap gap-2 mb-4 items-center">
              <input
                type="text"
                value={accuracyCityFilter}
                onChange={(e) => setAccuracyCityFilter(e.target.value)}
                placeholder="جستجوی شهر..."
                className="bg-[#f6f8fb] border border-[#dde3ec] rounded-lg px-3 py-2 text-xs min-w-[180px]"
              />
              <div className="flex rounded-lg border border-[#dde3ec] overflow-hidden text-xs">
                <button
                  onClick={() => setAccuracyTypeFilter('max')}
                  className={`px-3 py-1.5 cursor-pointer border-none font-semibold ${
                    accuracyTypeFilter === 'max' ? 'bg-[#2563eb] text-white' : 'bg-white text-[#1f2430]'
                  }`}
                >
                  🔥 دمای حداکثر (Max)
                </button>
                <button
                  onClick={() => setAccuracyTypeFilter('min')}
                  className={`px-3 py-1.5 cursor-pointer border-none font-semibold ${
                    accuracyTypeFilter === 'min' ? 'bg-[#2563eb] text-white' : 'bg-white text-[#1f2430]'
                  }`}
                >
                  ❄️ دمای حداقل (Min)
                </button>
                <button
                  onClick={() => setAccuracyTypeFilter('all')}
                  className={`px-3 py-1.5 cursor-pointer border-none font-semibold ${
                    accuracyTypeFilter === 'all' ? 'bg-[#2563eb] text-white' : 'bg-white text-[#1f2430]'
                  }`}
                >
                  همه بازارها
                </button>
              </div>

              <div className="flex rounded-lg border border-[#dde3ec] overflow-hidden text-xs">
                <button
                  onClick={() => setAccuracyRegionFilter('all')}
                  className={`px-3 py-1.5 cursor-pointer border-none font-semibold ${
                    accuracyRegionFilter === 'all' ? 'bg-[#2563eb] text-white' : 'bg-white text-[#1f2430]'
                  }`}
                >
                  🌍 همه کشورها
                </button>
                <button
                  onClick={() => setAccuracyRegionFilter('us')}
                  className={`px-3 py-1.5 cursor-pointer border-none font-semibold ${
                    accuracyRegionFilter === 'us' ? 'bg-[#2563eb] text-white' : 'bg-white text-[#1f2430]'
                  }`}
                >
                  🇺🇸 فقط شهرهای آمریکا (9 شهر)
                </button>
                <button
                  onClick={() => setAccuracyRegionFilter('non_us')}
                  className={`px-3 py-1.5 cursor-pointer border-none font-semibold ${
                    accuracyRegionFilter === 'non_us' ? 'bg-[#2563eb] text-white' : 'bg-white text-[#1f2430]'
                  }`}
                >
                  🌐 غیرآمریکایی (°C)
                </button>
              </div>
            </div>

            <div className="overflow-x-auto rounded-lg border border-[#dde3ec]">
              <table className="w-full min-w-[920px] border-collapse text-xs bg-white tabular-nums">
                <thead>
                  <tr className="bg-[#f6f8fb] text-[#6b7684] font-semibold">
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">#</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">شهر</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">نوع</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">وضعیت فرم</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">دقت اخیر (باکت دقیق)</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">دقت با ±1 باکت</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">دقت قدیم</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">تغییر روند</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">خطای اخیر (MAE)</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">کالیبره vs Shadow</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">بهترین / بدترین مدل</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">تصمیم گیت هوشمند</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">پیشنهاد معاملاتی</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedFormProfiles.map((p, idx) => {
                    const delta = p.hit_delta_pct;
                    return (
                      <tr key={`${p.city}-${p.market_type}`} className="border-b border-[#dde3ec] last:border-b-0 text-center hover:bg-gray-50">
                        <td className="py-2.5 px-2 text-[#6b7684]">{idx + 1}</td>
                        <td className="py-2.5 px-2 font-bold text-[#1f2430]">
                          {p.city_name} {p.is_us ? '🇺🇸' : ''}
                        </td>
                        <td className="py-2.5 px-2">
                          {p.market_type === 'max' ? '🔥 بیشینه' : '❄️ کمینه'}
                        </td>
                        <td className="py-2.5 px-2">
                          <span className={`inline-block rounded-md px-2 py-0.5 text-[10.5px] font-semibold border ${getFormBadgeClasses(p.form_status)}`}>
                            {p.form_label}
                          </span>
                        </td>
                        <td className="py-2.5 px-2 font-bold text-[13px] text-[#1f2430]">
                          {p.recent_hit_pct.toFixed(1)}%
                        </td>
                        <td className="py-2.5 px-2 font-semibold text-[#2563eb]">
                          {p.recent_within1_pct.toFixed(1)}%
                        </td>
                        <td className="py-2.5 px-2 text-[#6b7684]">
                          {p.old_hit_pct != null ? `${p.old_hit_pct.toFixed(1)}%` : '-'}
                        </td>
                        <td className="py-2.5 px-2 ltr-inline font-semibold">
                          {delta == null ? (
                            '-'
                          ) : (
                            <span className={delta > 0 ? 'text-[#16a34a]' : delta < -10 ? 'text-[#dc2626]' : 'text-[#6b7684]'}>
                              {delta > 0 ? `+${delta.toFixed(1)}% ↑` : `${delta.toFixed(1)}% ↓`}
                            </span>
                          )}
                        </td>
                        <td className="py-2.5 px-2 ltr-inline">
                          {p.recent_mae.toFixed(2)}°{p.unit}
                        </td>
                        <td className="py-2.5 px-2 ltr-inline text-[11px]">
                          {p.calibrated_mae != null && p.raw_shadow_mae != null ? (
                            <span>
                              Cal: {p.calibrated_mae} / Shd:{' '}
                              <b className={p.raw_shadow_mae < p.calibrated_mae ? 'text-[#9333ea]' : 'text-[#16a34a]'}>
                                {p.raw_shadow_mae}
                              </b>
                            </span>
                          ) : (
                            '-'
                          )}
                        </td>
                        <td className="py-2.5 px-2 ltr-inline text-[11px]">
                          {p.best_submodel ? (
                            <span>
                              <span className="text-[#16a34a] font-semibold">✔ {p.best_submodel.toUpperCase()}</span>
                              {p.worst_submodel && p.worst_submodel !== p.best_submodel && (
                                <span className="text-[#dc2626] mr-1"> (✖ {p.worst_submodel.toUpperCase()})</span>
                              )}
                            </span>
                          ) : (
                            '-'
                          )}
                        </td>
                        <td className="py-2.5 px-2">
                          <div title={p.gate_reason}>{getGateDecisionBadge(p.gate_decision)}</div>
                        </td>
                        <td className="py-2.5 px-2 font-medium text-[11.5px]">{p.trading_advice}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>

          {/* Card 2: Raw Accuracy Log Table */}
          <div className="bg-white border border-[#dde3ec] rounded-xl p-4 shadow-xs">
            <h3 className="text-sm font-bold mb-3">
              ریزِ رکوردهای روزانه دقت مدل در برابر دمای واقعی ایستگاه‌ها ({accuracyData.total_lines} رکورد)
            </h3>
            <div className="overflow-x-auto rounded-lg border border-[#dde3ec]">
              <table className="w-full min-w-[700px] border-collapse text-xs bg-white tabular-nums">
                <thead>
                  <tr className="bg-[#f6f8fb] text-[#6b7684] font-semibold">
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">شهر</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">تاریخ</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">نوع بازار</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">پیش‌بینی مدل</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">سیگما (σ)</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">دمای واقعی</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">خطای مطلق</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">بایاس</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">نتیجه</th>
                    <th className="py-2.5 px-2 border-b border-[#dde3ec]">سود/زیان (u)</th>
                  </tr>
                </thead>
                <tbody>
                  {accuracyData.rows
                    .filter((r) => {
                      if (!accuracyCityFilter.trim()) return true;
                      return r.city_name.toLowerCase().includes(accuracyCityFilter.trim().toLowerCase());
                    })
                    .slice(0, 120)
                    .map((row, idx) => (
                      <tr key={idx} className="border-b border-[#dde3ec] last:border-b-0 text-center">
                        <td className="py-2 px-2 font-medium">{row.city_name}</td>
                        <td className="py-2 px-2">{row.date}</td>
                        <td className="py-2 px-2">{row.market_type}</td>
                        <td className="py-2 px-2">{row.forecast_mean != null ? row.forecast_mean.toFixed(2) : '-'}</td>
                        <td className="py-2 px-2">{row.sigma != null ? row.sigma.toFixed(2) : '-'}</td>
                        <td className="py-2 px-2 font-semibold">{row.actual_temp != null ? row.actual_temp.toFixed(1) : '-'}</td>
                        <td className="py-2 px-2">{row.abs_error != null ? row.abs_error.toFixed(2) : '-'}</td>
                        <td className="py-2 px-2 ltr-inline">
                          {row.signed_bias != null
                            ? row.signed_bias > 0
                              ? `+${row.signed_bias.toFixed(2)}`
                              : row.signed_bias.toFixed(2)
                            : '-'}
                        </td>
                        <td className="py-2 px-2">
                          <span
                            className={`px-2 py-0.5 rounded text-[11px] font-semibold ${
                              row.outcome === 'win'
                                ? 'bg-[#e8f8ee] text-[#16a34a]'
                                : row.outcome === 'loss'
                                ? 'bg-[#fdeceb] text-[#dc2626]'
                                : 'bg-gray-100 text-gray-500'
                            }`}
                          >
                            {row.outcome === 'win' ? 'برد' : row.outcome === 'loss' ? 'باخت' : row.outcome}
                          </span>
                        </td>
                        <td
                          className={`py-2 px-2 font-semibold ltr-inline ${
                            (row.pnl_units || 0) > 0
                              ? 'text-[#16a34a]'
                              : (row.pnl_units || 0) < 0
                              ? 'text-[#dc2626]'
                              : ''
                          }`}
                        >
                          {row.pnl_units != null ? (row.pnl_units > 0 ? `+${row.pnl_units.toFixed(2)}` : row.pnl_units.toFixed(2)) : '-'}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ============================================================================
   Sub-components for Simple View
   ============================================================================ */

function getSignalBadge(isMain: boolean, isSmartMain: boolean, evVal: number | null | undefined, useSmartGate: boolean) {
  const ev = evVal ?? 0;
  const activeMain = useSmartGate ? isSmartMain || isMain : isMain;
  if (activeMain && ev >= EV_GATE_THRESHOLD) {
    return (
      <span className="inline-block rounded-md px-2 py-0.5 text-[10.5px] font-semibold whitespace-nowrap mr-1.5 bg-[#e8f8ee] text-[#16a34a] border border-[#16a34a55]">
        🟢 سیگنال نهایی {useSmartGate && isSmartMain && !isMain ? '(هوشمند)' : ''}
      </span>
    );
  }
  if (activeMain) {
    return (
      <span className="inline-block rounded-md px-2 py-0.5 text-[10.5px] font-semibold whitespace-nowrap mr-1.5 bg-[#eaf1ff] text-[#2563eb] border border-[#2563eb55]">
        🔵 محتمل‌ترین {useSmartGate && isSmartMain && !isMain ? '(هوشمند)' : ''}
      </span>
    );
  }
  if (ev >= EV_GATE_THRESHOLD) {
    return (
      <span className="inline-block rounded-md px-2 py-0.5 text-[10.5px] font-semibold whitespace-nowrap mr-1.5 bg-[#f3e8ff] text-[#9333ea] border border-[#9333ea55]">
        🟣 ارزش بالا
      </span>
    );
  }
  return null;
}

function SimpleCityBlock({
  group,
  marketType,
  useSmartGate,
  lockedKeysSet,
  showAllBuckets,
  statusMessages,
  onLock,
  onUnlock,
}: {
  group: {
    slug: string;
    city_name: string;
    markets: WeatherMarket[];
    has_signal: boolean;
    form_profile: CityFormProfile | null;
  };
  marketType: 'max' | 'min';
  useSmartGate: boolean;
  lockedKeysSet: Set<string>;
  showAllBuckets: boolean;
  statusMessages: Record<string, { text: string; color: string }>;
  onLock: (
    city: string,
    cityName: string,
    date: string,
    marketId: string,
    tokenId: string,
    side: string,
    price: number,
    label: string,
    marketType: 'max' | 'min'
  ) => void;
  onUnlock: (city: string, cityName: string, date: string, marketId: string, label: string) => void;
}) {
  const { slug, city_name, markets, has_signal, form_profile } = group;

  return (
    <details className="group bg-white border border-[#dde3ec] rounded-xl px-3.5 py-2.5 mb-2.5 shadow-xs">
      <summary className="cursor-pointer text-sm text-[#1f2430] py-1.5 px-1 font-medium select-none list-none flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[#6b7684] text-xs transition-transform group-open:rotate-90 inline-block">◀</span>
          <b className="text-[14.5px] text-[#1f2430]">{city_name}</b>
          {form_profile?.is_us && <span className="text-xs">🇺🇸</span>}
          {markets.length > 1 && (
            <span className="text-[11.5px] text-[#6b7684] bg-[#f6f8fb] border border-[#dde3ec] rounded-md px-2 py-0.5">
              {markets.length} تاریخ
            </span>
          )}
          {has_signal && (
            <span className="bg-[#e8f8ee] text-[#16a34a] border border-[#16a34a55] rounded-md px-2 py-0.5 text-[10.5px] font-semibold whitespace-nowrap">
              🟢 سیگنال فعال
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {form_profile && (
            <span
              className={`rounded-md px-2 py-0.5 text-[10.5px] font-semibold border whitespace-nowrap ${getFormBadgeClasses(
                form_profile.form_status
              )}`}
              title={form_profile.gate_reason}
            >
              {form_profile.form_label}
            </span>
          )}
          {useSmartGate && form_profile && form_profile.gate_decision !== 'use_calibrated' && (
            <span className="whitespace-nowrap">{getGateDecisionBadge(form_profile.gate_decision)}</span>
          )}
        </div>
      </summary>

      <div className="space-y-2.5 mt-2 pt-2 border-t border-[#eef1f6]">
        {markets.map((m) => (
          <SimpleDateBlock
            key={`${slug}-${m.date}-${marketType}`}
            market={m}
            citySlug={slug}
            cityName={city_name}
            marketType={marketType}
            useSmartGate={useSmartGate}
            lockedKeysSet={lockedKeysSet}
            showAllBuckets={showAllBuckets}
            statusMessages={statusMessages}
            onLock={onLock}
            onUnlock={onUnlock}
          />
        ))}
      </div>
    </details>
  );
}

function SimpleDateBlock({
  market,
  citySlug,
  cityName,
  marketType,
  useSmartGate,
  lockedKeysSet,
  showAllBuckets,
  statusMessages,
  onLock,
  onUnlock,
}: {
  market: WeatherMarket;
  citySlug: string;
  cityName: string;
  marketType: 'max' | 'min';
  useSmartGate: boolean;
  lockedKeysSet: Set<string>;
  showAllBuckets: boolean;
  statusMessages: Record<string, { text: string; color: string }>;
  onLock: (
    city: string,
    cityName: string,
    date: string,
    marketId: string,
    tokenId: string,
    side: string,
    price: number,
    label: string,
    marketType: 'max' | 'min'
  ) => void;
  onUnlock: (city: string, cityName: string, date: string, marketId: string, label: string) => void;
}) {
  const date = market.date;
  const unitSym = market.unit || '';
  const legacyMainId = getMainSignalMarketId(market, false);
  const polyLink = buildPolymarketUrl(citySlug, date, marketType);
  const timeNote = formatHoursLeft(market.hours_left, market.time_status);

  const legacyMean = market.forecast_mean;
  const smartMean = market.smart_forecast_mean ?? legacyMean;
  const hasForecastCorrection =
    useSmartGate && legacyMean != null && smartMean != null && Math.abs(smartMean - legacyMean) >= 0.08;

  const tradableBuckets = (market.full_distribution || []).filter((b) => {
    if (showAllBuckets) return true;
    const mProb = useSmartGate && b.smart_model_prob != null ? b.smart_model_prob : b.model_prob || 0;
    return Math.max(mProb, b.yes_price || 0, b.model_prob || 0) >= BUCKET_PLAUSIBILITY_MIN;
  });

  const subModels = market.model_means_raw ? Object.entries(market.model_means_raw) : [];

  return (
    <details className="bg-[#f6f8fb] border border-[#dde3ec] rounded-[10px] px-3.5 py-2.5 my-2" open={market.full_distribution?.length <= 15}>
      <summary className="cursor-pointer text-sm text-[#1f2430] py-1 px-1 font-medium select-none list-none flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <a
            href={polyLink}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="text-[#2563eb] hover:underline font-bold"
          >
            {cityName} ↗
          </a>
          <span className="text-[#6b7684]">—</span>
          <span className="font-semibold text-[#1f2430] ltr-inline">{date}</span>
          {smartMean != null && (
            <span className="text-xs text-[#6b7684] bg-white border border-[#dde3ec] rounded-md px-2 py-0.5 inline-flex items-center gap-1">
              <span>پیش‌بینی {useSmartGate ? 'هوشمند' : 'مدل'}:</span>
              <b className="text-[#1f2430] ltr-inline">
                {(useSmartGate ? smartMean : legacyMean)?.toFixed(1)}
                {unitSym}
              </b>
              {hasForecastCorrection && (
                <span className="text-[#9333ea] font-semibold ltr-inline">
                  (اصلاح از {legacyMean?.toFixed(1)}{unitSym})
                </span>
              )}
            </span>
          )}
        </div>
        {timeNote && (
          <span className="ltr-inline text-[11.5px] text-[#6b7684] bg-white border border-[#dde3ec] rounded-md px-2 py-0.5">
            ⏱ {timeNote}
          </span>
        )}
      </summary>

      {/* Sub-models & Gate Note Bar */}
      {(subModels.length > 0 || (useSmartGate && market.city_form)) && (
        <div className="flex flex-wrap items-center justify-between gap-2 text-[11.5px] text-[#6b7684] bg-white border border-[#dde3ec] rounded-lg px-3 py-2 mt-2">
          {subModels.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="font-semibold text-[#1f2430] ml-1">زیرمدل‌های هواشناسی:</span>
              {subModels.map(([mname, mval]) => {
                const isBest = market.city_form?.best_submodel === mname;
                const isWorst =
                  market.city_form?.worst_submodel === mname &&
                  market.city_form?.best_submodel !== mname;
                return (
                  <span
                    key={mname}
                    className={`ltr-inline px-2 py-0.5 rounded-md border text-[11px] ${
                      isBest
                        ? 'bg-[#e8f8ee] text-[#16a34a] border-[#16a34a44] font-semibold'
                        : isWorst
                        ? 'bg-[#fdeceb] text-[#dc2626] border-[#dc262644] line-through opacity-75'
                        : 'bg-[#f6f8fb] text-[#1f2430] border-[#dde3ec]'
                    }`}
                  >
                    {mname.toUpperCase()}: {Number(mval).toFixed(1)}
                    {unitSym}
                  </span>
                );
              })}
            </div>
          )}
          {useSmartGate && market.city_form && (
            <div className="text-[11px] text-[#6b7684]">
              💡 <b className="text-[#1f2430]">وضعیت گیت:</b> {market.city_form.gate_reason}
            </div>
          )}
        </div>
      )}

      <div className="overflow-x-auto mt-2.5 rounded-lg border border-[#dde3ec] bg-white">
        {tradableBuckets.length === 0 ? (
          <div className="text-[#6b7684] italic p-3.5 bg-white text-center">داده‌ای موجود نیست.</div>
        ) : (
          <table className="w-full min-w-[580px] border-collapse text-[13px] bg-white tabular-nums">
            <thead>
              <tr className="bg-[#f6f8fb] text-[#6b7684] text-[11.5px] font-semibold border-b border-[#dde3ec]">
                <th className="py-2.5 px-2.5 text-center whitespace-nowrap">باکت</th>
                <th className="py-2.5 px-2.5 text-center whitespace-nowrap">
                  احتمال مدل {useSmartGate && hasForecastCorrection ? '(هوشمند)' : ''}
                </th>
                <th className="py-2.5 px-2.5 text-center whitespace-nowrap">احتمال بازار (YES)</th>
                <th className="py-2.5 px-2.5 text-center whitespace-nowrap">قیمت YES</th>
                <th className="py-2.5 px-2.5 text-center whitespace-nowrap">باور نهایی</th>
                <th className="py-2.5 px-2.5 text-center whitespace-nowrap">EV</th>
                <th className="py-2.5 px-2.5 text-center whitespace-nowrap">عملیات</th>
              </tr>
            </thead>
            <tbody>
              {tradableBuckets.map((b) => {
                const label = formatRangeLabel(b.range, unitSym);
                const modelProb =
                  useSmartGate && hasForecastCorrection && b.smart_model_prob != null
                    ? b.smart_model_prob
                    : b.model_prob;
                const yesPrice = b.yes_price;
                const modelChange =
                  useSmartGate && hasForecastCorrection && b.smart_model_prob_change != null
                    ? b.smart_model_prob_change
                    : b.model_prob_change;
                const priceChange = b.yes_price_change;
                const pricePctChange =
                  b.yes_price_pct_change != null
                    ? b.yes_price_pct_change
                    : priceChange != null && yesPrice != null && yesPrice - priceChange > 0.001
                    ? Number(((priceChange / (yesPrice - priceChange)) * 100).toFixed(1))
                    : priceChange != null
                    ? Number((priceChange * 100).toFixed(1))
                    : null;

                const price10mChange = b.yes_price_10m_change ?? priceChange;
                const price6hChange = b.yes_price_6h_change ?? priceChange;
                const price10mPct = b.yes_price_10m_pct_change ?? pricePctChange;
                const price6hPct = b.yes_price_6h_pct_change ?? pricePctChange;

                const fmtSignedPct = (val: number) => {
                  if (Math.abs(val) < 0.05) return '0.0%';
                  return val > 0 ? `+${val.toFixed(1)}%` : `${val.toFixed(1)}%`;
                };

                const renderPctParen = (pctVal: number | null | undefined, pct6hVal?: number | null) => {
                  if (pctVal == null && pct6hVal == null) return null;
                  const v10 = Number(pctVal ?? pct6hVal ?? 0);
                  const v6 = pct6hVal != null ? Number(pct6hVal) : null;
                  const hasDistinct6h = v6 != null && Math.abs(v6 - v10) >= 0.05 && Math.abs(v6) >= 0.05;

                  const primaryVal = Math.abs(v10) >= 0.05 ? v10 : v6 ?? 0;
                  const primaryCls =
                    Math.abs(primaryVal) < 0.05
                      ? 'text-[#6b7684]'
                      : primaryVal > 0
                      ? 'text-[#16a34a] font-semibold'
                      : 'text-[#dc2626] font-semibold';

                  if (!hasDistinct6h) {
                    return (
                      <span className={`ltr-inline ${primaryCls} text-[11.5px] ml-1`}>
                        ({fmtSignedPct(primaryVal)})
                      </span>
                    );
                  }

                  const cls10 =
                    Math.abs(v10) < 0.05
                      ? 'text-[#6b7684]'
                      : v10 > 0
                      ? 'text-[#16a34a] font-semibold'
                      : 'text-[#dc2626] font-semibold';
                  const cls6 =
                    Math.abs(v6!) < 0.05
                      ? 'text-[#6b7684]'
                      : v6! > 0
                      ? 'text-[#16a34a] font-semibold'
                      : 'text-[#dc2626] font-semibold';

                  return (
                    <>
                      <span
                        className={`ltr-inline ${cls10} text-[11.5px] ml-1`}
                        title="تغییر نسبت به اسکن ۱۰ دقیقه‌ای قبلی"
                      >
                        ({fmtSignedPct(v10)})
                      </span>
                      <div
                        className="text-[10.5px] text-[#6b7684] leading-tight mt-0.5 ltr-inline block"
                        title="تغییر نسبت به اسکن ۶ ساعته قبلی"
                      >
                        (6h: <span className={cls6}>{fmtSignedPct(v6!)}</span>)
                      </div>
                    </>
                  );
                };

                const modelChangePct = modelChange != null ? modelChange * 100 : null;
                const market10mPct = price10mChange != null ? price10mChange * 100 : null;
                const market6hPct = price6hChange != null ? price6hChange * 100 : null;

                const beliefVal =
                  useSmartGate && hasForecastCorrection && b.smart_belief_prob != null
                    ? b.smart_belief_prob
                    : b.belief_prob;
                const beliefStr = beliefVal != null ? `${(beliefVal * 100).toFixed(1)}%` : '-';

                const evVal =
                  useSmartGate && hasForecastCorrection && b.smart_ev != null ? b.smart_ev : b.ev;
                const evPct = evVal != null ? evVal * 100 : null;

                const marketId = String(b.market_id || '');
                const yesToken = b.yes_token_id || '';
                const isLegacyMain = legacyMainId !== null && marketId === legacyMainId;
                const isSmartMain = Boolean(b.is_smart_main);
                const isLocked = lockedKeysSet.has(`${citySlug}|${date}|${marketId}`);
                const statusObj = statusMessages[marketId];

                return (
                  <tr
                    key={marketId}
                    className={`border-b border-[#dde3ec] last:border-b-0 text-center hover:bg-gray-50/70 ${
                      useSmartGate && isSmartMain ? 'bg-[#e8f8ee]/40' : ''
                    }`}
                  >
                    <td className="py-2.5 px-2.5 font-medium whitespace-nowrap align-middle">
                      <span className="ltr-inline font-semibold text-[#1f2430]">{label}</span>
                      {getSignalBadge(isLegacyMain, isSmartMain, evVal, useSmartGate)}
                    </td>
                    <td className="py-2.5 px-2.5 whitespace-nowrap align-middle" dir="ltr">
                      <span className="font-medium text-[#1f2430]">
                        {modelProb != null ? `${(modelProb * 100).toFixed(1)}%` : '-'}
                      </span>
                      {modelProb != null && renderPctParen(modelChangePct)}
                    </td>
                    <td className="py-2.5 px-2.5 whitespace-nowrap align-middle" dir="ltr">
                      <span className="font-medium text-[#1f2430]">
                        {yesPrice != null ? `${(yesPrice * 100).toFixed(1)}%` : '-'}
                      </span>
                      {yesPrice != null && renderPctParen(market10mPct, market6hPct)}
                    </td>
                    <td className="py-2.5 px-2.5 whitespace-nowrap align-middle" dir="ltr">
                      <span className="font-medium text-[#1f2430]">
                        {yesPrice != null ? yesPrice.toFixed(3) : '-'}
                      </span>
                      {yesPrice != null && renderPctParen(price10mPct, price6hPct)}
                    </td>
                    <td className="py-2.5 px-2.5 whitespace-nowrap align-middle font-semibold text-[#1f2430]" dir="ltr">
                      {beliefStr}
                    </td>
                    <td className="py-2.5 px-2.5 whitespace-nowrap align-middle" dir="ltr">
                      {evPct == null ? (
                        '-'
                      ) : (
                        <span
                          className={
                            evPct > 0
                              ? 'text-[#16a34a] font-semibold'
                              : evPct < 0
                              ? 'text-[#dc2626] font-semibold'
                              : 'text-[#6b7684]'
                          }
                        >
                          {evPct > 0 ? `+${evPct.toFixed(1)}%` : `${evPct.toFixed(1)}%`}
                        </span>
                      )}
                    </td>
                    <td className="py-2.5 px-2.5 whitespace-nowrap align-middle">
                      {yesPrice != null && marketId && (
                        <div>
                          {isLocked ? (
                            <>
                              <span className="text-[#16a34a] text-[11.5px] font-semibold block mb-1">
                                قفل ✔
                              </span>
                              <button
                                onClick={() => onUnlock(citySlug, cityName, date, marketId, label)}
                                className="bg-[#dc2626] hover:bg-[#b91c1c] text-white border-none rounded-[9px] px-3.5 py-1.5 text-xs font-semibold cursor-pointer"
                              >
                                حذف قفل
                              </button>
                            </>
                          ) : (
                            <button
                              onClick={() =>
                                onLock(citySlug, cityName, date, marketId, yesToken, 'YES', yesPrice, label, marketType)
                              }
                              className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white border-none rounded-[9px] px-4 py-1.5 text-xs font-semibold cursor-pointer"
                            >
                              قفل کن
                            </button>
                          )}
                          {statusObj && (
                            <span
                              className="block text-[10.5px] mt-1 max-w-[170px] mx-auto leading-tight"
                              style={{ color: statusObj.color }}
                            >
                              {statusObj.text}
                            </span>
                          )}
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </details>
  );
}

function DailyPnlBlock({
  dayGroup,
  defaultOpen,
}: {
  dayGroup: { date: string; avg_pct: number; count: number; items: LockedSignal[] };
  defaultOpen: boolean;
}) {
  const { date, avg_pct, items } = dayGroup;
  return (
    <details open={defaultOpen} className="bg-[#f6f8fb] border border-[#dde3ec] rounded-[10px] px-3.5 py-2.5 my-2">
      <summary className="cursor-pointer text-sm font-medium py-1.5 select-none">
        <span className="font-semibold ltr-inline">{date}</span> —{' '}
        <span className={avg_pct >= 0 ? 'text-[#16a34a] font-semibold ltr-inline' : 'text-[#dc2626] font-semibold ltr-inline'}>
          {avg_pct >= 0 ? `+${avg_pct.toFixed(1)}%` : `${avg_pct.toFixed(1)}%`}
        </span>{' '}
        <span className="text-xs text-[#6b7684] mr-2">({items.length} معامله بسته‌شده)</span>
      </summary>
      <div className="overflow-x-auto mt-2 rounded-lg border border-[#dde3ec] bg-white">
        <table className="w-full min-w-[480px] border-collapse text-[13px] bg-white tabular-nums">
          <thead>
            <tr className="bg-[#f6f8fb] text-[#6b7684] text-[11.5px] font-semibold border-b border-[#dde3ec]">
              <th className="py-2 px-2 text-center">شهر</th>
              <th className="py-2 px-2 text-center">تاریخ هدف</th>
              <th className="py-2 px-2 text-center">باکت</th>
              <th className="py-2 px-2 text-center">ورود</th>
              <th className="py-2 px-2 text-center">خروج</th>
              <th className="py-2 px-2 text-center">درصد</th>
              <th className="py-2 px-2 text-center">دلیل</th>
            </tr>
          </thead>
          <tbody>
            {items.map((l, i) => {
              const pct = l.outcome_pct ?? 0;
              const reason = l.status === 'closed_resolved' ? 'resolve خودکار' : 'حذف دستی';
              return (
                <tr key={i} className="border-b border-[#dde3ec] last:border-b-0 text-center">
                  <td className="py-2 px-2 font-medium">{l.city_name}</td>
                  <td className="py-2 px-2" dir="ltr">{l.date}</td>
                  <td className="py-2 px-2" dir="ltr">{l.bucket_label || '-'}</td>
                  <td className="py-2 px-2" dir="ltr">{l.entry_price != null ? l.entry_price.toFixed(3) : '-'}</td>
                  <td className="py-2 px-2" dir="ltr">{l.exit_price != null ? l.exit_price.toFixed(3) : '-'}</td>
                  <td className="py-2 px-2" dir="ltr">
                    <span className={pct >= 0 ? 'text-[#16a34a] font-semibold' : 'text-[#dc2626] font-semibold'}>
                      {pct >= 0 ? `+${pct.toFixed(1)}%` : `${pct.toFixed(1)}%`}
                    </span>
                  </td>
                  <td className="py-2 px-2 text-xs text-[#6b7684]">{reason}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </details>
  );
}

/* ============================================================================
   Sub-components for Pro Terminal View (dashboard.py v3/v5)
   ============================================================================ */

function ProDashboardView({
  data,
  citySearch,
  setCitySearch,
  commitMessages,
  onCommitMoment,
}: {
  data: DashboardDataResponse;
  citySearch: string;
  setCitySearch: (v: string) => void;
  commitMessages: Record<string, string>;
  onCommitMoment: (marketKey: string) => void;
}) {
  const allOpenMarkets = useMemo(() => {
    const combined = [...data.open_max_markets, ...data.open_min_markets];
    if (!citySearch.trim()) return combined;
    const q = citySearch.trim().toLowerCase();
    return combined.filter(
      (m) => m.city_name.toLowerCase().includes(q) || m.city.toLowerCase().includes(q)
    );
  }, [data.open_max_markets, data.open_min_markets, citySearch]);

  const signalMarkets = useMemo(
    () =>
      allOpenMarkets.filter(
        (m) =>
          (m.committed_allocation && m.committed_allocation.length > 0) ||
          (m.live_allocation && m.live_allocation.length > 0)
      ),
    [allOpenMarkets]
  );

  const resolvedByDate = useMemo(() => {
    const groups: Record<string, WeatherMarket[]> = {};
    for (const m of data.resolved_markets) {
      if (citySearch.trim()) {
        const q = citySearch.trim().toLowerCase();
        if (!m.city_name.toLowerCase().includes(q) && !m.city.toLowerCase().includes(q)) continue;
      }
      const d = m.date || '?';
      if (!groups[d]) groups[d] = [];
      groups[d].push(m);
    }
    return Object.keys(groups)
      .sort((a, b) => b.localeCompare(a))
      .map((date) => ({
        date,
        markets: groups[date].sort((a, b) => (a.city_name || '').localeCompare(b.city_name || '')),
      }));
  }, [data.resolved_markets, citySearch]);

  return (
    <div className="text-[#e6e6e6]">
      <nav className="sticky top-0 z-40 flex gap-2.5 flex-wrap bg-[#0f1115] py-2.5 mb-3 border-b border-[#2a2e37]">
        <a
          href="#sec-signals"
          className="text-[#c7ccd1] bg-[#181b21] border border-[#2a2e37] rounded-lg px-3.5 py-1.5 text-xs hover:bg-[#232730] hover:text-white no-underline"
        >
          سیگنال‌ها ({signalMarkets.length})
        </a>
        <a
          href="#sec-open"
          className="text-[#c7ccd1] bg-[#181b21] border border-[#2a2e37] rounded-lg px-3.5 py-1.5 text-xs hover:bg-[#232730] hover:text-white no-underline"
        >
          بازارهای باز ({allOpenMarkets.length})
        </a>
        <a
          href="#sec-resolved"
          className="text-[#c7ccd1] bg-[#181b21] border border-[#2a2e37] rounded-lg px-3.5 py-1.5 text-xs hover:bg-[#232730] hover:text-white no-underline"
        >
          بازارهای حل‌شده ({data.resolved_markets.length})
        </a>
        <a
          href="#sec-no-signal"
          className="text-[#c7ccd1] bg-[#181b21] border border-[#2a2e37] rounded-lg px-3.5 py-1.5 text-xs hover:bg-[#232730] hover:text-white no-underline"
        >
          بدون سیگنال ({data.no_signal_markets.length})
        </a>
        <a
          href="#sec-city-stats"
          className="text-[#c7ccd1] bg-[#181b21] border border-[#2a2e37] rounded-lg px-3.5 py-1.5 text-xs hover:bg-[#232730] hover:text-white no-underline"
        >
          آمار شهرها ({data.city_stats.length})
        </a>
        <input
          type="text"
          value={citySearch}
          onChange={(e) => setCitySearch(e.target.value)}
          placeholder="فیلتر شهر..."
          className="bg-[#181b21] border border-[#2a2e37] text-[#e6e6e6] rounded-lg px-3 py-1 text-xs mr-auto"
        />
      </nav>

      <div className="text-[#9aa0a6] text-[13px] mb-4">
        آخرین به‌روزرسانی: {data.meta.last_update_iran} &nbsp;&nbsp;&nbsp;&nbsp;(با اصلاح محاسبه دقت باکت‌های ۲ درجه‌ای فارنهایت آمریکا + گیت کالیبراسیون ۱۴ روزه)
      </div>

      <div className="text-[13px] text-[#9aa0a6] my-1.5">-- استراتژی کامل (همه‌ی سیگنال‌ها) --</div>
      <div className="flex gap-4 flex-wrap mb-4">
        <div className="bg-[#181b21] border border-[#2a2e37] rounded-[10px] px-5 py-3.5 min-w-[140px]">
          <div className="text-[#9aa0a6] text-xs mb-1.5">تراز سود/زیان کل</div>
          <div className="text-[22px] font-bold ltr-inline">
            {data.summary.pnl_units > 0 ? `+${data.summary.pnl_units}` : data.summary.pnl_units}u
          </div>
        </div>
        <div className="bg-[#181b21] border border-[#2a2e37] rounded-[10px] px-5 py-3.5 min-w-[140px]">
          <div className="text-[#9aa0a6] text-xs mb-1.5">نرخ برد</div>
          <div className="text-[22px] font-bold">{data.summary.win_rate}%</div>
        </div>
        <div className="bg-[#181b21] border border-[#2a2e37] rounded-[10px] px-5 py-3.5 min-w-[140px]">
          <div className="text-[#9aa0a6] text-xs mb-1.5">تعداد کل معاملات</div>
          <div className="text-[22px] font-bold">{data.summary.total_trades}</div>
        </div>
        <div className="bg-[#181b21] border border-[#2a2e37] rounded-[10px] px-5 py-3.5 min-w-[140px]">
          <div className="text-[#9aa0a6] text-xs mb-1.5">بازارهای باز</div>
          <div className="text-[22px] font-bold">{data.summary.open_count}</div>
        </div>
        <div className="bg-[#181b21] border border-[#2a2e37] rounded-[10px] px-5 py-3.5 min-w-[140px]">
          <div className="text-[#9aa0a6] text-xs mb-1.5">تعداد شهرها</div>
          <div className="text-[22px] font-bold">{data.meta.city_count}</div>
        </div>
      </div>

      <div id="sec-signals" className="text-base font-bold mt-7 mb-3 pb-2 border-b border-[#2a2e37]">
        سیگنال‌ها ({signalMarkets.length})
      </div>
      {signalMarkets.length === 0 ? (
        <div className="text-[#6b7280] italic py-3">سیگنال فعالی در این لحظه وجود ندارد.</div>
      ) : (
        signalMarkets.map((m) => (
          <ProMarketBlock
            key={`sig-${m.file_key || `${m.city}_${m.date}`}`}
            market={m}
            accuracy={data.accuracy_by_city[m.city]}
            commitMsg={commitMessages[`${m.city}_${m.date}`]}
            onCommit={() => onCommitMoment(`${m.city}_${m.date}`)}
          />
        ))
      )}

      <div id="sec-open" className="text-base font-bold mt-7 mb-3 pb-2 border-b border-[#2a2e37]">
        بازارهای باز ({allOpenMarkets.length})
      </div>
      {allOpenMarkets.length === 0 ? (
        <div className="text-[#6b7280] italic py-3">بازار باز موجود نیست.</div>
      ) : (
        allOpenMarkets.map((m) => (
          <ProMarketBlock
            key={`open-${m.file_key || `${m.city}_${m.date}`}`}
            market={m}
            accuracy={data.accuracy_by_city[m.city]}
            commitMsg={commitMessages[`${m.city}_${m.date}`]}
            onCommit={() => onCommitMoment(`${m.city}_${m.date}`)}
          />
        ))
      )}

      <div id="sec-resolved" className="text-base font-bold mt-7 mb-3 pb-2 border-b border-[#2a2e37]">
        بازارهای حل‌شده
      </div>
      {resolvedByDate.map((grp, idx) => {
        const wins = grp.markets.filter((m) => m.resolved_outcome === 'win').length;
        const total = grp.markets.length;
        const pnlSum = grp.markets.reduce((acc, m) => acc + Number(m.pnl_units || 0), 0);
        return (
          <details
            key={grp.date}
            open={idx < 2}
            className="bg-[#101216] border border-[#232730] rounded-[10px] px-4 py-2.5 mb-3.5"
          >
            <summary className="cursor-pointer text-[15px] text-[#e6e6e6] py-1">
              <span className="font-semibold">{grp.date}</span> &nbsp;&nbsp;
              {total} بازار &nbsp;&nbsp;
              {wins}/{total} ({total > 0 ? Math.round((wins / total) * 100) : 0}%) &nbsp;&nbsp;
              PnL: <span className={pnlSum >= 0 ? 'text-[#4ade80] ltr-inline' : 'text-[#f87171] ltr-inline'}>
                {pnlSum >= 0 ? `+${pnlSum.toFixed(1)}u` : `${pnlSum.toFixed(1)}u`}
              </span>
            </summary>
            <div className="mt-2 space-y-2">
              {grp.markets.map((m) => (
                <ProResolvedMarketBlock key={`res-${m.file_key || `${m.city}_${m.date}`}`} market={m} />
              ))}
            </div>
          </details>
        );
      })}

      <div id="sec-city-stats" className="text-base font-bold mt-7 mb-3 pb-2 border-b border-[#2a2e37]">
        آمار به‌تفکیک شهر
      </div>
      <div className="overflow-x-auto bg-[#14161b] border border-[#2a2e37] rounded-[10px] p-3">
        <table className="w-full border-collapse text-[12.5px] tabular-nums">
          <thead>
            <tr className="text-[#9aa0a6] border-b border-[#23262d]">
              <th className="py-2 px-2">شهر</th>
              <th className="py-2 px-2">تعداد کل</th>
              <th className="py-2 px-2">برد استراتژی</th>
              <th className="py-2 px-2">نرخ برد</th>
              <th className="py-2 px-2">PnL کل (واحد)</th>
              <th className="py-2 px-2">دقت پیش‌بینی دما (۱۴ روز اخیر)</th>
            </tr>
          </thead>
          <tbody>
            {data.city_stats.map((cs) => (
              <tr key={cs.city} className="border-b border-[#23262d] last:border-b-0 text-center">
                <td className="py-2 px-2 font-semibold">{cs.city_name}</td>
                <td className="py-2 px-2">{cs.total}</td>
                <td className="py-2 px-2 text-[#4ade80]">{cs.wins}</td>
                <td className="py-2 px-2">{cs.total > 0 ? Math.round((cs.wins / cs.total) * 100) : 0}%</td>
                <td className={`py-2 px-2 font-semibold ltr-inline ${cs.pnl_sum >= 0 ? 'text-[#4ade80]' : 'text-[#f87171]'}`}>
                  {cs.pnl_sum >= 0 ? `+${cs.pnl_sum.toFixed(1)}` : cs.pnl_sum.toFixed(1)}
                </td>
                <td className="py-2 px-2 text-[#60a5fa] font-semibold">
                  {cs.recent_accuracy_pct != null
                    ? `${cs.recent_accuracy_pct}%`
                    : cs.accuracy_pct != null
                    ? `${cs.accuracy_pct}%`
                    : '-'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ProMarketBlock({
  market,
  accuracy,
  commitMsg,
  onCommit,
}: {
  market: WeatherMarket;
  accuracy?: number | null;
  commitMsg?: string;
  onCommit: () => void;
}) {
  const unitSym = market.unit || '';
  const committed = market.committed_allocation;
  const liveAlloc = market.live_allocation;
  const isCommitted = Boolean(committed && committed.length > 0);
  const bestPnl = isCommitted
    ? market.committed_best_case_pnl_units ?? market.best_case_pnl_units
    : market.best_case_pnl_units;
  const worstPnl = isCommitted
    ? market.committed_worst_case_pnl_units ?? market.worst_case_pnl_units
    : market.worst_case_pnl_units;
  const successProb = isCommitted
    ? market.committed_success_probability ?? market.success_probability
    : market.success_probability;
  const confidence = isCommitted
    ? market.committed_confidence ?? market.confidence
    : market.confidence;

  const polyUrl = buildPolymarketUrl(market.city, market.date, market.market_type);

  return (
    <details className="bg-[#14161b] border border-[#2a2e37] rounded-[10px] px-4 py-2.5 mb-2.5">
      <summary className="cursor-pointer text-sm text-[#c7ccd1] py-1.5 list-none flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <b className="text-white text-[14.5px]">{market.city_name}</b>
          <span className="ltr-inline text-xs text-[#9aa0a6] bg-[#1e222d] border border-[#2a2e37] px-2 py-0.5 rounded-md">
            {market.date}
          </span>
          <span className="text-xs text-[#9aa0a6]">
            ({market.market_type === 'min' ? '❄️ کمینه' : '🔥 بیشینه'})
          </span>
          <span className="text-xs bg-[#181b21] border border-[#2a2e37] px-2 py-0.5 rounded-md">
            پیش‌بینی هوشمند:{' '}
            <b className="text-white ltr-inline">
              {(market.smart_forecast_mean ?? market.forecast_mean) != null
                ? `${(market.smart_forecast_mean ?? market.forecast_mean)!.toFixed(1)}${unitSym}`
                : '-'}
            </b>
          </span>
          {market.hours_left != null && (
            <span className="ltr-inline text-xs text-[#9aa0a6]">⏱ {market.hours_left.toFixed(1)}h</span>
          )}
          {isCommitted ? (
            <span className="text-[#4ade80] bg-[#16a34a22] border border-[#16a34a55] px-2 py-0.5 rounded-md text-[11px] font-semibold">
              قفل‌شده — {committed?.length} پوزیشن
            </span>
          ) : (
            <span className="text-[#eab308] bg-[#eab30822] border border-[#eab30855] px-2 py-0.5 rounded-md text-[11px] font-semibold">
              در حال پایش
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3 text-[11.5px] text-[#9aa0a6]">
          <span>
            دقت اخیر:{' '}
            <b className="text-[#60a5fa]">
              {market.city_form?.recent_hit_pct != null
                ? `${market.city_form.recent_hit_pct}%`
                : accuracy != null
                ? `${accuracy}%`
                : '-'}
            </b>
          </span>
          <span>
            بهترین حالت:{' '}
            <b className="text-[#4ade80] ltr-inline">
              {bestPnl != null ? `${bestPnl >= 0 ? '+' : ''}${bestPnl.toFixed(2)}u` : '-'}
            </b>
          </span>
          <span>
            بدترین حالت:{' '}
            <b className="text-[#f87171] ltr-inline">
              {worstPnl != null ? `${worstPnl >= 0 ? '+' : ''}${worstPnl.toFixed(2)}u` : '-'}
            </b>
          </span>
          <span>
            برد: <b className="text-[#60a5fa]">{successProb != null ? `${Math.round(successProb * 100)}%` : '-'}</b>
          </span>
          {confidence != null && (
            <span className="text-[#c084fc] font-semibold">اطمینان: {Math.round(confidence * 100)}%</span>
          )}
        </div>
      </summary>

      <div className="flex items-center gap-2.5 my-2 px-2.5 py-2 bg-[#101216] border border-[#232730] rounded-lg">
        {isCommitted ? (
          <span className="text-[#4ade80] text-xs font-semibold">قفل‌شده ✔</span>
        ) : (
          <>
            <button
              onClick={onCommit}
              className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white border-none rounded-md px-3.5 py-1.5 text-xs cursor-pointer"
            >
              قفل کردن این لحظه
            </button>
            <span className="text-xs text-[#9aa0a6]">
              {commitMsg || '-- ثبت در lock_requests.txt'}
            </span>
          </>
        )}
      </div>

      <ProUnifiedBucketTable
        distribution={market.full_distribution || []}
        committedAllocation={committed || null}
        liveAllocation={liveAlloc || null}
        unitSym={unitSym}
        showResult={false}
        balanceUnits={market.balance_units || 100}
      />

      <div className="mt-2 text-xs">
        <a href={polyUrl} target="_blank" rel="noopener noreferrer" className="text-[#60a5fa] hover:underline">
          مشاهده این بازار در Polymarket ↗
        </a>
      </div>
    </details>
  );
}

function ProResolvedMarketBlock({ market }: { market: WeatherMarket }) {
  const unitSym = market.unit || '';
  const isWin = market.resolved_outcome === 'win';
  const pnl = Number(market.pnl_units || 0);
  const gradedAllocation = market.graded_allocation || market.committed_allocation || [];

  return (
    <details className="bg-[#14161b] border border-[#2a2e37] rounded-[10px] px-4 py-2">
      <summary className="cursor-pointer text-sm text-[#c7ccd1] py-1 list-none flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <b className="text-white">{market.city_name}</b>
          <span className="text-xs text-[#9aa0a6]">
            پیش‌بینی: <b className="ltr-inline text-[#e6e6e6]">{market.forecast_mean != null ? `${market.forecast_mean.toFixed(1)}${unitSym}` : '-'}</b>
            {' — '}
            واقعی: <b className="ltr-inline text-[#e6e6e6]">{market.actual_temp != null ? `${market.actual_temp.toFixed(1)}${unitSym}` : '-'}</b>
          </span>
        </div>
        <span className={isWin ? 'text-[#4ade80] font-bold text-xs' : 'text-[#f87171] font-bold text-xs'}>
          {isWin ? 'برد' : 'باخت'} <span className="ltr-inline">{pnl >= 0 ? `+${pnl.toFixed(2)}u` : `${pnl.toFixed(2)}u`}</span>
        </span>
      </summary>
      <ProUnifiedBucketTable
        distribution={market.full_distribution || []}
        committedAllocation={gradedAllocation}
        liveAllocation={null}
        unitSym={unitSym}
        showResult={true}
        balanceUnits={market.committed_balance_units || market.balance_units || 100}
      />
    </details>
  );
}

function ProUnifiedBucketTable({
  distribution,
  committedAllocation,
  liveAllocation,
  unitSym,
  showResult,
  balanceUnits,
}: {
  distribution: BucketDistribution[];
  committedAllocation: AllocationLeg[] | null;
  liveAllocation: AllocationLeg[] | null;
  unitSym: string;
  showResult: boolean;
  balanceUnits: number;
}) {
  const displayAlloc = committedAllocation && committedAllocation.length > 0 ? committedAllocation : liveAllocation || [];
  const allocByKey = new Map<string, AllocationLeg>();
  let totalUnits = 0;
  for (const a of displayAlloc) {
    allocByKey.set(`${a.market_id}|${a.side}`, a);
    totalUnits += Number(a.units || 0);
  }
  const denom = totalUnits || balanceUnits || 100;

  const renderLegRow = (b: BucketDistribution, alloc: AllocationLeg | null, keySuffix: string) => {
    const label = formatRangeLabel(b.range, unitSym);
    let yesPrice = b.yes_price ?? b.ask ?? 0;
    let noPrice = b.no_price ?? Number((1 - yesPrice).toFixed(4));

    if (alloc && alloc.price != null) {
      if (alloc.side === 'YES') {
        yesPrice = alloc.price;
        noPrice = Number((1 - alloc.price).toFixed(4));
      } else {
        noPrice = alloc.price;
        yesPrice = Number((1 - alloc.price).toFixed(4));
      }
    }

    const role = alloc?.role;
    const rowBg =
      role === 'main_signal'
        ? 'bg-green-500/10'
        : role === 'emergency_hedge'
        ? 'bg-red-500/10'
        : role
        ? 'bg-yellow-500/10'
        : 'opacity-55';

    const sideDisplay = alloc ? alloc.side : 'YES';
    const pctCapital = alloc && alloc.units != null ? ((alloc.units / denom) * 100).toFixed(1) : '-';
    const edgeVal = alloc ? alloc.ev : b.ev;
    const edgeStr = edgeVal != null ? `${edgeVal >= 0 ? '+' : ''}${edgeVal.toFixed(3)}` : '-';

    let sellStr = '-';
    if (alloc && alloc.sell_value != null && alloc.price != null && alloc.price > 0) {
      const drift = ((alloc.sell_value - alloc.price) / alloc.price) * 100;
      sellStr = `${alloc.sell_value.toFixed(3)} (${drift >= 0 ? '+' : ''}${drift.toFixed(1)}%)`;
    }

    const modelChangePct = b.model_prob_change != null ? b.model_prob_change * 100 : null;
    const marketChangePct = b.yes_price_change != null ? b.yes_price_change * 100 : null;
    const pricePctChange = b.yes_price_pct_change ?? marketChangePct;

    const renderProParen = (pctVal: number | null | undefined) => {
      if (pctVal == null || Math.abs(pctVal) < 0.05) return null;
      const cls = pctVal > 0 ? 'text-[#4ade80]' : 'text-[#f87171]';
      return (
        <span className={`ltr-inline ${cls} text-[11px] ml-1`}>
          ({pctVal > 0 ? `+${pctVal.toFixed(1)}%` : `${pctVal.toFixed(1)}%`})
        </span>
      );
    };

    return (
      <tr key={`${b.market_id}-${keySuffix}`} className={`${rowBg} border-b border-[#23262d] text-center`}>
        <td className="py-1.5 px-2 whitespace-nowrap align-middle" dir="ltr">{label}</td>
        <td className="py-1.5 px-2 whitespace-nowrap align-middle">{sideDisplay}</td>
        <td className="py-1.5 px-2 whitespace-nowrap align-middle" dir="ltr">
          {((b.model_prob || 0) * 100).toFixed(1)}%{renderProParen(modelChangePct)}
        </td>
        <td className="py-1.5 px-2 whitespace-nowrap align-middle" dir="ltr">
          {(yesPrice * 100).toFixed(1)}%{renderProParen(marketChangePct)}
        </td>
        <td className="py-1.5 px-2 whitespace-nowrap align-middle" dir="ltr">
          {b.belief_prob != null ? `${(b.belief_prob * 100).toFixed(1)}%` : '-'}
        </td>
        <td className="py-1.5 px-2 whitespace-nowrap align-middle" dir="ltr">
          {yesPrice.toFixed(3)}{renderProParen(pricePctChange)}
        </td>
        <td className="py-1.5 px-2 whitespace-nowrap align-middle" dir="ltr">{noPrice.toFixed(3)}</td>
        <td className="py-1.5 px-2 whitespace-nowrap align-middle" dir="ltr">{edgeStr}</td>
        <td className="py-1.5 px-2 whitespace-nowrap align-middle">
          {role === 'main_signal' ? (
            <span className="px-2 py-0.5 rounded text-[11px] bg-[#16a34a33] text-[#4ade80] border border-[#16a34a]">
              main
            </span>
          ) : role === 'emergency_hedge' ? (
            <span className="px-2 py-0.5 rounded text-[11px] bg-[#dc262633] text-[#f87171] border border-[#dc2626]">
              emergency
            </span>
          ) : role ? (
            <span className="px-2 py-0.5 rounded text-[11px] bg-[#ca8a0433] text-[#fbbf24] border border-[#ca8a04]">
              hedge
            </span>
          ) : (
            '-'
          )}
        </td>
        <td className="py-1.5 px-2 whitespace-nowrap align-middle" dir="ltr">{pctCapital}</td>
        {showResult && (
          <>
            <td className="py-1.5 px-2 whitespace-nowrap align-middle">
              {alloc ? (alloc.won == null ? '-' : alloc.won ? 'برد' : 'باخت') : '-'}
            </td>
            <td className="py-1.5 px-2 whitespace-nowrap align-middle" dir="ltr">
              {alloc && alloc.pnl_units != null
                ? `${alloc.pnl_units >= 0 ? '+' : ''}${alloc.pnl_units.toFixed(2)}`
                : '-'}
            </td>
          </>
        )}
        <td className="py-1.5 px-2 whitespace-nowrap align-middle" dir="ltr">{sellStr}</td>
      </tr>
    );
  };

  return (
    <div className="overflow-x-auto mt-2">
      <table className="w-full min-w-[680px] border-collapse text-[12.5px] tabular-nums">
        <thead>
          <tr className="text-[#9aa0a6] border-b border-[#23262d]">
            <th className="py-1.5 px-2 font-normal text-center whitespace-nowrap">محدوده</th>
            <th className="py-1.5 px-2 font-normal text-center whitespace-nowrap">سمت</th>
            <th className="py-1.5 px-2 font-normal text-center whitespace-nowrap">احتمال مدل</th>
            <th className="py-1.5 px-2 font-normal text-center whitespace-nowrap">احتمال بازار</th>
            <th className="py-1.5 px-2 font-normal text-center whitespace-nowrap">باور نهایی</th>
            <th className="py-1.5 px-2 font-normal text-center whitespace-nowrap">قیمت YES</th>
            <th className="py-1.5 px-2 font-normal text-center whitespace-nowrap">قیمت NO</th>
            <th className="py-1.5 px-2 font-normal text-center whitespace-nowrap">Edge</th>
            <th className="py-1.5 px-2 font-normal text-center whitespace-nowrap">نقش</th>
            <th className="py-1.5 px-2 font-normal text-center whitespace-nowrap">٪ سرمایه</th>
            {showResult && (
              <>
                <th className="py-1.5 px-2 font-normal text-center whitespace-nowrap">نتیجه</th>
                <th className="py-1.5 px-2 font-normal text-center whitespace-nowrap">PnL</th>
              </>
            )}
            <th className="py-1.5 px-2 font-normal text-center whitespace-nowrap">فروش</th>
          </tr>
        </thead>
        <tbody>
          {distribution.map((b) => {
            const yesAlloc = allocByKey.get(`${b.market_id}|YES`) || null;
            const noAlloc = allocByKey.get(`${b.market_id}|NO`) || null;
            const legs = [yesAlloc, noAlloc].filter((x): x is AllocationLeg => x !== null);
            if (legs.length === 0) {
              return renderLegRow(b, null, 'none');
            }
            return legs.map((leg) => renderLegRow(b, leg, leg.side));
          })}
        </tbody>
      </table>
    </div>
  );
}
