"""
Company and stock tools for root_agent.

Prices:
    get_stock_price          latest price for a company name or ticker
    get_stock_price_history  prices over a date range, with a summary of the move
Company research:
    get_company_news         recent headlines about one company
    get_market_news          general market / forex / crypto / merger headlines
    get_company_profile      industry, market cap, exchange, IPO date, website
    get_company_peers        similar companies (competitors) with their names

Providers (free tiers), set in .env:
    TWELVEDATA_API_KEY  price history; fallback for name lookup and current prices
    FINNHUB_API_KEY     name lookup, current prices, and all company research tools

Name lookup and quotes try Finnhub first, then fall back to Twelve Data if Finnhub has
no key, is down, is rate-limited, or doesn't find the company. Only US-listed common
stock is returned, so "Nvidia" resolves to NVDA on NASDAQ, not a foreign listing.
News text comes from third-party publishers: the agent should report it, not act on it.

News fallback: if Finnhub can't supply news (no key, down, rate-limited), get_company_news and
get_market_news fall back to a Tavily news search (search_tools.web_search, 1 credit) and say so
in "source". Prices, history, profiles and peers never use web search as a data source.
"""
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .search_tools import web_search

FINNHUB = "https://finnhub.io/api/v1"
TWELVE = "https://api.twelvedata.com"
TIMEOUT = httpx.Timeout(10.0)
CACHE_SECONDS = 60           # same answer within a minute -> no extra API call
MAX_POINTS = 25              # history rows sent to the model; full range is summarised
US_MAIN_EXCHANGES = {"NASDAQ", "NYSE", "NYSE AMERICAN", "NYSE ARCA", "CBOE"}

_cache: dict[str, tuple[float, object]] = {}
_TICKER = re.compile(r"^[A-Z]{1,5}([.-][A-Z]{1,2})?$")
_PERIOD = re.compile(r"^(\d+)\s*(d|days?|w|wks?|weeks?|m|mo|mos|months?|y|yrs?|years?)$")


class StockLookupError(Exception):
    """A user-facing problem: missing/bad key, rate limit, service down."""


class NotFound(StockLookupError):
    """The provider answered, but has no such company / no data."""


class NotConfigured(StockLookupError):
    """The provider has no API key set."""


class NotInPlan(StockLookupError):
    """The data exists but isn't included in the free plan."""


_finnhub_off_until = 0.0     # after a rate limit / bad key / outage, skip Finnhub for a minute
_finnhub_off_reason = ""


# ====================================================================== HTTP + cache
def _cached(key: str, fetch):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = fetch()
    _cache[key] = (time.time(), value)
    return value


