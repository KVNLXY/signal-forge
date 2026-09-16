# SignalForge

Halal spot trading bot. It reads crypto signals from the Telegram channels you
follow, checks each one against a fixed set of rules, and — only for
pre-approved halal coins — buys on **MEXC Spot** and sells at TP or SL.

**Spot only.** `USDT → COIN → USDT`. No futures, no margin, no leverage, no
shorting, no borrowing. A SHORT signal is ignored, always.

```
Telegram channel → parse → halal? → valid? → wait for entry
                 → MEXC spot BUY → TP/SL monitor → SELL → Telegram report
```

## The rules that decide everything

Any single "no" means no trade:

| Gate | Rejects when |
|---|---|
| Parse | coin, direction, entry, TP or SL cannot be read |
| Direction | the signal is SHORT / SELL |
| Halal | the coin is not in `HALAL_COINS` |
| Completeness | entry, SL or TP missing, or SL is not below entry |
| Expiry | the signal is older than `SIGNAL_EXPIRY_MINUTES` |
| Duplicate | a position (or a waiting signal) for that coin already exists |
| Entry zone | a breakout entry the price has already run past (a level under the market waits for the pullback instead) |
| Risk | `MAX_OPEN_TRADES` is full (the signal keeps waiting) |

> `UNKNOWN → NO TRADE`, `NON-HALAL → NO TRADE`, `SHORT → NO TRADE`,
> `NO SL → NO TRADE`, `EXPIRED → NO TRADE`, `DUPLICATE → NO TRADE`.

The bot never marks a coin halal by itself. `HALAL_COINS` is mirrored into the
`assets` table at start-up and read from there at run time; a coin removed from
the config is disabled, not deleted, so old trades stay readable.

Two independent Shariah sources help the admin keep that list honest, and
neither of them ever edits it:

```bash
python scripts/hukm.py                        # every coin in HALAL_COINS
python scripts/hukm.py PYTH,OM                # a few
python scripts/hukm.py --md HALAL_RULINGS.md  # and rewrite the dated record
```

* **Crypto Islam / Crypto Halal** through `@CryptoGulfHalal_Bot` — one verdict
  per coin (permissible / not permissible / no ruling yet).
* **Sharlife** (`sharlife.my/crypto-shariah`) — Shariah / Grey / Non-Shariah
  screening of ~1,200 coins, fetched once a week into `data/sharlife.json`.
  Matched by ticker, so the project name is always shown: SOPH is *SophiaVerse*
  on Sharlife and *Sophon* on MEXC.

`python -m app.main check` prints the Sharlife screen of every whitelisted coin
that is Grey or Non-Shariah. It is information for the admin, not a gate — a
Grey is "questionable", and where the two sources disagree the admin decides
which fatwa to follow. The current record is [HALAL_RULINGS.md](HALAL_RULINGS.md).

## Signal formats it understands

```
BTC LONG                    🚀 BTCUSDT LONG          #BTC LONG
                                                      
Entry: 100000-101000        Entry 100500             BUY 100500
TP1: 102000                 TP 103000                TP1 102000
TP2: 104000                 SL 99000                 TP2 104000
SL: 98000                                            STOP 99000
```

Also handled: `ETH/USDT`, `$SOL`, lowercase text, `Entry Zone: 3,500 - 3,550`,
`Take Profit 1:`, `Targets: 160 / 170 / 180`, `Stop-Loss:`, en-dashes, and
percentages in brackets (`3,900 (+10%)` reads as `3900`). Field-style posts
(`Pair: AVAX-PERP` / `Direction: LONG`), `Get in :`, `Buy around`, a bare
`Zone:`, `LONG : 1.38`, values on the line after the label, and numbered
lists (`1) 36670`, `1️⃣ 0.16`) are read too, including a target list that
runs over several lines (Cornix layout: `Targets:` then `1) …`, `2) …`, with a
`Stop Targets:` block after it). A numbered entry ladder (`Entry 1: 5.2`,
`Entry 2: 5.0`) is bought as one zone, `5.0–5.2`. `BTC/USD` and `BTC/USDC` are
the same market as `BTCUSDT`; a coin-quoted pair such as `ETH/BTC` is refused,
because nothing in it can be bought with USDT.

