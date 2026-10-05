"use strict";

// Everything from the API is rendered with textContent, never innerHTML.

const REFRESH_MS = 5000;
// A hidden tab still checks for new alerts, just less often and without the
// log and chart.
const HIDDEN_REFRESH_MS = 30000;
const LOG_LINES = 200;
const HISTORY_ROWS = 50;
const CHART_BARS_SHOWN = 120;

const $ = (id) => document.getElementById(id);

const state = {
  config: null,
  digits: 2,
  ticker: "",
  health: "no_data",
  close: null,
  ageBase: null,     // age_seconds from the last status response...
  ageAt: 0,          // ...and when it arrived, so the age can tick locally
  alertSentAt: null,
  barsFor: undefined,
  alertKey: undefined,
  alerts: [],
  bars: [],
  logLines: [],
  lastRefresh: 0,
  refreshing: false,
};

// ---- helpers ----

// Per-viewer preferences only. Storage can be unavailable (private mode).
const prefs = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* memory only */ } },
};

function setText(id, value) {
  $(id).textContent = value === null || value === undefined || value === "" ? "–" : String(value);
}

function digitsFor(price) {
  const p = Math.abs(price);
  return p >= 1000 ? 2 : p >= 50 ? 3 : 5;
}

function num(value, digits = state.digits) {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })
    : null;
}

function ago(seconds) {
  if (typeof seconds !== "number") return null;
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function parseTime(text) {
  if (typeof text !== "string") return null;
  const d = new Date(text.replace(" ", "T"));
  return Number.isNaN(d.getTime()) ? null : d;
}

function fmtDate(d, withYear = false) {
  if (!d) return null;
  return d.toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    ...(withYear ? { year: "numeric" } : {}),
  });
}

const fmtTime = (text) => fmtDate(parseTime(text));

