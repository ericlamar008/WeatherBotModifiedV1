import React, { useState, useEffect, useMemo } from 'react';
import { LockedSignal, PolymarketAccountStatus } from './types';

export interface OrderModalContext {
  mode: 'create' | 'edit';
  city: string;
  cityName: string;
  date: string;
  marketId: string;
  tokenId: string;
  noTokenId?: string;
  side: 'YES' | 'NO';
  yesPrice: number;
  noPrice?: number;
  bestBid?: number | null;
  bestAsk?: number | null;
  modelProb?: number | null;
  beliefProb?: number | null;
  ev?: number | null;
  label: string;
  marketType: 'max' | 'min';
  forecastMean?: number | null;
  unit?: string;
  existingLock?: LockedSignal | null;
}

interface OrderConfigModalProps {
  context: OrderModalContext | null;
  onClose: () => void;
  onSuccess: () => Promise<void>;
}

/**
 * Pure helper implementing Chapter 5 of the Master Blueprint:
 * Smart Auto-Pilot calculation for Entry, Limit Buy, Model-Fair-Value TP, and Tiered SL.
 */
function computeSmartAutoPilot(params: {
  marketPrice: number;
  modelProb: number | null;
  beliefProb: number | null;
  ev: number | null;
  entryOrderType: 'IMMEDIATE_MARKETABLE' | 'CONDITIONAL_LIMIT_GTC';
  tickSize: number;
}) {
  const tick = params.tickSize > 0 ? params.tickSize : 0.01;
  const roundDownTick = (val: number) => {
    const steps = Math.floor((val + 1e-9) / tick);
    const res = Number((steps * tick).toFixed(4));
    return Math.max(tick, Math.min(1 - tick, res));
  };

  const ask = roundDownTick(Math.max(0.01, Math.min(0.99, params.marketPrice)));
  const mProb = params.modelProb != null && params.modelProb > 0 ? params.modelProb : ask;
  const bProb = params.beliefProb != null && params.beliefProb > 0 ? params.beliefProb : mProb;
  const evVal = params.ev ?? 0;

  // 1. Entry Price (Marketable vs Conditional Limit GTC)
  let entryPrice = ask;
  if (params.entryOrderType === 'CONDITIONAL_LIMIT_GTC') {
    const cand1 = ask - 0.02;
    const cand2 = mProb * 0.82;
    const rawLimit = mProb > 0.03 ? Math.min(cand1, cand2) : cand1;
    entryPrice = roundDownTick(Math.max(tick, rawLimit));
    if (entryPrice >= ask && ask > tick) {
      entryPrice = roundDownTick(ask - tick);
    }
  }

  // 2. Smart Take-Profit (Aligned with Calibrated Weather Model Fair Value)
  const minTp = Math.min(0.99, entryPrice + Math.max(0.03, tick * 3));
  let rawTp = entryPrice * 1.3;
  if (evVal >= 0.15 && mProb >= minTp) {
    rawTp = Math.max(mProb, bProb, entryPrice * 1.25);
  } else if (mProb > entryPrice + 0.04) {
    rawTp = Math.max(mProb, entryPrice * 1.25);
  }
  let tpPrice = roundDownTick(Math.min(0.99, Math.max(minTp, rawTp)));
  if (tpPrice <= entryPrice) {
    tpPrice = Math.min(0.999, Number((entryPrice + tick).toFixed(4)));
  }

  // 3. Tiered Smart Stop-Loss based on bucket price level
  const maxSl = Math.max(tick, entryPrice - tick);
  let rawSl = entryPrice * 0.72;
  if (entryPrice < 0.15) {
    rawSl = entryPrice * 0.62; // ~ -38% for low-cent volatile buckets
  } else if (entryPrice < 0.35) {
    rawSl = entryPrice * 0.72; // ~ -28% for mid-cent buckets
  } else {
    rawSl = entryPrice * 0.78; // ~ -22% for high-probability buckets
  }
  let slPrice = roundDownTick(Math.min(maxSl, Math.max(tick, rawSl)));
  if (slPrice >= entryPrice && entryPrice > 0.001) {
    slPrice = Math.max(0.001, Number((entryPrice - 0.001).toFixed(4)));
  }

  const tpPct = entryPrice > 0 ? Number((((tpPrice - entryPrice) / entryPrice) * 100).toFixed(1)) : 30.0;
  const slPct = entryPrice > 0 ? Number((((entryPrice - slPrice) / entryPrice) * 100).toFixed(1)) : 28.0;

  return {
    entryPrice,
    tpPrice,
    tpPct,
    slPrice,
    slPct,
  };
}