Uzbek and Russian labels count too, because that is how the channels actually
write - `Kirish` / `Xarid` / `Вход` for the entry, `Profit` / `Foyda` /
`Maqsad` / `Цель` for targets, `Stop` / `Zarar` / `Стоп` for the stop.
Telegram markdown is stripped first, so `**Kirish:** $12.6` reads fine:

```
#AVAX                          #SOL Setup
Kirish:  $12.6                 Kirish: 120
TP 1:    $13.75                Stop: 116
TP 2:    $14.82                Profit: 144
Stop:    $11.94
```

Neither post names a side. The direction is then read from the geometry: a
stop below the entry with a target above it can only be a buy, and the mirror
image can only be a short (which is ignored like any other short). Anything
ambiguous stays undecided and is not traded. Set
`REQUIRE_EXPLICIT_DIRECTION=true` to demand the word LONG/BUY instead.

## Signals that are only a picture

Some channels post the levels solely as a TradingView screenshot. The reader
is colour-first: on a Long Position drawing the label colour carries the
meaning, so the numbers are found before they are read.

```
green  label  ->  take profit        red    label  ->  stop loss
yellow label  ->  entry              white  label  ->  live price (ignored)
```

OCR alone is not trusted with money, so a reading has to pass three more
gates before it is even offered:

1. the geometry must be a long (`sl < entry < tp`) - a dropped `0,` turns
   `0,04957` into `4957` and is caught here;
2. the entry must be within `IMAGE_MAX_PRICE_DEVIATION_PERCENT` of the live
   MEXC price - a misread digit lands an order of magnitude away;
3. the usual gates: halal whitelist, expiry, no duplicate position.

What survives is sent to you as the chart plus the numbers and two buttons:

```
📷 SIGNAL FROM A CHART IMAGE          [ ✅ Confirm ]  [ ❌ Reject ]

ZAMAUSDT LONG                         Entry vs price: -9.28%
Entry: 0.05183   SL: 0.04957
TP: 0.06128                           Read by OCR - check it against
Live price: 0.05713                   the chart before confirming.
```

Nothing is bought until you press Confirm, and an unconfirmed signal expires
with everything else after `SIGNAL_EXPIRY_MINUTES`. Set
`IMAGE_SIGNAL_CONFIRMATION=false` to let image signals trade like text ones,
or `READ_CHART_IMAGES=false` to ignore pictures entirely.

The reader needs `rapidocr-onnxruntime`; without it the bot logs a warning and
keeps working on text signals.

Three more rules keep repeated pictures from becoming repeated trades:

* a caption that reports a result (`12%✅`, `1tp urdi`, `stop kotarib kutamiz`,
  `full tp`) is a management post, and its picture is not read at all;
* a chart whose entry and stop match a signal seen in the last 7 days (within
  0.2%) is the same setup - the VIP group and the public channel post the same
  image, and result screenshots repeat the drawing - and is ignored;
* in a discussion group only the admins' posts count (`GROUP_ADMIN_ONLY`), so
  a member asking "bunga kirsa bo'ladimi?" with a screenshot is never a signal.