function intervalSeconds(interval) {
  const m = /^(\d+)\s*([mhd])$/i.exec(String(interval || ""));
  if (!m) return 60;
  return Number(m[1]) * { m: 60, h: 3600, d: 86400 }[m[2].toLowerCase()];
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function getJSON(path) {
  const resp = await fetch(path, { cache: "no-store" });
  if (!resp.ok) throw new Error(`${path}: HTTP ${resp.status}`);
  return resp.json();
}

let toastTimer = 0;
function toast(message, kind = "") {
  const t = $("toast");
  t.textContent = message;
  t.className = `toast ${kind}`;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
}

function showBanner(message) {
  const banner = $("banner");
  banner.hidden = !message;
  banner.textContent = message || "";
}

// ---- status ----

function renderStatus(data) {
  state.health = data.health;
  const health = $("health");
  health.dataset.health = data.health;
  health.textContent = data.health.replace("_", " ");

  const s = data.status || {};
  if (s.ticker) {
    $("subtitle").textContent = `${s.interval} bars · polls every ${s.poll_seconds}s`;
    state.ticker = s.ticker;
  } else if (data.message) {
    $("subtitle").textContent = data.message;
  }
  if (typeof s.close === "number") {
    state.digits = digitsFor(s.close);
    state.close = s.close;
  }
  state.ageBase = data.age_seconds;
  state.ageAt = Date.now();

  setText("hero_close", num(s.close));
  const chip = $("hero_stack");
  chip.dataset.stack = s.stack || "";
  chip.textContent = s.stack ? `${s.stack} stack` : "no stack";

  setText("close", num(s.close));
  setText("ema_fast", num(s.ema_fast));
  setText("ema_mid", num(s.ema_mid));
  setText("ema_slow", num(s.ema_slow));
  setText("atr", num(s.atr));
  setText("last_bar_time", fmtTime(s.last_bar_time));

  const result = $("result");
  result.dataset.result = s.result || "";
  result.textContent = s.result ? s.result.replace("_", " ") : "–";
  setText("bars_loaded", s.bars_loaded);
  setText("consecutive_errors", s.consecutive_errors);
  setText("last_error", s.last_error);

  renderSetup(s, data.status !== null);
  tickClock();
  updateTitle();
}

function setStep(list, step, stateName, label) {
  const li = list.querySelector(`[data-step="${step}"]`);
  li.dataset.state = stateName;
  if (label) li.textContent = label;
  li.title = stateName ? `${li.textContent}: ${stateName}` : li.textContent;
}

function renderSetup(s, haveData) {
  const notes = [];
  for (const side of ["long", "short"]) {
    const list = $(`setup_${side}`);
    const want = side === "long" ? "bull" : "bear";
    const depth = s[`${side}_depth`];
    const vetoed = Boolean(s[`${side}_vetoed`]);
    const stackOk = s.stack === want;
    const pullOk = stackOk && typeof depth === "number" && depth > 0 && !vetoed;

    setStep(list, "stack", stackOk ? "done" : "");
    setStep(list, "pullback", vetoed ? "vetoed" : pullOk ? "done" : "",
      vetoed ? "Vetoed" : pullOk ? `Pullback d${depth}` : "Pullback");
    setStep(list, "trigger", pullOk ? "armed" : "");
    if (pullOk) {
      notes.push(`${side === "long" ? "Long" : "Short"} is armed: a confirmed ` +
        `${side === "long" ? "green" : "red"} fractal fires a ${side === "long" ? "BUY" : "SELL"}.`);
    }
  }
  $("setup_note").textContent = !haveData
    ? "Waiting for the bot's first poll."
    : notes.length ? notes.join(" ") : "Nothing armed: waiting for a stacked trend to pull back.";
}

// Ages tick between refreshes, so "12s ago" does not freeze for 5 seconds.
function tickClock() {
  if (typeof state.ageBase === "number") {
    const age = state.ageBase + (Date.now() - state.ageAt) / 1000;
    setText("poll_age", ago(age));
    $("hero_age").textContent = `polled ${ago(age)}`;
  } else {
    setText("poll_age", null);
    $("hero_age").textContent = "no polls yet";
  }
  if (state.alertSentAt) {
    $("last_signal_age").textContent = ago((Date.now() - state.alertSentAt) / 1000) || "";
  }
}

// ---- title flash and chime for new alerts ----

let flashTimer = 0;
let flashText = "";

function baseTitle() {
  const parts = [state.ticker || "Signal bot"];
  if (typeof state.close === "number") parts.push(num(state.close));
  if (state.health !== "ok") parts.push(`(${state.health.replace("_", " ")})`);
  return parts.join(" ");
}

// Refreshes call this every few seconds; a running flash owns the title.
function updateTitle() {
  if (!flashTimer) document.title = baseTitle();
}

function stopFlash() {
  clearInterval(flashTimer);
  flashTimer = 0;
  updateTitle();
}

function startFlash(text) {
  flashText = text;
  clearInterval(flashTimer);
  let on = false;
  let ticks = 0;
  flashTimer = setInterval(() => {
    on = !on;
    document.title = on ? flashText : baseTitle();
    // A tab the user is already looking at only needs a brief flash.
    if (document.hasFocus() && ++ticks > 8) stopFlash();
  }, 1000);
}

let audio = null;

function chime() {
  try {
    audio = audio || new AudioContext();
    if (audio.state === "suspended") audio.resume();
    const start = audio.currentTime;
    [[0, 880], [0.16, 1318.5]].forEach(([offset, freq]) => {
      const osc = audio.createOscillator();
      const gain = audio.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, start + offset);
      gain.gain.exponentialRampToValueAtTime(0.2, start + offset + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + offset + 0.35);
      osc.connect(gain).connect(audio.destination);
      osc.start(start + offset);
      osc.stop(start + offset + 0.4);
    });
  } catch { /* no audio: the title flash still runs */ }
}

function soundOn() { return prefs.get("dashboardSound") === "on"; }

function renderSoundToggle() {
  const b = $("sound_toggle");
  b.setAttribute("aria-pressed", String(soundOn()));
  b.textContent = soundOn() ? "Sound on" : "Sound off";
}

