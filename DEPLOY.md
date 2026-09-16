# Running SignalForge on a server

The bot has to stay awake: it holds a Telegram session, polls prices every few
seconds, and has to answer your Confirm button within `SIGNAL_EXPIRY_MINUTES`.
Anything that sleeps, hibernates or resets its disk will lose signals.

## What actually works for free

| Option | Free? | Always on? | Verdict |
|---|---|---|---|
| **Oracle Cloud Always Free** (ARM VM, 4 cores / 24 GB) | permanently free | yes | **best free choice** |
| Google Cloud `e2-micro` (1 shared vCPU / 1 GB, us-central1) | permanently free | yes | works, use the SQLite setup |
| Render / Railway / Koyeb free tiers | free tier | **no** — they sleep or the credit expires | not usable |
| Replit, Glitch | free tier | **no** — always-on is paid | not usable |
| GitHub Actions, Codespaces | free minutes | **no** — jobs are capped | not usable |
| A small VPS (Hetzner, DigitalOcean) | ~$4-5/mo | yes | simplest if you can pay |

Oracle and Google both ask for a credit card to verify identity. They do not
charge for the always-free instances, but the card is unavoidable — there is
no serious always-on host without it.

Resource use, measured: about **175 MB** of RAM idle with the OCR model
loaded, peaking around 400 MB while reading a chart image. Any 1 GB instance
is enough.

## Setup (Ubuntu 22.04/24.04, any provider)

Create the VM, then:

```bash
sudo apt update && sudo apt install -y python3-venv git libgl1 libglib2.0-0
sudo useradd --system --create-home --home-dir /opt/signalforge signalforge
sudo mkdir -p /opt/signalforge && sudo chown signalforge:signalforge /opt/signalforge
```

Copy the project up (from your PC, in the project folder):

```powershell
scp -r app scripts requirements.txt .env ubuntu@SERVER_IP:/tmp/signalforge/
```

Then on the server:

```bash
sudo mv /tmp/signalforge/* /opt/signalforge/
sudo chown -R signalforge:signalforge /opt/signalforge
sudo chmod 600 /opt/signalforge/.env        # it holds your Telegram session
sudo -u signalforge python3 -m venv /opt/signalforge/.venv
sudo -u signalforge /opt/signalforge/.venv/bin/pip install -r /opt/signalforge/requirements.txt
```

Point the database at a file inside the app directory:

```bash
sudo -u signalforge sed -i 's|^DATABASE_URL=.*|DATABASE_URL=sqlite+aiosqlite:///./signalforge.db|' /opt/signalforge/.env
```

Check everything before starting it for real:

```bash
cd /opt/signalforge && sudo -u signalforge .venv/bin/python -m app.main check
```

`RESULT: ready` means the server can reach Telegram and MEXC. Now install the
service:

```bash
sudo cp /opt/signalforge/deploy/signalforge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now signalforge
```

Watch it:

```bash
journalctl -u signalforge -f          # live log
systemctl status signalforge          # is it running
sudo systemctl restart signalforge    # after changing .env
```

That is it - the bot now survives reboots and crashes, and your PC can be off.

## Windows Server

The bot runs as a scheduled task configured to behave like a service: it
starts with Windows before anyone logs in, restarts itself if it dies, and has
no execution time limit (the default would kill it after three days).

Short path - unzip the project to `C:\SignalForge`, copy `.env` next to it,
then in an **Administrator** PowerShell:

```powershell
cd C:\SignalForge
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\setup-server.ps1
```

`setup-server.ps1` checks disk, RAM, the clock and the network, installs
Python 3.12 and the Visual C++ runtime if they are missing, builds the
virtualenv, runs `python -m app.main check` and registers the task. It can be
run again at any time; add `-NoService` to stop after the check.

The same by hand, once Python 3.12 is installed:

```powershell
cd C:\SignalForge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# admin PowerShell - runs the pre-flight check, then installs the task
.\deploy\windows\install-service.ps1
```

Day to day:

```powershell
.\deploy\windows\install-service.ps1 -Status     # state + last 20 log lines
Get-Content logs\bot.log -Tail 50 -Wait            # live log
Stop-ScheduledTask  -TaskName SignalForge
Start-ScheduledTask -TaskName SignalForge          # after editing .env
.\deploy\windows\install-service.ps1 -Uninstall
```

Put the project somewhere like `C:\SignalForge` rather than under a user
profile: the task runs as SYSTEM, and profile folders can be redirected or
locked down.

## With Docker instead

If you would rather not manage a virtualenv:

```bash
docker compose -f docker-compose.lite.yml up -d     # bot + SQLite, ~1 GB RAM
docker compose up -d                                # bot + PostgreSQL, needs ~2 GB
docker compose logs -f bot
```

Both read `.env` from the project directory. The lite file keeps the database
in a named volume, so `docker compose down` does not lose your trade history.

## Two things people get wrong

**The Telegram session is single-use per device in practice.** Copying `.env`
to the server means the same session string runs in two places if you also
start the bot on your PC. Run it in one place only, or generate a second
session with `scripts/login.py` for the server.

**Do not commit `.env`.** It is git-ignored here. On the server keep it at
`chmod 600` - that one file is full access to your Telegram account and, once
you go LIVE, to your MEXC trading permissions.