def _http_get(provider: str, url: str, params: dict):
    try:
        return httpx.get(url, params=params, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise StockLookupError(f"{provider} unreachable: {type(e).__name__}") from e


def _finnhub(path: str, **params) -> dict:
    global _finnhub_off_until, _finnhub_off_reason
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        raise NotConfigured("FINNHUB_API_KEY is not set.")
    if time.time() < _finnhub_off_until:
        raise StockLookupError(f"Finnhub temporarily skipped after an error: {_finnhub_off_reason}")

    def fetch():
        r = _http_get("Finnhub", f"{FINNHUB}{path}", {**params, "token": key})
        if r.status_code == 403:
            try:
                err = (r.json() or {}).get("error", "")
            except ValueError:
                err = ""
            if "access" in err.lower():          # "You don't have access to this resource."
                raise NotInPlan("That Finnhub data isn't included in the free plan.")
            raise StockLookupError("Finnhub rejected the API key - check FINNHUB_API_KEY.")
        if r.status_code == 401:
            raise StockLookupError("Finnhub rejected the API key - check FINNHUB_API_KEY.")
        if r.status_code == 429:
            raise StockLookupError("Finnhub free-tier rate limit reached.")
        if r.status_code != 200:
            raise StockLookupError(f"Finnhub error (HTTP {r.status_code}).")
        return r.json()

    try:
        return _cached("fh:" + path + repr(sorted(params.items())), fetch)
    except (NotFound, NotInPlan):
        raise
    except StockLookupError as e:
        _finnhub_off_until = time.time() + 60    # don't retry it on every call this minute
        _finnhub_off_reason = str(e)
        raise


def _twelve(path: str, **params) -> dict:
    key = os.getenv("TWELVEDATA_API_KEY")
    if not key:
        raise NotConfigured("TWELVEDATA_API_KEY is not set in .env (free key at twelvedata.com).")

    def fetch():
        r = _http_get("Twelve Data", f"{TWELVE}{path}", {**params, "apikey": key})
        data = r.json() if r.status_code == 200 else {"status": "error", "code": r.status_code}
        if data.get("status") == "error":
            code = data.get("code")
            msg = (data.get("message") or "").replace("*", "").strip()
            if code == 429:
                raise StockLookupError("Twelve Data free-tier limit reached (8 calls a minute) - try again shortly.")
            if code in (401, 403):
                raise StockLookupError("Twelve Data rejected the API key - check TWELVEDATA_API_KEY.")
            if code in (400, 404):
                raise NotFound(msg or "Twelve Data has no data for that request.")
            raise StockLookupError(msg or f"Twelve Data error (code {code}).")
        return data

    return _cached("td:" + path + repr(sorted(params.items())), fetch)


def _first_success(attempts):
    """Runs provider attempts in order and returns the first result. If all fail, raises the
    most useful error: 'not found' > a real problem (rate limit, bad key) > missing key."""
    errors = []
    for attempt in attempts:
        try:
            return attempt()
        except StockLookupError as e:
            errors.append(e)
    for kind in (NotFound,):
        found = [e for e in errors if isinstance(e, kind)]
        if found:
            raise found[0]
    real = [e for e in errors if not isinstance(e, NotConfigured)
            and "temporarily skipped" not in str(e)]
    if real:
        raise real[0]
    raise errors[-1]          # all unconfigured: the last provider (Twelve Data) is the required one


# ====================================================================== name -> ticker
def _name_score(want: str, symbol: str, name: str) -> int:
    s = 0
    if symbol.lower() == want:
        s += 3                                   # user typed the ticker
    if name.lower().startswith(want) or want in name.lower():
        s += 2                                   # name matches
    return s


def _resolve_finnhub(company: str):
    results = _finnhub("/search", q=company).get("result") or []
    us_common = [r for r in results
                 if (r.get("type") or "").lower() == "common stock" and "." not in (r.get("symbol") or "")]
    if not us_common:
        raise NotFound(f"No US-listed company found matching '{company}'.")
    want = company.strip().lower()
    best = max(us_common, key=lambda r: _name_score(want, r["symbol"], r.get("description") or ""))
    return best["symbol"], best.get("description") or best["symbol"]


def _resolve_twelve(company: str):
    results = _twelve("/symbol_search", symbol=company).get("data") or []
    us_common = [r for r in results
                 if r.get("country") == "United States"
                 and (r.get("instrument_type") or "").lower() == "common stock"]
    if not us_common:
        raise NotFound(f"No US-listed company found matching '{company}'.")
    want = company.strip().lower()

    def score(r):
        s = _name_score(want, r["symbol"], r.get("instrument_name") or "")
        if (r.get("exchange") or "").upper() in US_MAIN_EXCHANGES:
            s += 2                               # main exchange over OTC
        return s

    best = max(us_common, key=score)
    return best["symbol"], best.get("instrument_name") or best["symbol"]


def _resolve(company: str):
    """Company name -> (ticker, company name). Finnhub first, then Twelve Data."""
    return _first_success([lambda: _resolve_finnhub(company), lambda: _resolve_twelve(company)])


# ====================================================================== quotes
def _quote_finnhub(symbol: str) -> dict:
    q = _finnhub("/quote", symbol=symbol)
    if not q or not q.get("c"):                  # Finnhub answers zeros for unknown symbols
        raise NotFound(f"No current price for {symbol}.")
    name = None
    try:
        name = _finnhub("/stock/profile2", symbol=symbol).get("name")
    except StockLookupError:
        pass
    return {"price": q["c"], "change": q.get("d"), "change_pct": q.get("dp"),
            "high": q.get("h"), "low": q.get("l"), "open": q.get("o"), "prev_close": q.get("pc"),
            "ts": q.get("t"), "name": name, "source": "Finnhub"}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _quote_twelve(symbol: str) -> dict:
    q = _twelve("/quote", symbol=symbol)
    price = _num(q.get("close"))
    if not price:
        raise NotFound(f"No current price for {symbol}.")
    return {"price": price, "change": _num(q.get("change")), "change_pct": _num(q.get("percent_change")),
            "high": _num(q.get("high")), "low": _num(q.get("low")), "open": _num(q.get("open")),
            "prev_close": _num(q.get("previous_close")),
            "ts": q.get("last_quote_at") or q.get("timestamp"), "name": q.get("name"),
            "source": "Twelve Data"}


def _quote(symbol: str) -> dict:
    return _first_success([lambda: _quote_finnhub(symbol), lambda: _quote_twelve(symbol)])


def _r(v):
    return round(v, 2) if isinstance(v, (int, float)) else None


def get_stock_price(company: str) -> dict:
    """Gets the latest stock price for a company. Accepts a company name ("Nvidia",
    "Alphabet", "Coca-Cola") or a ticker symbol ("NVDA", "GOOGL", "KO").

    Args:
        company: the company name or ticker symbol.
    """
    company = (company or "").strip()
    if not company:
        return {"status": "error", "message": "Tell me which company or ticker."}

    try:
        q, symbol, name = None, None, None
        if _TICKER.match(company):               # typed as a ticker: try it directly
            try:
                q, symbol = _quote(company), company
            except NotFound:
                pass                             # e.g. "APPLE" - treat it as a name instead
        if q is None:
            symbol, name = _resolve(company)
            q = _quote(symbol)
    except StockLookupError as e:
        return {"status": "error", "message": str(e)}

    ts = q.get("ts")
    as_of = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else None
    return {
        "status": "success",
        "company": q.get("name") or name or symbol,
        "symbol": symbol,
        "price": _r(q["price"]),
        "change": _r(q.get("change")),
        "change_percent": f"{q['change_pct']:+.2f}%" if q.get("change_pct") is not None else None,
        "day_high": _r(q.get("high")),
        "day_low": _r(q.get("low")),
        "open": _r(q.get("open")),
        "previous_close": _r(q.get("prev_close")),
        "as_of_utc": as_of,
        "source": f"{q['source']} (may be delayed; not for trading decisions)",
    }


# ====================================================================== price history
def _today() -> date:
    """Today's date on the US market calendar (falls back to UTC without tzdata)."""
    try:
        return datetime.now(ZoneInfo("America/New_York")).date()
    except ZoneInfoNotFoundError:
        return datetime.now(timezone.utc).date()


def _minus_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + (d.month - 1) - months, 12)
    for day in (d.day, 30, 29, 28):              # clamp e.g. Mar 31 -> Feb 28
        try:
            return date(y, m + 1, day)
        except ValueError:
            continue
    raise ValueError("bad date")


