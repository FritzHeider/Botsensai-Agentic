import React, { useState, useEffect } from 'react';
import { Sun, Moon, Sparkles, Activity, Shield, RefreshCw } from 'lucide-react';
import KpiCards from './components/KpiCards';
import LivePositions from './components/LivePositions';
import SignalsTable from './components/SignalsTable';
import ChartsPanel from './components/ChartsPanel';
import ChatPanel from './components/ChatPanel';

export default function App() {
  const [isDarkMode, setIsDarkMode] = useState(true);
  const [isChatOpen, setIsChatOpen] = useState(false);
  const [wallet, setWallet] = useState(null);
  const [positions, setPositions] = useState([]);
  const [signals, setSignals] = useState([]);
  const [lastUpdated, setLastUpdated] = useState(new Date());

  const fetchData = async () => {
    try {
      const [wRes, pRes, sRes] = await Promise.all([
        fetch('/api/wallet').then(r => r.json()),
        fetch('/api/positions').then(r => r.json()),
        fetch('/api/signals').then(r => r.json()),
      ]);
      setWallet(wRes);
      setPositions(pRes.positions || []);
      setSignals(sRes.signals || []);
      setLastUpdated(new Date());
    } catch (e) {
      console.error('Failed to fetch dashboard data:', e);
    }
  };

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 4000);
    return () => clearInterval(interval);
  }, []);

  const toggleDarkMode = () => {
    setIsDarkMode(!isDarkMode);
    if (!isDarkMode) {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
  };

  return (
    <div className={`min-h-screen ${isDarkMode ? 'dark bg-[#09090b] text-zinc-50' : 'bg-zinc-50 text-zinc-950'} transition-colors duration-200`}>
      {/* Top Header */}
      <header className="border-b border-zinc-200 dark:border-zinc-800 bg-white/80 dark:bg-[#0c0c0f]/80 backdrop-blur sticky top-0 z-40">
        <div className="max-w-[1400px] mx-auto px-6 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-blue-600 flex items-center justify-center text-white font-bold text-sm shadow-sm">
              ⚡
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-sm font-bold tracking-tight">BOTSENSAI</h1>
                <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-emerald-100 text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-400">
                  LIVE TRADING
                </span>
              </div>
              <p className="text-[11px] text-zinc-500 dark:text-zinc-400">Autonomous Solana Meme Execution Engine</p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {/* Live Indicator */}
            <div className="hidden sm:flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-zinc-100 dark:bg-zinc-800 text-[11px] text-zinc-600 dark:text-zinc-300">
              <span className="w-2 h-2 rounded-full bg-emerald-500 animate-ping" />
              <span>EC2 Daemon Active</span>
            </div>

            {/* Refresh */}
            <button 
              onClick={fetchData} 
              className="p-2 rounded-lg border border-zinc-200 dark:border-zinc-800 hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors"
              title="Refresh Data"
            >
              <RefreshCw className="w-4 h-4" />
            </button>

            {/* Theme Toggle */}
            <button 
              onClick={toggleDarkMode} 
              className="p-2 rounded-lg border border-zinc-200 dark:border-zinc-800 hover:bg-zinc-100 dark:hover:bg-zinc-800 text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200 transition-colors"
              title="Toggle Theme"
            >
              {isDarkMode ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
            </button>

            {/* Chat Trigger Button */}
            <button 
              onClick={() => setIsChatOpen(!isChatOpen)}
              className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-3.5 py-1.5 rounded-lg text-xs font-medium transition-colors shadow-sm ml-2"
            >
              <Sparkles className="w-3.5 h-3.5" />
              <span>Ask Gemini</span>
            </button>
          </div>
        </div>
      </header>

      {/* Main Container */}
      <main className="max-w-[1400px] mx-auto px-6 py-8">
        <KpiCards wallet={wallet} positions={positions} />
        
        {/* Main Split: Live Positions & Velocity Chart */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
          <LivePositions positions={positions} />
          <ChartsPanel isDarkMode={isDarkMode} />
        </div>

        {/* Signals Telemetry */}
        <SignalsTable signals={signals} />
      </main>

      {/* Gemini AI Chat Drawer */}
      <ChatPanel isOpen={isChatOpen} onClose={() => setIsChatOpen(false)} />
    </div>
  );
}
