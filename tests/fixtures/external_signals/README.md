# External signal samples

Real channel posts collected by other open-source signal bots, kept verbatim so
the parser is tested against shapes nobody here wrote.  `tests/test_parser_external.py`
runs every file through `parse_signal`.

| Folder | Source | Snapshot | Licence |
|---|---|---|---|
| `joostmbakker/` | [joostmbakker/telegram-crypto-signal-parser](https://github.com/joostmbakker/telegram-crypto-signal-parser) `test_signals/` | `c0d130c` (2022-08-10) | none declared - the files are quoted Telegram posts, used here as test data only |
| `vooi/` | [vooi-app/vooi-signals-bot-example](https://github.com/vooi-app/vooi-signals-bot-example) `fixtures/appendix_b_signals.json` | `bb81ee0` (2026-06-04) | MIT, see `vooi/LICENSE` |

The vooi file carries its own `expected` block written for a futures bot; the
test maps it onto our rules (a SHORT is reported and never traded, no SL means no
trade, leverage is ignored).  The joostmbakker files have no expectations - ours
are written in the test.

Shapes these samples added to the parser: `Get in :`, `Buy around`, `Zone:`,
`LONG : 1.38`, `Pair: AVAX-PERP` + `Direction: LONG`, `NEAR Protocol LONG`,
`ETH / BTC` (rejected as a non-USDT pair), `1) 36670` list markers, an empty
`Target :` line followed by `TARGET 1 : 1.43`, `1️⃣` keycap indexes, target lists
that run over several lines (`Stop Targets:` ends them), and numbered DCA entry
legs merged into one zone.
