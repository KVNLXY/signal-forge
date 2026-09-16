"""Backtest of a Telegram group's chart signals, step 2 of 3.

    python scripts/backtest/collect.py <out_dir>            read the signals (OCR)
    python scripts/backtest/run.py     <out_dir>            replay them on MEXC candles
    python scripts/backtest/report.py  <out_dir> <out.pdf>  write the PDF

Group id, topics and the start date are constants at the top of collect.py.
"""


import asyncio
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.config import get_settings  # noqa: E402
from app.mexc.client import MexcClient  # noqa: E402

OUT_DIR = pathlib.Path(sys.argv[1])
AMOUNT = 100.0
FEE = 0.0005
TOLERANCE = 0.003            # ENTRY_TOLERANCE_PERCENT
LIMIT_HOURS = 24             # LIMIT_ENTRY_EXPIRY_HOURS
BREAKOUT_MINUTES = 15        # SIGNAL_EXPIRY_MINUTES
SPLITS = [0.30, 0.30, 0.40]  # TP_SPLITS
MAX_DAYS = 45
CANDLE_MS = 15 * 60 * 1000
NOW = datetime.now(timezone.utc)


async def klines(client: MexcClient, symbol: str, start: datetime, days: int) -> list[list[float]]:
    out: list[list[float]] = []
    cursor = int(start.timestamp() * 1000)
    end = min(int((start + timedelta(days=days)).timestamp() * 1000), int(NOW.timestamp() * 1000))
    while cursor < end:
        rows = await client.request("GET", "/api/v3/klines", {
            "symbol": symbol, "interval": "15m", "startTime": cursor,
            "endTime": end, "limit": 1000,
        })
        if not rows:
            break
        for r in rows:
            out.append([int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])])
        last = int(rows[-1][0])
        if last <= cursor:
            break
        cursor = last + CANDLE_MS
        await asyncio.sleep(0.12)
    return out


def splits_for(n_tp: int) -> list[float]:
    tps = SPLITS[:n_tp]
    if not tps:
        return []
    tps[-1] = round(1 - sum(tps[:-1]), 6)
    return tps


def simulate(entry: float, sl: float, tps: list[float], candles: list[list[float]],
             strategy: str) -> dict:
    """strategy: bot | tp1_all | no_breakeven"""
    entry_high = entry * (1 + TOLERANCE)
    if not candles:
        return {"status": "no_data"}

    # ---- entry -------------------------------------------------------------
    price0 = candles[0][1]
    fill = None
    fill_index = None
    if price0 > entry_high:                                    # limit: wait for the pullback
        deadline = candles[0][0] + LIMIT_HOURS * 3600 * 1000
        for i, (t, o, h, l, c) in enumerate(candles):
            if t > deadline:
                break
            if l <= sl:                                        # crashed through the stop first
                return {"status": "cancelled_sl_first", "at": t}
            if l <= entry_high:
                fill = min(entry_high, o) if o <= entry_high else entry
                fill_index = i
                break
        if fill is None:
            return {"status": "expired_no_pullback"}
    elif price0 >= entry:                                      # already in the zone
        fill, fill_index = price0, 0
    else:                                                      # below: needs to rise soon
        deadline = candles[0][0] + BREAKOUT_MINUTES * 60 * 1000
        for i, (t, o, h, l, c) in enumerate(candles):
            if t > deadline:
                break
            if h >= entry:
                fill, fill_index = entry, i
                break
        if fill is None:
            return {"status": "expired_no_breakout"}

    # ---- exits ---------------------------------------------------------------
    qty = AMOUNT * (1 - FEE) / fill
    remaining = qty
    proceeds = 0.0
    current_sl = sl
    targets = tps if strategy != "tp1_all" else tps[:1]
    parts = splits_for(len(targets)) if strategy != "tp1_all" else [1.0]
    tp_hit = 0
    exits = []
    close_reason = None
    close_time = None

    for t, o, h, l, c in candles[fill_index:]:
        if l <= current_sl:                                    # pessimistic: stop first
            proceeds += remaining * current_sl * (1 - FEE)
            exits.append({"t": t, "price": current_sl, "qty": remaining,
                          "reason": "SL" if current_sl == sl else "BE"})
            close_reason = "SL" if current_sl == sl else "BE"
            remaining = 0.0
            close_time = t
            break
        while tp_hit < len(targets) and h >= targets[tp_hit]:
            portion = remaining if tp_hit == len(targets) - 1 else min(qty * parts[tp_hit], remaining)
            proceeds += portion * targets[tp_hit] * (1 - FEE)
            exits.append({"t": t, "price": targets[tp_hit], "qty": portion, "reason": f"TP{tp_hit + 1}"})
            remaining -= portion
            tp_hit += 1
            if tp_hit == 1 and strategy == "bot" and remaining > 1e-12:
                current_sl = max(current_sl, fill)             # breakeven
        if remaining <= 1e-12:
            close_reason = f"TP{tp_hit}"
            close_time = t
            break

    last_close = candles[-1][4]
    open_value = remaining * last_close * (1 - FEE)
    realized_pnl = proceeds - AMOUNT if remaining <= 1e-12 else None
    marked_pnl = proceeds + open_value - AMOUNT
    return {
        "status": "closed" if remaining <= 1e-12 else "open",
        "fill": fill,
        "fill_time": candles[fill_index][0],
        "close_reason": close_reason,
        "close_time": close_time,
        "tp_hit": tp_hit,
        "realized_pnl": realized_pnl,
        "marked_pnl": marked_pnl,
        "remaining_pct": remaining / qty * 100 if qty else 0,
        "exits": exits,
        "candles": len(candles),
    }


