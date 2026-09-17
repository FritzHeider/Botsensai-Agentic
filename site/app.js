/**
 * Botsensai.com — Client Application Logic
 * Fetches and renders live Solana quantitative alpha, top sniper picks, and telemetry.
 */

let currentSnapshot = null;
let activeFilter = 'all';

async function fetchSnapshot() {
  try {
    const res = await fetch('/api/snapshot', { cache: 'no-cache' });
    if (res.ok) {
      currentSnapshot = await res.json();
    } else {
      throw new Error(`API error ${res.status}`);
    }
  } catch (err) {
    console.warn('Falling back to static snapshot.json:', err);
    try {
      const fallback = await fetch('snapshot.json');
      currentSnapshot = await fallback.json();
    } catch (fallbackErr) {
      console.error('Failed to load snapshot.json:', fallbackErr);
      return;
    }
  }
  renderDashboard(currentSnapshot);
}

function renderDashboard(data) {
  if (!data) return;

  // 1. Meta / Global Telemetry
  const meta = data.meta || {};
  const elLaunches = document.getElementById('meta-launches');
  if (elLaunches) elLaunches.innerText = Number(meta.total_launches || 36286).toLocaleString();

  const elSignals = document.getElementById('meta-signals');
  if (elSignals) elSignals.innerText = meta.total_signals || 27;

  const elWinRate = document.getElementById('meta-winrate');
  if (elWinRate) elWinRate.innerText = `${meta.canary_win_rate_pct || 75.0}%`;

  const elRegime = document.getElementById('meta-regime');
  if (elRegime) elRegime.innerText = meta.regime || 'HOT';

  // 2. Render Hero Sniper Pick
  const top = data.top_sniper_pick;
  if (top) {
    renderHeroSniper(top);
  }

  // 3. Render Signal Matrix Table
  renderSignalTable(data.signals || [], data.candidates || []);

  // 4. Update Ticker Content
  renderTicker(data);
}

function renderHeroSniper(top) {
  const symbolEl = document.getElementById('hero-symbol');
  if (symbolEl) symbolEl.innerText = top.symbol || 'MEMEBID';

  const nameEl = document.getElementById('hero-name');
  if (nameEl) nameEl.innerText = top.name || 'MemeBID';

  const mintEl = document.getElementById('hero-mint');
  const mintTrunc = top.mint ? `${top.mint.slice(0, 8)}...${top.mint.slice(-6)}` : 'solana:...pump';
  if (mintEl) mintEl.innerText = mintTrunc;

  const copyBtn = document.getElementById('hero-copy-btn');
  if (copyBtn) {
    copyBtn.onclick = () => {
      navigator.clipboard.writeText(top.mint || '');
      copyBtn.innerText = '✓ Copied';
      setTimeout(() => { copyBtn.innerText = 'Copy'; }, 1500);
    };
  }

  // Dynamic token image
  const imgEl = document.getElementById('hero-img');
  const emojiEl = document.getElementById('hero-emoji');
  if (top.image_uri && imgEl) {
    imgEl.src = top.image_uri;
    imgEl.alt = top.symbol || 'Token';
    imgEl.classList.remove('hidden');
    if (emojiEl) emojiEl.classList.add('hidden');
  } else if (imgEl) {
    imgEl.classList.add('hidden');
    if (emojiEl) emojiEl.classList.remove('hidden');
  }

  const scoreEl = document.getElementById('hero-score');
  if (scoreEl) scoreEl.innerText = top.composite ? top.composite.toFixed(3) : '0.899';

  const covEl = document.getElementById('hero-coverage');
  if (covEl) covEl.innerText = `${top.coverage_pct || 50}%`;

  const progEl = document.getElementById('hero-bonding-prog');
  if (progEl) progEl.innerText = `${top.bonding_curve_progress || 35.0}%`;

  // Gauges
  const exitDepth = top.exit_depth_contrib || 0.341;
  const exitDepthPct = Math.min(100, Math.round((exitDepth / 0.35) * 100));
  const elExitGauge = document.getElementById('gauge-exit-depth');
  if (elExitGauge) elExitGauge.style.width = `${exitDepthPct}%`;

  const deployer = top.deployer_behaviour_contrib || 0.186;
  const deployerPct = Math.min(100, Math.round((deployer / 0.20) * 100));
  const elDeployerGauge = document.getElementById('gauge-deployer');
  if (elDeployerGauge) elDeployerGauge.style.width = `${deployerPct}%`;

  // Links
  const mint = top.mint || '9CHnozHgtQVCYu6Z7SzWkxdNJEdMy8Tt3bh7B2wQpump';
  setLink('hero-link-dex', `https://dexscreener.com/solana/${mint}`);
  setLink('hero-link-pump', `https://pump.fun/${mint}`);
  setLink('hero-link-solscan', `https://solscan.io/token/${mint}`);
  setLink('hero-link-photon', `https://photon-sol.tinyastro.io/en/lp/${mint}`);
}

