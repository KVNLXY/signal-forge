"""Backtest of a Telegram group's chart signals, step 3 of 3.

    python scripts/backtest/collect.py <out_dir>            read the signals (OCR)
    python scripts/backtest/run.py     <out_dir>            replay them on MEXC candles
    python scripts/backtest/report.py  <out_dir> <out.pdf>  write the PDF

Group id, topics and the start date are constants at the top of collect.py.
"""


import collections
import json
import pathlib
import sys
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402
from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate,  # noqa: E402
                                Spacer, Table, TableStyle)

OUT_DIR = pathlib.Path(sys.argv[1])
PDF = pathlib.Path(sys.argv[2])
data = json.loads((OUT_DIR / "results.json").read_text(encoding="utf-8"))
rows = data["results"]
NOT_ENTERED_LABELS = {'fetch_error': "MEXC'da yo'q", 'expired_no_pullback': '24 soatda tushib kelmadi', 'expired_no_breakout': "15 daqiqada ko'tarilmadi", 'cancelled_sl_first': "to'lgunicha stop'dan o'tdi", 'no_data': "narx tarixi yo'q"}

pdfmetrics.registerFont(TTFont("Arial", r"C:\Windows\Fonts\arial.ttf"))
pdfmetrics.registerFont(TTFont("Arial-Bold", r"C:\Windows\Fonts\arialbd.ttf"))
styles = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=styles["Heading1"], fontName="Arial-Bold", fontSize=18, spaceAfter=6)
H2 = ParagraphStyle("h2", parent=styles["Heading2"], fontName="Arial-Bold", fontSize=13, spaceBefore=10, spaceAfter=4)
P = ParagraphStyle("p", parent=styles["BodyText"], fontName="Arial", fontSize=9.5, leading=13)
SMALL = ParagraphStyle("s", parent=P, fontSize=8, leading=10.5, textColor=colors.HexColor("#444444"))
CELL = ParagraphStyle("c", parent=P, fontSize=8, leading=10)


def money(x):
    return "-" if x is None else f"{x:+.2f} $"


def pct(x):
    return "-" if x is None else f"{x:.1f}%"