function notifyNewAlert(sig) {
  const what = [sig.tier, sig.signal].filter(Boolean).join(" ");
  startFlash(`\u{1F514} ${what} ${state.ticker}`);
  toast(`New alert: ${what}`, sig.signal === "SELL" ? "fail" : "ok");
  if (soundOn()) chime();
}

// ---- last alert and history ----

function alertKey(sig) {
  return sig ? `${sig.signal}|${sig.bar_time}` : null;
}

// The alert log carries the levels the state file does not.
function matchingAlert(sig) {
  if (!sig) return null;
  return state.alerts.find((a) => a.signal === sig.signal && a.bar_time === sig.bar_time) || null;
}

function renderLastSignal(data) {
  const sig = data.last_signal;
  const head = $("last_signal");
  const full = matchingAlert(sig);
  $("levels").hidden = !full;
  $("strength").hidden = !(full && typeof full.strength === "number");
  state.alertSentAt = null;
  $("last_signal_age").textContent = "";

  if (!sig) {
    head.className = "big muted";
    head.textContent = data.message || "–";
    $("last_signal_detail").textContent = "";
    $("lvl_reason").textContent = "";
    return;
  }
  head.className = `big ${sig.signal === "BUY" ? "buy" : sig.signal === "SELL" ? "sell" : ""}`;
  head.textContent = [sig.tier, sig.signal].filter(Boolean).join(" ");
  const detail = [];
  if (sig.depth !== null && sig.depth !== undefined) detail.push(`depth ${sig.depth}`);
  if (sig.bar_time) detail.push(`bar ${fmtTime(sig.bar_time)}`);
  $("last_signal_detail").textContent = detail.join(" · ");

  if (full) {
    setText("lvl_price", num(full.price));
    setText("lvl_sl", num(full.sl));
    setText("lvl_tp", num(full.tp));
    setText("lvl_rr", typeof full.rr === "number" ? `1:${full.rr.toFixed(2)}` : null);
    if (typeof full.strength === "number") {
      const pct = Math.round(Math.max(0, Math.min(1, full.strength)) * 100);
      $("strength_fill").style.width = `${pct}%`;
      $("strength_text").textContent = `${pct}%`;
    }
    $("lvl_reason").textContent = full.reason || "";
    const sent = parseTime(full.sent_utc);
    state.alertSentAt = sent ? sent.getTime() : null;
  } else {
    $("lvl_reason").textContent = "";
    const bar = parseTime(sig.bar_time);
    state.alertSentAt = bar ? bar.getTime() : null;
  }
  tickClock();
}

function renderHistory(data) {
  const body = $("history_rows");
  body.replaceChildren();
  const alerts = data.alerts || [];
  for (const a of alerts) {
    const tr = el("tr");
    tr.tabIndex = 0;
    tr.title = a.reason || "";
    const side = a.signal === "BUY" ? "buy" : a.signal === "SELL" ? "sell" : "";
    tr.append(
      el("td", "", fmtTime(a.bar_time) || "–"),
      el("td", side, [a.tier, a.signal].filter(Boolean).join(" ") || "–"),
      el("td", "num", num(a.price) || "–"),
      el("td", "num", num(a.sl) || "–"),
      el("td", "num", num(a.tp) || "–"),
      el("td", "num", typeof a.rr === "number" ? `1:${a.rr.toFixed(2)}` : "–"),
      el("td", "num", typeof a.strength === "number" ? `${Math.round(a.strength * 100)}%` : "–"),
    );
    tr.addEventListener("click", () => focusAlert(a));
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") focusAlert(a); });
    body.append(tr);
  }
  const empty = $("history_empty");
  empty.hidden = alerts.length > 0;
  empty.textContent = data.message || "";
  const note = [];
  if (alerts.length) note.push(`newest ${alerts.length} · click a row to find it on the chart`);
  if (data.skipped) note.push(`${data.skipped} unreadable line${data.skipped === 1 ? "" : "s"} skipped`);
  $("history_note").textContent = note.join(" · ");
}