function setLink(id, url) {
  const el = document.getElementById(id);
  if (el) el.href = url;
}

function renderSignalTable(signals, candidates) {
  const tbody = document.getElementById('signal-table-body');
  if (!tbody) return;

  // Build merged map of tokens
  const rows = [];

  signals.forEach((s) => {
    const cand = candidates.find((c) => c.signal_id === s.id || c.mint === s.mint) || {};
    const isVetoed = cand.is_vetoed || s.is_vetoed || s.status === 'VETOED';
    const score = s.score || cand.composite || 0.0;
    const size = s.size_native || cand.size_sol || 0.005;
    const multiple = (s.outcome && s.outcome.max_multiple) || cand.max_multiple;

    let perfLabel = 'Signal Confirmed';
    let perfClass = 'text-slate-300';
    let rowType = 'active';

    if (isVetoed) {
      perfLabel = 'Honeypot Intercepted 🛡️';
      perfClass = 'text-rose-400 font-bold';
      rowType = 'vetoed';
    } else if (multiple && multiple >= 1.3) {
      perfLabel = `Peak ${multiple.toFixed(2)}x (+${Math.round((multiple - 1) * 100)}%) 🚀`;
      perfClass = 'text-emerald-400 font-bold';
      rowType = 'runners elite';
    } else if (score >= 0.88) {
      perfLabel = 'High Conviction 🎯';
      perfClass = 'text-cyan-300 font-semibold';
      rowType = 'elite';
    }

    rows.push({
      id: s.id,
      symbol: s.symbol,
      mint: s.mint,
      image_uri: s.image_uri,
      score: score.toFixed(3),
      coverage: (cand.coverage_pct || s.coverage_pct) ? `${cand.coverage_pct || s.coverage_pct}%` : '50%',
      regime: (cand.regime || s.regime || 'HOT').toUpperCase(),
      size: `${size.toFixed(4)} SOL`,
      perfLabel,
      perfClass,
      rowType,
      isVetoed,
      notes: cand.explanation || s.explanation || `Signal #${s.id} evaluated under live radar pipeline.`,
    });
  });

  // Render rows
  tbody.innerHTML = rows.map((r) => `
    <tr class="hover:bg-white/[0.04] transition-colors border-b border-white/5" data-type="${r.rowType}">
      <td class="py-3.5 px-4 font-bold ${r.isVetoed ? 'text-rose-400' : 'text-cyan-400'}">#${r.id}</td>
      <td class="py-3.5 px-4">
        <div class="flex items-center gap-2.5">
          <span class="w-7 h-7 rounded-lg ${r.isVetoed ? 'bg-rose-500/20 border-rose-500/40 text-rose-300' : 'bg-cyan-500/20 border-cyan-500/40 text-cyan-300'} border flex items-center justify-center font-bold text-xs overflow-hidden">
            ${r.image_uri ? `<img src="${r.image_uri}" alt="" class="w-full h-full object-cover" onerror="this.remove()">` : (r.isVetoed ? '🚫' : '⚡')}
          </span>
          <div>
            <span class="font-bold ${r.isVetoed ? 'text-slate-400 line-through' : 'text-white'} block">${r.symbol}</span>
            <span class="text-[10px] text-slate-500">${r.mint ? r.mint.slice(0, 10) + '...' : ''}</span>
          </div>
        </div>
      </td>
      <td class="py-3.5 px-4">
        <span class="px-2 py-0.5 rounded font-bold text-xs ${r.isVetoed ? 'bg-rose-500/20 text-rose-300 border border-rose-500/30' : 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'}">
          ${r.score}
        </span>
      </td>
      <td class="py-3.5 px-4 text-slate-300">${r.coverage}</td>
      <td class="py-3.5 px-4">
        <span class="text-xs font-semibold ${r.isVetoed ? 'text-rose-400' : 'text-amber-300'}">${r.regime}</span>
      </td>
      <td class="py-3.5 px-4 text-slate-300">${r.isVetoed ? '0 SOL (BLOCKED)' : r.size}</td>
      <td class="py-3.5 px-4 ${r.perfClass}">${r.perfLabel}</td>
      <td class="py-3.5 px-4 text-right">
        <button onclick="openDossier('${r.symbol}', '${r.score}', '${r.coverage}', '${r.size}', '${r.mint}', \`${r.notes}\`)" class="px-2.5 py-1 rounded bg-white/5 hover:bg-cyan-500/20 text-cyan-300 border border-white/10 hover:border-cyan-500/30 text-xs transition-colors">
          Dossier ↗
        </button>
      </td>
    </tr>
  `).join('');

  applyActiveFilter();
}