def tstr(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d.%m %H:%M") if ms else "-"


# ------------------------------------------------------------------ statistics
def stats_for(key: str, subset):
    sim = [r for r in subset if r[key]["status"] in ("closed", "open")]
    closed = [r for r in sim if r[key]["status"] == "closed"]
    opened = [r for r in sim if r[key]["status"] == "open"]
    wins = [r for r in closed if r[key]["realized_pnl"] > 0]
    losses = [r for r in closed if r[key]["realized_pnl"] <= 0]
    realized = sum(r[key]["realized_pnl"] for r in closed)
    marked = sum(r[key]["marked_pnl"] for r in sim)
    gross_win = sum(r[key]["realized_pnl"] for r in wins)
    gross_loss = -sum(r[key]["realized_pnl"] for r in losses)
    # equity curve on realized, in close order
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in sorted(closed, key=lambda r: r[key]["close_time"] or 0):
        eq += r[key]["realized_pnl"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    reasons = collections.Counter(r[key]["close_reason"] for r in closed)
    not_entered = collections.Counter(r[key]["status"] for r in subset if r[key]["status"] not in ("closed", "open"))
    return {
        "signals": len(subset), "entered": len(sim), "closed": len(closed), "open": len(opened),
        "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / len(closed) * 100) if closed else None,
        "realized": realized, "marked": marked,
        "avg_win": gross_win / len(wins) if wins else None,
        "avg_loss": -gross_loss / len(losses) if losses else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss else None,
        "max_dd": dd, "reasons": reasons, "not_entered": not_entered,
        "invested": AMOUNT_TOTAL(sim),
    }


def AMOUNT_TOTAL(sim):
    return len(sim) * data["amount"]


bot_all = stats_for("bot", rows)
bot_wl = stats_for("bot", [r for r in rows if r["whitelisted"]])
tp1_all = stats_for("tp1_all", rows)
nobe_all = stats_for("no_breakeven", rows)

# ------------------------------------------------------------------ charts
closed_sorted = sorted([r for r in rows if r["bot"]["status"] == "closed"],
                       key=lambda r: r["bot"]["close_time"])
eq_x, eq_y, eq = [], [], 0.0
for r in closed_sorted:
    eq += r["bot"]["realized_pnl"]
    eq_x.append(datetime.fromtimestamp(r["bot"]["close_time"] / 1000, tz=timezone.utc))
    eq_y.append(eq)
fig, ax = plt.subplots(figsize=(7.2, 3.0), dpi=150)
if eq_x:
    ax.plot(eq_x, eq_y, color="#1f77b4", linewidth=1.8)
    ax.fill_between(eq_x, eq_y, 0, where=[y >= 0 for y in eq_y], color="#1f77b4", alpha=0.12)
    ax.fill_between(eq_x, eq_y, 0, where=[y < 0 for y in eq_y], color="#d62728", alpha=0.12)
ax.axhline(0, color="#888888", linewidth=0.8)
ax.set_title("Yig'ilgan foyda/zarar, $ (har savdo 100 $, bot qoidalari)", fontsize=9)
ax.tick_params(labelsize=7)
ax.grid(alpha=0.3)
fig.autofmt_xdate()
fig.tight_layout()
chart1 = OUT_DIR / "equity.png"
fig.savefig(chart1)
plt.close(fig)

# monthly bars
monthly = collections.defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
for r in rows:
    b = r["bot"]
    if b["status"] != "closed":
        continue
    m = r["date"][:7]
    monthly[m]["n"] += 1
    monthly[m]["pnl"] += b["realized_pnl"]
    monthly[m]["wins"] += 1 if b["realized_pnl"] > 0 else 0
months = sorted(monthly)
fig, ax = plt.subplots(figsize=(7.2, 2.6), dpi=150)
vals = [monthly[m]["pnl"] for m in months]
ax.bar(months, vals, color=["#2ca02c" if v >= 0 else "#d62728" for v in vals])
ax.axhline(0, color="#888888", linewidth=0.8)
ax.set_title("Oylik natija, $ (signal sanasi bo'yicha)", fontsize=9)
ax.tick_params(labelsize=7)
ax.grid(axis="y", alpha=0.3)
fig.tight_layout()
chart2 = OUT_DIR / "monthly.png"
fig.savefig(chart2)
plt.close(fig)

# ------------------------------------------------------------------ document
doc = SimpleDocTemplate(str(PDF), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                        topMargin=14 * mm, bottomMargin=14 * mm,
                        title="PRO VIP SIGNAL - 2026 backtest", author="SignalForge")
story = []
gen = datetime.fromisoformat(data["generated"]).strftime("%d.%m.%Y %H:%M UTC")
st = data["stats"]

story.append(Paragraph("PRO VIP SIGNAL — 2026-yil signallari: backtest hisoboti", H1))
story.append(Paragraph(f"Tayyorlandi: {gen}. Davr: 01.01.2026 — bugun. Manba: guruhning "
                       f"«SIGNAL ORTA VA QISQA VAHTGA» va «SCALP SIGNAL» topic'laridagi admin postlari.", SMALL))
story.append(Spacer(1, 6))

# ---- summary box
verdict_pnl = bot_all["realized"]
verdict = "FOYDA" if verdict_pnl > 0 else "ZARAR"
summary = [
    ["Ko'rsatkich", "Qiymat"],
    ["Admin joylagan chart-postlar", f"{st['admin_photo_posts']} ta (shundan {st['result_updates']} ta natija-xabar)"],
    ["OCR o'qiy olgan signallar", f"{st['readable']} ta ({st['unreadable']} ta o'qilmadi)"],
    ["Takroriy setup'lar olib tashlangach", f"{data['unique']} ta noyob signal"],
    ["Savdoga kirilgan (entry'ga kelgan)", f"{bot_all['entered']} ta — {bot_all['closed']} ta yopilgan, {bot_all['open']} ta hali ochiq"],
    ["Kirilmagan", ", ".join(f"{NOT_ENTERED_LABELS.get(k, k)}: {v}" for k, v in bot_all["not_entered"].items()) or "-"],
    ["Yutgan / yutqazgan", f"{bot_all['wins']} / {bot_all['losses']}  (win rate {pct(bot_all['win_rate'])})"],
    ["O'rtacha yutuq / o'rtacha zarar", f"{money(bot_all['avg_win'])} / {money(bot_all['avg_loss'])}"],
    ["Profit factor", f"{bot_all['profit_factor']:.2f}" if bot_all["profit_factor"] else "-"],
    ["Eng katta pasayish (drawdown)", money(bot_all["max_dd"])],
    ["Ochiq pozitsiyalar bugungi narxda", money(bot_all["marked"] - bot_all["realized"]) if bot_all["open"] else "-"],
    ["YOPILGAN SAVDOLAR JAMI", f"{money(bot_all['realized'])}   →  {verdict}"],
    ["Ochiqlar bilan birga", money(bot_all["marked"])],
    ["Sarflangan kapital (100 $ x savdolar)", f"{bot_all['invested']:.0f} $  → daromadlilik {bot_all['marked'] / bot_all['invested'] * 100 if bot_all['invested'] else 0:+.1f}%"],
]
t = Table([[Paragraph(a, CELL), Paragraph(b, CELL)] for a, b in summary], colWidths=[70 * mm, 105 * mm])
t.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3b5c")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTNAME", (0, 0), (-1, 0), "Arial-Bold"),
    ("BACKGROUND", (0, 11), (-1, 11), colors.HexColor("#e8f5e9") if verdict_pnl > 0 else colors.HexColor("#fdecea")),
    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb")),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
]))
story.append(t)
story.append(Spacer(1, 8))
story.append(Image(str(chart1), width=178 * mm, height=74 * mm))
story.append(Spacer(1, 4))
story.append(Image(str(chart2), width=178 * mm, height=64 * mm))

