"""One-off helper: create the TELEGRAM_SESSION string.

    python scripts/login.py           code is sent to the Telegram app
    python scripts/login.py --sms     ask Telegram to resend it as an SMS
    python scripts/login.py --qr      log in by scanning a QR code (no code at all)

The login code normally arrives in the Telegram app itself (the official
"Telegram" chat, id 777000), not by SMS.  If no code ever shows up, --qr is the
reliable way in: it is the same flow as "Link Desktop Device".

Prints a StringSession at the end.  Paste it into .env as TELEGRAM_SESSION and
treat it like a password: it is full access to your Telegram account.
"""

from __future__ import annotations

import asyncio
import os
import sys
from getpass import getpass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import TelegramClient  # noqa: E402
from telethon.errors import (  # noqa: E402
    ApiIdInvalidError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession  # noqa: E402


def ask(name: str) -> str:
    value = os.getenv(name)
    if value:
        print(f"{name}: taken from the environment")
        return value
    return input(f"{name}: ").strip()


async def code_login(client: TelegramClient, force_sms: bool) -> bool:
    """Classic phone + login code flow."""
    phone = input("Phone number (international, e.g. +998901234567): ").strip()
    phone = phone.replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = "+" + phone

    try:
        sent = await client.send_code_request(phone, force_sms=force_sms)
    except PhoneNumberInvalidError:
        print("That phone number is not valid. Use the full international form.")
        return False
    except ApiIdInvalidError:
        print("api_id / api_hash do not match. Check them on my.telegram.org.")
        return False
    except FloodWaitError as exc:
        print(f"Telegram is rate limiting this number. Wait {exc.seconds}s and retry.")
        return False

    where = type(sent.type).__name__.replace("SentCodeType", "")
    print()
    print(f"Code sent via: {where}")
    if where.lower().startswith("app"):
        print('Look in the Telegram app -> the official "Telegram" chat (777000).')
        print("No code at all? Press Ctrl+C and use:  scripts/login.py --qr")
    print()

    code = input("Enter the 5-digit code (numbers only): ").strip().replace(" ", "")
    if not code.isdigit():
        print(f"{code!r} is not a login code - it must be the digits Telegram sent you.")
        return False

    try:
        await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        await client.sign_in(password=getpass("Two-step verification password: "))
    except PhoneCodeInvalidError:
        print("Wrong code. Type the digits by hand - a copied code often has spaces.")
        return False
    except PhoneCodeExpiredError:
        print("That code expired. Run the script again for a fresh one.")
        return False
    return True


def print_qr(url: str) -> bool:
    """Draw the QR in the terminal (light background, so a camera can read it)."""
    import qrcode  # type: ignore

    code = qrcode.QRCode(border=2)
    code.add_data(url)

    # The Windows console is often not UTF-8; block characters would explode.
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "█".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        print("This terminal cannot draw the QR code (encoding: " + str(encoding) + ").")
        print("Run  chcp 65001  in PowerShell, then start this script again.")
        return False

    lines = [
        "".join("  " if cell else "██" for cell in row)
        for row in code.get_matrix()
    ]
    print("\n".join(lines))
    return True


async def qr_login(client: TelegramClient) -> bool:
    """Scan-to-login, exactly like linking Telegram Desktop."""
    try:
        import qrcode  # noqa: F401
    except ImportError:
        print("The QR flow needs one small package:")
        print("    .venv/Scripts/python.exe -m pip install qrcode")
        return False

    print()
    print("On your phone:  Telegram -> Settings -> Devices -> Link Desktop Device")
    print("Then point the camera at the code below.")
    print()

    qr = await client.qr_login()
    while True:
        if not print_qr(qr.url):
            return False
        print("Waiting for the scan... (the code refreshes automatically)")
        try:
            await qr.wait(60)
            return True
        except asyncio.TimeoutError:
            print("Code expired, showing a new one.")
            await qr.recreate()
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass("Two-step verification password: "))
            return True


async def main(mode: str) -> int:
    api_id = ask("TELEGRAM_API_ID")
    api_hash = ask("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        print("api_id / api_hash are required - get them from https://my.telegram.org")
        return 1

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.connect()
    try:
        if mode == "qr":
            authorised = await qr_login(client)
        else:
            authorised = await code_login(client, force_sms=(mode == "sms"))
        if not authorised:
            return 1

        me = await client.get_me()
        print()
        print(f"logged in as: {getattr(me, 'username', None) or getattr(me, 'id', '?')}")
        print()
        print("TELEGRAM_SESSION=" + client.session.save())
        print()
        print("Copy the line above into your .env file. Do not share it.")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        # Load .env if python-dotenv happens to be available; not required.
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
    except Exception:
        pass
    chosen = "qr" if "--qr" in sys.argv else "sms" if "--sms" in sys.argv else "code"
    sys.exit(asyncio.run(main(chosen)))