export default function OrderConfigModal({ context, onClose, onSuccess }: OrderConfigModalProps) {
  if (!context) return null;

  const isEdit = context.mode === 'edit' && context.existingLock != null;
  const existing = context.existingLock;

  // Account Status (MetaMask / Polymarket CLOB)
  const [accountStatus, setAccountStatus] = useState<PolymarketAccountStatus | null>(null);

  // Side & Order Type State
  const [side, setSide] = useState<'YES' | 'NO'>(
    (existing?.side as 'YES' | 'NO') || context.side || 'YES'
  );
  const [executionMode, setExecutionMode] = useState<'PAPER' | 'SHADOW' | 'LIVE'>(
    existing?.execution_mode || 'SHADOW'
  );
  const [entryOrderType, setEntryOrderType] = useState<'IMMEDIATE_MARKETABLE' | 'CONDITIONAL_LIMIT_GTC'>(
    existing?.entry_order_type || 'IMMEDIATE_MARKETABLE'
  );
  const [smartAutopilot, setSmartAutopilot] = useState<boolean>(
    existing?.smart_autopilot != null ? existing.smart_autopilot : true
  );
  const [exitPolicy, setExitPolicy] = useState<'MARKETABLE_FAK' | 'MARKETABLE_FOK' | 'STRICT_LIMIT'>(
    existing?.exit_policy || 'MARKETABLE_FAK'
  );

  // Current market price for selected side
  const currentSideMarketPrice = useMemo(() => {
    const rawYes = Number(context.yesPrice || 0.25);
    const rawNo = context.noPrice != null ? Number(context.noPrice) : Math.max(0.01, 1 - rawYes);
    return side === 'YES'
      ? Math.max(0.01, Math.min(0.99, rawYes))
      : Math.max(0.01, Math.min(0.99, rawNo));
  }, [side, context.yesPrice, context.noPrice]);

  // Model Fair Probability for selected side
  const sideModelProb = useMemo(() => {
    if (context.modelProb == null) return null;
    const p = side === 'YES' ? Number(context.modelProb) : 1 - Number(context.modelProb);
    return Math.max(0.005, Math.min(0.995, p));
  }, [context.modelProb, side]);

  const sideBeliefProb = useMemo(() => {
    if (context.beliefProb == null) return sideModelProb;
    const p = side === 'YES' ? Number(context.beliefProb) : 1 - Number(context.beliefProb);
    return Math.max(0.005, Math.min(0.995, p));
  }, [context.beliefProb, side, sideModelProb]);

  // Entry Price (Marketable or Conditional Limit Price)
  const [entryPrice, setEntryPrice] = useState<number>(() => {
    if (isEdit && existing?.entry_price) return Number(existing.entry_price);
    return currentSideMarketPrice;
  });

  // Position Sizing ($USDC & Shares)
  const [usdcAmount, setUsdcAmount] = useState<number>(() => {
    if (existing?.usdc_amount != null) return Number(existing.usdc_amount);
    return 15;
  });
  const [quantityShares, setQuantityShares] = useState<number>(() => {
    if (existing?.quantity_shares != null) return Number(existing.quantity_shares);
    return Math.max(1, Math.round((15 / currentSideMarketPrice) * 100) / 100);
  });

  // Auto-Exit Rules State
  const [autoExitEnabled, setAutoExitEnabled] = useState<boolean>(
    existing?.auto_exit_enabled != null ? existing.auto_exit_enabled : true
  );
  const [tpEnabled, setTpEnabled] = useState<boolean>(
    existing?.tp_enabled != null ? existing.tp_enabled : true
  );
  const [slEnabled, setSlEnabled] = useState<boolean>(
    existing?.sl_enabled != null ? existing.sl_enabled : true
  );
  const [tickSize, setTickSize] = useState<number>(existing?.tick_size || 0.01);
  const [maxSlippagePct, setMaxSlippagePct] = useState<number>(existing?.max_slippage_pct || 2.0);

  const [tpPct, setTpPct] = useState<number>(existing?.tp_pct ?? 35);
  const [tpPriceCents, setTpPriceCents] = useState<number>(
    existing?.tp_price != null ? Number((existing.tp_price * 100).toFixed(1)) : 35
  );
  const [slPct, setSlPct] = useState<number>(existing?.sl_pct ?? 28);
  const [slPriceCents, setSlPriceCents] = useState<number>(
    existing?.sl_price != null ? Number((existing.sl_price * 100).toFixed(1)) : 18
  );

  // Weather-Smart Guards
  const [trailingStopEnabled, setTrailingStopEnabled] = useState<boolean>(
    existing?.trailing_stop_enabled || false
  );
  const [trailingOffsetPct, setTrailingOffsetPct] = useState<number>(
    existing?.trailing_offset_pct || 8.0
  );
  const [metarAutoStop, setMetarAutoStop] = useState<boolean>(
    existing?.metar_auto_stop != null ? existing.metar_auto_stop : true
  );
  const [autoSell99c, setAutoSell99c] = useState<boolean>(
    existing?.auto_sell_99c != null ? existing.auto_sell_99c : true
  );

  // Live Tick Simulator State (in Edit mode)
  const [simBidCents, setSimBidCents] = useState<string>(() => {
    const startP = existing?.last_price ?? existing?.entry_price ?? currentSideMarketPrice;
    return (Number(startP) * 100).toFixed(1);
  });
  const [simMetarElim, setSimMetarElim] = useState<boolean>(false);
  const [simResultMsg, setSimResultMsg] = useState<{ text: string; isError: boolean } | null>(null);
  const [simRunning, setSimRunning] = useState<boolean>(false);

  const [submitting, setSubmitting] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  // Fetch MetaMask / Polymarket Account Status on mount
  useEffect(() => {
    fetch('/api/polymarket/account-status')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d) setAccountStatus(d);
      })
      .catch(() => {});
  }, []);

  // Recompute Smart Auto-Pilot parameters whenever Smart Auto-Pilot is enabled
  useEffect(() => {
    if (!smartAutopilot) return;
    if (isEdit && existing) {
      // In edit mode, only recompute if user toggles smartAutopilot on or changes entryOrderType
    }
    const smart = computeSmartAutoPilot({
      marketPrice: currentSideMarketPrice,
      modelProb: sideModelProb,
      beliefProb: sideBeliefProb,
      ev: context.ev ?? null,
      entryOrderType,
      tickSize,
    });

    const nextEntry = isEdit && existing?.position_state !== 'ENTRY_PENDING'
      ? Number(existing?.entry_price || smart.entryPrice)
      : smart.entryPrice;

    setEntryPrice(nextEntry);
    setQuantityShares(Math.max(0.01, Number((usdcAmount / nextEntry).toFixed(2))));
    setAutoExitEnabled(true);
    setTpEnabled(true);
    setTpPriceCents(Number((smart.tpPrice * 100).toFixed(1)));
    setTpPct(smart.tpPct);
    setSlEnabled(true);
    setSlPriceCents(Number((smart.slPrice * 100).toFixed(1)));
    setSlPct(smart.slPct);
    setMetarAutoStop(true);
    setAutoSell99c(true);
    setExitPolicy('MARKETABLE_FAK');
    setMaxSlippagePct(2.0);
  }, [smartAutopilot, side, entryOrderType, currentSideMarketPrice, sideModelProb, sideBeliefProb, tickSize]);

  // Handlers for manual Entry / Sizing / TP / SL adjustments
  const handleEntryPriceCentsChange = (centsVal: number) => {
    const clampedCents = Math.max(0.5, Math.min(99.5, centsVal));
    const newEntry = Number((clampedCents / 100).toFixed(4));
    setEntryPrice(newEntry);
    setQuantityShares(Math.max(0.01, Number((usdcAmount / newEntry).toFixed(2))));
    // Keep TP/SL percentages consistent around new entry price
    const newTpCents = Math.min(99.5, Number((clampedCents * (1 + tpPct / 100)).toFixed(1)));
    const newSlCents = Math.max(0.5, Number((clampedCents * (1 - slPct / 100)).toFixed(1)));
    setTpPriceCents(newTpCents);
    setSlPriceCents(newSlCents);
  };

  const handleUsdcChange = (val: number) => {
    const clean = Math.max(0.5, val);
    setUsdcAmount(clean);
    if (entryPrice > 0) {
      setQuantityShares(Number((clean / entryPrice).toFixed(2)));
    }
  };

  const handleSharesChange = (val: number) => {
    const clean = Math.max(0.1, val);
    setQuantityShares(clean);
    setUsdcAmount(Number((clean * entryPrice).toFixed(2)));
  };

  const handleTpPctChange = (pct: number) => {
    setSmartAutopilot(false);
    const cleanPct = Math.max(1, pct);
    setTpPct(cleanPct);
    const rawCents = entryPrice * 100 * (1 + cleanPct / 100);
    setTpPriceCents(Math.min(99.5, Number(rawCents.toFixed(1))));
  };

  const handleTpCentsChange = (cents: number) => {
    setSmartAutopilot(false);
    const cleanCents = Math.max(0.5, Math.min(99.9, cents));
    setTpPriceCents(cleanCents);
    const entryCents = entryPrice * 100;
    if (entryCents > 0) {
      setTpPct(Number((((cleanCents - entryCents) / entryCents) * 100).toFixed(1)));
    }
  };

  const handleSlPctChange = (pct: number) => {
    setSmartAutopilot(false);
    const cleanPct = Math.max(1, Math.min(98, Math.abs(pct)));
    setSlPct(cleanPct);
    const rawCents = entryPrice * 100 * (1 - cleanPct / 100);
    setSlPriceCents(Math.max(0.1, Number(rawCents.toFixed(1))));
  };

  const handleSlCentsChange = (cents: number) => {
    setSmartAutopilot(false);
    const cleanCents = Math.max(0.1, Math.min(99.0, cents));
    setSlPriceCents(cleanCents);
    const entryCents = entryPrice * 100;
    if (entryCents > 0) {
      setSlPct(Number((((entryCents - cleanCents) / entryCents) * 100).toFixed(1)));
    }
  };

  const applyModelFairValueTp = () => {
    if (sideModelProb == null) return;
    const fairCents = Math.min(99.5, Math.max(entryPrice * 100 + 2, Number((sideModelProb * 100).toFixed(1))));
    setTpPriceCents(fairCents);
    const entryCents = entryPrice * 100;
    setTpPct(Number((((fairCents - entryCents) / entryCents) * 100).toFixed(1)));
  };

  // Computed validation & PnL preview
  const tpDecimal = Number((tpPriceCents / 100).toFixed(4));
  const slDecimal = Number((slPriceCents / 100).toFixed(4));
  const entryCents = Number((entryPrice * 100).toFixed(1));
  const currentMarketCents = Number((currentSideMarketPrice * 100).toFixed(1));

  const validationError = useMemo(() => {
    if (entryPrice <= 0 || entryPrice >= 1) {
      return 'قیمت ورود باید بین 0.1¢ و 99.9¢ باشد.';
    }
    if (quantityShares <= 0 || usdcAmount <= 0) {
      return 'مبلغ سرمایه‌گذاری و تعداد سهام باید بزرگ‌تر از صفر باشد.';
    }
    if (autoExitEnabled) {
      if (tpEnabled && tpDecimal <= entryPrice) {
        return `حد سود (${tpPriceCents}¢) باید بالاتر از قیمت ورود (${entryCents}¢) باشد.`;
      }
      if (tpEnabled && tpDecimal >= 1) {
        return 'حد سود باید کمتر از 100¢ باشد.';
      }
      if (slEnabled && slDecimal >= entryPrice) {
        return `حد ضرر (${slPriceCents}¢) باید پایین‌تر از قیمت ورود (${entryCents}¢) باشد.`;
      }
      if (slEnabled && slDecimal <= 0) {
        return 'حد ضرر باید بزرگ‌تر از 0¢ باشد.';
      }
    }
    return null;
  }, [entryPrice, quantityShares, usdcAmount, autoExitEnabled, tpEnabled, tpDecimal, tpPriceCents, entryCents, slEnabled, slDecimal, slPriceCents]);

  const estTpProfitUsdc = tpEnabled ? Number((quantityShares * (tpDecimal - entryPrice)).toFixed(2)) : 0;
  const estSlLossUsdc = slEnabled ? Number((quantityShares * (entryPrice - slDecimal)).toFixed(2)) : 0;
  const riskRewardRatio =
    estSlLossUsdc > 0 && estTpProfitUsdc > 0 ? Number((estTpProfitUsdc / estSlLossUsdc).toFixed(2)) : null;

  const worstFloorTpCents = Number((tpPriceCents * (1 - maxSlippagePct / 100)).toFixed(1));
  const worstFloorSlCents = Number((slPriceCents * (1 - maxSlippagePct / 100)).toFixed(1));

  const handleSubmitOrder = async (e: React.FormEvent) => {
    e.preventDefault();
    if (validationError) {
      setErrorMsg(validationError);
      return;
    }
    setSubmitting(true);
    setErrorMsg(null);
    setSuccessMsg(null);

    try {
      const selectedTokenId =
        side === 'NO' && context.noTokenId ? context.noTokenId : context.tokenId;

      if (isEdit && existing) {
        const res = await fetch('/api/locks/rules', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            city: context.city,
            date: context.date,
            market_id: context.marketId,
            source_version: existing.rule_version || 1,
            execution_mode: executionMode,
            entry_order_type: entryOrderType,
            limit_buy_price: entryOrderType === 'CONDITIONAL_LIMIT_GTC' ? entryPrice : null,
            smart_autopilot: smartAutopilot,
            exit_policy: exitPolicy,
            quantity_shares: quantityShares,
            usdc_amount: usdcAmount,
            auto_exit_enabled: autoExitEnabled,
            tp_enabled: tpEnabled,
            tp_price: tpEnabled ? tpDecimal : null,
            sl_enabled: slEnabled,
            sl_price: slEnabled ? slDecimal : null,
            max_slippage_pct: maxSlippagePct,
            trailing_stop_enabled: trailingStopEnabled,
            trailing_offset_pct: trailingOffsetPct,
            metar_auto_stop: metarAutoStop,
            auto_sell_99c: autoSell99c,
          }),
        });
        const json = await res.json();
        if (!res.ok || !json.ok) {
          throw new Error(json.error || 'خطا در بروزرسانی تنظیمات پوزیشن');
        }
        setSuccessMsg(json.message || 'تنظیمات با موفقیت ذخیره شد.');
        await onSuccess();
        setTimeout(() => onClose(), 700);
      } else {
        const res = await fetch('/api/locks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            city: context.city,
            date: context.date,
            market_id: context.marketId,
            token_id: selectedTokenId,
            side,
            price: currentSideMarketPrice,
            limit_buy_price: entryOrderType === 'CONDITIONAL_LIMIT_GTC' ? entryPrice : null,
            entry_order_type: entryOrderType,
            smart_autopilot: smartAutopilot,
            market_type: context.marketType,
            execution_mode: executionMode,
            exit_policy: exitPolicy,
            quantity_shares: quantityShares,
            usdc_amount: usdcAmount,
            auto_exit_enabled: autoExitEnabled,
            tp_enabled: tpEnabled,
            tp_price: tpEnabled ? tpDecimal : null,
            tp_pct: tpEnabled ? tpPct : null,
            sl_enabled: slEnabled,
            sl_price: slEnabled ? slDecimal : null,
            sl_pct: slEnabled ? slPct : null,
            max_slippage_pct: maxSlippagePct,
            tick_size: tickSize,
            trailing_stop_enabled: trailingStopEnabled,
            trailing_offset_pct: trailingOffsetPct,
            metar_auto_stop: metarAutoStop,
            auto_sell_99c: autoSell99c,
          }),
        });
        const json = await res.json();
        if (!res.ok || !json.ok) {
          throw new Error(json.error || 'خطا در ثبت سفارش');
        }
        setSuccessMsg(json.message || 'سفارش با موفقیت ثبت شد.');
        await onSuccess();
        setTimeout(() => onClose(), 800);
      }
    } catch (err: any) {
      setErrorMsg(err.message || 'خطا در ارتباط با سرور');
    } finally {
      setSubmitting(false);
    }
  };

  const handleRunSimulateTick = async () => {
    const bidVal = Number(simBidCents) / 100;
    if (isNaN(bidVal) || bidVal <= 0 || bidVal >= 1) {
      setSimResultMsg({ text: 'قیمت شبیه‌سازی باید بین 0.1 و 99.9 سنت باشد.', isError: true });
      return;
    }
    setSimRunning(true);
    setSimResultMsg(null);
    try {
      const res = await fetch('/api/locks/simulate-tick', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          city: context.city,
          date: context.date,
          market_id: context.marketId,
          simulated_bid: Number(bidVal.toFixed(4)),
          force_metar_eliminated: simMetarElim,
        }),
      });
      const json = await res.json();
      if (!res.ok || !json.ok) {
        setSimResultMsg({ text: json.error || 'خطا در اجرای شبیه‌ساز', isError: true });
      } else {
        setSimResultMsg({ text: json.message, isError: false });
        await onSuccess();
        if (json.triggered && !json.blocked) {
          setTimeout(() => onClose(), 1300);
        }
      }
    } catch (err: any) {
      setSimResultMsg({ text: err.message || 'خطا در ارتباط با سرور', isError: true });
    } finally {
      setSimRunning(false);
    }
  };

  const isPendingEntry = existing?.position_state === 'ENTRY_PENDING';

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 backdrop-blur-xs flex items-center justify-center p-2.5 md:p-5 overflow-y-auto"
      dir="rtl"
      onClick={onClose}
    >
      <div
        className="bg-white border border-[#dde3ec] rounded-2xl shadow-2xl w-full max-w-3xl overflow-hidden my-auto"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Top Header */}
        <div className="bg-[#0f172a] text-white px-5 py-4 flex items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-base md:text-lg font-bold">
                {isEdit
                  ? isPendingEntry
                    ? '⏳ مدیریت سفارش خرید شرطی و چتر خروج خودکار'
                    : '⚙️ تنظیمات پوزیشن فعال و شبیه‌ساز موتور Auto-Exit'
                  : '⚡ ثبت سفارش هوشمند و تنظیم اتوماتیک سیو سود / حد ضرر'}
              </span>
              <span className="px-2 py-0.5 rounded-md text-[11px] font-bold bg-blue-500/20 text-blue-300 border border-blue-400/30 ltr-inline">
                {context.marketType === 'min' ? '❄️ MIN TEMP' : '🔥 MAX TEMP'}
              </span>
              {isEdit && existing?.rule_version && (
                <span className="px-2 py-0.5 rounded-md text-[11px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-400/30 ltr-inline">
                  Rule v{existing.rule_version}
                </span>
              )}
            </div>
            <div className="text-xs text-slate-300 mt-1 flex flex-wrap items-center gap-2">
              <span>شهر: <b className="text-white">{context.cityName}</b></span>
              <span>•</span>
              <span className="ltr-inline">تاریخ: <b className="text-white">{context.date}</b></span>
              <span>•</span>
              <span>باکت دما: <b className="text-amber-300 ltr-inline">{context.label}</b></span>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="w-8 h-8 rounded-lg bg-white/10 hover:bg-white/20 text-white flex items-center justify-center text-base font-bold cursor-pointer border-none"
          >
            ✕
          </button>
        </div>

        {/* MetaMask × Polymarket Account Status Bar */}
        {accountStatus && (
          <div className="bg-[#f8fafc] border-b border-[#dde3ec] px-5 py-2 flex flex-wrap items-center justify-between gap-2 text-[11.5px]">
            <div className="flex items-center gap-2">
              <span className="font-bold text-[#1f2430]">🦊 وضعیت حساب پلی‌مارکت (MetaMask):</span>
              <span className={accountStatus.live_ready ? 'text-[#16a34a] font-semibold' : 'text-[#2563eb] font-medium'}>
                {accountStatus.status_text}
              </span>
            </div>
            <div className="text-[11px] text-[#6b7684] ltr-inline">
              Chain: Polygon ({accountStatus.chain_id}) • {accountStatus.signature_label}
            </div>
          </div>
        )}

        <form onSubmit={handleSubmitOrder} className="p-4 md:p-5 space-y-4 max-h-[82vh] overflow-y-auto">
          {/* Telemetry Summary Bar */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5 bg-[#f6f8fb] border border-[#dde3ec] rounded-xl p-3 text-center">
            <div className="bg-white border border-[#e5eaf2] rounded-lg p-2">
              <div className="text-[11px] text-[#6b7684]">قیمت فعلی بازار ({side})</div>
              <div className="text-sm font-bold text-[#1f2430] ltr-inline mt-0.5">
                {currentMarketCents}¢ <span className="text-[11px] font-normal text-[#6b7684]">(${currentSideMarketPrice.toFixed(3)})</span>
              </div>
            </div>
            <div className="bg-white border border-[#e5eaf2] rounded-lg p-2">
              <div className="text-[11px] text-[#6b7684]">ارزش منصفانه مدل هوشمند</div>
              <div className="text-sm font-bold text-[#2563eb] ltr-inline mt-0.5">
                {sideModelProb != null ? `${(sideModelProb * 100).toFixed(1)}¢ (${(sideModelProb * 100).toFixed(1)}%)` : '-'}
              </div>
            </div>
            <div className="bg-white border border-[#e5eaf2] rounded-lg p-2">
              <div className="text-[11px] text-[#6b7684]">باور نهایی (Belief)</div>
              <div className="text-sm font-bold text-[#1f2430] ltr-inline mt-0.5">
                {sideBeliefProb != null ? `${(sideBeliefProb * 100).toFixed(1)}%` : '-'}
              </div>
            </div>
            <div className="bg-white border border-[#e5eaf2] rounded-lg p-2">
              <div className="text-[11px] text-[#6b7684]">امید ریاضی سود (EV)</div>
              <div
                className={`text-sm font-bold ltr-inline mt-0.5 ${
                  (context.ev || 0) > 0 ? 'text-[#16a34a]' : 'text-[#6b7684]'
                }`}
              >
                {context.ev != null ? `${context.ev > 0 ? '+' : ''}${(context.ev * 100).toFixed(1)}%` : '-'}
              </div>
            </div>
          </div>

          {/* PROMINENT SMART AUTO-PILOT TOGGLE CARD */}
          <div
            className={`rounded-xl p-3.5 border transition-all ${
              smartAutopilot
                ? 'bg-[#e8f8ee] border-[#16a34a88] shadow-xs'
                : 'bg-[#f6f8fb] border-[#dde3ec]'
            }`}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <label className="flex items-center gap-2.5 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={smartAutopilot}
                  onChange={(e) => setSmartAutopilot(e.target.checked)}
                  className="w-4 h-4 accent-[#16a34a] cursor-pointer"
                />
                <div>
                  <div className="text-sm font-bold text-[#1f2430] flex items-center gap-1.5">
                    <span>🤖 تعیین اتوماتیک هوشمند (Smart Auto-Pilot)</span>
                    <span className="text-[10.5px] bg-[#16a34a] text-white px-2 py-0.5 rounded-md font-bold">
                      پیشنهادی برای جلوگیری از پیچیدگی
                    </span>
                  </div>
                  <div className="text-[11.5px] text-[#4b5563] mt-0.5">
                    محاسبهٔ خودکار بهترین نقطهٔ ورود، سیو سود منطبق با ارزش منصفانهٔ مدل کالیبره، حد ضرر پله‌ای و محافظ دمای فرودگاه (METAR)
                  </div>
                </div>
              </label>

              {smartAutopilot && (
                <button
                  type="button"
                  onClick={() => setSmartAutopilot(false)}
                  className="bg-white hover:bg-gray-50 text-[#1f2430] border border-[#dde3ec] rounded-lg px-2.5 py-1 text-[11px] font-semibold cursor-pointer"
                >
                  ⚙️ تغییر به تنظیم دستی
                </button>
              )}
            </div>

            {smartAutopilot && (
              <div className="mt-3 pt-2.5 border-t border-[#16a34a33] grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
                <div className="bg-white/90 rounded-lg px-2.5 py-1.5 border border-[#16a34a33]">
                  <span className="text-[#6b7684] block text-[10.5px]">
                    {entryOrderType === 'CONDITIONAL_LIMIT_GTC' ? 'قیمت خرید شرطی اتوماتیک:' : 'قیمت ورود بازار:'}
                  </span>
                  <b className="text-[#1f2430] ltr-inline text-sm">{entryCents}¢</b>
                </div>
                <div className="bg-white/90 rounded-lg px-2.5 py-1.5 border border-[#16a34a33]">
                  <span className="text-[#6b7684] block text-[10.5px]">🎯 حد سود هوشمند (TP):</span>
                  <b className="text-[#16a34a] ltr-inline text-sm">
                    {tpPriceCents}¢ (+{tpPct}%)
                  </b>
                </div>
                <div className="bg-white/90 rounded-lg px-2.5 py-1.5 border border-[#16a34a33]">
                  <span className="text-[#6b7684] block text-[10.5px]">🛑 حد ضرر پله‌ای (SL):</span>
                  <b className="text-[#dc2626] ltr-inline text-sm">
                    {slPriceCents}¢ (-{slPct}%)
                  </b>
                </div>
                <div className="bg-white/90 rounded-lg px-2.5 py-1.5 border border-[#16a34a33]">
                  <span className="text-[#6b7684] block text-[10.5px]">🛡️ محافظ‌های آب‌وهوا:</span>
                  <b className="text-[#2563eb] text-[11px]">METAR + فروش 99.5¢ فعال</b>
                </div>
              </div>
            )}
          </div>

          {/* Row 1: Execution Mode (PAPER / SHADOW / LIVE) + Entry Order Type (Immediate vs Conditional Limit Buy) */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {/* Execution Mode */}
            <div className="bg-[#f6f8fb] border border-[#dde3ec] rounded-xl p-3">
              <div className="text-xs font-bold text-[#1f2430] mb-2">۱. حالت اجرای ربات (Execution Mode)</div>
              <div className="grid grid-cols-3 gap-1.5">
                {(['PAPER', 'SHADOW', 'LIVE'] as const).map((m) => {
                  const active = executionMode === m;
                  const labelMap = {
                    PAPER: '🧪 PAPER (تست)',
                    SHADOW: '🛡️ SHADOW (پیشنهادی)',
                    LIVE: '🔥 LIVE (پول واقعی)',
                  };
                  return (
                    <button
                      key={m}
                      type="button"
                      onClick={() => setExecutionMode(m)}
                      className={`py-2 px-2 rounded-lg text-[11.5px] font-bold cursor-pointer border transition-all ${
                        active
                          ? m === 'LIVE'
                            ? 'bg-[#dc2626] text-white border-[#dc2626] shadow-xs'
                            : m === 'SHADOW'
                            ? 'bg-[#2563eb] text-white border-[#2563eb] shadow-xs'
                            : 'bg-[#1f2430] text-white border-[#1f2430]'
                          : 'bg-white text-[#4b5563] border-[#dde3ec] hover:bg-gray-50'
                      }`}
                    >
                      {labelMap[m]}
                    </button>
                  );
                })}
              </div>
              <div className="text-[10.5px] text-[#6b7684] mt-1.5">
                {executionMode === 'SHADOW' &&
                  'در حالت SHADOW، ربات روی اوردربوک زنده پلی‌مارکت تصمیم می‌گیرد و شبیه‌سازی واقعی انجام می‌دهد (بدون کسر پول).'}
                {executionMode === 'PAPER' &&
                  'در حالت PAPER، سفارش و تست‌های تیک کاملاً در محیط شبیه‌ساز داخلی اجرا می‌شوند.'}
                {executionMode === 'LIVE' &&
                  'در حالت LIVE، سفارش‌ها مستقیماً با امضای کیف‌پول متامسک شما به صرافی پلی‌مارکت ارسال می‌شوند.'}
              </div>
            </div>

            {/* Entry Order Type: Immediate vs Conditional Limit Buy */}
            <div className="bg-[#f6f8fb] border border-[#dde3ec] rounded-xl p-3">
              <div className="text-xs font-bold text-[#1f2430] mb-2">۲. نوع ورود به معامله (فوری یا خرید شرطی)</div>
              <div className="grid grid-cols-2 gap-1.5">
                <button
                  type="button"
                  onClick={() => setEntryOrderType('IMMEDIATE_MARKETABLE')}
                  className={`py-2 px-2.5 rounded-lg text-xs font-bold cursor-pointer border transition-all ${
                    entryOrderType === 'IMMEDIATE_MARKETABLE'
                      ? 'bg-[#2563eb] text-white border-[#2563eb] shadow-xs'
                      : 'bg-white text-[#4b5563] border-[#dde3ec] hover:bg-gray-50'
                  }`}
                >
                  ⚡ خرید فوری در قیمت بازار
                </button>
                <button
                  type="button"
                  onClick={() => setEntryOrderType('CONDITIONAL_LIMIT_GTC')}
                  className={`py-2 px-2.5 rounded-lg text-xs font-bold cursor-pointer border transition-all ${
                    entryOrderType === 'CONDITIONAL_LIMIT_GTC'
                      ? 'bg-[#9333ea] text-white border-[#9333ea] shadow-xs'
                      : 'bg-white text-[#4b5563] border-[#dde3ec] hover:bg-gray-50'
                  }`}
                >
                  ⏳ خرید شرطی (Limit Buy)
                </button>
              </div>
              <div className="text-[10.5px] text-[#6b7684] mt-1.5">
                {entryOrderType === 'IMMEDIATE_MARKETABLE'
                  ? 'همین لحظه در قیمت فعلی بازار خریداری شده و بلافاصله چتر TP/SL فعال می‌شود.'
                  : 'سفارش خرید در قیمت پایین‌تر منتظر می‌ماند؛ به محض رسیدن قیمت و پر شدن، چتر TP/SL خودکار فعال می‌شود (و اگر دمای METAR از باکت رد شود، خودکار لغو می‌گردد).'}
              </div>
            </div>
          </div>

          {/* Row 2: Side Selection + Investment Amount ($USDC) + Conditional Entry Price */}
          <div className="bg-[#f6f8fb] border border-[#dde3ec] rounded-xl p-3.5">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 items-end">
              {/* Side YES / NO */}
              <div>
                <label className="block text-xs font-bold text-[#1f2430] mb-1">سمت معامله (Side)</label>
                <div className="grid grid-cols-2 gap-1.5">
                  <button
                    type="button"
                    disabled={isEdit}
                    onClick={() => setSide('YES')}
                    className={`py-2 rounded-lg text-xs font-bold border cursor-pointer ${
                      side === 'YES'
                        ? 'bg-[#16a34a] text-white border-[#16a34a]'
                        : 'bg-white text-[#1f2430] border-[#dde3ec]'
                    }`}
                  >
                    خرید YES ({Math.round(context.yesPrice * 100)}¢)
                  </button>
                  <button
                    type="button"
                    disabled={isEdit}
                    onClick={() => setSide('NO')}
                    className={`py-2 rounded-lg text-xs font-bold border cursor-pointer ${
                      side === 'NO'
                        ? 'bg-[#dc2626] text-white border-[#dc2626]'
                        : 'bg-white text-[#1f2430] border-[#dde3ec]'
                    }`}
                  >
                    خرید NO ({Math.round((context.noPrice ?? 1 - context.yesPrice) * 100)}¢)
                  </button>
                </div>
              </div>

              {/* Entry / Limit Buy Price (in Cents) */}
              <div>
                <label className="block text-xs font-bold text-[#1f2430] mb-1">
                  {entryOrderType === 'CONDITIONAL_LIMIT_GTC'
                    ? '⏳ قیمت شرطی خرید (سنت ¢)'
                    : 'قیمت مبنای ورود (سنت ¢)'}
                </label>
                <div className="flex items-center gap-1.5">
                  <input
                    type="number"
                    step="0.1"
                    min="0.5"
                    max="99.5"
                    value={entryCents}
                    onChange={(e) => {
                      setSmartAutopilot(false);
                      handleEntryPriceCentsChange(Number(e.target.value));
                    }}
                    className="w-full bg-white border border-[#dde3ec] rounded-lg px-3 py-2 text-sm font-bold text-center ltr-inline"
                  />
                  <span className="text-xs font-bold text-[#6b7684]">¢</span>
                </div>
              </div>

              {/* USDC Investment Amount */}
              <div>
                <label className="block text-xs font-bold text-[#1f2430] mb-1">
                  مبلغ سرمایه‌گذاری ($USDC)
                </label>
                <div className="flex items-center gap-1.5">
                  <input
                    type="number"
                    step="1"
                    min="1"
                    value={usdcAmount}
                    onChange={(e) => handleUsdcChange(Number(e.target.value))}
                    className="w-full bg-white border border-[#dde3ec] rounded-lg px-3 py-2 text-sm font-bold text-center ltr-inline"
                  />
                  <span className="text-xs font-bold text-[#6b7684]">$</span>
                </div>
              </div>
            </div>

            {/* Quick USDC presets & Shares display */}
            <div className="flex flex-wrap items-center justify-between gap-2 mt-3 pt-2.5 border-t border-[#dde3ec]">
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className="text-[11px] text-[#6b7684] font-medium">انتخاب سریع مبلغ:</span>
                {[5, 10, 15, 25, 50, 100].map((amt) => (
                  <button
                    key={amt}
                    type="button"
                    onClick={() => handleUsdcChange(amt)}
                    className={`px-2.5 py-1 rounded-md text-xs font-bold cursor-pointer border ${
                      Math.abs(usdcAmount - amt) < 0.01
                        ? 'bg-[#2563eb] text-white border-[#2563eb]'
                        : 'bg-white text-[#1f2430] border-[#dde3ec] hover:bg-gray-50'
                    }`}
                  >
                    ${amt}
                  </button>
                ))}
              </div>
              <div className="text-xs text-[#1f2430] bg-white border border-[#dde3ec] rounded-lg px-3 py-1">
                تعداد سهام دریافتی:{' '}
                <b className="text-[#2563eb] ltr-inline">{quantityShares.toFixed(2)} Shares</b>
                <span className="text-[11px] text-[#6b7684] mr-1.5">
                  (ارزش در صورت برد ۱۰۰¢: <b className="text-[#16a34a] ltr-inline">${quantityShares.toFixed(2)}</b>)
                </span>
              </div>
            </div>
          </div>

          {/* Manual / Advanced TP & SL Controls (Always visible if smartAutopilot is unchecked, or expandable) */}
          {!smartAutopilot && (
            <div className="space-y-3 bg-[#f6f8fb] border border-[#dde3ec] rounded-xl p-3.5">
              <div className="flex flex-wrap items-center justify-between gap-2 pb-2 border-b border-[#dde3ec]">
                <label className="flex items-center gap-2 text-xs font-bold text-[#1f2430] cursor-pointer">
                  <input
                    type="checkbox"
                    checked={autoExitEnabled}
                    onChange={(e) => setAutoExitEnabled(e.target.checked)}
                    className="w-4 h-4 accent-[#2563eb]"
                  />
                  فعال‌سازی موتور خروج خودکار (Auto-Exit TP / SL)
                </label>
                {sideModelProb != null && (
                  <button
                    type="button"
                    onClick={applyModelFairValueTp}
                    className="bg-[#eaf1ff] hover:bg-[#dbeafe] text-[#2563eb] border border-[#2563eb55] rounded-lg px-2.5 py-1 text-[11px] font-bold cursor-pointer"
                  >
                    🎯 تنظیم TP روی ارزش منصفانه مدل ({(sideModelProb * 100).toFixed(1)}¢)
                  </button>
                )}
              </div>

              {autoExitEnabled && (
                <>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {/* Take Profit Box */}
                    <div className="bg-white border border-[#16a34a55] rounded-xl p-3">
                      <div className="flex items-center justify-between mb-2">
                        <label className="flex items-center gap-1.5 text-xs font-bold text-[#16a34a] cursor-pointer">
                          <input
                            type="checkbox"
                            checked={tpEnabled}
                            onChange={(e) => setTpEnabled(e.target.checked)}
                            className="accent-[#16a34a]"
                          />
                          🎯 حد سیو سود (Take-Profit)
                        </label>
                        {tpEnabled && (
                          <span className="text-[11px] font-bold text-[#16a34a] ltr-inline">
                            سود خالص: +${estTpProfitUsdc.toFixed(2)}
                          </span>
                        )}
                      </div>
                      {tpEnabled && (
                        <div className="grid grid-cols-2 gap-2">
                          <div>
                            <span className="block text-[10.5px] text-[#6b7684] mb-1">قیمت هدف (سنت ¢)</span>
                            <input
                              type="number"
                              step="0.5"
                              min="1"
                              max="99.5"
                              value={tpPriceCents}
                              onChange={(e) => handleTpCentsChange(Number(e.target.value))}
                              className="w-full border border-[#dde3ec] rounded-lg px-2.5 py-1.5 text-sm font-bold text-center ltr-inline"
                            />
                          </div>
                          <div>
                            <span className="block text-[10.5px] text-[#6b7684] mb-1">درصد سود هدف (%)</span>
                            <input
                              type="number"
                              step="1"
                              min="1"
                              max="900"
                              value={tpPct}
                              onChange={(e) => handleTpPctChange(Number(e.target.value))}
                              className="w-full border border-[#dde3ec] rounded-lg px-2.5 py-1.5 text-sm font-bold text-center ltr-inline"
                            />
                          </div>
                        </div>
                      )}
                      {tpEnabled && (
                        <div className="flex gap-1 mt-2 flex-wrap">
                          {[20, 35, 50, 75, 100].map((p) => (
                            <button
                              key={p}
                              type="button"
                              onClick={() => handleTpPctChange(p)}
                              className="px-2 py-0.5 rounded text-[10.5px] font-semibold bg-[#e8f8ee] text-[#16a34a] border border-[#16a34a33] cursor-pointer"
                            >
                              +{p}%
                            </button>
                          ))}
                        </div>
                      )}
                    </div>

                    {/* Stop Loss Box */}
                    <div className="bg-white border border-[#dc262655] rounded-xl p-3">
                      <div className="flex items-center justify-between mb-2">
                        <label className="flex items-center gap-1.5 text-xs font-bold text-[#dc2626] cursor-pointer">
                          <input
                            type="checkbox"
                            checked={slEnabled}
                            onChange={(e) => setSlEnabled(e.target.checked)}
                            className="accent-[#dc2626]"
                          />
                          🛑 حد ضرر خودکار (Stop-Loss)
                        </label>
                        {slEnabled && (
                          <span className="text-[11px] font-bold text-[#dc2626] ltr-inline">
                            حداکثر ریسک: -${estSlLossUsdc.toFixed(2)}
                          </span>
                        )}
                      </div>
                      {slEnabled && (
                        <div className="grid grid-cols-2 gap-2">
                          <div>
                            <span className="block text-[10.5px] text-[#6b7684] mb-1">قیمت استاپ (سنت ¢)</span>
                            <input
                              type="number"
                              step="0.5"
                              min="0.5"
                              max="98"
                              value={slPriceCents}
                              onChange={(e) => handleSlCentsChange(Number(e.target.value))}
                              className="w-full border border-[#dde3ec] rounded-lg px-2.5 py-1.5 text-sm font-bold text-center ltr-inline"
                            />
                          </div>
                          <div>
                            <span className="block text-[10.5px] text-[#6b7684] mb-1">درصد افت مجاز (%)</span>
                            <input
                              type="number"
                              step="1"
                              min="1"
                              max="95"
                              value={slPct}
                              onChange={(e) => handleSlPctChange(Number(e.target.value))}
                              className="w-full border border-[#dde3ec] rounded-lg px-2.5 py-1.5 text-sm font-bold text-center ltr-inline"
                            />
                          </div>
                        </div>
                      )}
                      {slEnabled && (
                        <div className="flex gap-1 mt-2 flex-wrap">
                          {[15, 25, 30, 40, 50].map((p) => (
                            <button
                              key={p}
                              type="button"
                              onClick={() => handleSlPctChange(p)}
                              className="px-2 py-0.5 rounded text-[10.5px] font-semibold bg-[#fdeceb] text-[#dc2626] border border-[#dc262633] cursor-pointer"
                            >
                              -{p}%
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Weather-Smart Guards & Execution Policy */}
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3 pt-1">
                    <div className="bg-white border border-[#dde3ec] rounded-xl p-3 space-y-2 text-xs">
                      <div className="font-bold text-[#1f2430]">🌡️ محافظ‌های هوشمند بازار آب‌وهوا</div>
                      <label className="flex items-center gap-2 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={metarAutoStop}
                          onChange={(e) => setMetarAutoStop(e.target.checked)}
                        />
                        <span>خروج فوری در صورت عبور دمای واقعی فرودگاه (METAR) از باکت</span>
                      </label>
                      <label className="flex items-center gap-2 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={autoSell99c}
                          onChange={(e) => setAutoSell99c(e.target.checked)}
                        />
                        <span>فروش خودکار در 99.5¢ (نقد کردن سود قبل از تسویه UMA)</span>
                      </label>
                      <div className="flex items-center justify-between gap-2">
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={trailingStopEnabled}
                            onChange={(e) => setTrailingStopEnabled(e.target.checked)}
                          />
                          <span>حد ضرر متحرک (Trailing Stop)</span>
                        </label>
                        {trailingStopEnabled && (
                          <div className="flex items-center gap-1">
                            <input
                              type="number"
                              step="1"
                              min="2"
                              max="50"
                              value={trailingOffsetPct}
                              onChange={(e) => setTrailingOffsetPct(Number(e.target.value))}
                              className="w-14 border border-[#dde3ec] rounded px-1.5 py-0.5 text-center ltr-inline font-bold"
                            />
                            <span>%</span>
                          </div>
                        )}
                      </div>
                    </div>

                    <div className="bg-white border border-[#dde3ec] rounded-xl p-3 space-y-2 text-xs">
                      <div className="font-bold text-[#1f2430]">⚙️ سیاست ارسال سفارش فروش به CLOB</div>
                      <div>
                        <span className="block text-[11px] text-[#6b7684] mb-1">نوع سفارش خروج (Exit Policy)</span>
                        <select
                          value={exitPolicy}
                          onChange={(e) => setExitPolicy(e.target.value as any)}
                          className="w-full border border-[#dde3ec] rounded-lg px-2.5 py-1.5 text-xs font-semibold bg-[#f6f8fb]"
                        >
                          <option value="MARKETABLE_FAK">MARKETABLE_FAK (پیشنهادی - فروش فوری تا کف مجاز)</option>
                          <option value="MARKETABLE_FOK">MARKETABLE_FOK (یا کل حجم یکجا یا لغو)</option>
                          <option value="STRICT_LIMIT">STRICT_LIMIT (سفارش لیمیت GTC در اوردربوک)</option>
                        </select>
                      </div>
                      <div className="grid grid-cols-2 gap-2">
                        <div>
                          <span className="block text-[10.5px] text-[#6b7684] mb-0.5">حداکثر اسلیپیج (%)</span>
                          <input
                            type="number"
                            step="0.5"
                            min="0.5"
                            max="15"
                            value={maxSlippagePct}
                            onChange={(e) => setMaxSlippagePct(Number(e.target.value))}
                            className="w-full border border-[#dde3ec] rounded px-2 py-1 text-center font-bold ltr-inline"
                          />
                        </div>
                        <div>
                          <span className="block text-[10.5px] text-[#6b7684] mb-0.5">گام قیمت (Tick Size)</span>
                          <select
                            value={tickSize}
                            onChange={(e) => setTickSize(Number(e.target.value))}
                            className="w-full border border-[#dde3ec] rounded px-2 py-1 text-center font-bold ltr-inline bg-white"
                          >
                            <option value={0.01}>0.01 (1¢)</option>
                            <option value={0.001}>0.001 (0.1¢)</option>
                          </select>
                        </div>
                      </div>
                      <div className="text-[10.5px] text-[#6b7684] ltr-inline">
                        Slippage Floor → TP min: {worstFloorTpCents}¢ | SL min: {worstFloorSlCents}¢
                      </div>
                    </div>
                  </div>
                </>
              )}
            </div>
          )}

          {/* Visual SL <-- Entry --> TP Risk/Reward Bar */}
          {autoExitEnabled && (
            <div className="bg-[#f6f8fb] border border-[#dde3ec] rounded-xl px-4 py-3">
              <div className="flex flex-wrap items-center justify-between text-xs mb-2 gap-2">
                <span className="font-bold text-[#dc2626] ltr-inline">
                  🛑 SL: {slEnabled ? `${slPriceCents}¢ (-$${estSlLossUsdc.toFixed(2)})` : 'OFF'}
                </span>
                <span className="font-bold text-[#1f2430] ltr-inline">
                  {entryOrderType === 'CONDITIONAL_LIMIT_GTC' ? '⏳ Limit Buy' : '⚡ Entry'}: {entryCents}¢ (${usdcAmount.toFixed(2)})
                </span>
                <span className="font-bold text-[#16a34a] ltr-inline">
                  🎯 TP: {tpEnabled ? `${tpPriceCents}¢ (+$${estTpProfitUsdc.toFixed(2)})` : 'OFF'}
                </span>
              </div>
              <div className="w-full h-2.5 bg-gray-200 rounded-full overflow-hidden flex">
                <div className="bg-[#dc2626] h-full" style={{ width: '32%' }} />
                <div className="bg-[#2563eb] h-full" style={{ width: '8%' }} />
                <div className="bg-[#16a34a] h-full" style={{ width: '60%' }} />
              </div>
              {riskRewardRatio != null && (
                <div className="text-[11px] text-[#6b7684] text-center mt-1.5">
                  نسبت ریسک به ریوارد (Risk / Reward):{' '}
                  <b className="text-[#1f2430] ltr-inline">1 : {riskRewardRatio}</b>
                </div>
              )}
            </div>
          )}

          {/* Live Tick Simulator (Available in Edit Mode to verify PAPER/SHADOW triggers immediately) */}
          {isEdit && existing && (
            <div className="bg-[#fffbeb] border border-[#f59e0b66] rounded-xl p-3.5 space-y-2.5">
              <div className="flex items-center justify-between flex-wrap gap-2">
                <div className="text-xs font-bold text-[#b45309]">
                  🧪 شبیه‌ساز زندهٔ تیک بازار و دمای فرودگاه (تست عملکرد موتور قبل از پول واقعی)
                </div>
                <span className="text-[11px] bg-white border border-[#f59e0b55] text-[#b45309] px-2 py-0.5 rounded font-semibold ltr-inline">
                  State: {existing.position_state || 'WATCHING'}
                </span>
              </div>
              <p className="text-[11.5px] text-[#78350f] m-0">
                {isPendingEntry
                  ? `این سفارش در وضعیت انتظار خرید شرطی (${entryCents}¢) است. قیمتی کمتر یا مساوی ${entryCents}¢ وارد کنید تا پر شدن سفارش خرید و مسلح شدن خودکار TP/SL را تست کنید، یا تیک عبور دمای METAR را بزنید تا لغو خودکار سفارش خرید را ببینید.`
                  : `یک قیمت Best Bid فرضی (مثلاً بالاتر از ${tpPriceCents}¢ برای تست سیو سود یا پایین‌تر از ${slPriceCents}¢ برای تست حد ضرر) وارد کنید تا اجرای آنی موتور Auto-Exit را ببینید:`}
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <div className="flex items-center gap-1 bg-white border border-[#f59e0b66] rounded-lg px-2.5 py-1.5">
                  <span className="text-xs text-[#78350f]">
                    {isPendingEntry ? 'قیمت فروشنده بازار (Ask ¢):' : 'قیمت خریدار بازار (Best Bid ¢):'}
                  </span>
                  <input
                    type="number"
                    step="0.5"
                    min="0.5"
                    max="99.9"
                    value={simBidCents}
                    onChange={(e) => setSimBidCents(e.target.value)}
                    className="w-20 font-bold text-sm text-center ltr-inline border-none focus:outline-none"
                  />
                  <span className="text-xs font-bold text-[#78350f]">¢</span>
                </div>

                <label className="flex items-center gap-1.5 bg-white border border-[#f59e0b66] rounded-lg px-2.5 py-1.5 text-xs text-[#78350f] cursor-pointer">
                  <input
                    type="checkbox"
                    checked={simMetarElim}
                    onChange={(e) => setSimMetarElim(e.target.checked)}
                  />
                  🌡️ شبیه‌سازی عبور دمای METAR از سقف باکت
                </label>

                <button
                  type="button"
                  disabled={simRunning}
                  onClick={handleRunSimulateTick}
                  className="bg-[#d97706] hover:bg-[#b45309] text-white border-none rounded-lg px-3.5 py-1.5 text-xs font-bold cursor-pointer"
                >
                  {simRunning ? '⏳ در حال ارزیابی...' : '⚡ ارسال تیک به موتور ربات'}
                </button>
              </div>
              {simResultMsg && (
                <div
                  className={`text-xs font-semibold px-3 py-2 rounded-lg border ${
                    simResultMsg.isError
                      ? 'bg-red-50 text-red-700 border-red-200'
                      : 'bg-emerald-50 text-emerald-800 border-emerald-200'
                  }`}
                >
                  {simResultMsg.text}
                </div>
              )}
            </div>
          )}

          {/* Validation / Server Feedback Messages */}
          {(validationError || errorMsg) && (
            <div className="bg-[#fdeceb] border border-[#dc262655] text-[#dc2626] rounded-xl px-3.5 py-2.5 text-xs font-semibold">
              ❌ {errorMsg || validationError}
            </div>
          )}
          {successMsg && (
            <div className="bg-[#e8f8ee] border border-[#16a34a55] text-[#16a34a] rounded-xl px-3.5 py-2.5 text-xs font-semibold">
              {successMsg}
            </div>
          )}

          {/* Footer Action Buttons */}
          <div className="flex items-center justify-end gap-2.5 pt-2 border-t border-[#dde3ec]">
            <button
              type="button"
              onClick={onClose}
              className="bg-white hover:bg-gray-50 text-[#4b5563] border border-[#dde3ec] rounded-xl px-4 py-2.5 text-xs font-semibold cursor-pointer"
            >
              انصراف
            </button>
            <button
              type="submit"
              disabled={submitting || Boolean(validationError)}
              className={`rounded-xl px-5 py-2.5 text-xs md:text-sm font-bold text-white border-none cursor-pointer shadow-sm transition-all ${
                validationError
                  ? 'bg-gray-400 cursor-not-allowed'
                  : entryOrderType === 'CONDITIONAL_LIMIT_GTC'
                  ? 'bg-[#9333ea] hover:bg-[#7e22ce]'
                  : 'bg-[#16a34a] hover:bg-[#15803d]'
              }`}
            >
              {submitting
                ? '⏳ در حال ثبت...'
                : isEdit
                ? `💾 ذخیره تغییرات (نسخه v${(existing?.rule_version || 1) + 1})`
                : entryOrderType === 'CONDITIONAL_LIMIT_GTC'
                ? `⏳ ثبت سفارش خرید شرطی در ${entryCents}¢ + مسلح‌سازی خودکار TP/SL`
                : `⚡ ثبت سفارش خرید فوری (${entryCents}¢) + فعال‌سازی TP/SL`}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