# ---- strategy comparison
story.append(PageBreak())
story.append(Paragraph("Strategiyalar taqqoslash (bir xil signallar)", H2))
story.append(Paragraph(
    "<b>Bot qoidalari</b>: TP 30/30/40, TP1'dan keyin stop entry'ga ko'tariladi. "
    "<b>Faqat TP1</b>: butun pozitsiya birinchi TP'da sotiladi. "
    "<b>Stop ko'tarilmaydi</b>: TP 30/30/40, stop joyida qoladi.", P))
comp = [["Strategiya", "Yopilgan", "Yutgan", "Win rate", "Jami PNL", "O'rt. yutuq", "O'rt. zarar", "Profit factor", "Max DD"]]
for name, s_ in (("Bot qoidalari", bot_all), ("Faqat TP1", tp1_all), ("Stop ko'tarilmaydi", nobe_all)):
    comp.append([name, s_["closed"], s_["wins"], pct(s_["win_rate"]), money(s_["realized"]),
                 money(s_["avg_win"]), money(s_["avg_loss"]),
                 f"{s_['profit_factor']:.2f}" if s_["profit_factor"] else "-", money(s_["max_dd"])])
t = Table(comp, repeatRows=1)
t.setStyle(TableStyle([
    ("FONTNAME", (0, 0), (-1, -1), "Arial"), ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dddddd")),
    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb")),
]))
story.append(t)

story.append(Paragraph("Halal whitelist'dagi coinlar bo'yicha (bot aslida savdo qiladiganlari)", H2))
wl = [["", "Signallar", "Kirilgan", "Yopilgan", "Win rate", "Jami PNL (yopilgan)", "Ochiqlar bilan"],
      ["Barcha coinlar", bot_all["signals"], bot_all["entered"], bot_all["closed"], pct(bot_all["win_rate"]), money(bot_all["realized"]), money(bot_all["marked"])],
      ["Faqat whitelist", bot_wl["signals"], bot_wl["entered"], bot_wl["closed"], pct(bot_wl["win_rate"]), money(bot_wl["realized"]), money(bot_wl["marked"])]]
t = Table(wl)
t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), "Arial"), ("FONTSIZE", (0, 0), (-1, -1), 8),
                       ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dddddd")),
                       ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb"))]))
story.append(t)

# ---- monthly
story.append(Paragraph("Oylar bo'yicha (bot qoidalari, yopilgan savdolar)", H2))
mt = [["Oy", "Savdolar", "Yutgan", "Win rate", "PNL"]]
for m in months:
    d = monthly[m]
    mt.append([m, d["n"], d["wins"], pct(d["wins"] / d["n"] * 100 if d["n"] else None), money(d["pnl"])])
t = Table(mt)
t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), "Arial"), ("FONTSIZE", (0, 0), (-1, -1), 8),
                       ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dddddd")),
                       ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb"))]))
story.append(t)

# ---- per coin
story.append(Paragraph("Coinlar bo'yicha (bot qoidalari)", H2))
per_coin = collections.defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
for r in rows:
    b = r["bot"]
    if b["status"] != "closed":
        continue
    per_coin[r["symbol"]]["n"] += 1
    per_coin[r["symbol"]]["pnl"] += b["realized_pnl"]
    per_coin[r["symbol"]]["wins"] += 1 if b["realized_pnl"] > 0 else 0
ct = [["Coin", "Savdolar", "Yutgan", "PNL"]]
for sym, d in sorted(per_coin.items(), key=lambda kv: -kv[1]["pnl"]):
    ct.append([sym, d["n"], d["wins"], money(d["pnl"])])
t = Table(ct, repeatRows=1)
t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), "Arial"), ("FONTSIZE", (0, 0), (-1, -1), 8),
                       ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dddddd")),
                       ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb"))]))
story.append(t)

# ---- exit reasons
story.append(Paragraph("Yopilish sabablari", H2))
story.append(Paragraph(", ".join(f"<b>{k}</b>: {v}" for k, v in sorted(bot_all["reasons"].items())) +
                       ". (BE = TP1'dan keyin entry'ga ko'tarilgan stop, ya'ni zararsiz chiqish.)", P))

