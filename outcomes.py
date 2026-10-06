"""
How a trade with a fixed stop and target ended. Shared by backtest.py and the
dashboard so the two cannot disagree about a trade. No imports on purpose:
backtest.py reconfigures the environment when imported, so the dashboard
cannot import it, and this module must stay safe for both.
"""


def resolve(highs, lows, entry_i, signal, sl, tp):
    """
    Walk forward from the bar after entry until the stop or the target is
    touched. Returns (outcome, exit_index) with outcome in
    {"win", "loss", "open"}.
    """
    for j in range(entry_i + 1, len(highs)):
        if signal == "BUY":
            hit_sl = lows[j] <= sl
            hit_tp = highs[j] >= tp
        else:
            hit_sl = highs[j] >= sl
            hit_tp = lows[j] <= tp
        if hit_sl:
            # Checked first on purpose: when one bar covers both levels the
            # order is unknowable, so the pessimistic reading wins.
            return "loss", j
        if hit_tp:
            return "win", j
    return "open", None
