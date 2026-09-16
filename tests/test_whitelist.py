"""Halal whitelist: only the admin list is tradable, ever."""

from __future__ import annotations


from app.database import repository as repo
from app.halal.whitelist import HalalWhitelist


def test_symbols_are_normalised():
    wl = HalalWhitelist(["BTC", "eth/usdt", "SOL-USDT"])
    assert wl.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert wl.is_allowed("btc")
    assert wl.is_allowed("BTCUSDT")
    assert "ETHUSDT" in wl


def test_everything_else_is_rejected():
    wl = HalalWhitelist(["BTCUSDT"])
    assert not wl.is_allowed("DOGEUSDT")
    assert not wl.is_allowed("ABC")


def test_an_empty_whitelist_allows_nothing():
    wl = HalalWhitelist([])
    assert len(wl) == 0
    assert not wl.is_allowed("BTCUSDT")


async def test_config_is_mirrored_into_the_assets_table(database):
    wl = HalalWhitelist()
    async with database.session() as session:
        await wl.sync_from_config(session, ["BTC", "ETH"])
        assets = await repo.list_assets(session, only_halal=False)

    assert wl.symbols == ["BTCUSDT", "ETHUSDT"]
    assert {a.symbol for a in assets} == {"BTCUSDT", "ETHUSDT"}
    assert all(a.is_halal for a in assets)
    assert {a.base_asset for a in assets} == {"BTC", "ETH"}


async def test_removing_a_coin_disables_it_but_keeps_the_row(database):
    wl = HalalWhitelist()
    async with database.session() as session:
        await wl.sync_from_config(session, ["BTC", "ETH"])
    async with database.session() as session:
        await wl.sync_from_config(session, ["BTC"])
        assets = {a.symbol: a for a in await repo.list_assets(session, only_halal=False)}

    assert wl.symbols == ["BTCUSDT"]
    assert not wl.is_allowed("ETHUSDT")
    assert assets["ETHUSDT"].is_halal is False        # history stays readable
    assert assets["BTCUSDT"].is_halal is True


async def test_reload_reads_the_table_back(database):
    async with database.session() as session:
        await HalalWhitelist().sync_from_config(session, ["SOL"])

    fresh = HalalWhitelist()
    async with database.session() as session:
        await fresh.load_from_db(session)
    assert fresh.symbols == ["SOLUSDT"]


async def test_a_column_added_later_is_created_on_start(tmp_path):
    """create_all only adds tables, so new model columns are patched in."""
    import sqlite3

    from app.database.database import Database

    path = tmp_path / "old-schema.db"
    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    await database.create_all()
    await database.close()

    # simulate a database written before from_image existed
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE signals DROP COLUMN from_image")
    connection.commit()
    assert "from_image" not in {
        row[1] for row in connection.execute("PRAGMA table_info(signals)")
    }
    connection.close()

    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    await database.create_all()
    await database.close()

    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(signals)")}
    connection.close()
    assert "from_image" in columns


async def test_a_source_written_after_the_ticker_is_recorded(database):
    from app.config import Settings

    settings = Settings(
        _env_file=None,
        halal_coins="BTC:https://sharlife.my/crypto-shariah/crypto/bitcoin, eth/usdt, SOL:CryptoIslam 2026-03, BTC",
    )
    assert settings.halal_symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert settings.halal_sources == {
        "BTCUSDT": "https://sharlife.my/crypto-shariah/crypto/bitcoin",
        "ETHUSDT": None,
        "SOLUSDT": "CryptoIslam 2026-03",
    }

    wl = HalalWhitelist()
    async with database.session() as session:
        await wl.sync_from_config(session, settings.halal_symbols, sources=settings.halal_sources)
        recorded = await wl.sources(session)
        # A later config without the source keeps the one already stored.
        await wl.sync_from_config(session, ["BTC", "ETH"], sources={"BTCUSDT": None})
        kept = await wl.sources(session)

    assert recorded["BTCUSDT"].startswith("https://sharlife")
    assert recorded["ETHUSDT"] is None
    assert kept == {"BTCUSDT": "https://sharlife.my/crypto-shariah/crypto/bitcoin", "ETHUSDT": None}
