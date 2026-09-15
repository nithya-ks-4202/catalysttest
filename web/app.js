/* Camera + SD card dashboard front-end.
 *
 * No framework and no build step - it polls /api/fleet and redraws. Charts are
 * hand-built SVG so the whole thing stays a single static file the Python
 * server can hand out.
 */
'use strict';

const SEVERITY = {
  good:     { label: 'Healthy',         icon: '●', color: 'var(--status-good)',
              track: 'var(--track-good)',     rank: 0 },
  unknown:  { label: 'Unknown',         icon: '?', color: 'var(--status-unknown)',
              track: 'var(--track-unknown)',  rank: 1 },
  warning:  { label: 'Warning',         icon: '▲', color: 'var(--status-warning)',
              track: 'var(--track-warning)',  rank: 2 },
  serious:  { label: 'Degraded',        icon: '◆', color: 'var(--status-serious)',
              track: 'var(--track-serious)',  rank: 3 },
  critical: { label: 'Critical',        icon: '■', color: 'var(--status-critical)',
              track: 'var(--track-critical)', rank: 4 },
};
const SEVERITY_ORDER = ['critical', 'serious', 'warning', 'unknown', 'good'];

const state = {
  data: null,
  status: 'all',
  view: 'cards',
  search: '',
  hours: 24,
  timer: null,
  failures: 0,
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || !isFinite(bytes)) return '–';
  if (bytes === 0) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let value = bytes;
  let i = 0;
  while (Math.abs(value) >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return i === 0 ? `${Math.round(value)} B` : `${value.toFixed(1)} ${units[i]}`;
}

