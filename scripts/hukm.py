"""Shariah rulings for coins from two independent sources.

    python scripts/hukm.py                     every coin in HALAL_COINS
    python scripts/hukm.py PYTH,KAITO,OM       just these
    python scripts/hukm.py --md HALAL_RULINGS.md   also rewrite the markdown record

Source 1 - @CryptoGulfHalal_Bot (Crypto Rahhal): the fatwas of Crypto Islam and
Crypto Halal, one verdict per coin, answered in Arabic:
    حكم العملة: مباح ✅  (permissible)  /  غير مباح ❌  (not permissible)

Source 2 - Sharlife (sharlife.my): Shariah / Grey / Non-Shariah screening,
matched by ticker - the project name is shown because tickers collide.

This script only collects and tabulates.  It never edits HALAL_COINS - that
decision is the admin's.  Uses the bot's Telegram session (.env); about 10 s
per coin for the Telegram bot, one request in total for Sharlife.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings  # noqa: E402
from app.halal.sharlife import SharlifeEntry, SharlifeIndex  # noqa: E402
from app.telegram.client import build_client, connect  # noqa: E402

BOT = "@CryptoGulfHalal_Bot"
RULING_RE = re.compile(r"حكم العملة:\s*(.+)")
LINK_RE = re.compile(r"رابط الحكم:\s*(\S+)")
NO_RULING = "لايوجد حكم"


def label(ruling: str) -> str:
    if "غير مباح" in ruling:
        return "NOT PERMISSIBLE ❌"
    if "مباح" in ruling:
        return "permissible ✅"
    if "شبه" in ruling:
        return "doubtful 🟠"
    return ruling


async def replies_after(client, sent_id: int, wait: float) -> list:
    await asyncio.sleep(wait)
    messages = await client.get_messages(BOT, min_id=sent_id, limit=10)
    return [m for m in messages if not m.out]


async def ask_bot(client, coin: str) -> tuple[str, str]:
    sent = await client.send_message(BOT, coin)
    replies = await replies_after(client, sent.id, 8)
    if not replies:
        replies = await replies_after(client, sent.id, 8)
    text = "\n".join((m.text or "") for m in replies)
    ruling = RULING_RE.search(text)
    link = LINK_RE.search(text)
    if ruling:
        return label(ruling.group(1).strip()), (link.group(1) if link else "")
    if NO_RULING in text:
        return "no ruling yet", ""
    if not replies:
        return "(no reply)", ""
    return "(unexpected reply) " + text.strip().splitlines()[0][:50], ""


def sharlife_cell(hits: list[SharlifeEntry]) -> str:
    if not hits:
        return "not listed"
    return "; ".join(f"{h.label} ({h.name})" for h in hits)


def needs_attention(bot_verdict: str, hits: list[SharlifeEntry]) -> bool:
    if not bot_verdict.startswith("permissible"):
        return True
    return any(h.status in ("grey", "failed") for h in hits)


async def main(coins: list[str], md_path: str | None) -> int:
    settings = get_settings()
    sharlife = await SharlifeIndex.load()
    print(f"Sharlife screen: {len(sharlife)} listings"
          + ("" if sharlife.entries else "  (unavailable - column will be empty)"))

    client = build_client(
        settings.telegram_api_id,
        settings.telegram_api_hash.get_secret_value(),
        settings.telegram_session.get_secret_value(),
    )
    await connect(client)
    rows: list[tuple[str, str, str, list[SharlifeEntry]]] = []
    try:
        print(f"\n{'COIN':<8} {'CRYPTO ISLAM (bot)':<24} SHARLIFE")
        print("-" * 90)
        for coin in coins:
            verdict, link = await ask_bot(client, coin)
            hits = sharlife.lookup(coin)
            rows.append((coin, verdict, link, hits))
            mark = "  <- check" if needs_attention(verdict, hits) else ""
            print(f"{coin:<8} {verdict:<24} {sharlife_cell(hits)}{mark}", flush=True)
            await asyncio.sleep(2)
    finally:
        await client.disconnect()

    flagged = [r for r in rows if needs_attention(r[1], r[3])]
    print()
    if flagged:
        print("Needs the admin's attention:")
        for coin, verdict, link, hits in flagged:
            print(f"  {coin:<8} bot: {verdict:<22} sharlife: {sharlife_cell(hits)}  {link}")
    else:
        print("Every coin is permissible on both sources.")

    if md_path:
        write_markdown(md_path, rows, settings)
        print(f"\nwritten {md_path}")
    return 0


def write_markdown(path: str, rows, settings) -> None:
    today = date.today().isoformat()
    whitelist = set(settings.halal_symbols)
    quote = settings.quote_asset
    lines = [
        "# Halal rulings behind HALAL_COINS",
        "",
        f"Checked on **{today}** from two independent sources. The decision to trade a",
        "coin stays with the admin; the bot only ever reads `HALAL_COINS`. Re-check with",
        "`python scripts/hukm.py --md HALAL_RULINGS.md`.",
        "",
        "* **Crypto Islam** — [@CryptoGulfHalal_Bot](https://t.me/CryptoGulfHalal_Bot) (Crypto",
        "  Rahhal), serving the fatwas of Crypto Islam and Crypto Halal. Every reply carries",
        "  the bot's own warning: *verify the link, the ruling may have changed*.",
        "* **Sharlife** — [sharlife.my/crypto-shariah](https://sharlife.my/crypto-shariah),",
        "  Shariah / Grey / Non-Shariah screening, matched by ticker, so the project name is",
        "  shown — a ticker can belong to a different project than the one traded.",
        "",
        "| Coin | In list | Crypto Islam | Sharlife | Links |",
        "|---|---|---|---|---|",
    ]
    for coin, verdict, link, hits in sorted(rows, key=lambda r: r[0]):
        symbol = coin if coin.endswith(quote) else f"{coin}{quote}"
        listed = "yes" if symbol in whitelist else "no"
        links = " · ".join(
            [f"[fatwa]({link})"] * bool(link) + [f"[sharlife]({h.url})" for h in hits[:2]]
        )
        lines.append(f"| {coin} | {listed} | {verdict} | {sharlife_cell(hits)} | {links} |")

    flagged = [r for r in rows if needs_attention(r[1], r[3])]
    lines += ["", "## Needs the admin's attention", ""]
    if flagged:
        lines += ["| Coin | Why |", "|---|---|"]
        for coin, verdict, link, hits in flagged:
            why = []
            if not verdict.startswith("permissible"):
                why.append(f"Crypto Islam: {verdict}")
            for h in hits:
                if h.status in ("grey", "failed"):
                    why.append(f"Sharlife: {h.label} for {h.name}")
            lines.append(f"| {coin} | {'; '.join(why)} |")
    else:
        lines.append("Nothing - every coin is permissible on both sources.")
    lines += [
        "",
        "A Sharlife *Grey* is \"questionable\", not a prohibition; a *Non-Shariah* is one.",
        "Where the two sources disagree, the admin decides which fatwa to follow.",
        "",
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    md = None
    if "--md" in args:
        i = args.index("--md")
        md = args[i + 1] if i + 1 < len(args) else "HALAL_RULINGS.md"
        del args[i:i + 2]
    if args:
        wanted = [c.strip().upper() for c in args[0].split(",") if c.strip()]
    else:
        quote = get_settings().quote_asset
        wanted = [s[: -len(quote)] if s.endswith(quote) else s for s in get_settings().halal_symbols]
    sys.exit(asyncio.run(main(wanted, md)))
