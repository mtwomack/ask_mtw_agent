"""
company_snapshot: one-shot company overview for adk web.

Fetches price, price history, profile, peers (with their latest prices) and news in
parallel, draws a price chart and returns a ready-made markdown card for the agent to display.

Peer prices come from Finnhub only (free tier: 60 calls/min). They never fall back to
Twelve Data, whose 8 calls/min would be used up by one snapshot's peers.

Chart placement: the tool HOLDS the chart. When the agent writes its final answer,
`attach_snapshot_charts` (after_model_callback) swaps the card's chart line for the image
itself, embedded in the markdown - so it appears inside the card, exactly where the line was.
`strip_inline_charts` (before_model_callback) removes those embedded images from the history
sent to the model on later turns, so they cost no tokens.

Needs matplotlib for the chart (uv add matplotlib); without it the card still works.
"""
import asyncio
import base64
import io
import re
from urllib.parse import quote as _urlquote
from collections import OrderedDict
from datetime import datetime

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.tools import ToolContext
from google.genai import types

from .company_tools import (
    StockLookupError, _history_rows, _quote_finnhub, _summarise, _symbol_for,
    get_company_news, get_company_peers, get_company_profile, get_stock_price,
)

PERIOD_LABELS = {"5d": "5-day", "1mo": "1-month", "3mo": "3-month", "6mo": "6-month",
                 "1y": "1-year", "2y": "2-year", "5y": "5-year", "ytd": "Year-to-date"}

# Charts waiting to be attached to the final answer, per invocation (turn).
_pending_charts: "OrderedDict[str, list[tuple[str, bytes]]]" = OrderedDict()
_MAX_PENDING_TURNS = 20

# Chart palette (light surface - readable in both adk web themes)
INK, MUTED, GRID, SURFACE = "#0b0b0b", "#898781", "#e1e0d9", "#fcfcfb"
LINE, UP, DOWN = "#2a78d6", "#0ca30c", "#d03b3b"


# ---------------------------------------------------------------------- helpers
def _arrow(value) -> str:
    try:
        return "▲" if float(str(value).rstrip("%")) >= 0 else "▼"
    except (TypeError, ValueError):
        return ""


def _md(text) -> str:
    """Escape characters that would break markdown links / tables."""
    return str(text or "").replace("|", "/").replace("[", "(").replace("]", ")").strip()


def _short_date(stamp: str | None) -> str:
    try:
        return datetime.strptime(stamp[:10], "%Y-%m-%d").strftime("%b %d").replace(" 0", " ")
    except (TypeError, ValueError):
        return ""


def _money(v) -> str:
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "—"


def _pct(v) -> str:
    return f"{_arrow(v)} {v:+.2f}%" if isinstance(v, (int, float)) else "—"


def _peer_quote(symbol: str) -> dict:
    """Latest price for one peer, Finnhub only; never raises (a failed peer shows '—')."""
    try:
        q = _quote_finnhub(symbol)
        return {"price": round(q["price"], 2), "change_pct": q.get("change_pct")}
    except StockLookupError:
        return {"price": None, "change_pct": None}


async def _peers_with_prices(symbol: str, limit: int = 5) -> dict:
    """get_company_peers, then each peer's latest price fetched in parallel."""
    peers = await asyncio.to_thread(get_company_peers, symbol, limit)
    if peers.get("status") == "success" and peers.get("peers"):
        quotes = await asyncio.gather(*(asyncio.to_thread(_peer_quote, p["symbol"])
                                        for p in peers["peers"]))
        for p, q in zip(peers["peers"], quotes):
            p.update(q)
    return peers


