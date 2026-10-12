import React from 'react';
import { ExternalLink, CheckCircle2, Flame } from 'lucide-react';

export default function LivePositions({ positions, onSelectToken }) {
  if (!positions || positions.length === 0) {
    return (
      <div className="bg-white dark:bg-[#0c0c0f] border border-zinc-200 dark:border-zinc-800 rounded-xl p-8 text-center">
        <p className="text-zinc-500 dark:text-zinc-400 text-sm">No open positions currently held. Waiting for sweep triggers.</p>
      </div>
    );
  }

  return (
    <div className="bg-white dark:bg-[#0c0c0f] border border-zinc-200 dark:border-zinc-800 rounded-xl overflow-hidden mb-6 shadow-sm">
      <div className="px-5 py-4 border-b border-zinc-200 dark:border-zinc-800 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Flame className="w-4 h-4 text-amber-500 animate-pulse" />
          <h2 className="text-sm font-semibold text-zinc-950 dark:text-zinc-50">Active On-Chain Holdings</h2>
        </div>
        <span className="text-xs font-mono text-zinc-400">{positions.length} position</span>
      </div>

      <div className="divide-y divide-zinc-100 dark:divide-zinc-800/60">
        {positions.map((pos) => {
          const isPos = pos.unrealized_pnl_pct >= 0;
          return (
            <div 
              key={pos.mint} 
              onClick={() => onSelectToken && onSelectToken(pos)}
              className="p-5 hover:bg-zinc-50/80 dark:hover:bg-zinc-800/40 cursor-pointer transition-colors"
            >
              <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                {/* Left: Token info */}
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-base font-bold text-zinc-950 dark:text-zinc-50">${pos.symbol}</span>
                    <span className="text-xs font-mono text-zinc-400 truncate max-w-[120px]">{pos.mint}</span>
                    <a 
                      href={`https://dexscreener.com/solana/${pos.mint}`} 
                      target="_blank" 
                      rel="noreferrer"
                      onClick={(e) => e.stopPropagation()}
                      className="text-zinc-400 hover:text-blue-500"
                    >
                      <ExternalLink className="w-3.5 h-3.5" />
                    </a>
                  </div>
                  <div className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
                    Holding: <span className="font-mono text-zinc-800 dark:text-zinc-200">{pos.amount.toLocaleString(undefined, { maximumFractionDigits: 2 })} tokens</span> (~{pos.value_sol} SOL)
                  </div>
                </div>

                {/* Center: Live PnL */}
                <div className="text-right">
                  <div className={`text-lg font-bold font-mono ${isPos ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}`}>
                    {isPos ? `+${pos.unrealized_pnl_pct.toFixed(2)}%` : `${pos.unrealized_pnl_pct.toFixed(2)}%`}
                  </div>
                  <div className="text-xs font-mono text-zinc-400">
                    {pos.current_price_sol.toFixed(8)} SOL (${pos.current_price_usd.toFixed(4)})
                  </div>
                </div>
              </div>

              {/* Bottom: 4-Tier Ladder Progress */}
              <div className="mt-4 pt-4 border-t border-zinc-100 dark:border-zinc-800/40">
                <div className="text-[11px] font-medium text-zinc-400 mb-2">4-Tier Moonbag Targets:</div>
                <div className="grid grid-cols-4 gap-2">
                  {pos.ladder_targets.map((tgt, i) => (
                    <div key={i} className="bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-lg p-2 text-center">
                      <div className="text-[10px] text-zinc-400">Rung {tgt.rung} ({tgt.pct}%)</div>
                      <div className="text-xs font-mono font-semibold text-zinc-700 dark:text-zinc-300 truncate">
                        {tgt.price_sol ? tgt.price_sol.toFixed(8) : '-'}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