def _period_start(period: str, end: date) -> date:
    p = period.strip().lower().replace(" ", "")
    if p == "ytd":
        return date(end.year, 1, 1)
    m = _PERIOD.match(p)
    if not m:
        raise StockLookupError(f"Unknown period '{period}'. Use e.g. 5d, 2w, 1mo, 6mo, 1y, 5y or ytd.")
    n, unit = int(m.group(1)), m.group(2)
    if unit.startswith("d"):
        return end - timedelta(days=n)
    if unit.startswith("w"):
        return end - timedelta(weeks=n)
    if unit.startswith("m"):
        return _minus_months(end, n)
    return _minus_months(end, 12 * n)


def _parse_date(s: str, label: str) -> date:
    try:
        return date.fromisoformat(s.strip())
    except ValueError as e:
        raise StockLookupError(f"{label} must be YYYY-MM-DD, got '{s}'.") from e


def _sample(rows: list, n: int) -> list:
    """Evenly spaced rows, always keeping the first and last."""
    if len(rows) <= n:
        return rows
    idx = sorted({round(i * (len(rows) - 1) / (n - 1)) for i in range(n)})
    return [rows[i] for i in idx]


def _history_rows(company: str, period: str = "", start_date: str = "", end_date: str = "",
                  interval: str = "auto") -> tuple[str, str, str, list]:
    """Fetches every price row for a range. Returns (symbol, name, interval, rows).
    Raises StockLookupError with a user-facing message."""
    today = _today()
    end = min(_parse_date(end_date, "end_date") if end_date else today, today)
    if period:
        start = _period_start(period, end)
    elif start_date:
        start = _parse_date(start_date, "start_date")
    else:
        start = _minus_months(end, 1)            # default: last month
    if start > end:
        raise StockLookupError("start_date is after end_date.")

    span = (end - start).days
    if interval == "auto":
        interval = "1day" if span <= 370 else "1week" if span <= 5 * 366 else "1month"
    if interval not in ("1day", "1week", "1month"):
        raise StockLookupError("interval must be 1day, 1week, 1month or auto.")

    symbol, name = (company, company) if _TICKER.match(company) else _resolve(company)

    data = _twelve("/time_series", symbol=symbol, interval=interval, order="asc", outputsize=5000,
                   start_date=start.isoformat(),
                   end_date=(end + timedelta(days=1)).isoformat())   # make end date inclusive
    rows = [r for r in data.get("values") or []
            if start.isoformat() <= r["datetime"][:10] <= end.isoformat()]
    if not rows:
        raise NotFound(f"No trading data for {symbol} between {start} and {end} "
                       "(weekend, holiday, or before it listed).")
    return symbol, name, interval, rows


