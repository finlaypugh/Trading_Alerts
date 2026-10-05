"use strict";

// Everything from the API is rendered with textContent, never innerHTML.

const REFRESH_MS = 5000;
const $ = (id) => document.getElementById(id);

function setText(id, value) {
  $(id).textContent = value === null || value === undefined || value === "" ? "–" : String(value);
}

function num(value, digits = 2) {
  return typeof value === "number"
    ? value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })
    : null;
}

function ago(seconds) {
  if (typeof seconds !== "number") return null;
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  return `${(seconds / 3600).toFixed(1)}h ago`;
}

function pullback(depth, vetoed) {
  if (typeof depth !== "number") return null;
  const where = { 0: "none", 1: "depth 1 (fast EMA)", 2: "depth 2 (mid EMA)" }[depth] ?? `depth ${depth}`;
  return vetoed ? `${where}, vetoed` : where;
}

async function getJSON(path) {
  const resp = await fetch(path, { cache: "no-store" });
  if (!resp.ok) throw new Error(`${path}: HTTP ${resp.status}`);
  return resp.json();
}

function renderStatus(data) {
  const health = $("health");
  health.dataset.health = data.health;
  health.textContent = data.health.replace("_", " ");

  const s = data.status || {};
  if (s.ticker) $("subtitle").textContent = `${s.ticker} · ${s.interval} · polls every ${s.poll_seconds}s`;
  else if (data.message) $("subtitle").textContent = data.message;

  setText("close", num(s.close));
  const stack = $("stack");
  setText("stack", s.stack);
  stack.className = s.stack === "bull" || s.stack === "bear" ? s.stack : "";
  setText("ema_fast", num(s.ema_fast));
  setText("ema_mid", num(s.ema_mid));
  setText("ema_slow", num(s.ema_slow));
  setText("atr", num(s.atr));
  setText("long_pullback", pullback(s.long_depth, s.long_vetoed));
  setText("short_pullback", pullback(s.short_depth, s.short_vetoed));
  setText("last_bar_time", s.last_bar_time);

  setText("poll_age", ago(data.age_seconds));
  setText("result", s.result);
  setText("bars_loaded", s.bars_loaded);
  setText("consecutive_errors", s.consecutive_errors);
  setText("last_error", s.last_error);
}

function renderLastSignal(data) {
  const sig = data.last_signal;
  const el = $("last_signal");
  if (!sig) {
    el.className = "big muted";
    setText("last_signal", data.message);
    setText("last_signal_detail", "");
    return;
  }
  el.className = `big ${sig.signal === "BUY" ? "buy" : sig.signal === "SELL" ? "sell" : ""}`;
  setText("last_signal", [sig.tier, sig.signal].filter(Boolean).join(" "));
  const detail = [];
  if (sig.depth !== null && sig.depth !== undefined) detail.push(`depth ${sig.depth}`);
  if (sig.bar_time) detail.push(`bar ${sig.bar_time}`);
  $("last_signal_detail").textContent = detail.join(" · ");
}

function renderLogs(data) {
  const pre = $("logs");
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 20;
  pre.textContent = data.lines.length ? data.lines.join("\n") : data.message || "(empty)";
  $("log_source").textContent = data.source ? `· ${data.source}` : "";
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

function renderConfig(data) {
  const dl = $("config");
  dl.replaceChildren();
  if (!data.config) {
    const dd = document.createElement("dd");
    dd.textContent = data.message;
    dl.append(dd);
    return;
  }
  for (const [key, value] of Object.entries(data.config)) {
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = key;
    dd.textContent = String(value);
    dl.append(dt, dd);
  }
  const c = data.config;
  if (c.ticker) $("title").textContent = c.ticker;
  $("ema_fast_label").textContent = `EMA ${c.ema_fast}`;
  $("ema_mid_label").textContent = `EMA ${c.ema_mid}`;
  $("ema_slow_label").textContent = `EMA ${c.ema_slow}`;
}

function showBanner(message) {
  const banner = $("banner");
  banner.hidden = !message;
  banner.textContent = message || "";
}

async function refresh() {
  try {
    const [status, last, logs] = await Promise.all([
      getJSON("/api/status"), getJSON("/api/last-signal"), getJSON("/api/logs?n=100"),
    ]);
    renderStatus(status);
    renderLastSignal(last);
    renderLogs(logs);
    showBanner(null);
  } catch (err) {
    showBanner(`Dashboard unreachable: ${err.message}`);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  getJSON("/api/config").then(renderConfig).catch(() => {});
  refresh();
  setInterval(refresh, REFRESH_MS);
});