function filterTable(type, btn) {
  activeFilter = type;
  document.querySelectorAll('.tab-btn').forEach((b) => {
    b.classList.remove('bg-cyan-500/20', 'text-cyan-300', 'border', 'border-cyan-500/30', 'font-bold');
    b.classList.add('text-slate-400');
  });
  if (btn) {
    btn.classList.add('bg-cyan-500/20', 'text-cyan-300', 'border', 'border-cyan-500/30', 'font-bold');
    btn.classList.remove('text-slate-400');
  }
  applyActiveFilter();
}

function applyActiveFilter() {
  const rows = document.querySelectorAll('#signal-table-body tr');
  rows.forEach((r) => {
    const dataType = r.getAttribute('data-type') || '';
    if (activeFilter === 'all') {
      r.style.display = '';
    } else {
      r.style.display = dataType.includes(activeFilter) ? '' : 'none';
    }
  });
}

function renderTicker(data) {
  const tickerTrack = document.getElementById('ticker-track');
  if (!tickerTrack) return;

  const signals = data.signals || [];
  const topSignals = signals.slice(0, 6);

  const itemsHtml = `
    <div class="flex items-center gap-2">
      <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
      <span class="text-slate-400">ENGINE STATUS:</span>
      <span class="text-emerald-400 font-bold">LIVE SWEEP (HOT REGIME)</span>
    </div>
    ${topSignals.map((s) => `
      <div class="flex items-center gap-2">
        <span class="text-cyan-400 font-bold">🎯 ${s.symbol}:</span>
        <span>Score ${s.score.toFixed(3)}</span>
        <span class="${s.is_vetoed || s.status === 'VETOED' ? 'text-rose-400' : 'text-emerald-400'}">${s.is_vetoed || s.status === 'VETOED' ? 'VETOED' : 'CONFIRMED'}</span>
      </div>
    `).join('')}
    <div class="flex items-center gap-2">
      <span class="text-purple-400 font-bold">⚡ JITO EXECUTION:</span>
      <span class="text-slate-300">350ms Bundle Latency</span>
    </div>
    <div class="flex items-center gap-2">
      <span class="text-amber-400 font-bold">🛡️ SOCIAL SESSIONS:</span>
      <span class="text-slate-300">Active (@Botsensaix)</span>
    </div>
  `;

  // Repeat twice for seamless infinite scrolling
  tickerTrack.innerHTML = itemsHtml + itemsHtml;
}

// Countdown timer to 30-min sniper refresh epoch
function updateCountdown() {
  const now = new Date();
  const minutes = now.getUTCMinutes();
  const seconds = now.getUTCSeconds();
  const remMinutes = (minutes < 30 ? 29 - minutes : 59 - minutes);
  const remSeconds = 59 - seconds;
  const el = document.getElementById('hero-timer');
  if (el) {
    el.innerText = `${String(remMinutes).padStart(2, '0')}:${String(remSeconds).padStart(2, '0')}`;
  }
}

// Token Dossier Drawer
function openDossier(symbol, score, coverage, size, mint, notes) {
  const drawer = document.getElementById('dossier-drawer');
  if (!drawer) return;

  document.getElementById('dossier-title').innerText = `${symbol} DOSSIER`;
  document.getElementById('dossier-score').innerText = score;
  document.getElementById('dossier-coverage').innerText = coverage;
  document.getElementById('dossier-size').innerText = size;
  document.getElementById('dossier-notes').innerText = notes || 'No extra notes recorded.';

  const cleanMint = mint || '';
  document.getElementById('dossier-link-dex').href = `https://dexscreener.com/solana/${cleanMint}`;
  document.getElementById('dossier-link-pump').href = `https://pump.fun/${cleanMint}`;
  document.getElementById('dossier-link-solscan').href = `https://solscan.io/token/${cleanMint}`;
  document.getElementById('dossier-link-photon').href = `https://photon-sol.tinyastro.io/en/lp/${cleanMint}`;

  drawer.classList.remove('translate-x-full');
}

function closeDossier() {
  const drawer = document.getElementById('dossier-drawer');
  if (drawer) drawer.classList.add('translate-x-full');
}

// Initialise
window.addEventListener('DOMContentLoaded', () => {
  fetchSnapshot();
  setInterval(fetchSnapshot, 30000); // Poll every 30s
  setInterval(updateCountdown, 1000);
  updateCountdown();
});