# ---- method
story.append(Paragraph("Metodika va cheklovlar", H2))
method = [
    "Signallar guruhning ikkita signal-topic'idan olindi; faqat adminning (anonim) chart-postlari, a'zolar postlari emas. "
    "Natija-xabarlar («12%✅», «1tp urdi», «stop kotarib») hisobga olinmadi; 7 kun ichida bir xil entry/SL li takroriy chartlar bitta deb olindi.",
    "Entry, SL va TP darajalari rasmdan OCR bilan o'qildi (bot ishlatadigan o'sha reader). O'qilmagan chartlar (sariq entry yorlig'i yo'q, coin nomi yo'q va h.k.) tahlildan chiqib qoldi — "
    f"ular {st['unreadable']} ta. Bu tanlanma xatoligi (selection bias) bo'lishi mumkin: o'qilmaganlar orasida ham yutgan, ham yutqazgan signallar bor.",
    "Narxlar: MEXC spot, 15 daqiqalik shamlar, signal vaqtidan boshlab 45 kungacha. 45 kunda yopilmagan pozitsiya «ochiq» deb bugungi narxda baholandi.",
    "Kirish: bot qoidalari — narx entry'dan yuqorida bo'lsa 24 soatgacha tushib kelishini kutish (limit), entry'ga 0.3% chegara; pastda bo'lsa 15 daqiqa ichida ko'tarilishi. "
    "Limit to'lgunicha narx stop'dan o'tib ketsa — signal bekor.",
    "Chiqish: TP 30/30/40 (oxirgi TP qolgan hammasini oladi), TP1'dan keyin stop entry'ga ko'tariladi. Bitta shamda ham SL, ham TP tegsa — pessimistik: SL deb hisoblandi.",
    "Har savdoga 100 $, komissiya 0.05% har tomonga. Slippage hisobga olinmadi. Adminning qo'lda boshqaruvi («stopni ko'taring», «50% soting», «yopib turamiz») modellashtirilmadi — "
    "hisobot aynan bot qoidalari bilan avtomatik savdo qilinganda nima bo'lishini ko'rsatadi.",
    "Bu tarixiy simulyatsiya; kelajak natijasini kafolatlamaydi.",
]
for line in method:
    story.append(Paragraph("• " + line, SMALL))

# ---- trade list
story.append(PageBreak())
story.append(Paragraph("Barcha signallar (bot qoidalari)", H2))
lt = [["Sana", "Coin", "Entry", "SL", "TP1", "SL%", "TP1%", "Holat", "Sabab", "PNL"]]
for r in sorted(rows, key=lambda r: r["date"]):
    b = r["bot"]
    status = {"closed": "yopildi", "open": "ochiq", "expired_no_pullback": "kelmadi",
              "expired_no_breakout": "kelmadi", "cancelled_sl_first": "SL oldin", "no_data": "narx yo'q",
              "fetch_error": "xato"}.get(b["status"], b["status"])
    pnl = b.get("realized_pnl") if b["status"] == "closed" else (b.get("marked_pnl") if b["status"] == "open" else None)
    lt.append([r["date"][5:16].replace("T", " "), r["symbol"].replace("USDT", ""),
               f"{r['entry']:g}", f"{r['sl']:g}", f"{r['tps'][0]:g}" if r["tps"] else "-",
               f"{r['sl_pct']:.1f}", f"{r['tp1_pct']:.1f}" if r["tp1_pct"] is not None else "-",
               status, b.get("close_reason") or "-", money(pnl) if pnl is not None else "-"])
t = Table(lt, repeatRows=1, colWidths=[22 * mm, 16 * mm, 20 * mm, 20 * mm, 20 * mm, 12 * mm, 12 * mm, 17 * mm, 13 * mm, 20 * mm])
style = [("FONTNAME", (0, 0), (-1, -1), "Arial"), ("FONTSIZE", (0, 0), (-1, -1), 7),
         ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dddddd")),
         ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc"))]
for i, r in enumerate(sorted(rows, key=lambda r: r["date"]), start=1):
    b = r["bot"]
    if b["status"] == "closed":
        style.append(("BACKGROUND", (9, i), (9, i),
                      colors.HexColor("#e8f5e9") if b["realized_pnl"] > 0 else colors.HexColor("#fdecea")))
t.setStyle(TableStyle(style))
story.append(t)

doc.build(story)
print("PDF:", PDF, PDF.stat().st_size, "bytes")
print(json.dumps({k: v for k, v in bot_all.items() if k not in ("reasons", "not_entered")}, default=str))
print("reasons:", dict(bot_all["reasons"]), "not_entered:", dict(bot_all["not_entered"]))
print("tp1_all:", tp1_all["realized"], "no_breakeven:", nobe_all["realized"], "whitelist:", bot_wl["realized"])
