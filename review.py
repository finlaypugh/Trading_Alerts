#!/usr/bin/env python3
"""
Weekly review of the bot's closed alerts: statistics computed here, read by
an LLM, written up as a markdown report. Advisory only.

    python review.py              # stats + LLM reading -> reports/YYYY-MM-DD.md
    python review.py --no-llm     # stats only, no API call
    python review.py --dry-run    # print the prompt, call nothing, write nothing

Nothing in this file changes the bot. It is never imported by signal_bot or
the dashboard, it reads the alert log read-only, and its suggestions are
printed as a diff for a person to backtest and apply, or not. The guardrails
live in code, not in the prompt:

  * below REVIEW_MIN_N closed alerts on the current config there is no API
    call at all, only an "insufficient data" report;
  * every number in the report is computed here; the model refers to buckets
    by id and its prose is labelled as its reading;
  * a proposed change survives only if every bucket it cites exists, has at
    least REVIEW_BUCKET_MIN_N alerts and is significantly negative after a
    Bonferroni correction across all buckets, and its setting is on a short
    allowlist that excludes the strategy's hard constraints;
  * whatever goes wrong, a report is still written and the exit code says so.

Needs ANTHROPIC_API_KEY (or another credential the Anthropic SDK resolves),
and `pip install -r requirements-review.txt`, for the LLM step only.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import alert_log
import signal_bot

ROOT = Path(__file__).resolve().parent
LEARNINGS = ROOT / "docs" / "LEARNINGS.md"


def _env(name, default, cast=str):
    try:
        return cast(os.environ.get(name, default))
    except ValueError:
        return cast(default)


MODEL = _env("REVIEW_MODEL", "claude-opus-5-5")
EFFORT = _env("REVIEW_EFFORT", "high")
WINDOW_DAYS = _env("REVIEW_WINDOW_DAYS", 28, int)
MAX_ALERTS = _env("REVIEW_MAX_ALERTS", 400, int)
MIN_N = _env("REVIEW_MIN_N", 100, int)
BUCKET_MIN_N = _env("REVIEW_BUCKET_MIN_N", 30, int)
ALPHA = _env("REVIEW_ALPHA", 0.05, float)
REPORTS_DIR = ROOT / _env("REVIEW_REPORTS_DIR", "reports")
POST_DISCORD = _env("REVIEW_POST_DISCORD", "false").lower() == "true"

# $ per million tokens (input, output), for the cost line in the report.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0)}

# The only settings a review may propose changing: .env key -> signal_bot
# attribute. Left out on purpose: the ticker, bar size and poll rate, and the
# hard constraints in docs/LEARNINGS.md (fractal lag, closed candles, session
# gaps).
ALLOWED_KEYS = {
    "SIGNAL_EMA_FAST": "EMA_FAST",
    "SIGNAL_EMA_MID": "EMA_MID",
    "SIGNAL_EMA_SLOW": "EMA_SLOW",
    "SIGNAL_FRACTAL_MAX_PLATEAU": "FRACTAL_MAX_PLATEAU",
    "SIGNAL_PULLBACK_EXPIRY_BARS": "PULLBACK_EXPIRY_BARS",
    "SIGNAL_REQUIRE_PULLBACK": "REQUIRE_PULLBACK",
    "SIGNAL_REQUIRE_PIVOT_IN_PULLBACK": "REQUIRE_PIVOT_IN_PULLBACK",
    "SIGNAL_MIN_STACK_BARS": "MIN_STACK_BARS",
    "SIGNAL_SHORT_MAX_DEPTH": "SHORT_MAX_DEPTH",
    "SIGNAL_RR": "RR",
    "SIGNAL_SL_BUFFER_ATR": "SL_BUFFER_ATR",
    "SIGNAL_MIN_STACK_SEP_ATR": "MIN_STACK_SEP_ATR",
    "SIGNAL_MAX_RISK_ATR": "MAX_RISK_ATR",
    "SIGNAL_ATR_LEN": "ATR_LEN",
    "SIGNAL_COOLDOWN_BARS": "COOLDOWN_BARS",
}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "loss_clusters", "negative_buckets",
                 "proposals", "hypotheses", "caveats"],
    "properties": {
        "verdict": {"type": "string", "enum": ["insufficient_data", "no_action", "proposals"]},
        "summary": {"type": "string"},
        "loss_clusters": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["description", "bucket_ids", "alert_ids"],
            "properties": {
                "description": {"type": "string"},
                "bucket_ids": {"type": "array", "items": {"type": "string"}},
                "alert_ids": {"type": "array", "items": {"type": "string"}},
            },
        }},
        "negative_buckets": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["bucket_id", "interpretation"],
            "properties": {
                "bucket_id": {"type": "string"},
                "interpretation": {"type": "string"},
            },
        }},
        "proposals": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["env_key", "proposed_value", "bucket_ids", "rationale",
                         "backtest_command"],
            "properties": {
                "env_key": {"type": "string"},
                "proposed_value": {"type": "string"},
                "bucket_ids": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string"},
                "backtest_command": {"type": "string"},
            },
        }},
        "hypotheses": {"type": "array", "items": {"type": "string"}},
        "caveats": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM_PROMPT = """\