async function syncAlerts(last) {
  const key = alertKey(last.last_signal);
  if (key === state.alertKey) {
    renderLastSignal(last);
    return;
  }
  try {
    const data = await getJSON(`/api/alerts?n=${HISTORY_ROWS}`);
    state.alerts = data.alerts || [];
    renderHistory(data);
    if (state.alertKey !== undefined && key) notifyNewAlert(last.last_signal);
    state.alertKey = key;
  } finally {
    // Even if the history failed, the state file's view of the last alert is current.
    renderLastSignal(last);
  }
  // Last, so a chart error cannot cost the notification above.
  redrawOverlays();
}

// ---- chart ----

const chart = {
  api: null, candles: null, fast: null, mid: null, slow: null,
  priceLines: [], byTs: new Map(), barSeconds: 60, framed: false,
};

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function chartMessage(text) {
  const p = $("chart_message");
  p.hidden = !text;
  p.textContent = text || "";
}

function initChart() {
  const LWC = window.LightweightCharts;
  if (!LWC) {
    chartMessage("Chart library failed to load.");
    $("chart").hidden = true;
    return;
  }
  chart.api = LWC.createChart($("chart"), {
    autoSize: true,
    layout: { attributionLogo: false, fontFamily: getComputedStyle(document.body).fontFamily },
    crosshair: { mode: LWC.CrosshairMode.Normal },
    rightPriceScale: { scaleMargins: { top: 0.1, bottom: 0.08 } },
    timeScale: {
      timeVisible: true, secondsVisible: false, rightOffset: 4,
      tickMarkFormatter: (t, type) => {
        const d = new Date(t * 1000);
        return type <= 2
          ? d.toLocaleDateString([], { month: "short", day: "numeric" })
          : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      },
    },
    localization: { timeFormatter: (t) => fmtDate(new Date(t * 1000), true) },
  });
  chart.candles = chart.api.addCandlestickSeries({ borderVisible: false });
  const line = { lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
  chart.fast = chart.api.addLineSeries(line);
  chart.mid = chart.api.addLineSeries(line);
  chart.slow = chart.api.addLineSeries(line);
  chart.api.subscribeCrosshairMove((param) => {
    renderLegend(param && param.time !== undefined ? chart.byTs.get(param.time) : null);
  });
  chartTheme();
}

function chartTheme() {
  if (!chart.api) return;
  const border = cssVar("--border");
  chart.api.applyOptions({
    layout: { background: { type: "solid", color: cssVar("--card") }, textColor: cssVar("--muted") },
    grid: { vertLines: { color: cssVar("--grid") }, horzLines: { color: cssVar("--grid") } },
    rightPriceScale: { borderColor: border },
    timeScale: { borderColor: border },
  });
  const up = cssVar("--ok");
  const down = cssVar("--bad");
  chart.candles.applyOptions({ upColor: up, downColor: down, wickUpColor: up, wickDownColor: down });
  chart.fast.applyOptions({ color: cssVar("--ema-fast") });
  chart.mid.applyOptions({ color: cssVar("--ema-mid") });
  chart.slow.applyOptions({ color: cssVar("--ema-slow") });
  redrawOverlays();
}

function renderLegend(bar) {
  const legend = $("chart_legend");
  legend.replaceChildren();
  bar = bar || state.bars[state.bars.length - 1];
  if (!bar) return;
  const c = state.config || {};
  const item = (label, value, cls) => {
    const span = el("span", cls, `${label} `);
    span.append(el("b", "", num(value) || "–"));
    legend.append(span, " ");
  };
  item("O", bar.open); item("H", bar.high); item("L", bar.low); item("C", bar.close);
  item(`EMA${c.ema_fast || ""}`, bar.ema_fast, "ef");
  item(`EMA${c.ema_mid || ""}`, bar.ema_mid, "em");
  item(`EMA${c.ema_slow || ""}`, bar.ema_slow, "es");
}

function setBars(data) {
  if (!chart.api) return;
  chart.barSeconds = intervalSeconds(data.interval);
  // The chart needs strictly ascending, unique times and complete candles.
  const bars = [];
  for (const b of data.bars || []) {
    if (![b.open, b.high, b.low, b.close].every(Number.isFinite)) continue;
    if (bars.length && b.ts <= bars[bars.length - 1].ts) continue;
    bars.push(b);
  }
  state.bars = bars;
  chart.byTs = new Map(bars.map((b) => [b.ts, b]));
  if (!bars.length) {
    chartMessage(data.message || "No bars yet.");
    return;
  }
  chartMessage(null);

  const digits = digitsFor(bars[bars.length - 1].close);
  chart.candles.applyOptions({ priceFormat: { type: "price", precision: digits, minMove: 10 ** -digits } });
  chart.candles.setData(bars.map((b) => ({ time: b.ts, open: b.open, high: b.high, low: b.low, close: b.close })));
  const lineData = (key) => bars.filter((b) => Number.isFinite(b[key])).map((b) => ({ time: b.ts, value: b[key] }));
  chart.fast.setData(lineData("ema_fast"));
  chart.mid.setData(lineData("ema_mid"));
  chart.slow.setData(lineData("ema_slow"));

  if (!chart.framed) {
    const n = bars.length;
    if (n > CHART_BARS_SHOWN) {
      chart.api.timeScale().setVisibleLogicalRange({ from: n - CHART_BARS_SHOWN, to: n + 4 });
    } else {
      chart.api.timeScale().fitContent();
    }
    chart.framed = true;
  }
  redrawOverlays();
  renderLegend(null);
}

function redrawOverlays() {
  applyMarkers();
  drawPriceLines();
}

function applyMarkers() {
  if (!chart.api) return;
  const up = cssVar("--ok");
  const down = cssVar("--bad");
  const markers = [];
  for (const b of state.bars) {
    if (b.green_arrow) markers.push({ time: b.ts, position: "belowBar", shape: "circle", color: up, size: 0.4 });
    if (b.red_arrow) markers.push({ time: b.ts, position: "aboveBar", shape: "circle", color: down, size: 0.4 });
  }
  for (const a of state.alerts) {
    if (!chart.byTs.has(a.ts)) continue;
    const buy = a.signal === "BUY";
    markers.push({
      time: a.ts, position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown",
      color: buy ? up : down, size: 1.6, text: [a.tier, a.signal].filter(Boolean).join(" "),
    });
  }
  markers.sort((x, y) => x.time - y.time);
  chart.candles.setMarkers(markers);
}

// Entry, stop and target of the newest alert, while its bar is on the chart.
function drawPriceLines() {
  if (!chart.api) return;
  for (const line of chart.priceLines) chart.candles.removePriceLine(line);
  chart.priceLines = [];
  const a = state.alerts[0];
  if (!a || !chart.byTs.has(a.ts)) return;
  const add = (price, color, title, lineStyle) => {
    if (typeof price !== "number") return;
    chart.priceLines.push(chart.candles.createPriceLine({
      price, color, title, lineStyle, lineWidth: 1, axisLabelVisible: true,
    }));
  };
  add(a.price, cssVar("--muted"), "Entry", 1);
  add(a.sl, cssVar("--bad"), "SL", 2);
  add(a.tp, cssVar("--ok"), "TP", 2);
}

function focusAlert(a) {
  if (!chart.api || !chart.byTs.has(a.ts)) {
    toast("That alert is older than the bars on the chart.");
    return;
  }
  const span = chart.barSeconds;
  chart.api.timeScale().setVisibleRange({ from: a.ts - 60 * span, to: a.ts + 20 * span });
  $("chart").scrollIntoView({ behavior: "smooth", block: "center" });
}

async function syncBars(status) {
  if (!chart.api) return;
  const key = (status.status && status.status.last_bar_time) || null;
  if (key === state.barsFor && state.bars.length) return;
  setBars(await getJSON("/api/bars"));
  // The bot writes the bars file and the status file separately, and a failed
  // bars write is retried on its next poll within the same bar. Until the
  // chart holds the status file's last bar, keep refetching rather than
  // marking it synced and freezing one bar behind.
  const want = parseTime(key);
  const have = state.bars[state.bars.length - 1];
  if (want && have && have.ts < want.getTime() / 1000) {
    chartMessage(`Chart is behind the bot: it ends at ${fmtDate(new Date(have.ts * 1000))}, ` +
      `the bot is on ${fmtDate(want)}. Retrying.`);
    return;
  }
  state.barsFor = key;
}

// ---- logs ----

function lineClass(line) {
  if (/\b(error|exception|traceback|failed)\b/i.test(line)) return "err";
  if (/\bsent \w+ (BUY|SELL)\b/.test(line)) return "sent";
  if (/\b(suppressed|skipped|refused|warning)\b/i.test(line)) return "warn";
  return "";
}

function drawLogs() {
  const pre = $("logs");
  const filter = $("log_filter").value.trim().toLowerCase();
  const lines = filter ? state.logLines.filter((l) => l.toLowerCase().includes(filter)) : state.logLines;
  const frag = document.createDocumentFragment();
  for (const line of lines) {
    frag.append(el("span", lineClass(line), line), "\n");
  }
  pre.replaceChildren(frag);
  if (!lines.length) pre.textContent = filter ? "(no matching lines)" : state.logMessage || "(empty)";
  if ($("log_follow").checked) pre.scrollTop = pre.scrollHeight;
}

function renderLogs(data) {
  state.logLines = data.lines || [];
  state.logMessage = data.message;
  $("log_source").textContent = data.source ? `· ${data.source}` : "";
  drawLogs();
}

// ---- config ----

function renderConfig(data) {
  const dl = $("config");
  dl.replaceChildren();
  if (!data.config) {
    dl.append(el("dd", "", data.message));
    return;
  }
  for (const [key, value] of Object.entries(data.config)) {
    dl.append(el("dt", "", key), el("dd", "", String(value)));
  }
  const c = data.config;
  state.config = c;
  if (c.ticker) {
    $("title").textContent = c.ticker;
    state.ticker = state.ticker || c.ticker;
  }
  for (const k of ["fast", "mid", "slow"]) {
    $(`ema_${k}_label`).textContent = `EMA ${c[`ema_${k}`]}`;
    $(`key_${k}`).textContent = `EMA ${c[`ema_${k}`]}`;
  }
  updateTitle();
  renderLegend(null);
}

// ---- refresh loop ----

// Each panel updates on its own, so one failing endpoint or a chart error
// cannot freeze the rest of the page.
async function refresh(force = false) {
  const hidden = document.hidden;
  if (state.refreshing) return;
  if (hidden && !force && Date.now() - state.lastRefresh < HIDDEN_REFRESH_MS) return;
  state.refreshing = true;
  state.lastRefresh = Date.now();
  const failed = [];
  const run = async (panel, fn) => {
    try {
      await fn();
    } catch (err) {
      console.error(`[dashboard] ${panel}:`, err);
      failed.push({ panel, err });
    }
  };
  try {
    const requests = [getJSON("/api/status"), getJSON("/api/last-signal")];
    if (!hidden) requests.push(getJSON(`/api/logs?n=${LOG_LINES}`));
    const results = await Promise.allSettled(requests);
    const take = (i) => {
      if (results[i].status === "rejected") throw results[i].reason;
      return results[i].value;
    };

    let status = null;
    await run("status", () => { status = take(0); renderStatus(status); });
    await run("alerts", () => syncAlerts(take(1)));
    if (!state.config) await run("config", async () => renderConfig(await getJSON("/api/config")));
    if (!hidden) {
      await run("log", () => renderLogs(take(2)));
      if (status) await run("chart", () => syncBars(status));
    }

    // fetch() rejects with a TypeError only when the server cannot be reached.
    const unreachable = results.every((r) => r.status === "rejected" && r.reason instanceof TypeError);
    if (unreachable) {
      showBanner(`Dashboard unreachable: ${results[0].reason.message}`);
    } else if (failed.length) {
      showBanner(`Not updating: ${failed.map((f) => `${f.panel} (${f.err.message})`).join("; ")}. ` +
        "Details in the browser console.");
    } else {
      showBanner(null);
    }
  } finally {
    state.refreshing = false;
  }
}

// ---- theme ----

const THEMES = ["auto", "light", "dark"];

function applyTheme(theme) {
  if (theme === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = theme;
  $("theme_toggle").textContent = theme[0].toUpperCase() + theme.slice(1);
  $("theme_toggle").title = `Colour theme: ${theme}`;
  chartTheme();
}

function currentTheme() {
  const t = prefs.get("dashboardTheme");
  return THEMES.includes(t) ? t : "auto";
}

// ---- quick commands ----

// Kept for this tab only. Storage can be unavailable (private mode), so the
// token then lives in memory until reload.
const TOKEN_KEY = "dashboardToken";
let memoryToken = "";

function getToken() {
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch { return memoryToken; }
}

function setToken(value) {
  memoryToken = value;
  try {
    if (value) sessionStorage.setItem(TOKEN_KEY, value);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* memory only */ }
  const tokenState = $("token_state");
  if (tokenState) tokenState.textContent = value ? "Token saved for this tab." : "Enter the token to run commands.";
}

function showOutput(text, ok) {
  const details = $("action_details");
  details.hidden = false;
  details.classList.toggle("fail", !ok);
  $("action_output").textContent = text;
}

async function runAction(button) {
  const name = button.dataset.action;
  const label = button.textContent.trim();
  if (!getToken()) {
    toast(`${label}: enter the action token first.`, "fail");
    $("token_input")?.focus();
    return;
  }
  if (button.dataset.confirm && !window.confirm(`${label}?`)) return;

  const buttons = document.querySelectorAll("button[data-action]");
  buttons.forEach((b) => { b.dataset.wasDisabled = b.disabled ? "1" : ""; b.disabled = true; });
  button.classList.add("busy");
  toast(`${label}: running…`);
  try {
    const resp = await fetch(`/api/action/${encodeURIComponent(name)}`, {
      method: "POST", headers: { "X-Token": getToken() },
    });
    let body;
    try { body = await resp.json(); } catch { body = { ok: false, output: `HTTP ${resp.status}` }; }
    if (resp.status === 401) setToken("");
    const took = typeof body.duration_ms === "number" ? ` (${body.duration_ms} ms)` : "";
    toast(`${label}: ${body.ok ? "done" : "failed"}${took}`, body.ok ? "ok" : "fail");
    showOutput(`${label}: ${body.ok ? "ok" : "failed"}${took}\n\n${body.output || ""}`, body.ok);
  } catch (err) {
    toast(`${label}: ${err.message}`, "fail");
    showOutput(`${label}: ${err.message}`, false);
  } finally {
    button.classList.remove("busy");
    buttons.forEach((b) => { b.disabled = b.dataset.wasDisabled === "1"; });
    refresh(true);
  }
}

// ---- wiring ----

document.addEventListener("DOMContentLoaded", () => {
  initChart();
  applyTheme(currentTheme());
  renderSoundToggle();

  $("theme_toggle").addEventListener("click", () => {
    const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
    prefs.set("dashboardTheme", next);
    applyTheme(next);
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", chartTheme);

  $("sound_toggle").addEventListener("click", () => {
    prefs.set("dashboardSound", soundOn() ? "off" : "on");
    renderSoundToggle();
    // This click is the user gesture browsers need before audio can play.
    if (soundOn()) chime();
  });

  $("chart_fit").addEventListener("click", () => chart.api?.timeScale().fitContent());
  $("chart_latest").addEventListener("click", () => chart.api?.timeScale().scrollToRealTime());

  $("log_filter").addEventListener("input", drawLogs);
  $("log_follow").addEventListener("change", drawLogs);
  $("logs").addEventListener("scroll", () => {
    const pre = $("logs");
    $("log_follow").checked = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 20;
  });

  const form = $("token_form");
  if (form) {
    setToken(getToken());
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      setToken($("token_input").value.trim());
      $("token_input").value = "";
    });
  }
  document.querySelectorAll("button[data-action]").forEach((b) => {
    b.addEventListener("click", () => runAction(b));
  });

  window.addEventListener("focus", stopFlash);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh(true);
  });

  refresh(true);
  setInterval(refresh, REFRESH_MS);
  setInterval(tickClock, 1000);
});