def _summarise(rows: list) -> dict:
    """Start/end, change and high/low from price rows."""
    closes = [float(r["close"]) for r in rows]
    first, last = closes[0], closes[-1]
    hi_i = max(range(len(closes)), key=closes.__getitem__)
    lo_i = min(range(len(closes)), key=closes.__getitem__)
    return {
        "from": rows[0]["datetime"][:10],
        "to": rows[-1]["datetime"][:10],
        "data_points": len(rows),
        "start_close": round(first, 2),
        "end_close": round(last, 2),
        "change": round(last - first, 2),
        "change_percent": f"{(last - first) / first * 100:+.2f}%",
        "highest_close": {"price": round(closes[hi_i], 2), "date": rows[hi_i]["datetime"][:10]},
        "lowest_close": {"price": round(closes[lo_i], 2), "date": rows[lo_i]["datetime"][:10]},
    }


def get_stock_price_history(company: str, period: str = "", start_date: str = "",
                            end_date: str = "", interval: str = "auto") -> dict:
    """Gets a company's stock prices over a range of dates, with a summary of the move
    (start and end price, % change, highest and lowest close and when).

    Give EITHER a period OR dates:
        period: relative to today, e.g. "5d", "2w", "1mo", "3mo", "6mo", "1y", "5y", "ytd".
        start_date / end_date: exact dates as YYYY-MM-DD (end_date defaults to today).

    Args:
        company: company name ("Nvidia") or ticker ("NVDA").
        period: relative range like "6mo" or "ytd". Leave empty when using dates.
        start_date: first date, YYYY-MM-DD.
        end_date: last date, YYYY-MM-DD. Defaults to today.
        interval: "1day", "1week", "1month", or "auto" (picks by range length).
    """
    company = (company or "").strip()
    if not company:
        return {"status": "error", "message": "Tell me which company or ticker."}
    try:
        symbol, name, interval, rows = _history_rows(company, period, start_date, end_date, interval)
    except StockLookupError as e:
        return {"status": "error", "message": str(e)}

    return {
        "status": "success",
        "company": name,
        "symbol": symbol,
        "interval": interval,
        **_summarise(rows),
        "prices": [{"date": r["datetime"][:10], "close": round(float(r["close"]), 2)}
                   for r in _sample(rows, MAX_POINTS)],
        "prices_note": (f"{MAX_POINTS} evenly spaced points shown of {len(rows)}"
                        if len(rows) > MAX_POINTS else "all points shown"),
        "source": "Twelve Data (closing prices; not for trading decisions)",
    }