You review alerts from a rules-based XAU_USD pullback strategy. You are advisory: a person \
reads your output and decides. Nothing you write is applied automatically.

Facts you must reason from:
- RR = {rr}. Breakeven win rate = 1/(1+RR) = {breakeven:.0%}, i.e. mean R = 0, before costs. \
Spread and slippage are not modelled, so real breakeven is higher.
- Every number you need is in STATS, computed by code. Do not compute or restate \
statistics; refer to buckets by bucket_id and to alerts by alert_id.
- {k} buckets were tested. At 95% about {chance:.1f} of them would look significant by \
chance alone. Only buckets with sig_negative=true (at least {bucket_min_n} alerts, \
Bonferroni-corrected z > {z}) may justify a proposal. Buckets with noise=true are too small \
to say anything about; do not build an argument on them.
- If nothing qualifies, say so: verdict "insufficient_data" or "no_action". Never guess, and \
never stretch a small or non-significant bucket into a recommendation.
- You may only propose values for these .env keys: {allowed}. Never propose changing the \
hard constraints in LEARNINGS. Ideas that would need code (a new filter, a new rule) go in \
"hypotheses", each with the backtest that would test it, not in "proposals".
- Each proposal's backtest_command must be a python backtest.py command a person can run to \
check it before changing anything.
"""


class ReviewError(Exception):
    """The LLM step failed in a way the report should name."""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def current_config():
    """Every allowed key's current value, as the bot itself reads it."""
    return {key: getattr(signal_bot, attr) for key, attr in ALLOWED_KEYS.items()}


def load_window(conn, config_hash, now, days=None, max_alerts=None):
    """
    (window, all_rows): closed live alerts under this config that exited in
    the last `days`, newest `max_alerts` of them, annotated; and every live
    alert under this config, for the data-quality counts.
    """
    days = WINDOW_DAYS if days is None else days
    max_alerts = MAX_ALERTS if max_alerts is None else max_alerts
    rows = alert_log.annotate(alert_log.load_rows(conn, "live", config_hash=config_hash))
    cutoff = (now - timedelta(days=days)).isoformat()
    closed = [a for a in rows if a["outcome"] in alert_log.CLOSED and a["r"] is not None
              and (a["exit_time"] or "") >= cutoff]
    return closed[-max_alerts:], rows


def data_quality(rows):
    def count(pred):
        return sum(1 for a in rows if pred(a))

    return {
        "alerts_logged": len(rows),
        "duplicate_sends": count(lambda a: (a.get("duplicate_sends") or 0) > 0),
        "exit_gapped": count(lambda a: a.get("exit_gapped") == 1),
        "entry_mismatch": count(lambda a: a.get("last_error") == "entry_mismatch"),
        "still_open": count(lambda a: a["outcome"] in alert_log.UNRESOLVED),
        "expired": count(lambda a: a["outcome"] == "expired"),
        "unresolvable": count(lambda a: a["outcome"] == "unresolvable"),
    }


