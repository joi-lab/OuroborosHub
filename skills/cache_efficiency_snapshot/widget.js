/**
 * Cache Efficiency Snapshot — Standalone Module Widget
 * Dark Glassmorphic Dashboard with Canvas 2D Charts and Multi-Timeframe Analytics.
 * Zero external CDN dependencies.
 */
(function () {
  const root = document.getElementById('root');
  if (!root) return;

  // Injected Scoped CSS
  const style = document.createElement('style');
  style.textContent = `
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body, #root {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'SF Pro Display', Inter, sans-serif;
      background: #0d0b0f;
      color: #f8fafc;
      width: 100%;
      min-height: 100%;
      overflow-x: hidden;
      padding: 16px 18px;
      -webkit-font-smoothing: antialiased;
    }
    
    /* Layout */
    .ces-container {
      display: flex;
      flex-direction: column;
      gap: 14px;
      width: 100%;
      max-width: 1200px;
      margin: 0 auto;
    }

    /* Glass Card */
    .ces-card {
      background: rgba(23, 18, 28, 0.75);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border: 1px solid rgba(240, 122, 134, 0.14);
      border-radius: 12px;
      padding: 14px 18px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.36);
      transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }
    .ces-card:hover {
      border-color: rgba(240, 122, 134, 0.28);
      box-shadow: 0 10px 36px rgba(232, 93, 111, 0.08);
    }

    /* Error Banner */
    .ces-error-banner {
      display: none;
      background: rgba(244, 63, 94, 0.16);
      border: 1px solid rgba(244, 63, 94, 0.45);
      color: #fca5a5;
      padding: 10px 14px;
      border-radius: 8px;
      font-size: 0.8rem;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }

    /* Header */
    .ces-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
    }
    .ces-title-group {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .ces-pulse {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #34d399;
      box-shadow: 0 0 12px #34d399;
      animation: ces-pulse-glow 2s infinite ease-in-out;
    }
    @keyframes ces-pulse-glow {
      0%, 100% { transform: scale(1); opacity: 0.85; }
      50% { transform: scale(1.3); opacity: 1; box-shadow: 0 0 16px #34d399; }
    }
    .ces-pulse.error {
      background: #f43f5e;
      box-shadow: 0 0 12px #f43f5e;
      animation: none;
    }
    .ces-title {
      font-size: 1.12rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: #f8fafc;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .ces-subtitle {
      font-size: 0.76rem;
      color: #94a3b8;
      margin-top: 2px;
    }

    /* Controls */
    .ces-controls {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .ces-timeframe-group {
      display: inline-flex;
      background: rgba(13, 11, 15, 0.85);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 8px;
      padding: 3px;
    }
    .ces-tf-btn {
      background: transparent;
      border: none;
      color: #94a3b8;
      font-size: 0.75rem;
      font-weight: 600;
      padding: 5px 12px;
      border-radius: 6px;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .ces-tf-btn:hover {
      color: #f8fafc;
      background: rgba(255, 255, 255, 0.05);
    }
    .ces-tf-btn.active {
      color: #f8fafc;
      background: rgba(232, 93, 111, 0.22);
      border: 1px solid rgba(232, 93, 111, 0.4);
      box-shadow: 0 0 10px rgba(232, 93, 111, 0.25);
    }

    .ces-btn {
      background: rgba(23, 18, 28, 0.9);
      border: 1px solid rgba(240, 122, 134, 0.2);
      color: #f8fafc;
      font-size: 0.78rem;
      font-weight: 600;
      padding: 6px 14px;
      border-radius: 8px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s ease;
    }
    .ces-btn:hover {
      background: rgba(232, 93, 111, 0.15);
      border-color: rgba(232, 93, 111, 0.45);
    }
    .ces-btn:active { transform: scale(0.97); }
    .ces-btn.spinning svg {
      animation: ces-spin 0.8s linear infinite;
    }
    @keyframes ces-spin { 100% { transform: rotate(360deg); } }

    /* KPI Grid */
    .ces-kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }
    .ces-kpi-card {
      position: relative;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 8px;
    }
    .ces-kpi-card::before {
      content: '';
      position: absolute;
      top: 0; left: 0; bottom: 0;
      width: 4px;
      border-radius: 4px 0 0 4px;
    }
    .ces-kpi-card.crimson::before { background: #e85d6f; box-shadow: 0 0 8px #e85d6f; }
    .ces-kpi-card.cyan::before { background: #38bdf8; box-shadow: 0 0 8px #38bdf8; }
    .ces-kpi-card.emerald::before { background: #34d399; box-shadow: 0 0 8px #34d399; }
    .ces-kpi-card.violet::before { background: #a855f7; box-shadow: 0 0 8px #a855f7; }

    .ces-kpi-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .ces-kpi-label {
      font-size: 0.74rem;
      font-weight: 600;
      color: #94a3b8;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .ces-kpi-badge {
      font-size: 0.68rem;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.08);
      color: #f8fafc;
    }
    .ces-kpi-val {
      font-size: 1.75rem;
      font-weight: 800;
      letter-spacing: -0.03em;
      color: #ffffff;
      font-variant-numeric: tabular-nums;
      display: flex;
      align-items: baseline;
      gap: 4px;
    }
    .ces-kpi-unit {
      font-size: 0.95rem;
      font-weight: 600;
      color: #94a3b8;
    }
    .ces-kpi-sub {
      font-size: 0.74rem;
      color: #64748b;
      display: flex;
      align-items: center;
      gap: 5px;
    }

    /* Chart Section */
    .ces-chart-card {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .ces-chart-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
    }
    .ces-chart-title {
      font-size: 0.92rem;
      font-weight: 700;
      color: #f8fafc;
    }
    .ces-chart-mode-group {
      display: inline-flex;
      background: rgba(13, 11, 15, 0.85);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 6px;
      padding: 2px;
    }
    .ces-chart-mode-btn {
      background: transparent;
      border: none;
      color: #94a3b8;
      font-size: 0.72rem;
      font-weight: 600;
      padding: 4px 10px;
      border-radius: 4px;
      cursor: pointer;
    }
    .ces-chart-mode-btn.active {
      color: #38bdf8;
      background: rgba(56, 189, 248, 0.15);
    }

    .ces-canvas-wrap {
      position: relative;
      width: 100%;
      height: 230px;
      background: rgba(13, 11, 15, 0.5);
      border-radius: 10px;
      border: 1px solid rgba(255, 255, 255, 0.04);
      overflow: hidden;
    }
    canvas {
      display: block;
      width: 100%;
      height: 100%;
    }
    .ces-tooltip {
      position: absolute;
      pointer-events: none;
      display: none;
      background: rgba(17, 13, 22, 0.94);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(240, 122, 134, 0.3);
      box-shadow: 0 6px 20px rgba(0,0,0,0.6);
      border-radius: 8px;
      padding: 8px 12px;
      font-size: 0.74rem;
      color: #f8fafc;
      z-index: 100;
      white-space: nowrap;
      transform: translate(-50%, -110%);
      transition: opacity 0.1s ease;
    }

    /* Model Table */
    .ces-table-wrap {
      overflow-x: auto;
      margin-top: 6px;
    }
    table.ces-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.78rem;
      text-align: left;
    }
    table.ces-table th {
      padding: 8px 10px;
      font-weight: 600;
      color: #94a3b8;
      text-transform: uppercase;
      font-size: 0.68rem;
      letter-spacing: 0.05em;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      cursor: pointer;
      user-select: none;
    }
    table.ces-table th:hover { color: #f8fafc; }
    table.ces-table td {
      padding: 10px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
      color: #e2e8f0;
      font-variant-numeric: tabular-nums;
    }
    table.ces-table tr:hover td {
      background: rgba(255, 255, 255, 0.02);
    }
    .ces-bar-wrap {
      width: 100%;
      min-width: 90px;
      height: 5px;
      background: rgba(255, 255, 255, 0.08);
      border-radius: 3px;
      overflow: hidden;
      margin-top: 4px;
    }
    .ces-bar-fill {
      height: 100%;
      border-radius: 3px;
      background: linear-gradient(90deg, #e85d6f, #38bdf8);
    }

    /* Diagnostics Drawer */
    details.ces-drawer {
      border-top: 1px solid rgba(255, 255, 255, 0.06);
      padding-top: 8px;
    }
    summary.ces-drawer-summary {
      font-size: 0.76rem;
      font-weight: 600;
      color: #94a3b8;
      cursor: pointer;
      list-style: none;
      display: flex;
      align-items: center;
      gap: 6px;
      user-select: none;
    }
    summary.ces-drawer-summary::-webkit-details-marker { display: none; }
    summary.ces-drawer-summary:hover { color: #f8fafc; }
    .ces-drawer-content {
      margin-top: 10px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      font-size: 0.74rem;
      color: #94a3b8;
    }
    .ces-diag-item {
      background: rgba(13, 11, 15, 0.6);
      padding: 7px 10px;
      border-radius: 6px;
      border: 1px solid rgba(255, 255, 255, 0.03);
    }
    .ces-diag-item b { color: #f8fafc; }
  `;
  document.head.appendChild(style);

  // App State
  const state = {
    timeframe: '24H',
    chartMode: 'trend',
    autoRefreshSec: 30,
    pollTimer: null,
    data: null,
    loading: false,
    errorMessage: '',
    reqSequence: 0,
    sortCol: 'prompt_tokens',
    sortAsc: false,
  };

  // Helpers
  function formatTokens(val) {
    if (val === null || val === undefined || isNaN(val)) return '0';
    if (val >= 1e9) return (val / 1e9).toFixed(2) + ' B';
    if (val >= 1e6) return (val / 1e6).toFixed(2) + ' M';
    if (val >= 1e3) return (val / 1e3).toFixed(1) + ' K';
    return Number(val).toLocaleString();
  }

  function formatUsd(val) {
    if (val === null || val === undefined || isNaN(val)) return '$0.00';
    return '$' + Number(val).toFixed(2);
  }

  // Render Skeleton Layout
  root.innerHTML = `
    <div class="ces-container">
      <div class="ces-error-banner" id="errorBanner">
        <span id="errorText">Failed to load live metrics</span>
        <button class="ces-btn" id="retryBtn" style="padding:4px 10px; font-size:0.72rem;">Retry</button>
      </div>

      <div class="ces-card ces-header">
        <div class="ces-title-group">
          <div class="ces-pulse" id="livePulse"></div>
          <div>
            <div class="ces-title">Cache Efficiency Snapshot</div>
            <div class="ces-subtitle" id="timeRangeSub">Observed prompt caching metrics & monetary savings</div>
          </div>
        </div>
        <div class="ces-controls">
          <div class="ces-timeframe-group" id="tfGroup">
            <button class="ces-tf-btn" data-tf="1H">1H</button>
            <button class="ces-tf-btn" data-tf="6H">6H</button>
            <button class="ces-tf-btn active" data-tf="24H">24H</button>
            <button class="ces-tf-btn" data-tf="7D">7D</button>
            <button class="ces-tf-btn" data-tf="ALL">ALL</button>
          </div>
          <button class="ces-btn" id="refreshBtn">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
            Refresh
          </button>
        </div>
      </div>

      <div class="ces-kpi-grid">
        <div class="ces-card ces-kpi-card crimson">
          <div class="ces-kpi-header">
            <span class="ces-kpi-label">Cache Read Rate</span>
            <span class="ces-kpi-badge" id="rateBadge">Token-Weighted</span>
          </div>
          <div class="ces-kpi-val"><span id="kpiRate">--</span><span class="ces-kpi-unit">%</span></div>
          <div class="ces-kpi-sub" id="kpiHitRateSub">Call Hit Rate: --%</div>
        </div>

        <div class="ces-card ces-kpi-card cyan">
          <div class="ces-kpi-header">
            <span class="ces-kpi-label">Tokens Saved</span>
            <span class="ces-kpi-badge" id="tokensRatioBadge">--%</span>
          </div>
          <div class="ces-kpi-val"><span id="kpiSavedTokens">--</span></div>
          <div class="ces-kpi-sub" id="kpiCachedSub">Cached: -- | Total: --</div>
        </div>

        <div class="ces-card ces-kpi-card emerald">
          <div class="ces-kpi-header">
            <span class="ces-kpi-label">Est. Cost Saved</span>
            <span class="ces-kpi-badge" style="color:#34d399">Net Savings</span>
          </div>
          <div class="ces-kpi-val"><span id="kpiSavedCost">--</span></div>
          <div class="ces-kpi-sub" id="kpiCostSub">Net Spend: -- | Gross: --</div>
        </div>

        <div class="ces-card ces-kpi-card violet">
          <div class="ces-kpi-header">
            <span class="ces-kpi-label">Prompt Volume</span>
            <span class="ces-kpi-badge" id="callsBadge">-- calls</span>
          </div>
          <div class="ces-kpi-val"><span id="kpiVolume">--</span></div>
          <div class="ces-kpi-sub" id="kpiUncachedSub">Uncached: --</div>
        </div>
      </div>

      <div class="ces-card ces-chart-card">
        <div class="ces-chart-header">
          <div class="ces-chart-title" id="chartTitle">Cache-Read Rate Trend (%)</div>
          <div class="ces-chart-mode-group">
            <button class="ces-chart-mode-btn active" data-mode="trend">📈 Rate Trend</button>
            <button class="ces-chart-mode-btn" data-mode="volume">📊 Token Volume</button>
          </div>
        </div>
        <div class="ces-canvas-wrap" id="canvasWrap">
          <canvas id="chartCanvas"></canvas>
          <div class="ces-tooltip" id="chartTooltip"></div>
        </div>
      </div>

      <div class="ces-card">
        <div class="ces-chart-title" style="margin-bottom:10px;">Model Efficiency Breakdown</div>
        <div class="ces-table-wrap">
          <table class="ces-table" id="modelsTable">
            <thead>
              <tr>
                <th data-sort="display_name">Model</th>
                <th data-sort="calls">Settled Calls</th>
                <th data-sort="prompt_tokens">Prompt Tokens</th>
                <th data-sort="cached_tokens">Cached Tokens</th>
                <th data-sort="rate">Hit Rate (%)</th>
                <th data-sort="savings_usd">Net Savings ($)</th>
              </tr>
            </thead>
            <tbody id="modelsTbody">
              <tr><td colspan="6" style="text-align:center; color:#64748b;">Loading analytics data...</td></tr>
            </tbody>
          </table>
        </div>

        <details class="ces-drawer" style="margin-top:14px;">
          <summary class="ces-drawer-summary">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
            Data Quality & System Diagnostics
          </summary>
          <div class="ces-drawer-content" id="diagnosticsContent">
            <div class="ces-diag-item">Window Samples: <b id="diagWindow">--</b></div>
            <div class="ces-diag-item">Total Ledger Settled: <b id="diagTotal">--</b></div>
            <div class="ces-diag-item">Window Span: <b id="diagSpan">--</b></div>
            <div class="ces-diag-item">Cache Hit Samples: <b id="diagHits">--</b></div>
          </div>
        </details>
      </div>
    </div>
  `;

  // Canvas & Interaction Engine
  const canvas = document.getElementById('chartCanvas');
  const ctx = canvas.getContext('2d');
  const canvasWrap = document.getElementById('canvasWrap');
  const tooltip = document.getElementById('chartTooltip');

  let mouseX = -1;
  let isHovering = false;

  function resizeCanvas() {
    const rect = canvasWrap.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    if (typeof ctx.resetTransform === 'function') {
      ctx.resetTransform();
    } else {
      ctx.setTransform(1, 0, 0, 1, 0, 0);
    }
    ctx.scale(dpr, dpr);
    renderChart();
  }

  window.addEventListener('resize', resizeCanvas);

  canvasWrap.addEventListener('mousemove', (e) => {
    const rect = canvasWrap.getBoundingClientRect();
    mouseX = e.clientX - rect.left;
    isHovering = true;
    renderChart();
  });

  canvasWrap.addEventListener('mouseleave', () => {
    isHovering = false;
    mouseX = -1;
    tooltip.style.display = 'none';
    renderChart();
  });

  function renderChart() {
    const rect = canvasWrap.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;
    ctx.clearRect(0, 0, w, h);

    if (!state.data || !state.data.buckets || state.data.buckets.length === 0) {
      ctx.fillStyle = '#64748b';
      ctx.font = '12px system-ui';
      ctx.textAlign = 'center';
      ctx.fillText(state.errorMessage ? 'Data unavailable: ' + state.errorMessage : 'No activity records in this timeframe', w / 2, h / 2);
      return;
    }

    const buckets = state.data.buckets;
    const padding = { top: 20, right: 20, bottom: 26, left: 45 };
    const chartW = w - padding.left - padding.right;
    const chartH = h - padding.top - padding.bottom;

    if (state.chartMode === 'trend') {
      renderTrendChart(buckets, padding, chartW, chartH, w, h);
    } else {
      renderVolumeChart(buckets, padding, chartW, chartH, w, h);
    }
  }

  function renderTrendChart(buckets, pad, cw, ch, w, h) {
    const maxRate = 100;
    const n = buckets.length;
    const stepX = n > 1 ? cw / (n - 1) : cw;

    // Grid lines
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.06)';
    ctx.lineWidth = 1;
    ctx.fillStyle = '#64748b';
    ctx.font = '10px monospace';
    ctx.textAlign = 'right';

    for (let r = 0; r <= 100; r += 25) {
      const y = pad.top + ch - (r / maxRate) * ch;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + cw, y);
      ctx.stroke();
      ctx.fillText(r + '%', pad.left - 6, y + 3);
    }

    // Spline Points
    const points = buckets.map((b, i) => {
      const x = pad.left + (n > 1 ? i * stepX : cw / 2);
      const y = pad.top + ch - (Math.min(100, Math.max(0, b.rate)) / maxRate) * ch;
      return { x, y, data: b };
    });

    if (points.length > 0) {
      // Draw Gradient Fill
      const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + ch);
      grad.addColorStop(0, 'rgba(232, 93, 111, 0.35)');
      grad.addColorStop(0.7, 'rgba(56, 189, 248, 0.12)');
      grad.addColorStop(1, 'rgba(13, 11, 15, 0.0)');

      ctx.beginPath();
      ctx.moveTo(points[0].x, pad.top + ch);
      for (let i = 0; i < points.length; i++) {
        if (i === 0) {
          ctx.lineTo(points[i].x, points[i].y);
        } else {
          const xc = (points[i].x + points[i - 1].x) / 2;
          const yc = (points[i].y + points[i - 1].y) / 2;
          ctx.quadraticCurveTo(points[i - 1].x, points[i - 1].y, xc, yc);
        }
      }
      ctx.lineTo(points[points.length - 1].x, points[points.length - 1].y);
      ctx.lineTo(points[points.length - 1].x, pad.top + ch);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      // Draw Stroke Line
      ctx.beginPath();
      for (let i = 0; i < points.length; i++) {
        if (i === 0) {
          ctx.moveTo(points[i].x, points[i].y);
        } else {
          const xc = (points[i].x + points[i - 1].x) / 2;
          const yc = (points[i].y + points[i - 1].y) / 2;
          ctx.quadraticCurveTo(points[i - 1].x, points[i - 1].y, xc, yc);
        }
      }
      ctx.lineTo(points[points.length - 1].x, points[points.length - 1].y);
      ctx.strokeStyle = '#e85d6f';
      ctx.lineWidth = 2.5;
      ctx.stroke();

      // Draw Dots
      for (let p of points) {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
        ctx.fillStyle = '#0d0b0f';
        ctx.fill();
        ctx.strokeStyle = '#38bdf8';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
    }

    // X-Axis Labels
    ctx.fillStyle = '#94a3b8';
    ctx.textAlign = 'center';
    const labelStep = Math.max(1, Math.floor(n / 6));
    for (let i = 0; i < n; i += labelStep) {
      ctx.fillText(buckets[i].label, points[i].x, h - 8);
    }

    // Hover Crosshair
    if (isHovering && points.length > 0) {
      let closest = points[0];
      let minDist = Math.abs(mouseX - points[0].x);
      for (let p of points) {
        const d = Math.abs(mouseX - p.x);
        if (d < minDist) {
          minDist = d;
          closest = p;
        }
      }

      ctx.strokeStyle = 'rgba(240, 122, 134, 0.45)';
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(closest.x, pad.top);
      ctx.lineTo(closest.x, pad.top + ch);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.beginPath();
      ctx.arc(closest.x, closest.y, 5, 0, Math.PI * 2);
      ctx.fillStyle = '#e85d6f';
      ctx.fill();
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = 2;
      ctx.stroke();

      // Tooltip content
      const d = closest.data;
      tooltip.innerHTML = `
        <div style="font-weight:700; color:#f8fafc; margin-bottom:4px;">${d.label}</div>
        <div style="color:#e85d6f">Cache Rate: <b>${d.rate}%</b></div>
        <div style="color:#38bdf8">Cached: <b>${formatTokens(d.cached)}</b></div>
        <div style="color:#94a3b8">Uncached: <b>${formatTokens(d.uncached)}</b></div>
        <div style="color:#34d399">Saved: <b>${formatUsd(d.savings_usd)}</b></div>
      `;
      tooltip.style.left = closest.x + 'px';
      tooltip.style.top = Math.max(pad.top + 20, closest.y - 10) + 'px';
      tooltip.style.display = 'block';
    }
  }

  function renderVolumeChart(buckets, pad, cw, ch, w, h) {
    const maxVol = Math.max(...buckets.map(b => b.prompt), 1);
    const n = buckets.length;
    const barWidth = Math.max(3, (cw / n) * 0.65);
    const stepX = cw / n;

    // Grid lines
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.06)';
    ctx.lineWidth = 1;
    ctx.fillStyle = '#64748b';
    ctx.font = '10px monospace';
    ctx.textAlign = 'right';

    for (let r = 0; r <= 4; r++) {
      const v = (maxVol / 4) * r;
      const y = pad.top + ch - (r / 4) * ch;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + cw, y);
      ctx.stroke();
      ctx.fillText(formatTokens(v), pad.left - 6, y + 3);
    }

    // Stacked Bars
    const points = [];
    buckets.forEach((b, i) => {
      const x = pad.left + (i * stepX) + (stepX - barWidth) / 2;
      const totalH = (b.prompt / maxVol) * ch;
      const cachedH = (b.cached / maxVol) * ch;
      const uncachedH = Math.max(0, totalH - cachedH);

      const yBase = pad.top + ch;
      const yCached = yBase - cachedH;
      const yUncached = yCached - uncachedH;

      // Cached portion (Emerald)
      if (cachedH > 0) {
        ctx.fillStyle = '#34d399';
        ctx.fillRect(x, yCached, barWidth, cachedH);
      }
      // Uncached portion (Slate)
      if (uncachedH > 0) {
        ctx.fillStyle = '#64748b';
        ctx.fillRect(x, yUncached, barWidth, uncachedH);
      }

      points.push({ x: x + barWidth / 2, y: yUncached, data: b });
    });

    // X-Axis Labels
    ctx.fillStyle = '#94a3b8';
    ctx.textAlign = 'center';
    const labelStep = Math.max(1, Math.floor(n / 6));
    for (let i = 0; i < n; i += labelStep) {
      ctx.fillText(buckets[i].label, points[i].x, h - 8);
    }

    // Hover Tooltip
    if (isHovering && points.length > 0) {
      let closest = points[0];
      let minDist = Math.abs(mouseX - points[0].x);
      for (let p of points) {
        const d = Math.abs(mouseX - p.x);
        if (d < minDist) {
          minDist = d;
          closest = p;
        }
      }

      const d = closest.data;
      tooltip.innerHTML = `
        <div style="font-weight:700; color:#f8fafc; margin-bottom:4px;">${d.label}</div>
        <div style="color:#34d399">Cached: <b>${formatTokens(d.cached)}</b> (${d.rate}%)</div>
        <div style="color:#94a3b8">Uncached: <b>${formatTokens(d.uncached)}</b></div>
        <div style="color:#f8fafc">Total: <b>${formatTokens(d.prompt)}</b></div>
        <div style="color:#38bdf8">Saved: <b>${formatUsd(d.savings_usd)}</b></div>
      `;
      tooltip.style.left = closest.x + 'px';
      tooltip.style.top = Math.max(pad.top + 20, closest.y - 10) + 'px';
      tooltip.style.display = 'block';
    }
  }

  // Data Fetching
  async function fetchData() {
    const seq = ++state.reqSequence;
    state.loading = true;
    const btn = document.getElementById('refreshBtn');
    const pulse = document.getElementById('livePulse');
    const errorBanner = document.getElementById('errorBanner');
    const errorText = document.getElementById('errorText');

    if (btn) btn.classList.add('spinning');

    try {
      const resp = await window.fetch(`/api/extensions/cache_efficiency_snapshot/data?timeframe=${state.timeframe}`);
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
      }
      const json = await resp.json();

      if (seq === state.reqSequence) {
        state.data = json;
        state.errorMessage = '';
        if (errorBanner) errorBanner.style.display = 'none';
        if (pulse) pulse.classList.remove('error');
        updateUI();
      }
    } catch (err) {
      console.error('[CacheEfficiency] Fetch error:', err);
      if (seq === state.reqSequence) {
        state.errorMessage = err.message || 'Connection failed';
        if (errorBanner) {
          errorBanner.style.display = 'flex';
          if (errorText) errorText.textContent = `Error fetching data: ${state.errorMessage} (showing last known data)`;
        }
        if (pulse) pulse.classList.add('error');
      }
    } finally {
      if (seq === state.reqSequence) {
        state.loading = false;
        if (btn) btn.classList.remove('spinning');
      }
    }
  }

  function updateUI() {
    if (!state.data || !state.data.summary) return;
    const s = state.data.summary;
    const q = state.data.quality || {};
    const errorBanner = document.getElementById('errorBanner');
    const errorText = document.getElementById('errorText');
    const pulse = document.getElementById('livePulse');

    if (state.data.status === 'degraded' || q.read_error) {
      if (errorBanner) {
        errorBanner.style.display = 'flex';
        if (errorText) errorText.textContent = `Warning: Ledger state degraded (${q.read_error || 'partial read'}). Data may be incomplete.`;
      }
      if (pulse) pulse.classList.add('error');
    }

    // KPIs
    document.getElementById('kpiRate').textContent = s.cache_read_rate.toFixed(1);
    document.getElementById('kpiHitRateSub').textContent = `Call Hit Rate: ${s.call_hit_rate.toFixed(1)}%`;

    const tokenRatio = s.prompt_tokens > 0 ? ((s.cached_tokens / s.prompt_tokens) * 100).toFixed(1) : '0';
    document.getElementById('kpiSavedTokens').textContent = formatTokens(s.cached_tokens);
    document.getElementById('tokensRatioBadge').textContent = `${tokenRatio}% of prompt`;
    document.getElementById('kpiCachedSub').textContent = `Cached: ${formatTokens(s.cached_tokens)} | Total: ${formatTokens(s.prompt_tokens)}`;

    document.getElementById('kpiSavedCost').textContent = formatUsd(s.net_savings_usd);
    const estNotice = s.has_estimated_pricing ? ' (incl. est. models)' : '';
    document.getElementById('kpiCostSub').textContent = `Net: ${formatUsd(s.net_cost_usd)} | Base: ${formatUsd(s.gross_cost_usd)}${estNotice}`;

    document.getElementById('kpiVolume').textContent = formatTokens(s.prompt_tokens);
    document.getElementById('callsBadge').textContent = `${s.total_calls.toLocaleString()} calls`;
    document.getElementById('kpiUncachedSub').textContent = `Uncached: ${formatTokens(s.uncached_tokens)}`;

    // Diagnostics
    document.getElementById('diagWindow').textContent = `${q.window_records || 0} calls`;
    document.getElementById('diagTotal').textContent = `${q.settled_records_total || 0} records`;
    document.getElementById('diagHits').textContent = `${q.cache_hits || 0} hits`;
    if (q.oldest_ts && q.newest_ts) {
      document.getElementById('diagSpan').textContent = `${q.oldest_ts.slice(0,10)} → ${q.newest_ts.slice(0,10)}`;
    }

    renderTable();
    renderChart();
  }

  function renderTable() {
    const tbody = document.getElementById('modelsTbody');
    if (!tbody || !state.data || !state.data.models) return;

    const models = [...state.data.models];
    models.sort((a, b) => {
      let va = a[state.sortCol];
      let vb = b[state.sortCol];
      if (typeof va === 'string') return state.sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
      return state.sortAsc ? (va - vb) : (vb - va);
    });

    if (models.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:#64748b;">No model activity in this timeframe</td></tr>';
      return;
    }

    tbody.innerHTML = models.map(m => `
      <tr>
        <td style="font-weight:600; color:#f8fafc;">
          ${m.display_name} ${m.pricing_estimated ? '<span style="font-size:0.65rem; color:#94a3b8; background:rgba(255,255,255,0.08); padding:1px 5px; border-radius:4px; font-weight:normal; margin-left:4px;">≈ est</span>' : ''}
          <div class="ces-bar-wrap"><div class="ces-bar-fill" style="width:${Math.min(100, Math.max(0, m.rate))}%"></div></div>
        </td>
        <td>${m.calls.toLocaleString()}</td>
        <td>${formatTokens(m.prompt_tokens)}</td>
        <td style="color:#38bdf8">${formatTokens(m.cached_tokens)}</td>
        <td><b style="color:#e85d6f">${m.rate}%</b></td>
        <td style="color:#34d399; font-weight:700;">${formatUsd(m.savings_usd)}</td>
      </tr>
    `).join('');
  }

  // Event Listeners
  document.getElementById('tfGroup').addEventListener('click', (e) => {
    if (e.target.dataset.tf) {
      document.querySelectorAll('#tfGroup .ces-tf-btn').forEach(b => b.classList.remove('active'));
      e.target.classList.add('active');
      state.timeframe = e.target.dataset.tf;
      fetchData();
    }
  });

  document.getElementById('refreshBtn').addEventListener('click', () => {
    fetchData();
  });

  const retryBtn = document.getElementById('retryBtn');
  if (retryBtn) {
    retryBtn.addEventListener('click', () => {
      fetchData();
    });
  }

  document.querySelector('.ces-chart-mode-group').addEventListener('click', (e) => {
    if (e.target.dataset.mode) {
      document.querySelectorAll('.ces-chart-mode-btn').forEach(b => b.classList.remove('active'));
      e.target.classList.add('active');
      state.chartMode = e.target.dataset.mode;
      document.getElementById('chartTitle').textContent = state.chartMode === 'trend' ? 'Cache-Read Rate Trend (%)' : 'Token Volume Composition';
      renderChart();
    }
  });

  document.querySelectorAll('table.ces-table th').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.sort;
      if (state.sortCol === col) {
        state.sortAsc = !state.sortAsc;
      } else {
        state.sortCol = col;
        state.sortAsc = false;
      }
      renderTable();
    });
  });

  // Polling Lifecycle
  function startPolling() {
    stopPolling();
    if (state.autoRefreshSec > 0) {
      state.pollTimer = setInterval(fetchData, state.autoRefreshSec * 1000);
    }
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  // Widget Disposal Hook
  window.__ouroWidgetOnDispose = function () {
    stopPolling();
    window.removeEventListener('resize', resizeCanvas);
  };

  // Initial Load
  resizeCanvas();
  fetchData();
  startPolling();
})();