async def main() -> None:
    data = json.loads((OUT_DIR / "signals.json").read_text(encoding="utf-8"))
    settings = get_settings()
    whitelist = set(settings.halal_symbols)
    client = MexcClient()

    readable = [r for r in data["records"] if r["ok"]]
    readable.sort(key=lambda r: r["date"])

    # the bot's own repeat filter: same symbol, entry and stop within 0.2%, 7 days
    kept: list[dict] = []
    for r in readable:
        e, s_ = float(r["entry"]), float(r["sl"])
        dup = None
        for k in kept:
            if k["symbol"] != r["symbol"]:
                continue
            dt = datetime.fromisoformat(r["date"]) - datetime.fromisoformat(k["date"])
            if dt.days > 7:
                continue
            if abs(float(k["entry"]) - e) <= e * 0.002 and abs(float(k["sl"]) - s_) <= s_ * 0.002:
                dup = k
                break
        if dup is None:
            kept.append(r)
    print(f"readable={len(readable)} unique={len(kept)}", flush=True)

    results = []
    cache: dict[tuple, list] = {}
    try:
        for n, r in enumerate(kept, 1):
            symbol = r["symbol"]
            start = datetime.fromisoformat(r["date"])
            key = (symbol, start.strftime("%Y-%m-%d %H"))
            try:
                if key not in cache:
                    cache[key] = await klines(client, symbol, start, MAX_DAYS)
                candles = cache[key]
            except Exception as exc:
                candles = []
                r["fetch_error"] = str(exc)[:120]
            entry, sl = float(r["entry"]), float(r["sl"])
            tps = [float(t) for t in r["tps"]]
            row = {
                "id": r["id"], "date": r["date"], "topic": r["topic"], "caption": r["caption"],
                "symbol": symbol, "entry": entry, "sl": sl, "tps": tps,
                "whitelisted": symbol in whitelist,
                "sl_pct": (entry - sl) / entry * 100,
                "tp1_pct": (tps[0] - entry) / entry * 100 if tps else None,
                "bot": simulate(entry, sl, tps, candles, "bot"),
                "tp1_all": simulate(entry, sl, tps, candles, "tp1_all"),
                "no_breakeven": simulate(entry, sl, tps, candles, "no_breakeven"),
            }
            if not candles:
                row["bot"]["status"] = row["tp1_all"]["status"] = row["no_breakeven"]["status"] = \
                    "no_data" if "fetch_error" not in r else "fetch_error"
            results.append(row)
            b = row["bot"]
            print(f"{n:>3}/{len(kept)} {start:%m-%d} {symbol:<10} {b['status']:<22} "
                  f"{b.get('close_reason') or '':<4} pnl={b.get('marked_pnl') if b.get('marked_pnl') is not None else '-'}",
                  flush=True)
    finally:
        await client.close()

    (OUT_DIR / "results.json").write_text(
        json.dumps({"generated": NOW.isoformat(), "amount": AMOUNT, "fee": FEE,
                    "max_days": MAX_DAYS, "stats": data["stats"],
                    "readable": len(readable), "unique": len(kept), "results": results},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print("done")


asyncio.run(main())