def _draw_chart(symbol: str, label: str, rows: list, summary: dict) -> bytes | None:
    """Returns PNG bytes, or None if matplotlib isn't installed."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    dates = [datetime.strptime(r["datetime"][:10], "%Y-%m-%d") for r in rows]
    closes = [float(r["close"]) for r in rows]
    up = closes[-1] >= closes[0]

    fig, ax = plt.subplots(figsize=(9, 3.6), dpi=100)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    ax.plot(dates, closes, color=LINE, linewidth=2, solid_joinstyle="round", solid_capstyle="round")
    spread = (max(closes) - min(closes)) or max(closes) * 0.02
    floor = min(closes) - spread * 0.16          # room for the "Low" label above the axis
    ax.fill_between(dates, closes, floor, color=LINE, alpha=0.10, linewidth=0)
    ax.set_ylim(floor, max(closes) + spread * 0.14)

    # end dot + last price
    ax.scatter([dates[-1]], [closes[-1]], s=40, color=LINE, edgecolor=SURFACE, linewidth=2, zorder=3)
    ax.annotate(f"${closes[-1]:,.2f}", (dates[-1], closes[-1]), xytext=(8, 0),
                textcoords="offset points", va="center", fontsize=9, color=INK, fontweight="bold")

    # high / low markers
    for key, va, dy in (("highest_close", "bottom", 6), ("lowest_close", "top", -6)):
        point = summary[key]
        d = datetime.strptime(point["date"], "%Y-%m-%d")
        if d in (dates[0], dates[-1]):            # already marked by the line ends
            continue
        ax.scatter([d], [point["price"]], s=14, color=MUTED, zorder=3)
        ax.annotate(f"{'High' if dy > 0 else 'Low'} ${point['price']:,.2f}", (d, point["price"]),
                    xytext=(0, dy), textcoords="offset points", ha="center", va=va,
                    fontsize=8, color=MUTED)

    # title: symbol, period and change (change coloured, with arrow so colour isn't the only cue)
    ax.set_title(f"{symbol}  ·  {label}", loc="left", fontsize=11, color=INK, fontweight="bold", pad=12)
    ax.text(1.0, 1.04, f"{'▲' if up else '▼'} {summary['change_percent']}", transform=ax.transAxes,
            ha="right", va="bottom", fontsize=11, fontweight="bold", color=UP if up else DOWN)

    # recessive axes
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=1)
    ax.tick_params(colors=MUTED, labelsize=8, length=0)
    ax.yaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")
    locator = mdates.AutoDateLocator(minticks=4, maxticks=7)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()


def _card(name, symbol, label, quote, summary, profile, peers, news, chart_note) -> str:
    """Builds the markdown card. Each section degrades gracefully if its data failed."""
    lines = []
    exchange = profile.get("exchange", "").split(" - ")[0] if profile.get("status") == "success" else ""
    title = f"{_md(name)} · {symbol}" if name and name.upper() != symbol.upper() else symbol
    lines.append(f"## {title}" + (f"  \n*{_md(exchange)}*" if exchange else ""))

    if quote.get("status") == "success":
        lines.append(f"**{_money(quote['price'])}**  {_arrow(quote.get('change_percent'))} "
                     f"**{quote.get('change'):+,.2f} ({quote.get('change_percent')})** today"
                     + (f" · as of {quote['as_of_utc']}" if quote.get("as_of_utc") else ""))
    else:
        lines.append(f"*Price unavailable: {_md(quote.get('message'))}*")

    cells = {}
    if summary:
        cells[f"{label} change"] = f"{_arrow(summary['change_percent'])} {summary['change_percent']}"
        cells[f"{label} range"] = (f"{_money(summary['lowest_close']['price'])} – "
                                   f"{_money(summary['highest_close']['price'])}")
    if profile.get("status") == "success":
        if profile.get("market_cap"):
            cells["Market cap"] = profile["market_cap"]
        if profile.get("industry"):
            cells["Industry"] = _md(profile["industry"])
        if profile.get("ipo_date"):
            cells["Listed since"] = profile["ipo_date"][:4]
    if cells:
        lines += ["", "| " + " | ".join(cells) + " |",
                  "|" + "|".join([":--"] * len(cells)) + "|",
                  "| " + " | ".join(cells.values()) + " |"]

    lines += ["", chart_note]

    if peers.get("status") == "success" and peers.get("peers"):
        lines += ["", "**Peers** *(click one for its snapshot)*", "", "| Company | Price | Today |", "|:--|--:|--:|"]
        for p in peers["peers"]:
            who = f"{_md(p['name'])} ({p['symbol']})" if p["name"] != p["symbol"] else p["symbol"]
            # "#ask=..." link: the chat page sends it as a new message -> a snapshot of that peer
            who = f"[{who}](#ask={_urlquote('Give me a snapshot of ' + p['symbol'])})"
            lines.append(f"| {who} | {_money(p.get('price'))} | {_pct(p.get('change_pct'))} |")
        if all(p.get("price") is None for p in peers["peers"]):
            lines.append("*Peer prices unavailable right now.*")

    lines += ["", "**Latest news**" + ("  *(from web search)*" if news.get("fallback") else "")]
    if news.get("status") == "success" and news.get("articles"):
        for i, a in enumerate(news["articles"], 1):
            title = _md(a["headline"])
            link = f"[{title}]({a['url']})" if a.get("url") else title
            lines.append(f"{i}. {link} — {_md(a.get('source'))}, {_short_date(a.get('published'))}")
    else:
        lines.append(f"*{_md(news.get('message') or 'No recent news.')}*")

    if profile.get("website"):
        lines += ["", f"[{profile['website']}]({profile['website']})"]
    lines += ["", "*Quotes may be delayed · Data: Finnhub, Twelve Data · Not investment advice.*"]
    return "\n".join(lines)


# ---------------------------------------------------------------------- tool
async def company_snapshot(company: str, tool_context: ToolContext, period: str = "6mo") -> dict:
    """Builds a one-page snapshot of a company: current price, a price chart, key numbers
    (market cap, industry, range), competitors with their latest prices and the latest news. Use this when the user
    asks for an overview, a snapshot, or "tell me about <company>".

    Args:
        company: company name ("Nvidia") or ticker ("NVDA").
        period: chart range: "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y" or "ytd". Default "6mo".
    """
    company = (company or "").strip()
    if not company:
        return {"status": "error", "message": "Tell me which company or ticker."}
    period = (period or "6mo").strip().lower()
    label = PERIOD_LABELS.get(period, period)

    try:
        symbol, name = await asyncio.to_thread(_symbol_for, company)
    except StockLookupError as e:
        return {"status": "error", "message": str(e)}

    async def history():
        try:
            _, _, _, rows = await asyncio.to_thread(_history_rows, symbol, period)
            return rows
        except StockLookupError as e:
            return e

    quote, rows, profile, peers, news = await asyncio.gather(
        asyncio.to_thread(get_stock_price, symbol),
        history(),
        asyncio.to_thread(get_company_profile, symbol),
        _peers_with_prices(symbol, 5),
        asyncio.to_thread(get_company_news, symbol, 7, 5),
    )

    if profile.get("status") == "success":
        name = profile["company"]
    elif quote.get("status") == "success":
        name = quote.get("company") or name

    summary, chart_file = None, None
    if isinstance(rows, list):
        summary = _summarise(rows)
        png = await asyncio.to_thread(_draw_chart, symbol, label, rows, summary)
        if png is None:
            chart_note = "*Chart unavailable: install matplotlib (`uv add matplotlib`).*"
        else:
            chart_file = f"{symbol}_{period}.png"
            _pending_charts.setdefault(tool_context.invocation_id, []).append((chart_file, png))
            while len(_pending_charts) > _MAX_PENDING_TURNS:    # drop turns that never finished
                _pending_charts.popitem(last=False)
            chart_note = f"**{label} chart:** `{chart_file}` (image attached)"
    else:
        chart_note = f"*{label} history unavailable: {_md(str(rows))}*"

    failed: dict[str, list[str]] = {}          # reason -> sections, so one reason is shown once
    for section, r in (("Profile", profile), ("Peers", peers)):
        if r.get("status") != "success":
            failed.setdefault(_md(r.get("message")), []).append(section)
    card = _card(name, symbol, label, quote, summary, profile, peers, news, chart_note)
    if failed:
        note = "  \n".join(f"*{' and '.join(sections)} unavailable - {reason}*"
                           for reason, sections in failed.items())
        card = card.replace("\n\n*Quotes may be delayed", f"\n\n{note}\n\n*Quotes may be delayed", 1)
    return {
        "status": "success",
        "display_markdown": card,
        "chart_artifact": chart_file,
        "facts": {   # compact numbers so the agent can answer follow-up questions
            "symbol": symbol, "company": name,
            "price": quote.get("price"), "change_percent_today": quote.get("change_percent"),
            "period": label, **({"period_change": summary["change_percent"]} if summary else {}),
            "peers": [{"symbol": p["symbol"], "price": p.get("price"),
                       "change_percent_today": p.get("change_pct")}
                      for p in peers.get("peers") or []],
        },
    }


# ---------------------------------------------------------------------- callback
_CHART_LINE = re.compile(r"^\*\*[^*\n]*chart:\*\* `(?P<file>[^`]+\.png)`.*$", re.M)
_INLINE_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]\(data:image/[^)]+\)")


def _image_markdown(filename: str, png: bytes) -> str:
    symbol, _, period = filename.rsplit(".", 1)[0].partition("_")
    alt = f"{symbol} {PERIOD_LABELS.get(period, period)} price chart"
    return f"![{alt}](data:image/png;base64,{base64.b64encode(png).decode('ascii')})"


async def attach_snapshot_charts(callback_context: CallbackContext,
                                 llm_response: LlmResponse) -> LlmResponse | None:
    """after_model_callback: puts held charts inside the final answer, replacing the card's
    chart line (or appending the image if the model dropped that line)."""
    if getattr(llm_response, "partial", False):
        return None                                  # wait for the complete response
    parts = (llm_response.content.parts if llm_response.content else None) or []
    if any(getattr(p, "function_call", None) for p in parts):
        return None                                  # still calling tools - not the answer yet
    charts = dict(_pending_charts.pop(callback_context.invocation_id, None) or [])
    text_parts = [p for p in parts if p.text]
    if not charts or not text_parts:
        return None

    def swap(match: re.Match) -> str:
        png = charts.pop(match.group("file"), None)
        return _image_markdown(match.group("file"), png) if png else match.group(0)

    for p in text_parts:
        p.text = _CHART_LINE.sub(swap, p.text)
    for filename, png in charts.items():             # chart line missing: add image at the end
        text_parts[-1].text += "\n\n" + _image_markdown(filename, png)
    return llm_response


async def strip_inline_charts(callback_context: CallbackContext,
                              llm_request: LlmRequest) -> None:
    """before_model_callback: replaces embedded chart images in the conversation history with
    a short note, so earlier snapshots don't resend ~50 KB of image data every turn."""
    for content in llm_request.contents or []:
        for p in content.parts or []:
            if p.text and "data:image/" in p.text:
                p.text = _INLINE_IMAGE.sub(lambda m: f"[{m.group('alt')} shown to the user]", p.text)
    return None