def backtest_prior(conn, rr):
    source = alert_log.latest_backtest_source(conn)
    if source is None:
        return None
    rows = alert_log.annotate(alert_log.load_rows(conn, source))
    stats = alert_log.bucket_stats(rows, ["signal", "depth"], min_n=BUCKET_MIN_N,
                                   alpha=ALPHA, rr=rr)
    return {"source": source, **stats}


def build_review(conn, now, config_hash, rr):
    window, rows = load_window(conn, config_hash, now)
    stats = alert_log.bucket_stats(window, list(alert_log.DIMENSIONS),
                                   min_n=BUCKET_MIN_N, alpha=ALPHA, rr=rr)
    return {
        "now": now,
        "config_hash": config_hash,
        "window": window,
        "start": window[0]["bar_time"] if window else None,
        "end": window[-1]["exit_time"] if window else None,
        "stats": stats,
        "quality": data_quality(rows),
        "prior": backtest_prior(conn, rr),
    }


# ---------------------------------------------------------------------------
# Prompt and model
# ---------------------------------------------------------------------------

STAT_KEYS = ("bucket_id", "n", "win_rate", "mean_r", "se_r", "noise", "sig_negative")
ALERT_COLS = ("alert_id", "signal", "depth", "session", "bars_since_last_alert",
              "stack_bars", "strength", "prev_same_dir", "overlaps_open", "outcome", "r")


def _csv_cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.3g}"
    return str(v).replace(",", ";")


def _compact(bucket):
    return None if bucket is None else {k: bucket[k] for k in STAT_KEYS}


def build_prompt(review, learnings, config):
    stats = review["stats"]
    system = SYSTEM_PROMPT.format(
        rr=stats["rr"], breakeven=stats["breakeven_win_rate"], k=stats["k"],
        chance=stats["k"] * ALPHA, bucket_min_n=stats["min_n"], z=stats["z"],
        allowed=", ".join(ALLOWED_KEYS),
    )
    prior = review["prior"]
    prior_text = "none" if prior is None else json.dumps({
        "source": prior["source"], "n": prior["n"], "overall": _compact(prior["overall"]),
        "buckets": [_compact(b) for b in prior["buckets"]],
    })
    lines = [",".join(ALERT_COLS)] + [
        ",".join(_csv_cell(a.get(c)) for c in ALERT_COLS) for a in review["window"]
    ]
    user = (
        f"CONFIG (current strategy settings): {json.dumps(config)}\n\n"
        f"STATS (live alerts, config {review['config_hash']}, {review['start']} to "
        f"{review['end']}, n={stats['n']}):\n"
        f"overall: {json.dumps(_compact(stats['overall']))}\n"
        f"buckets: {json.dumps([_compact(b) for b in stats['buckets']])}\n\n"
        f"DATA QUALITY: {json.dumps(review['quality'])}\n\n"
        f"BACKTEST PRIOR (a different population, for comparison only): {prior_text}\n\n"
        "ALERTS (csv):\n" + "\n".join(lines) + "\n\n"
        f"LEARNINGS:\n{learnings}"
    )
    return system, user