Blue price labels above the entry are read as targets too, because plain
horizontal lines (the group's "oq lina, ko'k yozuv") carry the TP on charts
that are not drawn with the Long Position tool.

## Quick start

```bash
cp .env.example .env          # then fill it in
python scripts/login.py       # prints TELEGRAM_SESSION, paste it into .env
                              # no login code arrives? use --qr (scan to log in)
docker compose up -d          # Postgres + bot
```

Running it on a server (including the free always-on options) is covered in
[DEPLOY.md](DEPLOY.md).

Locally instead of Docker:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
python -m app.main check      # pre-flight: config, DB, MEXC, Telegram, whitelist
python -m app.main            # run
```

Python 3.11–3.13 is the tested range (`asyncpg` has no 3.14 wheels yet). The
Docker image uses 3.12.

### Commands

| Command | Does |
|---|---|
| `python -m app.main` | run the bot |
| `python -m app.main check` | verify everything without trading a cent |
| `python -m app.main stats` | print statistics and open positions |

In Telegram, the notification bot answers `/status`, `/positions`, `/stats`,
`/halal`, `/pause`, `/resume`, `/close SYMBOL` (or `/close all`),
`/reconcile` and `/help` — only in the admin chat — and carries the Confirm /
Reject buttons for signals read from chart images, for signals read by
Claude, and for follow-up posts about a running trade.

`/stats` ends with a per-channel scoreboard — signals, trades, W/L and PNL for
each channel — so a channel that does not earn its place is easy to spot.

## Configuration

Everything lives in `.env` (see `.env.example`).

| Variable | Default | Meaning |
|---|---|---|
| `TRADING_MODE` | `PAPER` | `PAPER` = simulated orders, real prices. `LIVE` = real MEXC orders. Never defaults to LIVE. |
| `TELEGRAM_API_ID` / `_API_HASH` | — | from my.telegram.org |
| `TELEGRAM_SESSION` | — | StringSession from `scripts/login.py` |
| `TELEGRAM_CHANNELS` | — | `@one,@two` — the channels to read |
| `MEXC_API_KEY` / `_SECRET` | — | spot read + trade only, **withdrawal OFF** |
| `TELEGRAM_ADMIN_BOT_TOKEN` / `_CHAT_ID` | — | where reports are sent |
| `HALAL_COINS` | — | the only tradable symbols; `BTC:https://…` records where the ruling came from (`/halal` shows it) |
| `TRADE_AMOUNT_USDT` | `100` | spent per trade |
| `MAX_OPEN_TRADES` | `3` | positions at once |
| `MAX_DAILY_LOSS_USDT` | `0` (off) | realised loss over the UTC day at which buying stops until midnight |
| `MAX_CONSECUTIVE_LOSSES` | `0` (off) | losing trades in a row at which buying pauses until `/resume` |
| `MAX_ENTRY_SPREAD_PERCENT` | `1` | a buy waits while `(ask − bid) / mid` is wider than this |
| `MAX_HOLD_HOURS` / `MAX_HOLD_ACTION` | `0` (off) / `warn` | a position stuck between SL and TP this long is reported once, or sold (`close`) |
| `MOVE_SL_TO_ENTRY_AFTER_TP1` | `true` | after TP1 the stop moves up to the entry price |
| `CATCH_UP_ON_START` | `true` | on (re)start read back the last `SIGNAL_EXPIRY_MINUTES` of every channel |
| `CHANNEL_UPDATES` / `UPDATE_CONFIRMATION` | `true` / `true` | read "50% yopamiz" / "stop to entry" / "bekor" posts; act only after your button |
| `LLM_FALLBACK` / `LLM_API_KEY` / `LLM_MODEL` | `true` / — / `claude-opus-5` | hand a post the parser cannot read to Claude (needs the `anthropic` package) |
| `LLM_SIGNAL_CONFIRMATION` | `true` | a signal read by Claude waits for your Confirm button |
| `SIGNAL_EXPIRY_MINUTES` | `15` | how long a breakout entry may wait, and the window for the Confirm button |
| `ORDER_TIMEOUT_SECONDS` | `60` | unfilled LIMIT order is cancelled after this |
| `USE_MARKET_ORDER` | `false` | LIMIT orders by default |
| `REQUIRE_EXPLICIT_DIRECTION` | `false` | when true, only signals that write LONG/BUY are traded |
| `ENTRY_ABOVE_ZONE` | `wait` | entry below the live price: `wait` for the pullback or `skip` it |
| `LIMIT_ENTRY_EXPIRY_HOURS` | `24` | how long a pullback entry may wait |
| `ENTRY_TOLERANCE_PERCENT` | `0.3` | a single-price entry is bought up to this far above the level |
| `MOVE_SL_TO_ENTRY_AFTER_TP1` | `true` | after TP1 the stop moves to the entry price |
| `GROUP_ADMIN_ONLY` | `true` | in a discussion group only admin posts are signals |
| `READ_CHART_IMAGES` | `true` | read entry/TP/SL off chart screenshots |
| `IMAGE_SIGNAL_CONFIRMATION` | `true` | an image signal waits for your Confirm button |
| `IMAGE_MAX_PRICE_DEVIATION_PERCENT` | `20` | drop a reading whose entry is that far from the live price |
| `LIMIT_SLIPPAGE_PERCENT` | `0.1` | how far past the last price a LIMIT order is placed so it fills |
| `TP_SPLITS` | `30,30,40` | share of the position sold at TP1/TP2/TP3 |
| `PRICE_POLL_SECONDS` | `5` | entry / TP / SL check interval |
| `PRICE_FEED` / `PRICE_FEED_MAX_AGE_SECONDS` | `rest` / `10` | `websocket` reads the exchange's pushed top-of-book instead of polling (needs `websockets`) |
| `HEARTBEAT_URL` / `HEARTBEAT_INTERVAL_SECONDS` | — / `60` | dead man's switch: pinged after every good price-loop pass |
| `MESSAGE_RETENTION_DAYS` | `90` | channel posts older than this are deleted hourly (signals and trades stay) |
| `PAPER_FEE_RATE` | `0.0005` | fee applied to paper fills so simulated PNL is honest |
| `DATABASE_URL` | local Postgres | SQLAlchemy async URL |

### Entry, TP and SL

With `Entry: 100000-101000`:

* price below `100000` → **wait** for it to rise into the zone (up to
  `SIGNAL_EXPIRY_MINUTES`)
* price inside the zone → **buy**
* price above `101000` → the entry is a **pullback level** ("kelsa" - buy when
  it comes down). The signal becomes a resting limit order: it buys the moment
  the price touches the zone from above, waits up to `LIMIT_ENTRY_EXPIRY_HOURS`,
  and is cancelled if the price crashes through the stop first.

That last rule is what the channels actually mean when they post a level
under the market. Set `ENTRY_ABOVE_ZONE=skip` to get the stricter original
rule instead (a price above the zone = a missed move, no trade).

With `TP_SPLITS=30,30,40` the position is sold in parts. If a signal carries
fewer TPs than splits, the last TP takes the rest (two TPs → 30% / 70%), so the
position always closes fully. SL sells everything that is left, and a jump past
several targets between two polls is handled in one pass.

PNL is `what came back − what went in`, including fees.

After TP1 the stop is raised to the entry price (`MOVE_SL_TO_ENTRY_AFTER_TP1`),
so what is left of the position can no longer lose - the signal authors' own
rule, and the reason a partial exit is worth taking.

### Circuit breakers

Three breakers sit above the per-signal gates. Each one only ever blocks a
**new buy** — an open position is always watched and closed at TP or SL,
halted or not — and each one is announced in Telegram once when it trips.

| Breaker | Trips when | Clears when |
|---|---|---|
| `/pause` | you send it | `/resume` |
| `MAX_CONSECUTIVE_LOSSES` | the last N closed trades all lost | a winning trade, or `/resume` (forgives the streak) |
| `MAX_DAILY_LOSS_USDT` | today's realised PNL (UTC) is at or below `−N` | midnight UTC — `/resume` does not override it |
| `MAX_ENTRY_SPREAD_PERCENT` | the order book is wider than N % at the moment of the buy | the book tightens; the signal keeps waiting and expires on its own schedule |

A signal that arrives while buying is halted is skipped with the reason; a
signal that was already waiting meets the breaker when its entry is reached
and is skipped then. The spread gate is per symbol: it never halts the bot,
it holds that one entry. If the book cannot be read at all, nothing is bought
that tick — never blindly.

`/pause` and the forgiven streak live in memory on purpose: after a restart the
bot re-reads its trades and, if the last N are still losses, pauses again.
`/status` shows every breaker and today's PNL.

### Follow-up posts about a running trade

A channel keeps talking after the entry: "TP1 urdi, 50% yopamiz", "Stopni
olgan joyga ko'taring", "SL -> 0.118", "Bekor". With `CHANNEL_UPDATES=true`
such a post — one that is not a signal of its own — is read as one of three
instructions (sell all or a share, move the stop, cancel a waiting signal) and
tied to the position it is about: the coin it names, else the post it replies
to, else the channel's only open position. Nothing is guessed when that is
ambiguous.

```
📝 CHANNEL UPDATE                      [ ✅ Do it ]  [ ❌ Ignore ]

BTCUSDT: sell 50% of what is left
Position: entry 100500, SL 98000, 0.00099 left, TP hit 0/2
Live price: 101500
«TP1 yaqin, 50% yopamiz»
- @alpha_signals
```

The instruction waits for the button (`UPDATE_CONFIRMATION=false` acts at
once) and expires with everything else after `SIGNAL_EXPIRY_MINUTES`. Two
rules hold whatever the post says: a stop is only ever moved **up**, and a
"close" for a signal that never entered is a cancel. A sale made through the
bot — this way or with `/close` — keeps the books straight; one made by hand
on the exchange does not, which is what `/reconcile` is for.

### Posts the parser cannot read

The regex parser covers the shapes the channels actually use, but not every
one. With `LLM_FALLBACK=true` and an `LLM_API_KEY`, a post the parser gives up
on — one that still looks like it carries levels: signal-shaped, or with at
least two numbers in it — is handed to Claude (`LLM_MODEL`, default
`claude-opus-5`) with a fixed JSON shape to fill in: coin, side, entry, stop,
targets, copied as written, never computed. The reading then passes the same
gates a chart reading does — halal list, live-price sanity, no duplicate — and
waits for the same Confirm button:

```
🤖 SIGNAL READ BY CLAUDE              [ ✅ Confirm ]  [ ❌ Reject ]

BTCUSDT LONG
Entry: 100000   SL: 98000   TP: 105000 / 108000
Live price: 100200
Read by Claude (side inferred from the levels)
```

Chatter and result posts never cost a call. Without the `anthropic` package
or a key the bot logs a warning and works exactly as before; a reader outage
drops the post, it never stops the bot. `LLM_SIGNAL_CONFIRMATION=false` trades
the reading like text — that is the model's word alone, so leave it on unless
paper trading has earned the trust.

### Keeping the stop alive

The stop-loss lives in this process: MEXC spot has no stop orders, so a bot
that is down leaves every position unprotected. Three things cover that:

* **Heartbeat.** `HEARTBEAT_URL` (healthchecks.io, Uptime Kuma, …) is pinged
  after every price-loop pass that actually read prices and checked every
  position — a loop that is alive but failing sends nothing, on purpose. The
  monitor's missed-ping alert is what reaches your phone.
* **Catch-up.** On every (re)connect the last `SIGNAL_EXPIRY_MINUTES` of each
  channel are read back, oldest first, so a signal posted while the bot was
  down is still traded if fresh; anything older or already handled is dropped.
* **Reconciliation.** In LIVE mode the open trades are lined up with the
  account at start (and on `/reconcile`): a position the account still holds in
  part is tracked at the real size; one that is gone is closed as `EXTERNAL`
  with the current price standing in for the unknown proceeds.

### Live prices

`PRICE_FEED=websocket` subscribes to the exchange's own top-of-book stream
(`spot@public.aggre.bookTicker`, about ten pushes a second per coin) and the
price loop reads the cache instead of polling — set `PRICE_POLL_SECONDS=1`
to act on it. The stream is protobuf; the two messages that matter are decoded
by hand, so the only extra dependency is `websockets`. REST stays the
fallback: a coin whose last push is older than `PRICE_FEED_MAX_AGE_SECONDS`,
or a feed that is reconnecting, is priced over REST as before, and the spread
gate reads the same book. `/status` shows the feed's state and push count.

### Housekeeping

* `MAX_HOLD_HOURS` — a position that has sat between SL and TP for that long
  is reported once with its unrealised PNL (`warn`) or sold at market
  (`MAX_HOLD_ACTION=close`), so it stops occupying a `MAX_OPEN_TRADES` slot.
* `MESSAGE_RETENTION_DAYS` — channel posts older than that are deleted once
  an hour; every signal keeps its own text, so the trade history loses nothing.

## Live mode

`TRADING_MODE=LIVE` is the only switch, and it is never the default. Before it
runs, the bot checks the API key can trade, warns loudly if withdrawal
permission is enabled, and syncs its clock with MEXC.

* LIMIT orders by default; unfilled after `ORDER_TIMEOUT_SECONDS` → cancelled,
  and the signal goes back to waiting (`USE_MARKET_ORDER=true` to skip this).
* A partially filled buy is kept — the trade opens with what was actually
  bought, never with a quantity you do not own.
* Exits are guaranteed: if a LIMIT sell does not fill in time it is cancelled
  and the remainder goes out at market, so an SL always closes.
* Sell sizes are capped by the real free balance, because MEXC takes the buy
  commission out of the base asset.

Run `python -m app.main check` first — it verifies the whitelist symbols are
actually listed on MEXC spot and that `TRADE_AMOUNT_USDT` clears each symbol's
minimum order size.

## Safety

* Secrets only in `.env`, which is git-ignored. `.env` is never read by the
  image build.
* API secrets are `SecretStr` and are scrubbed from every log line by a
  redaction filter, so a stray debug log cannot leak them.
* The bot has no withdrawal code path at all.
* PAPER mode never calls a private endpoint, even with a valid key.
* Telegram disconnects, MEXC errors and timeouts, bad signals, unknown coins,
  invalid prices, insufficient balance, rejected orders and database errors are
  caught, logged, and reported to Telegram (throttled, so one broken loop
  cannot spam you). The bot keeps running.

## Backtesting a channel

`scripts/backtest/` replays a group's chart signals with the bot's own rules
on MEXC 15-minute candles and writes a PDF:

```bash
python scripts/backtest/collect.py out/            # OCR every admin chart post
python scripts/backtest/run.py     out/            # replay on MEXC candles
python scripts/backtest/report.py  out/ report.pdf # the PDF (needs reportlab, matplotlib)
```

Rules replayed: pullback entry with a 24 h wait, 0.3 % entry tolerance, TP
30/30/40, stop to entry after TP1, $100 per trade, 0.05 % fee, and the
pessimistic same-candle rule (SL before TP). Unreadable charts and entries
more than 20 % from the price at signal time are excluded, exactly as the
live bot would exclude them. The first report - PRO VIP SIGNAL, 2026 - is
in `reports/`.

The chart reader reads price labels twice: once to find the pair, then again
with the live price as a scale hint, which is how `11,430` becomes 11.43 on a
Russian-locale chart instead of eleven thousand.

## Tests

```bash
python -m pytest
```

222 tests, no network, no real orders — MEXC is mocked with `httpx.MockTransport`
and the database is a temporary SQLite file. They cover the required cases:
`BTC LONG` parsed, `BTC SHORT` rejected, valid entry accepted, missing SL
rejected, unknown coin rejected, non-halal coin rejected, expired signal
rejected, duplicate trade rejected, paper BUY/SELL, TP and SL, plus the MEXC
signature, order parameters, retries, and the LIVE order lifecycle.
`tests/test_parser_uzbek.py` is built from real posts in the configured
channels, `tests/test_parser_external.py` runs 24 posts collected by other
open-source signal bots (sources and licences in
`tests/fixtures/external_signals/README.md`), `tests/test_breakers.py` trips
every circuit breaker and checks that exits still run, and
`tests/test_chart_images.py` covers the image path end to end - confirmation,
rejection, misread prices and expiry. `test_channel_updates.py`,
`test_llm_reader.py` (Claude replaced by an injected reader), `test_feed.py`
(hand-built protobuf frames), `test_listener.py` (a fake Telethon client),
`test_heartbeat.py` and `test_housekeeping.py` cover the rest.

## Layout

```
app/
├── main.py               entry point + wiring (run / check / stats)
├── config.py             .env settings
├── logging_setup.py      logging with secret redaction
├── heartbeat.py          dead man's switch ping
├── telegram/
│   ├── client.py         Telethon user session
│   ├── listener.py       channel subscription + catch-up on start
│   ├── parser.py         text → signal
│   ├── updates.py        follow-up posts → close / stop / cancel
│   └── llm_reader.py     Claude as the second reader
├── mexc/
│   ├── client.py         signed REST transport
│   ├── market.py         prices + symbol rules + order book
│   ├── feed.py           WebSocket top-of-book (protobuf, decoded by hand)
│   └── orders.py         spot orders
├── trading/
│   ├── base.py           Fill / Executor interface shared by paper and live
│   ├── engine.py         all the decisions
│   ├── paper.py          simulated fills
│   └── live.py           real orders
├── halal/whitelist.py    the allowed coins
├── vision/chart.py       reads levels off chart screenshots (OCR + colour)
├── database/
│   ├── models.py         channels, messages, signals, trades, assets, trade_updates
│   ├── database.py       async engine + sessions
│   └── repository.py     queries
└── notifications/telegram.py   reports + admin commands
```

`trading/base.py` is the one file added to the layout in the brief: it holds the
`Fill` type and the executor interface that `paper.py` and `live.py` both
implement, which keeps the engine identical in both modes.