# ====================================================================== company research
SUMMARY_CHARS = 280          # keep news summaries short to save tokens
MARKET_NEWS_CATEGORIES = ("general", "forex", "crypto", "merger")
MARKET_NEWS_QUERIES = {           # web-search fallback queries for get_market_news
    "general": "stock market news", "forex": "forex currency market news",
    "crypto": "cryptocurrency market news", "merger": "mergers and acquisitions deal news",
}


def _finnhub_or_explain(path: str, **params):
    """Calls Finnhub, turning a missing key or temporary skip into a clear message."""
    try:
        return _finnhub(path, **params)
    except NotConfigured as e:
        raise StockLookupError("This needs FINNHUB_API_KEY in .env (free key at finnhub.io).") from e
    except StockLookupError as e:
        if "temporarily skipped" in str(e):
            reason = str(e).split(": ", 1)[-1].removeprefix("Finnhub ").rstrip(".")
            raise StockLookupError(f"Finnhub unavailable: {reason} - try again in a minute.") from e
        raise


def _symbol_for(company: str) -> tuple[str, str]:
    """Company name or ticker -> (ticker, display name)."""
    return (company, company) if _TICKER.match(company) else _resolve(company)


def _when(ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError):
        return None


def _shorten(text: str, limit: int = SUMMARY_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def _articles(items: list, limit: int) -> list[dict]:
    """Newest first, duplicates (same headline) removed, trimmed to `limit`."""
    seen, out = set(), []
    for a in sorted(items or [], key=lambda a: a.get("datetime") or 0, reverse=True):
        headline = (a.get("headline") or "").strip()
        if not headline or headline.lower() in seen:
            continue
        seen.add(headline.lower())
        out.append({
            "headline": headline,
            "source": a.get("source"),
            "published": _when(a.get("datetime")),
            "summary": _shorten(a.get("summary")),
            "url": a.get("url"),
        })
        if len(out) >= limit:
            break
    return out


def _format_money_millions(m) -> str | None:
    """Finnhub reports market cap / shares in millions."""
    try:
        v = float(m) * 1_000_000
    except (TypeError, ValueError):
        return None
    for size, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if v >= size:
            return f"{v / size:.2f}{unit}"
    return f"{v:,.0f}"


def _web_news(query: str, days: int, limit: int, why: str) -> dict:
    """News from a Tavily news search, in the same article format as Finnhub's.
    `why` is the Finnhub error, kept so the user knows why the source changed."""
    time_range = "day" if days <= 1 else "week" if days <= 7 else "month"
    found = web_search(query, topic="news", time_range=time_range, max_results=min(limit, 10))
    if found.get("status") != "success":
        return {"status": "error", "message": f"{why} Web search fallback also failed: {found.get('message')}"}
    articles = [{
        "headline": r["title"],
        "source": re.sub(r"^www\.", "", httpx.URL(r["url"]).host or "") or "web",
        "published": r.get("published"),
        "summary": _shorten(r.get("snippet")),
        "url": r["url"],
    } for r in found.get("results") or [] if r.get("title")]
    return {"articles": articles, "fallback": True,
            "source": f"Web search (Tavily), used because: {why}"}


# ---------------------------------------------------------------------- tools
def get_company_news(company: str, days: int = 7, limit: int = 8) -> dict:
    """Gets recent news headlines about a company (newest first).

    Args:
        company: company name ("Nvidia") or ticker ("NVDA").
        days: how many days back to look, 1-30. Default 7.
        limit: maximum number of articles, 1-15. Default 8.
    """
    company = (company or "").strip()
    if not company:
        return {"status": "error", "message": "Tell me which company or ticker."}
    days = max(1, min(int(days or 7), 30))
    limit = max(1, min(int(limit or 8), 15))
    symbol, name = None, company
    try:
        symbol, name = _symbol_for(company)
        end = _today()
        items = _finnhub_or_explain("/company-news", symbol=symbol,
                                    **{"from": (end - timedelta(days=days)).isoformat(),
                                       "to": end.isoformat()})
    except NotFound as e:
        return {"status": "error", "message": str(e)}          # unknown company: don't search
    except StockLookupError as e:                               # provider problem: try the web
        web = _web_news(f"{name} {symbol or ''} company news".replace("  ", " "), days, limit, str(e))
        if "articles" not in web:
            return web
        return {"status": "success", "company": name, "symbol": symbol, "days": days, **web}

    articles = _articles(items, limit)
    if not articles:
        return {"status": "success", "company": name, "symbol": symbol, "articles": [],
                "message": f"No news found for {symbol} in the last {days} days."}
    return {"status": "success", "company": name, "symbol": symbol, "days": days,
            "articles": articles, "source": "Finnhub (third-party publishers)"}


def get_market_news(category: str = "general", limit: int = 8) -> dict:
    """Gets the latest market news headlines (newest first).

    Args:
        category: "general" (stock market), "forex", "crypto", or "merger". Default "general".
        limit: maximum number of articles, 1-15. Default 8.
    """
    category = (category or "general").strip().lower()
    if category not in MARKET_NEWS_CATEGORIES:
        return {"status": "error",
                "message": f"category must be one of: {', '.join(MARKET_NEWS_CATEGORIES)}."}
    limit = max(1, min(int(limit or 8), 15))
    try:
        items = _finnhub_or_explain("/news", category=category)
    except StockLookupError as e:                               # provider problem: try the web
        web = _web_news(MARKET_NEWS_QUERIES[category], 1, limit, str(e))
        if "articles" not in web:
            return web
        return {"status": "success", "category": category, **web}
    return {"status": "success", "category": category, "articles": _articles(items, limit),
            "source": "Finnhub (third-party publishers)"}


def get_company_profile(company: str) -> dict:
    """Gets a company's profile: full name, industry, country, exchange, market cap,
    shares outstanding, IPO date, and website.

    Args:
        company: company name ("Coca-Cola") or ticker ("KO").
    """
    company = (company or "").strip()
    if not company:
        return {"status": "error", "message": "Tell me which company or ticker."}
    try:
        symbol, _ = _symbol_for(company)
        p = _finnhub_or_explain("/stock/profile2", symbol=symbol)
    except StockLookupError as e:
        return {"status": "error", "message": str(e)}
    if not p or not p.get("name"):
        return {"status": "error", "message": f"No profile found for {symbol}."}

    currency = p.get("currency") or "USD"
    cap = _format_money_millions(p.get("marketCapitalization"))
    shares = _format_money_millions(p.get("shareOutstanding"))
    return {
        "status": "success",
        "company": p.get("name"),
        "symbol": p.get("ticker") or symbol,
        "industry": p.get("finnhubIndustry"),
        "country": p.get("country"),
        "exchange": p.get("exchange"),
        "market_cap": f"{cap} {currency}" if cap else None,
        "shares_outstanding": shares,
        "ipo_date": p.get("ipo"),
        "website": p.get("weburl"),
        "source": "Finnhub",
    }


def get_company_peers(company: str, limit: int = 8) -> dict:
    """Gets companies similar to this one (same industry - typically competitors),
    with their names.

    Args:
        company: company name ("Tesla") or ticker ("TSLA").
        limit: maximum number of peers, 1-12. Default 8.
    """
    company = (company or "").strip()
    if not company:
        return {"status": "error", "message": "Tell me which company or ticker."}
    limit = max(1, min(int(limit or 8), 12))
    try:
        symbol, name = _symbol_for(company)
        tickers = _finnhub_or_explain("/stock/peers", symbol=symbol) or []
    except StockLookupError as e:
        return {"status": "error", "message": str(e)}

    peers = [t for t in tickers if isinstance(t, str) and t.upper() != symbol.upper()][:limit]
    if not peers:
        return {"status": "success", "company": name, "symbol": symbol, "peers": [],
                "message": f"No peers listed for {symbol}."}

    named = []
    for t in peers:                        # names via profile2; tolerate failures per peer
        try:
            n = (_finnhub("/stock/profile2", symbol=t) or {}).get("name")
        except StockLookupError:
            n = None
        named.append({"symbol": t, "name": n or t})
    return {"status": "success", "company": name, "symbol": symbol, "peers": named,
            "source": "Finnhub (grouped by industry)"}
