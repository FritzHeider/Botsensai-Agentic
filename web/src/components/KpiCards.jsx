import React from 'react';
import { Wallet, TrendingUp, ShieldCheck, Target, ArrowUpRight, ArrowDownRight } from 'lucide-react';

export default function KpiCards({ wallet, positions }) {
  const activePnl = positions.length > 0 ? positions[0].unrealized_pnl_pct : 0;
  const isPositive = activePnl >= 0;

  return (
    <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
      {/* 1. Hot Wallet */}
      <div className="bg-white dark:bg-[#0c0c0f] border border-zinc-200 dark:border-zinc-800 rounded-xl p-5 shadow-sm transition-all hover:border-zinc-300 dark:hover:border-zinc-700">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-medium text-zinc-500 dark:text-zinc-400">Hot Wallet Balance</span>
          <div className="p-2 rounded-lg bg-blue-50 dark:bg-blue-950/40 text-blue-600 dark:text-blue-400">
            <Wallet className="w-4 h-4" />
          </div>
        </div>
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold font-mono text-zinc-950 dark:text-zinc-50">
            {wallet ? wallet.balance_sol.toFixed(4) : '0.0000'}
          </span>
          <span className="text-xs font-semibold text-zinc-400">SOL</span>
        </div>
        <div className="mt-2 flex items-center gap-1.5">
          <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[11px] font-medium bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-400">
            +{wallet ? wallet.buffer_sol.toFixed(4) : '0.0000'} Buffer
          </span>
          <span className="text-[11px] text-zinc-400">above 0.010 floor</span>
        </div>
      </div>

      {/* 2. Active PnL */}
      <div className="bg-white dark:bg-[#0c0c0f] border border-zinc-200 dark:border-zinc-800 rounded-xl p-5 shadow-sm transition-all hover:border-zinc-300 dark:hover:border-zinc-700">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-medium text-zinc-500 dark:text-zinc-400">Active Runner PnL</span>
          <div className={`p-2 rounded-lg ${isPositive ? 'bg-emerald-50 dark:bg-emerald-950/40 text-emerald-600 dark:text-emerald-400' : 'bg-rose-50 dark:bg-rose-950/40 text-rose-600 dark:text-rose-400'}`}>
            <TrendingUp className="w-4 h-4" />
          </div>
        </div>
        <div className="flex items-baseline gap-2">
          <span className={`text-2xl font-bold font-mono ${isPositive ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}`}>
            {isPositive ? `+${activePnl.toFixed(2)}%` : `${activePnl.toFixed(2)}%`}
          </span>
        </div>
        <div className="mt-2 flex items-center gap-1.5">
          <span className="text-[11px] font-medium text-zinc-500 dark:text-zinc-400">
            {positions.length > 0 ? `$${positions[0].symbol} Live Position` : 'No active trade'}
          </span>
        </div>
      </div>

      {/* 3. Portfolio Capacity */}
      <div className="bg-white dark:bg-[#0c0c0f] border border-zinc-200 dark:border-zinc-800 rounded-xl p-5 shadow-sm transition-all hover:border-zinc-300 dark:hover:border-zinc-700">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-medium text-zinc-500 dark:text-zinc-400">Portfolio Slots</span>
          <div className="p-2 rounded-lg bg-purple-50 dark:bg-purple-950/40 text-purple-600 dark:text-purple-400">
            <ShieldCheck className="w-4 h-4" />
          </div>
        </div>
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold font-mono text-zinc-950 dark:text-zinc-50">
            {positions.length} / 3
          </span>
          <span className="text-xs font-semibold text-zinc-400">Slots</span>
        </div>
        <div className="mt-2 flex items-center gap-1.5">
          <div className="w-full bg-zinc-100 dark:bg-zinc-800 rounded-full h-1.5 overflow-hidden">
            <div 
              className="bg-blue-500 h-1.5 rounded-full transition-all duration-500" 
              style={{ width: `${(positions.length / 3) * 100}%` }}
            />
          </div>
        </div>
      </div>

      {/* 4. Strategy Mode */}
      <div className="bg-white dark:bg-[#0c0c0f] border border-zinc-200 dark:border-zinc-800 rounded-xl p-5 shadow-sm transition-all hover:border-zinc-300 dark:hover:border-zinc-700">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-medium text-zinc-500 dark:text-zinc-400">Exit Ladder Calibration</span>
          <div className="p-2 rounded-lg bg-amber-50 dark:bg-amber-950/40 text-amber-600 dark:text-amber-400">
            <Target className="w-4 h-4" />
          </div>
        </div>
        <div className="flex items-baseline gap-2">
          <span className="text-lg font-bold font-mono text-zinc-950 dark:text-zinc-50">
            4-Tier Moonbag
          </span>
        </div>
        <div className="mt-2 text-[11px] font-mono text-zinc-400">
          1.40× · 2.00× · 4.00× · 10.0×
        </div>
      </div>
    </div>
  );
}
