import React, { useState } from 'react';
import { format } from 'date-fns';
import { Filter, ArrowUpDown, ChevronLeft, ChevronRight } from 'lucide-react';

export default function SignalsTable({ signals, onSelectSignal }) {
  const [filterSide, setFilterSide] = useState('ALL');
  const [currentPage, setCurrentPage] = useState(1);
  const pageSize = 10;

  const filtered = signals.filter(s => {
    if (filterSide === 'ALL') return true;
    return s.side === filterSide;
  });

  const totalPages = Math.ceil(filtered.length / pageSize) || 1;
  const paginated = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const getStatusBadge = (status) => {
    switch (status) {
      case 'LANDED':
        return <span className="px-2 py-0.5 rounded text-[11px] font-semibold bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-400">LANDED</span>;
      case 'PENDING':
        return <span className="px-2 py-0.5 rounded text-[11px] font-semibold bg-blue-100 text-blue-800 dark:bg-blue-950/50 dark:text-blue-400 animate-pulse">PENDING</span>;
      case 'REVERTED':
        return <span className="px-2 py-0.5 rounded text-[11px] font-semibold bg-amber-100 text-amber-800 dark:bg-amber-950/50 dark:text-amber-400">REVERTED</span>;
      case 'BLOCKED':
      case 'FAILED':
        return <span className="px-2 py-0.5 rounded text-[11px] font-semibold bg-rose-100 text-rose-800 dark:bg-rose-950/50 dark:text-rose-400">{status}</span>;
      default:
        return <span className="px-2 py-0.5 rounded text-[11px] font-semibold bg-zinc-100 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-400">{status}</span>;
    }
  };

  return (
    <div className="bg-white dark:bg-[#0c0c0f] border border-zinc-200 dark:border-zinc-800 rounded-xl overflow-hidden shadow-sm">
      {/* Table Toolbar */}
      <div className="px-5 py-3 border-b border-zinc-200 dark:border-zinc-800 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-zinc-950 dark:text-zinc-50">Signal Execution Telemetry</h3>
        <div className="flex items-center gap-2">
          <Filter className="w-3.5 h-3.5 text-zinc-400" />
          <select 
            value={filterSide}
            onChange={(e) => { setFilterSide(e.target.value); setCurrentPage(1); }}
            className="text-xs bg-zinc-50 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800 rounded-lg px-2 py-1 text-zinc-700 dark:text-zinc-300 focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            <option value="ALL">All Actions</option>
            <option value="BUY">BUY Only</option>
            <option value="SELL">SELL Only</option>
          </select>
        </div>
      </div>

      {/* Table Content */}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="bg-zinc-50 dark:bg-zinc-900/60 border-b border-zinc-200 dark:border-zinc-800 text-zinc-500 dark:text-zinc-400 uppercase tracking-wider font-medium">
            <tr>
              <th className="px-5 py-3">ID / Time</th>
              <th className="px-5 py-3">Token</th>
              <th className="px-5 py-3">Action</th>
              <th className="px-5 py-3">Size (SOL)</th>
              <th className="px-5 py-3">Score</th>
              <th className="px-5 py-3">Status</th>
              <th className="px-5 py-3 text-right">Jito Tip</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800/60 font-mono">
            {paginated.map((sig) => {
              const dt = sig.created_at ? new Date(sig.created_at * 1000) : new Date();
              const scorePct = Math.round((sig.score || 0) * 100);
              return (
                <tr 
                  key={sig.id}
                  onClick={() => onSelectSignal && onSelectSignal(sig)}
                  className="hover:bg-zinc-50/80 dark:hover:bg-zinc-800/40 cursor-pointer transition-colors"
                >
                  <td className="px-5 py-3 text-zinc-500 dark:text-zinc-400">
                    #{sig.id} <span className="text-[10px] text-zinc-400 block font-sans">{format(dt, 'HH:mm:ss')}</span>
                  </td>
                  <td className="px-5 py-3 font-semibold text-zinc-900 dark:text-zinc-100">
                    ${sig.symbol || 'TOKEN'}
                  </td>
                  <td className="px-5 py-3">
                    <span className={`font-semibold ${sig.side === 'BUY' ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}`}>
                      {sig.side}
                    </span>
                  </td>
                  <td className="px-5 py-3 text-zinc-800 dark:text-zinc-200">
                    {(sig.size_native || 0).toFixed(4)}
                  </td>
                  <td className="px-5 py-3">
                    <div className="flex items-center gap-2">
                      <div className="w-12 bg-zinc-200 dark:bg-zinc-800 rounded-full h-1.5 overflow-hidden">
                        <div 
                          className="bg-blue-500 h-1.5 rounded-full" 
                          style={{ width: `${scorePct}%` }}
                        />
                      </div>
                      <span className="text-[11px] text-zinc-400">{scorePct}%</span>
                    </div>
                  </td>
                  <td className="px-5 py-3">
                    {getStatusBadge(sig.status)}
                  </td>
                  <td className="px-5 py-3 text-right text-zinc-500 dark:text-zinc-400">
                    {(sig.jito_tip_lamports || 0).toLocaleString()}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Pagination Footer */}
      <div className="px-5 py-3 border-t border-zinc-200 dark:border-zinc-800 flex items-center justify-between text-xs text-zinc-500">
        <span>Showing {paginated.length} of {filtered.length} signals</span>
        <div className="flex items-center gap-1">
          <button 
            disabled={currentPage <= 1}
            onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
            className="p-1 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-40"
          >
            <ChevronLeft className="w-4 h-4" />
          </button>
          <span className="px-2 font-mono">{currentPage} / {totalPages}</span>
          <button 
            disabled={currentPage >= totalPages}
            onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))}
            className="p-1 rounded hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-40"
          >
            <ChevronRight className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