def call_llm(system, user, client=None, model=None, effort=None):
    """
    One structured-output request. Returns (parsed JSON, usage). Raises
    ReviewError for a refusal, a truncated answer or JSON that does not fit
    the schema; API errors propagate as the SDK raises them.
    """
    if client is None:
        import anthropic  # only here: the bot and dashboard never need it
        client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=model or MODEL,
        max_tokens=16000,
        # On a policy decline, the API re-runs the request on a fallback
        # model in the same call. Sent raw so an older SDK cannot reject it.
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},
        output_config={"effort": effort or EFFORT,
                       "format": {"type": "json_schema", "schema": SCHEMA}},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if response.stop_reason == "refusal":
        raise ReviewError("the model declined the request")
    if response.stop_reason == "max_tokens":
        raise ReviewError("the answer was cut off at max_tokens")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ReviewError("the answer had no text block")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ReviewError(f"the answer was not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ReviewError("the answer was not a JSON object")
    missing = [k for k in SCHEMA["required"] if k not in data]
    if missing or data.get("verdict") not in SCHEMA["properties"]["verdict"]["enum"]:
        raise ReviewError(f"the answer did not fit the schema (missing {missing})")
    return data, getattr(response, "usage", None)


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

def _env_text(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def _parse_like(current, text):
    text = str(text).strip()
    if isinstance(current, bool):
        if text.lower() not in ("true", "false"):
            raise ValueError(text)
        return text.lower() == "true"
    if isinstance(current, int):
        return int(text)
    return float(text)


def check_proposals(proposals, stats, config):
    """
    (accepted, rejected): each accepted proposal with its parsed value and
    the buckets it rests on; each rejected one with the reason.
    """
    buckets = {b["bucket_id"]: b for b in stats["buckets"]}
    accepted, rejected = [], []
    for p in proposals or []:
        key, cited = p.get("env_key"), p.get("bucket_ids") or []
        unknown = [b for b in cited if b not in buckets]
        weak = [b for b in cited if b in buckets and not buckets[b]["sig_negative"]]
        reason = value = None
        if key not in ALLOWED_KEYS:
            reason = f"{key} is not a setting a review may change"
        elif not cited:
            reason = "cites no bucket"
        elif unknown:
            reason = f"cites unknown buckets: {', '.join(unknown)}"
        elif weak:
            reason = f"cites buckets that are not significantly negative: {', '.join(weak)}"
        else:
            try:
                value = _parse_like(config[key], p.get("proposed_value"))
            except (TypeError, ValueError):
                reason = (f"value {p.get('proposed_value')!r} does not parse as "
                          f"{type(config[key]).__name__}")
            else:
                if value == config[key]:
                    reason = "proposes the current value"
        if reason:
            rejected.append({**p, "reason": reason})
        else:
            accepted.append({**p, "value": value, "buckets": [buckets[b] for b in cited]})
    return accepted, rejected


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _pct(x):
    return "–" if x is None else f"{x * 100:.0f}%"


def _r(x):
    return "–" if x is None else f"{x:+.2f}R"


def _se(x):
    return "" if x is None else f" ± {x:.2f}"


def _bucket_line(b):
    flag = "noise" if b["noise"] else ("**losing**" if b["sig_negative"] else "")
    return (f"| `{b['bucket_id']}` | {b['n']} | {_pct(b['win_rate'])} ± "
            f"{b['win_rate_se'] * 100:.0f} | {_r(b['mean_r'])}{_se(b['se_r'])} | "
            f"{b['net_r']:+.1f}R | {flag} |")


def _bucket_table(buckets):
    head = "| Bucket | n | Win % | Mean R | Net R | |\n|---|---|---|---|---|---|"
    return "\n".join([head] + [_bucket_line(b) for b in buckets])


def verdict_of(review, llm, accepted, error):
    if error:
        return "review failed"
    if llm is None:
        return "stats only" if review["stats"]["n"] >= MIN_N else "insufficient data"
    if llm["verdict"] == "proposals" and not accepted:
        return "no action (every proposal was rejected by the guardrails)"
    return llm["verdict"].replace("_", " ")


def render_report(review, llm=None, accepted=(), rejected=(), config=None, error=None,
                  usage=None, model=None):
    stats, q, now, o = review["stats"], review["quality"], review["now"], review["stats"]["overall"]
    out = [
        f"# Alert review {now:%Y-%m-%d}",
        "",
        "> **Advisory only.** Nothing here has been applied. Backtest any change "
        "before it goes near `.env`.",
        "",
        f"- **Verdict:** {verdict_of(review, llm, accepted, error)}",
        f"- **Window:** {review['start'] or '–'} to {review['end'] or '–'} "
        f"(last {WINDOW_DAYS} days, newest {MAX_ALERTS} at most)",
        f"- **Closed live alerts:** {stats['n']} (a review needs {MIN_N})",
        f"- **Config:** `{review['config_hash']}` only",
        f"- **Buckets tested:** {stats['k']}. A bucket is *losing* only with n ≥ "
        f"{stats['min_n']} and mean R below zero by z > {stats['z']} (Bonferroni, α={ALPHA}). "
        f"At 95% about {stats['k'] * ALPHA:.1f} would look significant by chance.",
        f"- **Breakeven:** {_pct(stats['breakeven_win_rate'])} win rate at {stats['rr']}R. "
        "Spread and slippage are not modelled.",
        "",
    ]
    if error:
        out += ["## Review failed", "", f"`{error}`", "",
                "The statistics below are complete; only the model's reading is missing.", ""]
    elif llm is None and stats["n"] < MIN_N:
        out += ["## Insufficient data", "",
                f"{stats['n']} closed alerts under this config; a review needs {MIN_N}. "
                "No model was asked, and nothing is suggested.", ""]

    if o:
        out += ["## Overall", "",
                f"{o['n']} closed, {_pct(o['win_rate'])} won, {_r(o['mean_r'])}{_se(o['se_r'])} "
                f"per trade, {o['net_r']:+.1f}R net.", ""]

    if llm is not None:
        out += ["## Summary (model's reading)", "", llm["summary"], ""]
        if llm["negative_buckets"]:
            out += ["## Negative buckets (model's reading)", ""]
            out += [f"- `{nb['bucket_id']}`: {nb['interpretation']}" for nb in llm["negative_buckets"]]
            out.append("")
        if llm["loss_clusters"]:
            out += ["## Loss clusters (model's reading)", ""]
            for c in llm["loss_clusters"]:
                ids = ", ".join(f"`{b}`" for b in c["bucket_ids"]) or "–"
                out.append(f"- {c['description']} (buckets {ids}; {len(c['alert_ids'])} alerts)")
            out.append("")

    out += ["## Proposed changes", ""]
    if accepted:
        out.append("```diff")
        for p in accepted:
            key = p["env_key"]
            basis = "; ".join(f"{b['bucket_id']}: n={b['n']}, {_r(b['mean_r'])}{_se(b['se_r'])}"
                              for b in p["buckets"])
            out += [f"- {key}={_env_text(config[key])}",
                    f"+ {key}={_env_text(p['value'])}   # {basis}"]
        out += ["```", ""]
        for p in accepted:
            out += [f"**{p['env_key']}**: {p['rationale']}", "",
                    f"Verify first: `{p['backtest_command']}`, before and after the change.", ""]
    else:
        out += ["None.", ""]
    if rejected:
        out += ["### Rejected by guardrail", ""]
        out += [f"- `{p.get('env_key')}` → `{p.get('proposed_value')}`: {p['reason']}"
                for p in rejected]
        out.append("")
    if llm is not None and llm["hypotheses"]:
        out += ["## Hypotheses needing code (backtest first)", ""]
        out += [f"- {h}" for h in llm["hypotheses"]] + [""]
    if llm is not None and llm["caveats"]:
        out += ["## Caveats (model's reading)", ""]
        out += [f"- {c}" for c in llm["caveats"]] + [""]

    if stats["buckets"]:
        out += ["## Buckets", "", _bucket_table(stats["buckets"]), ""]
    out += ["## Data quality", "",
            f"- Alerts logged under this config: {q['alerts_logged']}",
            f"- Bars alerted more than once: {q['duplicate_sends']}",
            f"- Exits that gapped through the level (real fill worse): {q['exit_gapped']}",
            f"- Entry price disagreeing with the candle: {q['entry_mismatch']}",
            f"- Still open: {q['still_open']} · expired: {q['expired']} · "
            f"unresolvable: {q['unresolvable']}", ""]
    prior = review["prior"]
    if prior and prior["buckets"]:
        out += [f"## Backtest prior ({prior['source']})", "",
                "A different population: one position at a time, cooldown per direction.", "",
                _bucket_table(prior["buckets"]), ""]
    if usage is not None:
        tin = getattr(usage, "input_tokens", 0) or 0
        tout = getattr(usage, "output_tokens", 0) or 0
        price = PRICES.get(model)
        cost = f", about ${(tin * price[0] + tout * price[1]) / 1e6:.2f}" if price else ""
        out += ["---", f"_Model {model}: {tin} input / {tout} output tokens{cost}._", ""]
    return "\n".join(out)


def discord_summary(review, verdict, accepted, path):
    stats, o = review["stats"], review["stats"]["overall"]
    losing = sum(1 for b in stats["buckets"] if b["sig_negative"])
    line = f"{_pct(o['win_rate'])} won, {_r(o['mean_r'])} per trade" if o else "no closed alerts"
    text = (f"📋 **Weekly alert review {review['now']:%Y-%m-%d}**: {verdict}\n"
            f"{stats['n']} closed alerts · {line} · {losing} losing bucket(s) of {stats['k']} · "
            f"{len(accepted)} proposed change(s)\n"
            f"Full report: `{path.name}` in the reports folder. "
            "_Advisory only; nothing has been applied._")
    return text[:1900]


def redact(text):
    text = signal_bot._redact(text)
    for name in ("ANTHROPIC_API_KEY", "OANDA_API_TOKEN"):
        value = os.environ.get(name, "")
        if len(value) >= 8:
            text = text.replace(value, "<redacted>")
    return text


def post_discord(text):
    try:
        resp = requests.post(signal_bot.DISCORD_WEBHOOK_URL, json={"content": text}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"WARNING: Discord summary not posted: {redact(str(exc))}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(no_llm=False, dry_run=False, post=None, client=None, now=None, db=None,
        reports_dir=None):
    """Returns an exit code: 0 report written, 1 the LLM step failed, 2 no alert log."""
    now = now or datetime.now(timezone.utc)
    db = db or alert_log.db_path(ROOT, signal_bot.TICKER)
    try:
        conn = alert_log.connect(db, readonly=True)
    except FileNotFoundError:
        print(f"No alert log at {db}: run resolve_alerts.py first.", file=sys.stderr)
        return 2
    try:
        review = build_review(conn, now, signal_bot.config_hash(), signal_bot.RR)
    finally:
        conn.close()

    config = current_config()
    llm = usage = error = None
    accepted, rejected = [], []
    enough = review["stats"]["n"] >= MIN_N
    if enough and not no_llm:
        learnings = LEARNINGS.read_text(encoding="utf-8") if LEARNINGS.exists() else "(none)"
        system, user = build_prompt(review, learnings, config)
        if dry_run:
            print(system + "\n\n" + user)
            return 0
        try:
            llm, usage = call_llm(system, user, client=client)
            accepted, rejected = check_proposals(llm["proposals"], review["stats"], config)
        except Exception as exc:  # any failure becomes a report, never a crash
            error = redact(f"{type(exc).__name__}: {exc}")
    elif dry_run:
        why = "--no-llm" if no_llm else f"only {review['stats']['n']} closed alerts, need {MIN_N}"
        print(f"No model would be asked ({why}).")
        return 0

    text = render_report(review, llm, accepted, rejected, config, error, usage, MODEL)
    reports_dir = Path(reports_dir or REPORTS_DIR)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{now:%Y-%m-%d}.md"
    path.write_text(text, encoding="utf-8")
    print(f"report written to {path}")

    if POST_DISCORD if post is None else post:
        post_discord(discord_summary(review, verdict_of(review, llm, accepted, error),
                                     accepted, path))
    if error:
        print(f"review failed: {error}", file=sys.stderr)
        return 1
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("--no-llm", action="store_true", help="statistics only, no API call")
    p.add_argument("--dry-run", action="store_true",
                   help="print the prompt; call nothing, write nothing")
    p.add_argument("--post-discord", action="store_true",
                   help="post a short summary to Discord (also REVIEW_POST_DISCORD=true)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not signal_bot.TICKER:
        print("SIGNAL_TICKER is not set.", file=sys.stderr)
        return 2
    return run(no_llm=args.no_llm, dry_run=args.dry_run,
               post=True if args.post_discord else None)


if __name__ == "__main__":
    sys.exit(main())