function formatPercent(value, digits = 0) {
  if (value === null || value === undefined || !isFinite(value)) return '–';
  return `${value.toFixed(digits)}%`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return '–';
  seconds = Math.max(0, Math.round(seconds));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(hours / 24)} days`;
}

function formatDays(days) {
  if (days === null || days === undefined) return '–';
  if (days < 1) return `${Math.round(days * 24)}h`;
  if (days < 10) return `${days.toFixed(1)} days`;
  return `${Math.round(days)} days`;
}

function formatClock(timestamp) {
  if (!timestamp) return 'never';
  return new Date(timestamp * 1000).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

function relativeTime(timestamp) {
  if (!timestamp) return 'never';
  const seconds = Math.max(0, Date.now() / 1000 - timestamp);
  if (seconds < 10) return 'just now';
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  return `${formatDuration(seconds)} ago`;
}

function severityOf(key) { return SEVERITY[key] || SEVERITY.unknown; }

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function svgEl(tag, attrs) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    node.setAttribute(key, value);
  }
  return node;
}

// ---------------------------------------------------------------------------
// Tooltip - enhances, never gates. Every value is also in the table view.
// ---------------------------------------------------------------------------

const tooltip = {
  node: null,
  show(html, x, y) {
    if (!this.node) this.node = $('tooltip');
    this.node.innerHTML = html;
    this.node.hidden = false;
    const rect = this.node.getBoundingClientRect();
    // Flip near the viewport edges so the tip never runs off screen.
    let left = x + 14;
    let top = y - rect.height - 12;
    if (left + rect.width > window.innerWidth - 8) left = x - rect.width - 14;
    if (top < 8) top = y + 18;
    this.node.style.left = `${Math.max(8, left)}px`;
    this.node.style.top = `${top}px`;
  },
  hide() {
    if (!this.node) this.node = $('tooltip');
    this.node.hidden = true;
  },
};

function attachTooltip(node, builder) {
  const show = (event) => tooltip.show(builder(), event.clientX, event.clientY);
  node.addEventListener('mouseenter', show);
  node.addEventListener('mousemove', show);
  node.addEventListener('mouseleave', () => tooltip.hide());
  // Keyboard focus shows the same content as hover.
  node.addEventListener('focus', () => {
    const rect = node.getBoundingClientRect();
    tooltip.show(builder(), rect.left + rect.width / 2, rect.top);
  });
  node.addEventListener('blur', () => tooltip.hide());
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function load({ quiet = false } = {}) {
  if (!quiet) document.body.classList.add('is-refreshing');
  try {
    const response = await fetch(`/api/fleet?hours=${state.hours}&spark=32`,
                                 { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    state.failures = 0;
    render();
    await loadTrend();
  } catch (error) {
    state.failures += 1;
    const status = $('poll-status');
    status.textContent = `Cannot reach the monitor (${error.message}). Retrying…`;
  } finally {
    document.body.classList.remove('is-refreshing');
  }
}

async function loadTrend() {
  const holder = $('trend-chart');
  try {
    const response = await fetch(`/api/history?hours=${state.hours}`,
                                 { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    drawTrend(holder, payload.points || []);
  } catch (error) {
    holder.replaceChildren(el('div', 'chart-empty', 'Trend unavailable.'));
  }
}

async function requestRefresh() {
  const button = $('refresh');
  button.classList.add('is-busy');
  button.disabled = true;
  try {
    await fetch('/api/refresh', { method: 'POST' });
    // Give the poll loop a moment to finish a cycle before re-reading.
    await new Promise((resolve) => setTimeout(resolve, 900));
    await load({ quiet: true });
  } catch (error) {
    /* the periodic reload will catch up */
  } finally {
    button.classList.remove('is-busy');
    button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function render() {
  const data = state.data;
  if (!data) return;
  renderStatusLine(data);
  renderOverview(data);
  renderStatusBar(data);

  const cameras = filterCameras(data.cameras || []);
  $('fleet-count').textContent =
    `${cameras.length} of ${(data.cameras || []).length} shown`;

  const showTable = state.view === 'table';
  $('camera-grid').hidden = showTable;
  $('table-view').hidden = !showTable;
  $('empty-state').hidden = cameras.length > 0;

  if (showTable) renderTable(cameras);
  else renderCards(cameras);
}

function renderStatusLine(data) {
  const parts = [];
  parts.push(`Last poll ${relativeTime(data.last_poll_at)}`);
  if (data.last_poll_duration) {
    parts.push(`${data.last_poll_duration.toFixed(1)}s to sweep the fleet`);
  }
  parts.push(`polling every ${formatDuration(data.poll_interval_seconds)}`);
  $('poll-status').textContent = parts.join(' · ');
}

function renderOverview(data) {
  const s = data.summary || {};
  const attention = s.needs_attention || 0;

  $('hero-value').textContent = String(attention);
  $('hero-value').style.color =
    attention === 0 ? 'var(--text-primary)' : severityOf(worstSeverity(data)).color;
  $('hero-detail').textContent = attention === 0
    ? `All ${s.cameras_total || 0} cameras healthy and recording`
    : `of ${s.cameras_total || 0} cameras · ${s.severity_counts?.critical || 0} critical`;

  $('tile-online').textContent = `${s.cameras_online || 0}/${s.cameras_total || 0}`;
  $('tile-online-meta').textContent = s.cameras_offline
    ? `${s.cameras_offline} not answering SNMP`
    : 'All cameras answering SNMP';

  $('tile-cards').textContent = `${s.cards_present || 0}/${s.cameras_total || 0}`;
  $('tile-cards-meta').textContent = s.cards_missing
    ? `${s.cards_missing} camera${s.cards_missing === 1 ? '' : 's'} with no card`
    : 'Every online camera has a card';

  $('tile-capacity').textContent = formatPercent(s.capacity_used_percent, 1);
  $('tile-capacity-meta').textContent =
    `${formatBytes(s.capacity_used_bytes)} of ${formatBytes(s.capacity_total_bytes)}`;

  $('tile-full').textContent = formatDays(s.soonest_full_days);
  $('tile-full-meta').textContent = s.soonest_full_days === null
    || s.soonest_full_days === undefined
    ? 'Not enough history to project'
    : 'Projected from recent fill rate';

  $('trend-sub').textContent =
    `Mean across all cameras with a card · last ${formatRangeLabel(state.hours)}`;
}

function worstSeverity(data) {
  const counts = data.summary?.severity_counts || {};
  for (const key of SEVERITY_ORDER) {
    if (counts[key]) return key;
  }
  return 'good';
}

function formatRangeLabel(hours) {
  if (hours <= 24) return `${hours} hours`;
  return `${Math.round(hours / 24)} days`;
}

/* Fleet status: a stacked bar of status colours. Segments are separated by a
   2px surface gap, labelled inline only when the count actually fits, and
   backed by a legend + the table view - so identity is never colour alone. */
function renderStatusBar(data) {
  const counts = data.summary?.severity_counts || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const bar = $('status-bar');
  const legend = $('status-legend');
  bar.replaceChildren();
  legend.replaceChildren();

  if (!total) {
    bar.appendChild(el('div', 'chart-empty', 'No cameras yet.'));
    return;
  }

  const present = SEVERITY_ORDER.filter((key) => counts[key] > 0);
  for (const key of present) {
    const meta = severityOf(key);
    const count = counts[key];
    const share = count / total;
    const segment = el('div', 'status-seg');
    segment.style.background = meta.color;
    segment.style.flex = `${share} 1 0`;
    segment.tabIndex = 0;
    segment.setAttribute('role', 'listitem');
    segment.setAttribute('aria-label',
      `${meta.label}: ${count} of ${total} cameras`);

    // Only label inside the segment when there is room - never clip text.
    if (share > 0.08) {
      const label = el('span', 'status-seg-label', String(count));
      // Ink chosen by fill luminance so the number always clears contrast.
      label.style.color = isLightFill(key) ? '#0b0b0b' : '#ffffff';
      segment.appendChild(label);
    }
    attachTooltip(segment, () =>
      `<div class="tooltip-title">${meta.icon} ${meta.label}</div>` +
      `<div class="tooltip-row">${count} of ${total} cameras ` +
      `(${(share * 100).toFixed(0)}%)</div>`);
    bar.appendChild(segment);
  }
  bar.setAttribute('role', 'list');

  for (const key of SEVERITY_ORDER) {
    const meta = severityOf(key);
    const count = counts[key] || 0;
    if (!count && key === 'unknown') continue;
    const item = el('li');
    const swatch = el('span', 'legend-swatch');
    swatch.style.background = meta.color;
    item.append(swatch, el('span', 'legend-count', String(count)),
                el('span', null, meta.label));
    legend.appendChild(item);
  }
}

/* Warning and yellow-ish fills need dark ink; the rest take white. */
function isLightFill(key) {
  return key === 'warning' || key === 'serious' || key === 'unknown';
}

function filterCameras(cameras) {
  const needle = state.search.trim().toLowerCase();
  return cameras.filter((camera) => {
    if (state.status === 'attention'
        && !['warning', 'serious', 'critical'].includes(camera.severity)) return false;
    if (state.status === 'critical' && camera.severity !== 'critical') return false;
    if (state.status === 'offline' && camera.reachable) return false;
    if (state.status === 'good' && camera.severity !== 'good') return false;
    if (!needle) return true;
    const haystack = [camera.name, camera.host, camera.site, camera.sd_label,
                      camera.sys_name, ...(camera.tags || [])]
      .filter(Boolean).join(' ').toLowerCase();
    return haystack.includes(needle);
  }).sort((a, b) => {
    // Worst first: the dashboard should lead with what needs a person.
    const delta = severityOf(b.severity).rank - severityOf(a.severity).rank;
    if (delta !== 0) return delta;
    return (b.sd_used_percent || 0) - (a.sd_used_percent || 0);
  });
}

function renderCards(cameras) {
  const grid = $('camera-grid');
  grid.replaceChildren();
  const thresholds = state.data.thresholds || {};
  for (const camera of cameras) {
    grid.appendChild(buildCameraCard(camera, thresholds));
  }
}

function buildCameraCard(camera, thresholds) {
  const meta = severityOf(camera.severity);
  const card = el('article', 'card camera-card');
  card.setAttribute('aria-label', `${camera.name}: ${meta.label}`);

  // --- head ---------------------------------------------------------------
  const head = el('div', 'camera-head');
  const title = el('div', 'camera-title');
  title.appendChild(el('div', 'camera-name', camera.name));
  const hostLine = [camera.host, camera.site].filter(Boolean).join(' · ');
  title.appendChild(el('div', 'camera-host', hostLine));
  head.appendChild(title);

  const pill = el('span', 'status-pill');
  const dot = el('span', 'status-dot');
  dot.style.background = meta.color;
  pill.append(dot, el('span', null, meta.label));
  pill.style.background = meta.track;
  head.appendChild(pill);
  card.appendChild(head);

  // --- capacity meter -----------------------------------------------------
  const meterBlock = el('div');
  const meterHead = el('div', 'meter-head');
  meterHead.appendChild(el('span', 'meter-label',
    camera.sd_present ? (camera.sd_label || 'SD card') : 'No SD card detected'));
  // The value is always visible beside the meter, so the fill colour never
  // has to carry the number on its own.
  meterHead.appendChild(el('span', 'meter-value',
    camera.sd_present ? formatPercent(camera.sd_used_percent, 1) : '–'));
  meterBlock.appendChild(meterHead);

  const meterSeverity = camera.sd_present
    ? capacitySeverity(camera.sd_used_percent, thresholds)
    : 'critical';
  const meterMeta = severityOf(meterSeverity);
  const meter = el('div', 'meter');
  meter.style.background = camera.sd_present ? meterMeta.track : 'var(--track-unknown)';
  meter.setAttribute('role', 'meter');
  meter.setAttribute('aria-valuemin', '0');
  meter.setAttribute('aria-valuemax', '100');
  meter.setAttribute('aria-valuenow',
    camera.sd_present ? String(Math.round(camera.sd_used_percent || 0)) : '0');
  meter.setAttribute('aria-label', `${camera.name} SD card used`);
  if (camera.sd_present && camera.sd_used_percent !== null) {
    const fill = el('div', 'meter-fill');
    fill.style.width = `${Math.max(1.5, Math.min(100, camera.sd_used_percent))}%`;
    fill.style.background = meterMeta.color;
    meter.appendChild(fill);
  }
  meterBlock.appendChild(meter);

  const foot = el('div', 'meter-foot');
  foot.appendChild(el('span', null, camera.sd_present
    ? `${formatBytes(camera.sd_free_bytes)} free`
    : 'Not recording'));
  foot.appendChild(el('span', null, camera.sd_present
    ? `${formatBytes(camera.sd_total_bytes)} card`
    : (camera.reachable ? 'Camera online' : 'Camera offline')));
  meterBlock.appendChild(foot);

  attachTooltip(meter, () => camera.sd_present
    ? `<div class="tooltip-title">${camera.sd_label || 'SD card'}</div>` +
      `<div class="tooltip-row">Used ${formatBytes(camera.sd_used_bytes)} ` +
      `(${formatPercent(camera.sd_used_percent, 1)})</div>` +
      `<div class="tooltip-row">Free ${formatBytes(camera.sd_free_bytes)}</div>` +
      `<div class="tooltip-row">Capacity ${formatBytes(camera.sd_total_bytes)}</div>` +
      `<div class="tooltip-row">Source: ${camera.sd_source || 'unknown'}</div>`
    : `<div class="tooltip-title">No SD card</div>` +
      `<div class="tooltip-row">This camera is not recording locally.</div>`);
  card.appendChild(meterBlock);

  // --- sparkline ----------------------------------------------------------
  const sparkRow = el('div', 'spark-row');
  const sparkHolder = el('div', 'spark-holder');
  const points = camera.sparkline || [];
  if (points.length >= 2) {
    drawSparkline(sparkHolder, points, camera);
    const first = points[0].v;
    const last = points[points.length - 1].v;
    const delta = last - first;
    const sign = delta >= 0 ? '+' : '−';
    sparkRow.append(sparkHolder,
      el('span', 'spark-caption', `${sign}${Math.abs(delta).toFixed(1)} pts`));
  } else {
    sparkHolder.appendChild(el('div', 'chart-empty', camera.sd_present
      ? 'Collecting history…'
      : 'No card — no usage history'));
    sparkRow.appendChild(sparkHolder);
  }
  card.appendChild(sparkRow);

  // --- issues -------------------------------------------------------------
  if (camera.issues && camera.issues.length) {
    const list = el('ul', 'issue-list');
    for (const issue of camera.issues) {
      const item = el('li');
      const icon = el('span', 'status-icon', meta.icon);
      icon.style.color = meta.color;
      icon.setAttribute('aria-hidden', 'true');
      item.append(icon, el('span', null, issue));
      list.appendChild(item);
    }
    card.appendChild(list);
  }

  // --- footer -------------------------------------------------------------
  const cardFoot = el('div', 'camera-foot');
  if (camera.days_until_full !== null && camera.days_until_full !== undefined) {
    cardFoot.appendChild(el('span', null, `Full in ~${formatDays(camera.days_until_full)}`));
  }
  if (camera.uptime_seconds !== null && camera.uptime_seconds !== undefined) {
    cardFoot.appendChild(el('span', null, `Up ${formatDuration(camera.uptime_seconds)}`));
  }
  if (camera.rtt_ms !== null && camera.rtt_ms !== undefined) {
    cardFoot.appendChild(el('span', null, `SNMP ${camera.rtt_ms.toFixed(0)} ms`));
  }
  if (camera.sd_health_percent !== null && camera.sd_health_percent !== undefined) {
    cardFoot.appendChild(el('span', null, `Card health ${camera.sd_health_percent}%`));
  }
  if (cardFoot.childElementCount) card.appendChild(cardFoot);

  return card;
}

function capacitySeverity(percent, thresholds) {
  if (percent === null || percent === undefined) return 'unknown';
  if (percent >= (thresholds.capacity_critical_percent ?? 90)) return 'critical';
  if (percent >= (thresholds.capacity_warning_percent ?? 75)) return 'warning';
  return 'good';
}

function renderTable(cameras) {
  const body = $('table-body');
  body.replaceChildren();
  for (const camera of cameras) {
    const meta = severityOf(camera.severity);
    const row = el('tr');

    const nameCell = el('td');
    nameCell.appendChild(el('div', null, camera.name));
    const sub = el('div', 'camera-host', camera.host);
    nameCell.appendChild(sub);
    row.appendChild(nameCell);

    const statusCell = el('td');
    const wrap = el('span', 'status-pill');
    const dot = el('span', 'status-dot');
    dot.style.background = meta.color;
    wrap.style.background = meta.track;
    wrap.append(dot, el('span', null, meta.label));
    statusCell.appendChild(wrap);
    row.appendChild(statusCell);

    row.appendChild(el('td', null,
      camera.sd_present ? (camera.sd_label || 'SD card') : 'None detected'));
    row.appendChild(el('td', 'num',
      camera.sd_present ? formatPercent(camera.sd_used_percent, 1) : '–'));
    row.appendChild(el('td', 'num',
      camera.sd_present ? formatBytes(camera.sd_free_bytes) : '–'));
    row.appendChild(el('td', 'num',
      camera.sd_present ? formatBytes(camera.sd_total_bytes) : '–'));
    row.appendChild(el('td', 'num', formatDays(camera.days_until_full)));
    row.appendChild(el('td', 'issues-cell',
      camera.issues && camera.issues.length ? camera.issues.join('; ') : 'None'));
    body.appendChild(row);
  }
}

// ---------------------------------------------------------------------------
// SVG charts
// ---------------------------------------------------------------------------

function drawSparkline(holder, points, camera) {
  const width = holder.clientWidth || 200;
  const height = 34;
  const pad = 5;
  const values = points.map((p) => p.v);
  // Anchor the scale to a window around the data so a card sitting flat at 40%
  // doesn't render as a dramatic zig-zag of rounding noise.
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (max - min < 4) { const mid = (max + min) / 2; min = mid - 2; max = mid + 2; }
  min = Math.max(0, min); max = Math.min(100, Math.max(max, min + 1));

  const x = (i) => pad + (i / (points.length - 1)) * (width - pad * 2);
  const y = (v) => height - pad - ((v - min) / (max - min)) * (height - pad * 2);

  const svg = svgEl('svg', {
    viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: 'none',
    role: 'img',
    'aria-label': `SD card usage trend, ${formatPercent(values[0], 1)} to ` +
                  `${formatPercent(values[values.length - 1], 1)}`,
  });

  const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ');
  svg.appendChild(svgEl('path', {
    class: 'spark-area',
    d: `${line} L${x(points.length - 1).toFixed(1)},${height} L${x(0).toFixed(1)},${height} Z`,
  }));
  svg.appendChild(svgEl('path', { class: 'spark-line', d: line }));
  // End-dot: >=8px with a 2px surface ring so it stays legible over the line.
  svg.appendChild(svgEl('circle', {
    class: 'spark-dot', cx: x(points.length - 1).toFixed(1),
    cy: y(values[values.length - 1]).toFixed(1), r: 4,
  }));

  holder.replaceChildren(svg);
  attachTooltip(holder, () =>
    `<div class="tooltip-title">${camera.name}</div>` +
    `<div class="tooltip-row">Now ${formatPercent(values[values.length - 1], 1)} used</div>` +
    `<div class="tooltip-row">${formatRangeLabel(state.hours)} ago ` +
    `${formatPercent(values[0], 1)}</div>` +
    `<div class="tooltip-row">Peak ${formatPercent(Math.max(...values), 1)}</div>`);
}

/* Fleet trend: one series, one y-axis, hairline solid gridlines, crosshair
   tooltip. A single series needs no legend - the card title names it. */
function drawTrend(holder, points) {
  holder.replaceChildren();
  if (!points || points.length < 2) {
    holder.appendChild(el('div', 'chart-empty',
      'Not enough history yet — the trend appears after a few poll cycles.'));
    return;
  }

  const width = holder.clientWidth || 480;
  const height = holder.clientHeight || 180;
  const margin = { top: 12, right: 14, bottom: 26, left: 38 };
  const plotW = Math.max(10, width - margin.left - margin.right);
  const plotH = Math.max(10, height - margin.top - margin.bottom);

  const values = points.map((p) => p.used_percent);
  const times = points.map((p) => p.timestamp);
  let min = Math.min(...values);
  let max = Math.max(...values);
  const padValue = Math.max(2, (max - min) * 0.25);
  min = Math.max(0, Math.floor((min - padValue) / 5) * 5);
  max = Math.min(100, Math.ceil((max + padValue) / 5) * 5);
  if (max <= min) max = min + 5;

  const t0 = times[0];
  const t1 = times[times.length - 1];
  const x = (t) => margin.left + ((t - t0) / Math.max(1, t1 - t0)) * plotW;
  const y = (v) => margin.top + plotH - ((v - min) / (max - min)) * plotH;

  const svg = svgEl('svg', {
    viewBox: `0 0 ${width} ${height}`, role: 'img',
    'aria-label': `Average SD card usage across the fleet over the last ` +
                  `${formatRangeLabel(state.hours)}`,
  });

  // Gridlines + y ticks: solid hairlines, clean round numbers.
  const tickCount = 4;
  for (let i = 0; i <= tickCount; i += 1) {
    const value = min + ((max - min) * i) / tickCount;
    const yy = y(value);
    svg.appendChild(svgEl('line', {
      class: 'grid-line', x1: margin.left, x2: margin.left + plotW, y1: yy, y2: yy,
    }));
    const label = svgEl('text', {
      class: 'axis-text', x: margin.left - 7, y: yy + 3.5, 'text-anchor': 'end',
    });
    label.textContent = `${Math.round(value)}%`;
    svg.appendChild(label);
  }

  svg.appendChild(svgEl('line', {
    class: 'axis-line', x1: margin.left, x2: margin.left + plotW,
    y1: margin.top + plotH, y2: margin.top + plotH,
  }));

  // X labels at the ends only - enough to orient, no collisions.
  const startLabel = svgEl('text', {
    class: 'axis-text', x: margin.left, y: height - 8, 'text-anchor': 'start',
  });
  startLabel.textContent = formatAxisTime(t0);
  const endLabel = svgEl('text', {
    class: 'axis-text', x: margin.left + plotW, y: height - 8, 'text-anchor': 'end',
  });
  endLabel.textContent = formatAxisTime(t1);
  svg.append(startLabel, endLabel);

  const path = points
    .map((p, i) => `${i ? 'L' : 'M'}${x(p.timestamp).toFixed(1)},${y(p.used_percent).toFixed(1)}`)
    .join(' ');
  svg.appendChild(svgEl('path', {
    class: 'trend-area',
    d: `${path} L${x(t1).toFixed(1)},${margin.top + plotH} ` +
       `L${x(t0).toFixed(1)},${margin.top + plotH} Z`,
  }));
  svg.appendChild(svgEl('path', { class: 'trend-line', d: path }));
  svg.appendChild(svgEl('circle', {
    class: 'trend-dot', cx: x(t1).toFixed(1),
    cy: y(values[values.length - 1]).toFixed(1), r: 4,
  }));

  // Direct label on the endpoint - the one value worth calling out.
  const endValue = svgEl('text', {
    class: 'axis-text', x: x(t1) - 8, y: y(values[values.length - 1]) - 9,
    'text-anchor': 'end',
  });
  endValue.setAttribute('fill', 'var(--text-primary)');
  endValue.setAttribute('font-weight', '600');
  endValue.textContent = formatPercent(values[values.length - 1], 1);
  svg.appendChild(endValue);

  // Crosshair layer: a nearest-point hit area across the whole plot, so the
  // hover target is the column, not the 8px dot.
  const crosshair = svgEl('line', {
    class: 'crosshair', y1: margin.top, y2: margin.top + plotH,
    x1: 0, x2: 0, opacity: 0,
  });
  const marker = svgEl('circle', { class: 'trend-dot', r: 4, opacity: 0 });
  svg.append(crosshair, marker);

  const capture = svgEl('rect', {
    x: margin.left, y: margin.top, width: plotW, height: plotH,
    fill: 'transparent', style: 'cursor: crosshair',
  });
  capture.addEventListener('mousemove', (event) => {
    const box = svg.getBoundingClientRect();
    const scale = width / box.width;
    const localX = (event.clientX - box.left) * scale;
    let best = 0;
    let bestDistance = Infinity;
    points.forEach((p, i) => {
      const distance = Math.abs(x(p.timestamp) - localX);
      if (distance < bestDistance) { bestDistance = distance; best = i; }
    });
    const point = points[best];
    const px = x(point.timestamp);
    const py = y(point.used_percent);
    crosshair.setAttribute('x1', px);
    crosshair.setAttribute('x2', px);
    crosshair.setAttribute('opacity', 1);
    marker.setAttribute('cx', px);
    marker.setAttribute('cy', py);
    marker.setAttribute('opacity', 1);
    tooltip.show(
      `<div class="tooltip-title">${formatPercent(point.used_percent, 1)} average</div>` +
      `<div class="tooltip-row">${new Date(point.timestamp * 1000).toLocaleString()}</div>` +
      `<div class="tooltip-row">${point.samples} sample${point.samples === 1 ? '' : 's'}</div>`,
      event.clientX, event.clientY);
  });
  capture.addEventListener('mouseleave', () => {
    crosshair.setAttribute('opacity', 0);
    marker.setAttribute('opacity', 0);
    tooltip.hide();
  });
  svg.appendChild(capture);

  holder.appendChild(svg);
}

function formatAxisTime(timestamp) {
  const date = new Date(timestamp * 1000);
  const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (state.hours <= 12) return time;
  // Past half a day the two ends can fall on different dates, and a bare
  // "05:10 PM … 04:55 PM" then reads as if time ran backwards.
  const day = date.toLocaleDateString([], { month: 'short', day: 'numeric' });
  if (state.hours <= 48) return `${day}, ${time}`;
  return day;
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function selectChip(group, attr, value) {
  for (const chip of group.querySelectorAll('.chip')) {
    const selected = chip.dataset[attr] === value;
    chip.classList.toggle('is-selected', selected);
    chip.setAttribute('aria-pressed', String(selected));
  }
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  try { localStorage.setItem('camwatch-theme', theme); } catch (e) { /* private mode */ }
}

function init() {
  try {
    const saved = localStorage.getItem('camwatch-theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
  } catch (e) { /* private mode */ }

  $('theme-toggle').addEventListener('click', () => {
    const current = document.documentElement.getAttribute('data-theme');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const isDark = current === 'dark' || (current !== 'light' && prefersDark);
    applyTheme(isDark ? 'light' : 'dark');
    if (state.data) { render(); loadTrend(); }
  });

  $('refresh').addEventListener('click', requestRefresh);

  let searchTimer = null;
  $('search').addEventListener('input', (event) => {
    state.search = event.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(render, 120);
  });

  const statusGroup = document.querySelector('.filter-chips');
  statusGroup.addEventListener('click', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    state.status = chip.dataset.status;
    selectChip(statusGroup, 'status', state.status);
    render();
  });

  const viewGroup = document.querySelector('.filter-view');
  viewGroup.addEventListener('click', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    state.view = chip.dataset.view;
    selectChip(viewGroup, 'view', state.view);
    render();
  });

  $('range').addEventListener('change', (event) => {
    state.hours = Number(event.target.value);
    load({ quiet: true });
  });

  // Redraw charts on resize - SVG geometry is computed from pixel widths.
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (state.data) { render(); loadTrend(); } }, 180);
  });

  load();
  // Re-read a little faster than the server polls, so the "last poll" line
  // stays honest without hammering the API.
  state.timer = setInterval(() => load({ quiet: true }), 10000);
}

document.addEventListener('DOMContentLoaded', init);
