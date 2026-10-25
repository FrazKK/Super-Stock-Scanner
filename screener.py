"""
Superstock Scanner v2
=====================
A free weekly screen of US, Canadian and UK stocks that runs four breakout
methods side by side and keeps score of how each one's picks perform.

Methods
  Stine       - Insider Buy Superstocks: cheap, small, profitable, breaking out
                of a base on huge weekly volume above a steep 30-week MA.
  Weinstein   - Stage 2: breakout from a flat "stage 1" base above the 30-week
                MA on volume, outperforming the market. No earnings needed.
  Minervini   - Trend Template (8 rules) plus a volatility contraction (VCP)
                and pivot breakout.
  Episodic    - Qullamaggie episodic pivot: gap up 10%+ on 3x+ volume after
                news, still holding the gap. No earnings needed.

Data (all free, unofficial): TradingView screener, Yahoo Finance prices,
OpenInsider (US insider purchases).

Run:  python screener.py           live data
      python screener.py --demo    simulated 30-week run on made-up stocks

Not financial advice.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import math
import os
import re
import shutil
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

MARKETS = {
    "america": {
        "label": "USA",
        "universe_price_min": 1.0,
        "stine_price": (2.0, 15.0),            # USD
        "mcap": (20e6, 1.5e9),
        "min_avg_value": 300_000,              # average daily traded value, USD (data hygiene)
        "stine_max_mcap": 500e6,
        "exchanges": {"NASDAQ", "NYSE", "AMEX", "NYSE ARCA", "NYSEARCA"},
        "bench": ["IWM", "SPY"],               # small caps first, fallback
    },
    "canada": {
        "label": "Canada",
        "universe_price_min": 0.10,
        "stine_price": (0.50, 15.0),           # CAD
        "mcap": (10e6, 1.5e9),
        "min_avg_value": 75_000,
        "stine_max_mcap": 700e6,
        "exchanges": {"TSX", "TSXV", "CSE", "NEO"},
        "bench": ["XCS.TO", "XIC.TO", "^GSPTSE"],
    },
    "uk": {
        "label": "UK",
        "universe_price_min": 1.0,             # pence
        "stine_price": (5.0, 1500.0),          # pence (5p to 15)
        "mcap": (5e6, 1.0e9),
        "min_avg_value": 30_000,               # GBP
        "stine_max_mcap": 400e6,
        "exchanges": {"LSE", "LSIN"},
        "bench": ["^FTAI", "^FTMC", "^FTSE"],  # AIM All-Share, FTSE 250, FTSE 100
    },
}

STRATEGIES = {
    "stine": {"label": "Stine superstocks", "short": "Stine",
              "blurb": "Cheap, small, profitable stocks breaking out of a base on huge volume. "
                       "Buy the pullback to the 10-week MA."},
    "weinstein": {"label": "Weinstein Stage 2", "short": "Weinstein",
                  "blurb": "Breakouts from a flat base above the 30-week MA, beating the market. "
                           "No earnings needed, so it suits explorers."},
    "minervini": {"label": "Minervini trend + VCP", "short": "Minervini",
                  "blurb": "Passes all 8 Trend Template rules. Best when pullbacks are shrinking "
                           "and it breaks out of the tightest point."},
    "episodic": {"label": "Episodic pivots", "short": "Episodic",
                 "blurb": "Gapped up 10%+ on 3x+ volume this week after news, still holding the gap. "
                          "Drill results and contract wins show up here."},
    "luk": {"label": "Luk pullback", "short": "Luk",
            "blurb": "Martin Luk (2025 US Investing Champion, record +969%): a fast mover up 30%+ dips to its "
                     "9-day EMA and bounces. Tight stop at the day's low."},
    "kell": {"label": "Kell wedge pop / EMA crossback", "short": "Kell",
             "blurb": "Oliver Kell (2020 US Investing Champion, +941%): a stock pops out of a drifting wedge back above "
                      "its 10/20-day EMAs on volume, or an uptrend dips to the 20-day EMA and bounces."},
    "slater": {"label": "Slater Zulu (growth at a low PEG)", "short": "Slater",
               "blurb": "Jim Slater's Zulu Principle, a UK small-cap classic: fast forecast earnings growth at a PEG "
                        "under 0.75, good returns on capital, beating the market."},
    "piotroski": {"label": "Piotroski F-Score value", "short": "Piotroski",
                  "blurb": "Cheap stocks (lowest price-to-book) with a financial-health score of 8 or 9 out of 9. "
                           "Works best in small caps."},
    "magic": {"label": "Greenblatt Magic Formula", "short": "Magic",
              "blurb": "Joel Greenblatt: good businesses (high return on capital) at bargain prices (high earnings "
                       "yield). Ranked across the whole market."},
    "stage2": {"label": "Simple Stage 2", "short": "Stage 2",
               "blurb": "Just entered Stage 2: above a rising 30-week MA, 10-week above 30-week, beating the "
                        "market, and not yet stretched. No volume or base rules, so it catches more names."},
}
VALUE_METHODS = ("slater", "piotroski", "magic")
# Value methods have no entry timing of their own; we only list them while the price is above
# its 50-day MA (an added momentum check, the "value + momentum" idea), so they're not falling knives.
STAGE2_MAX_WEEKS = 8           # entered Stage 2 within the last 8 weeks (early, not late)
STAGE2_MAX_EXT = 0.15          # no more than 15% above the 30-week MA

# Not a separate scan: stocks that two or more of the methods above picked the same week.
CONSENSUS = {"label": "Picked by 2+ methods", "short": "2+ methods",
             "blurb": "Stocks that more than one method flagged this week. Agreement between different "
                      "approaches can mean a stronger setup; the track record will show if it does."}
TRACKED = {**STRATEGIES, "consensus": CONSENSUS}

RESOURCE_SECTORS = {"Energy Minerals", "Non-Energy Minerals"}

REQUIRE_PROFIT = True          # Stine only: drop loss-makers when EPS data exists
MAX_CANDIDATES = 400           # per market, how many get full price analysis
SHOW_PER_LIST = 10             # max picks per method per market (buy-zone only)
RECORD_PER_LIST = 10           # how many of those go into the track record
                               # (Minervini "trend only" names are shown but not recorded)
NEW_WINDOW_WEEKS = 8           # a pick is "NEW" if not picked in this many weeks
WATCH_SHOW = 10

# Stine
STINE_BASE_WEEKS = 12
STINE_BASE_MAX_DEPTH = 0.40
STINE_BREAKOUT_LOOKBACK = 8    # breakout must be within the last 8 weeks
STINE_VOL_MULT = 3.0           # Stine: breakout week on huge volume...
STINE_LOW_FLOAT = 50e6         # ...in a low-float stock (shares); market cap is used when float is unknown
STINE_STEEP = 0.03             # 30-wk MA up 3%+ in 4 weeks
STINE_MIN_SCORE = 6
EPS_GROWTH_MIN = 25.0
PE_MAX = 20.0
SMALL_FLOAT = 30e6

# Weinstein
WEIN_LOOKBACK = 8
WEIN_VOL_MULT = 2.0            # Weinstein: breakout volume at least 2x the recent average

# Minervini
MIN_RS = 70                    # Minervini's Trend Template: RS rank 70 or higher
MIN_PIVOT_CHASE = 0.05         # Minervini: don't buy more than 5% above the pivot
EP_MAX_DAYS = 3                # episodic pivot: gap within the last 3 trading days
MARKET_FILTER = False          # True = hide Weinstein/Minervini buys when the market is in a downtrend
                               # (both authors advise against new buys then); False = show with a warning
MIN_STOP = 0.08

# Episodic pivot
EP_GAP = 0.10
EP_VOL_MULT = 3.0
EP_MIN_MOVE = 0.08

HORIZONS = [4, 12, 26]         # weeks, for the track record
RULES_VERSION = 3              # bump when the selection rules change; the track record restarts from then
RULES_SINCE = "2026-10-18"

ROOT = Path(__file__).resolve().parent

# US politicians: House members with the best estimated returns in Unusual Whales'
# 2024 and 2025 Congress-trading reports. Edit freely: (first name, last name).
# Senators can't be included: the Senate disclosure site blocks automated access from servers.
POLITICIANS = [
    ("Nancy", "Pelosi"), ("David", "Rouzer"), ("Dan", "Crenshaw"),            # 2024 standouts
    ("Warren", "Davidson"), ("Donald", "Norcross"), ("Terri", "Sewell"),      # 2025 top 10 (House)
    ("Bryan", "Steil"), ("Nick", "LaLota"), ("Michael", "Guest"),
    ("Tom", "McClintock"), ("Dwight", "Evans"),
]
POL_DAYS = 90                  # show and badge buys reported in the last 90 days
HOUSE_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PTR = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"

# UK director dealings (RNS "Director/PDMR Shareholding"), checked for UK picks only
UK_DEALING_DAYS = 90
INVESTEGATE = "https://www.investegate.co.uk"

# Holdings and alerts
REPO = os.getenv("GITHUB_REPOSITORY", "FrazKK/Super-Stock-Scanner")
BRANCH = "main"
SITE = os.getenv("SITE_URL") or "https://frazkk.github.io/Super-Stock-Scanner/"
WARN_BAND = {"stine": 0.03, "weinstein": 0.05, "minervini": 0.03, "episodic": 0.03, "stage2": 0.05,
             "luk": 0.02, "kell": 0.03, "other": 0.05}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def num(x):
    try:
        x = float(x)
        return None if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return None


def jdefault(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp, dt.date)):
        return o.isoformat()
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def to_weekly(d: pd.DataFrame) -> pd.DataFrame:
    w = d.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min",
                                 "Close": "last", "Volume": "sum"})
    return w.dropna(subset=["Close"])


def r4(x):
    return None if x is None else round(float(x), 4)


YAHOO_SUFFIX = {"TSX": ".TO", "TSXV": ".V", "CSE": ".CN", "NEO": ".NE", "LSE": ".L", "LSIN": ".L"}


def yahoo_symbol(exchange: str, name: str) -> str:
    sym = str(name).strip().rstrip(".").replace(".", "-")
    return sym + YAHOO_SUFFIX.get(str(exchange).upper(), "")


EXCLUDE_NAME = re.compile(
    r"\b(acquisition|spac|blank check|merger corp|investment company|fund|trust|reit|etf|etn|"
    r"income|bdc|finance corp|capital corp|investment corp|lending|units?|warrants?|rights?|"
    r"preferred|pref|notes?|debentures?|split corp|royalty)\b", re.I)
EXCLUDE_TYPESPEC = {"reit", "etf", "etn", "fund", "closedend", "closed-end", "spac", "unit",
                    "preferred", "trust", "right", "warrant"}
EXCLUDE_INDUSTRY = re.compile(r"(real estate investment trusts|investment trusts|closed end|"
                              r"financial conglomerates|investment managers)", re.I)


def is_excluded(row) -> bool:
    name = str(row.get("description") or "")
    tick = str(row.get("name") or "")
    if EXCLUDE_NAME.search(name) or EXCLUDE_INDUSTRY.search(str(row.get("industry") or "")):
        return True
    if re.search(r"\.(UN|U|WT|PR\.?\w*|DB)$", tick, re.I):
        return True
    specs = row.get("typespecs")
    if isinstance(specs, (list, tuple, np.ndarray)) and {str(x).lower() for x in specs} & EXCLUDE_TYPESPEC:
        return True
    price = num(row.get("close"))
    if (str(row.get("sector") or "") == "Finance" and price is not None and 9.5 <= price <= 11.5
            and str(row.get("currency") or "") == "USD"
            and re.search(r"(\b(I{1,3}|IV|VI{0,3}|\d+)\b|treasury|financial ii)", name, re.I)):
        return True
    return False


# ---------------------------------------------------------------------------
# Live data sources
# ---------------------------------------------------------------------------

TV_CORE = ["name", "description", "exchange", "close", "currency", "volume",
           "average_volume_10d_calc", "relative_volume_10d_calc", "SMA50", "SMA200",
           "price_52_week_high", "market_cap_basic", "type"]
TV_OPTIONAL = ["SMA150", "price_earnings_ttm", "earnings_per_share_diluted_ttm",
               "earnings_per_share_diluted_yoy_growth_fq", "float_shares_outstanding",
               "sector", "industry", "typespecs", "Perf.W", "Perf.3M", "Perf.6M", "Perf.Y"]
# fundamentals for the value methods; if TradingView rejects any, the scan carries on without them
TV_VALUE = ["piotroski_f_score_ttm", "price_book_fq", "enterprise_value_to_ebit_ttm", "return_on_invested_capital",
            "return_on_capital_employed_fy", "price_earnings_forward_fy", "earnings_per_share_forecast_next_fy",
            "altman_z_score_ttm"]


def live_tv(market: str, names: list[str] | None = None) -> pd.DataFrame:
    from tradingview_screener import Query, col

    cfg = MARKETS[market]
    cols = TV_CORE + TV_OPTIONAL + TV_VALUE
    dropped_value = False
    for _ in range(len(TV_OPTIONAL) + len(TV_VALUE) + 2):
        try:
            if names:
                q = Query().set_markets(market).select(*cols).where(col("name").isin(names)).limit(100)
            else:
                q = (Query().set_markets(market).select(*cols)
                     .where(col("type") == "stock",
                            col("market_cap_basic").between(*cfg["mcap"]),
                            col("average_volume_10d_calc") > 5000)
                     .limit(6000))
            total, df = q.get_scanner_data()
            print(f"  [{market}] TradingView returned {len(df)} of {total}")
            for c in TV_CORE + TV_OPTIONAL + TV_VALUE:
                if c not in df.columns:
                    df[c] = np.nan
            return df
        except Exception as e:
            bad = [b for b in re.findall(r'[Uu]nknown field "?([\w.|]+)"?', str(e))
                   if b in cols and b not in TV_CORE]
            if not bad and not dropped_value:
                bad, dropped_value = [c for c in TV_VALUE if c in cols], True     # fall back: no value fields
            if not bad:
                raise
            print(f"  [{market}] dropping unsupported field(s): {bad}")
            cols = [c for c in cols if c not in bad]
    raise RuntimeError("TradingView scan failed")


def live_prices(symbols: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    symbols = list(dict.fromkeys(symbols))
    for i in range(0, len(symbols), 60):
        chunk = symbols[i:i + 60]
        raw = None
        for attempt in range(2):
            try:
                raw = yf.download(chunk, period="2y", interval="1d", group_by="ticker",
                                  auto_adjust=True, progress=False, threads=True)
                break
            except Exception as e:
                print(f"  yfinance error ({attempt + 1}): {e}")
                time.sleep(5)
        if raw is None or raw.empty:
            continue
        for s in chunk:
            try:
                d = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
                d = d[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
                if d.index.tz is not None:
                    d.index = d.index.tz_localize(None)
                if len(d) >= 60:
                    out[s] = d
            except Exception:
                pass
        time.sleep(1)
    print(f"  prices: {len(out)} of {len(symbols)} symbols")
    return out


OPENINSIDER_URL = (
    "http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd=90&fdr=&td=0&tdr=&fdlyl=&fdlyh="
    "&daysago=&xp=1&vl=10&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999&grp=0&nfl=&nfh=&nil=&nih="
    "&nol=&noh=&v2l=&v2h=&oc2l=&oc2h=&sortcol=0&cnt=5000&page=1"
)


def live_insiders() -> dict[str, dict]:
    import requests

    try:
        resp = requests.get(OPENINSIDER_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0 superstock-scanner"})
        resp.raise_for_status()
        t = pd.read_html(io.StringIO(resp.text), attrs={"class": "tinytable"})[0]
    except Exception as e:
        print(f"  OpenInsider unavailable: {e}")
        return {}
    t.columns = [str(c).replace("\xa0", " ").strip() for c in t.columns]
    t["value"] = pd.to_numeric(t["Value"].astype(str).str.replace(r"[^\d.]", "", regex=True), errors="coerce")
    out = {}
    for tick, g in t.groupby("Ticker"):
        out[str(tick).strip().upper()] = {
            "buys": int(len(g)), "value": float(g["value"].sum()),
            "insiders": sorted({str(x) for x in g.get("Insider Name", [])})[:4],
            "latest": str(g.get("Trade Date", pd.Series([""])).max()),
        }
    print(f"  OpenInsider: {len(out)} US tickers with insider purchases in 90 days")
    return out


def insider_link(market, ticker):
    if market == "america":
        return f"http://openinsider.com/{ticker}"
    if market == "canada":
        return f"https://www.canadianinsider.com/node/7?menu_tickersearch={ticker}"
    return f"https://www.investegate.co.uk/company/{ticker}"


# ---------------------------------------------------------------------------
# Universe and candidates
# ---------------------------------------------------------------------------


def prepare_universe(df: pd.DataFrame, market: str) -> pd.DataFrame:
    cfg = MARKETS[market]
    d = df.copy()
    d = d[d["exchange"].astype(str).str.upper().isin(cfg["exchanges"])]
    d = d[~d.apply(is_excluded, axis=1)]
    pence = d["currency"].astype(str).str.upper().eq("GBX")
    value = d["average_volume_10d_calc"] * d["close"] / np.where(pence, 100.0, 1.0)
    d = d[(value >= cfg["min_avg_value"]) & (d["close"] >= cfg["universe_price_min"])].copy()
    # RS composite like IBD: weighted 3/6/12-month performance, ranked 1-99 in this market
    p3, p6, p12 = (pd.to_numeric(d[c], errors="coerce") for c in ("Perf.3M", "Perf.6M", "Perf.Y"))
    comp = (0.4 * p3 + 0.3 * p6.fillna(p3) + 0.3 * p12.fillna(p6).fillna(p3))
    d["rs_rank"] = (comp.rank(pct=True) * 98 + 1).round()
    d["ticker"] = d["name"].astype(str)
    d["ysym"] = [yahoo_symbol(e, n) for e, n in zip(d["exchange"], d["name"])]
    return d


def pick_candidates(u: pd.DataFrame) -> pd.DataFrame:
    up = u[u["close"] > u["SMA50"]]
    take = []
    n = MAX_CANDIDATES // 3
    for c in ("relative_volume_10d_calc", "Perf.W", "rs_rank"):
        take.append(up.sort_values(c, ascending=False).head(n))
    out = pd.concat(take).drop_duplicates("ysym")
    return out.head(MAX_CANDIDATES)


# ---------------------------------------------------------------------------
# Strategy 1: Stine
# ---------------------------------------------------------------------------


def stine(rec, w, insider, market):
    c, h, l, v = w["Close"], w["High"], w["Low"], w["Volume"].fillna(0)
    if len(w) < 45:
        return None
    ma10, ma30 = c.rolling(10).mean(), c.rolling(30).mean()
    last = c.iloc[-1]
    trend = bool(last > ma30.iloc[-1] and ma10.iloc[-1] > ma30.iloc[-1] and ma30.iloc[-1] > ma30.iloc[-5])
    slope = float(ma30.iloc[-1] / ma30.iloc[-5] - 1)

    bo, bo_back, bo_vol, depth = False, None, None, None
    n = len(w)
    for back in range(STINE_BREAKOUT_LOOKBACK):
        i = n - 1 - back
        if i - STINE_BASE_WEEKS - 10 < 0:
            break
        base_hi = h.iloc[i - STINE_BASE_WEEKS:i].max()
        base_lo = l.iloc[i - STINE_BASE_WEEKS:i].min()
        avg = v.iloc[i - 10:i].mean()
        vx = v.iloc[i] / avg if avg > 0 else 0
        if c.iloc[i] > base_hi and vx >= STINE_VOL_MULT:
            bo, bo_back, bo_vol, depth = True, back, float(vx), float(base_hi / base_lo - 1)
            break
    if depth is None:
        depth = float(h.iloc[-STINE_BASE_WEEKS - 1:-1].max() / l.iloc[-STINE_BASE_WEEKS - 1:-1].min() - 1)
    record_vol = bool(v.iloc[-STINE_BREAKOUT_LOOKBACK:].max() >= v.iloc[-26:].max())

    checks = []

    def add(label, ok, pts, note="", req=False):
        checks.append({"label": label, "pass": bool(ok), "pts": pts if ok else 0, "max": pts, "note": note, "req": req})

    add("Above rising 30-week MA", trend, 2, req=True)
    add(f"Breakout on {STINE_VOL_MULT:.0f}x+ weekly volume", bo, 2,
        f"{bo_vol:.1f}x volume, {bo_back} wk ago" if bo else "no breakout in 8 weeks", req=True)
    add("Steep 30-week MA", slope >= STINE_STEEP, 1, f"{slope * 100:+.1f}% in 4 wks")
    add("Biggest weekly volume in 6 months", record_vol, 1, req=True)
    add("Tight base", depth <= STINE_BASE_MAX_DEPTH, 1, f"{depth * 100:.0f}% range")
    eg, pe, flt = (num(rec.get(k)) for k in ("earnings_per_share_diluted_yoy_growth_fq",
                                              "price_earnings_ttm", "float_shares_outstanding"))
    add("EPS growth > 25%", eg is not None and eg >= EPS_GROWTH_MIN, 1, "no data" if eg is None else f"{eg:+.0f}% YoY")
    add("Low P/E", pe is not None and 0 < pe <= PE_MAX, 1, "no data" if pe is None else f"P/E {pe:.1f}")
    add("Small float", flt is not None and flt <= SMALL_FLOAT, 1, "no data" if flt is None else f"{flt / 1e6:.1f}M shares")
    rs = num(rec.get("rs_rank"))
    add("Market leader (RS rank 80+)", rs is not None and rs >= 80, 1, "no data" if rs is None else f"RS {rs:.0f}")
    cap, capmax = num(rec.get("market_cap_basic")), MARKETS[market]["stine_max_mcap"]
    if flt is not None:
        small, note = flt <= STINE_LOW_FLOAT, f"float {flt / 1e6:.0f}M shares"
    else:
        small = cap is not None and cap <= capmax
        note = "no data" if cap is None else f"no float data; mkt cap {cap / 1e6:,.0f}m"
    add("Low float / small company", small, 1, note, req=True)
    if market == "america":
        add("Insider buying (90 days)", bool(insider), 2,
            f"{insider['buys']} buy(s), ${insider['value']:,.0f}" if insider else "none found")
    else:
        checks.append({"label": "Insider buying", "pass": None, "pts": 0, "max": 0, "note": "check link", "req": False})

    dist = float(last / ma10.iloc[-1] - 1)
    if not bo:
        tag, cls = "No breakout yet", "z-ok"
    elif dist < -0.03:
        tag, cls = "Lost the magic line", "z-low"
    elif dist <= 0.06 and bo_back >= 1:
        tag, cls = "At magic line", "z-buy"
    elif dist >= 0.25:
        tag, cls = "Extended, wait", "z-ext"
    else:
        tag, cls = "Wait for pullback", "z-ok"
    plan = (f"Entry zone: near the 10-week MA ({fmt_num(ma10.iloc[-1])}). "
            f"Sell rule: weekly close below it.")
    return {"strategy": "stine", "checks": checks, "tag": tag, "tag_class": cls, "plan": plan,
            "stop": None, "required_ok": all(ch["pass"] for ch in checks if ch["req"]),
            "trend_ok": trend, "dist_ma10": dist}


# ---------------------------------------------------------------------------
# Strategy 2: Weinstein Stage 2
# ---------------------------------------------------------------------------


def mansfield_rs(w: pd.DataFrame, bench_w: pd.DataFrame | None):
    if bench_w is None or bench_w.empty:
        return None
    b = bench_w["Close"].reindex(w.index, method="ffill")
    rs = w["Close"] / b
    base = rs.rolling(52, min_periods=30).mean()
    m = rs / base - 1
    return m


def weinstein(rec, w, bench_w, explain=False):
    c, h, v = w["Close"], w["High"], w["Volume"].fillna(0)
    n = len(w)
    if n < 50:
        return None
    ma30 = c.rolling(30).mean()
    found = None
    for back in range(WEIN_LOOKBACK):
        i = n - 1 - back
        if i < 45:
            break
        resistance = h.iloc[i - 12:i].max()
        avg = v.iloc[i - 10:i].mean()
        vx = v.iloc[i] / avg if avg > 0 else 0
        flat = abs(ma30.iloc[i - 1] / ma30.iloc[i - 13] - 1) <= 0.06
        basing = int((c.iloc[i - 20:i] < ma30.iloc[i - 20:i]).sum()) >= 3
        if c.iloc[i] > resistance and c.iloc[i] > ma30.iloc[i] and vx >= WEIN_VOL_MULT:
            found = {"back": back, "res": float(resistance), "vx": float(vx), "flat": flat, "basing": basing}
            break
    if not found:
        if not explain:
            return None
        chk = []
        a = _check_adder(chk)
        a(f"Breakout above resistance on {WEIN_VOL_MULT:.0f}x+ volume (last {WEIN_LOOKBACK} weeks)", False, 2, req=True)
        a("Above a rising 30-week MA", bool(c.iloc[-1] > ma30.iloc[-1] and ma30.iloc[-1] > ma30.iloc[-5]), 1, req=True)
        return {"strategy": "weinstein", "checks": chk, "tag": "No Stage 2 breakout", "tag_class": "z-ok",
                "plan": "", "stop": None, "required_ok": False}
    last = c.iloc[-1]
    above = bool(last > ma30.iloc[-1])
    slope = float(ma30.iloc[-1] / ma30.iloc[-5] - 1)
    m = mansfield_rs(w, bench_w)
    mrs = num(m.iloc[-1]) if m is not None else None
    rs = num(rec.get("rs_rank"))

    checks = []

    def add(label, ok, pts, note="", req=False):
        checks.append({"label": label, "pass": bool(ok), "pts": pts if ok else 0, "max": pts, "note": note, "req": req})

    add(f"Breakout above resistance on {WEIN_VOL_MULT:.0f}x+ volume", True, 2, f"{found['vx']:.1f}x volume, {found['back']} wk ago", req=True)
    add("Came out of a flat Stage 1 base", found["flat"] and found["basing"], 1, req=True)
    add("Still above 30-week MA, MA not falling", above and slope >= -0.005, 1, f"MA {slope * 100:+.1f}% in 4 wks", req=True)
    add("Beating the market (Mansfield RS > 0)", mrs is not None and mrs > 0, 2, req=True, note=
        "no index data" if mrs is None else f"{mrs * 100:+.0f}%")
    add("30-week MA rising", slope > 0.005, 1)
    add("Breakout volume 3x+", found["vx"] >= 3, 1)
    add("Not extended (within 15% of breakout)", last <= found["res"] * 1.15, 1,
        f"{(last / found['res'] - 1) * 100:+.0f}% vs breakout")
    add("RS rank 70+", rs is not None and rs >= 70, 1, "no data" if rs is None else f"RS {rs:.0f}")

    if last > ma30.iloc[-1] * 1.25 or last > found["res"] * 1.15:
        tag, cls = "Extended, wait", "z-ext"
    elif found["back"] <= 1 and last <= found["res"] * 1.10:
        tag, cls = "Fresh breakout", "z-buy"
    elif last <= found["res"] * 1.05:
        tag, cls = "Back at breakout", "z-buy"
    else:
        tag, cls = "Stage 2 advancing", "z-ok"
    ok = all(ch["pass"] for ch in checks if ch["req"])
    return {"strategy": "weinstein", "checks": checks, "tag": tag, "tag_class": cls,
            "plan": f"Breakout level {fmt_num(found['res'])}. Sell rule: weekly close below the 30-week MA "
                    f"({fmt_num(ma30.iloc[-1])}).",
            "stop": None, "required_ok": ok}


# ---------------------------------------------------------------------------
# Strategy 3: Minervini Trend Template + VCP
# ---------------------------------------------------------------------------


def minervini(rec, d, explain=False):
    if len(d) < 260:
        return None
    c, h, l, v = d["Close"], d["High"], d["Low"], d["Volume"].fillna(0)
    s50, s150, s200 = c.rolling(50).mean(), c.rolling(150).mean(), c.rolling(200).mean()
    last = c.iloc[-1]
    lo52, hi52 = l.iloc[-252:].min(), h.iloc[-252:].max()
    rs = num(rec.get("rs_rank"))
    checks = []

    def add(label, ok, pts, note="", req=False):
        checks.append({"label": label, "pass": bool(ok), "pts": pts if ok else 0, "max": pts, "note": note, "req": req})

    add("Above 150 and 200-day MAs", last > s150.iloc[-1] and last > s200.iloc[-1], 1, req=True)
    add("150-day above 200-day", s150.iloc[-1] > s200.iloc[-1], 1, req=True)
    add("200-day rising for a month", s200.iloc[-1] > s200.iloc[-22], 1, req=True)
    add("50-day above 150 and 200-day", s50.iloc[-1] > s150.iloc[-1] and s50.iloc[-1] > s200.iloc[-1], 1, req=True)
    add("Above 50-day MA", last > s50.iloc[-1], 1, req=True)
    add("30%+ above 52-week low", last >= lo52 * 1.30, 1, f"{(last / lo52 - 1) * 100:+.0f}%", req=True)
    add("Within 25% of 52-week high", last >= hi52 * 0.75, 1, f"{(last / hi52 - 1) * 100:+.0f}%", req=True)
    add(f"RS rank {MIN_RS}+", rs is not None and rs >= MIN_RS, 1, "no data" if rs is None else f"RS {rs:.0f}", req=True)
    if not all(ch["pass"] for ch in checks):
        if not explain:
            return None
        return {"strategy": "minervini", "checks": checks, "tag": "Fails the Trend Template", "tag_class": "z-ok",
                "plan": "", "stop": None, "required_ok": False}

    def depth(x):
        return float((x["High"].max() - x["Low"].min()) / x["High"].max())

    da, db, dc = depth(d.iloc[-65:-35]), depth(d.iloc[-35:-15]), depth(d.iloc[-15:])
    pivot0 = float(d["High"].iloc[-25:-5].max())
    # each pullback clearly smaller than the last, final one tight, price near the pivot
    vcp = db <= 0.75 * da and dc <= 0.75 * db and dc <= 0.10 and last >= pivot0 * 0.92
    dry = v.iloc[-10:].mean() < 0.8 * v.iloc[-60:-10].mean()
    pivot = float(h.iloc[-25:-5].max())
    avg50 = v.iloc[-55:-5].mean()
    base_ok = depth(d.iloc[-25:-5]) <= 0.12          # a real consolidation before the pivot
    brk = bool(base_ok and (c.iloc[-5:] > pivot).any() and v.iloc[-5:].max() >= 1.4 * avg50 and last > pivot * 0.97)
    eg = num(rec.get("earnings_per_share_diluted_yoy_growth_fq"))

    add("Volatility contraction (VCP)", vcp, 2, f"pullbacks {da * 100:.0f}% > {db * 100:.0f}% > {dc * 100:.0f}%")
    add("Volume drying up", dry, 1)
    add("Pivot breakout on volume", brk, 2, f"pivot {fmt_num(pivot)}")
    add("EPS growth > 25%", eg is not None and eg >= EPS_GROWTH_MIN, 1, "no data" if eg is None else f"{eg:+.0f}% YoY")

    if brk and last > pivot * (1 + MIN_PIVOT_CHASE):
        tag, cls = "Extended from pivot", "z-ext"
    elif brk:
        tag, cls = "Breaking out", "z-buy"
    elif vcp:
        tag, cls = "VCP forming", "z-ok"
    else:
        return None               # trend alone isn't a Minervini buy signal
    return {"strategy": "minervini", "checks": checks, "tag": tag, "tag_class": cls,
            "plan": f"Buy point: above the pivot {fmt_num(pivot)}. Sell rule: 8% stop below your entry.",
            "stop_pct": MIN_STOP, "required_ok": True}


# ---------------------------------------------------------------------------
# Strategy 4: Episodic pivot
# ---------------------------------------------------------------------------


def episodic(rec, d, explain=False):
    if len(d) < 70:
        return None
    o, h, l, c, v = (d[k] for k in ("Open", "High", "Low", "Close", "Volume"))
    v = v.fillna(0)
    n = len(d)
    best = None
    for j in range(n - 5, n):
        prev = c.iloc[j - 1]
        avg = v.iloc[j - 50:j].mean()
        if prev <= 0 or avg <= 0:
            continue
        gap = o.iloc[j] / prev - 1
        vx = v.iloc[j] / avg
        move = c.iloc[j] / prev - 1
        if gap >= EP_GAP and vx >= EP_VOL_MULT and move >= EP_MIN_MOVE:
            if best is None or vx > best["vx"]:
                best = {"j": j, "gap": float(gap), "vx": float(vx), "move": float(move), "low": float(l.iloc[j])}
    last = c.iloc[-1]
    if not best or last < best["low"] or n - 1 - best["j"] > EP_MAX_DAYS:
        if not explain:
            return None
        chk = []
        a = _check_adder(chk)
        a(f"Gap up 10%+ on 3x+ volume in the last {EP_MAX_DAYS} days, still holding it", False, 2, req=True)
        return {"strategy": "episodic", "checks": chk, "tag": "No recent gap", "tag_class": "z-ok",
                "plan": "", "stop": None, "required_ok": False}
    j = best["j"]
    days_ago = n - 1 - j
    neglect = c.iloc[j - 1] / c.iloc[max(0, j - 64)] - 1 < 0.30
    after = d.iloc[j + 1:]
    pinned = (len(after) >= 2 and best["gap"] >= 0.20
              and bool((((after["High"] - after["Low"]) / after["Close"]) < 0.015).all()))

    checks = []

    def add(label, ok, pts, note="", req=False):
        checks.append({"label": label, "pass": bool(ok), "pts": pts if ok else 0, "max": pts, "note": note, "req": req})

    add("Gap up 10%+", True, 2, f"{best['gap'] * 100:+.0f}% gap", req=True)
    add("Volume 3x+ normal", True, 2, f"{best['vx']:.1f}x", req=True)
    add("Held the gap", True, 1, f"closed {best['move'] * 100:+.0f}% that day", req=True)
    add("Big gap (20%+)", best["gap"] >= 0.20, 1)
    add("Huge volume (6x+)", best["vx"] >= 6, 1)
    add("From neglect (not already run up)", neglect, 1)
    add("Not a takeover pin", not pinned, 1, "price frozen after gap" if pinned else "")

    tag = "Possible takeover bid" if pinned else ("Gap today" if days_ago == 0 else f"Gap {days_ago} day{'s' if days_ago > 1 else ''} ago")
    cls = "z-low" if pinned else "z-buy"
    if not pinned and last > c.iloc[j] * 1.10:
        tag, cls = "Ran on after gap, wait", "z-ext"
    return {"strategy": "episodic", "checks": checks, "tag": tag, "tag_class": cls,
            "plan": f"Check the news behind the gap. Sell rule: below the gap-day low ({fmt_num(best['low'])}).",
            "stop": best["low"], "required_ok": True, "gap_date": str(d.index[j].date())}


# ---------------------------------------------------------------------------
# Strategies 6-7: US Investing Championship winners (daily charts)
# ---------------------------------------------------------------------------


def ema(x, n):
    return x.ewm(span=n, adjust=False).mean()


def _check_adder(checks):
    def add(label, ok, pts, note="", req=False):
        checks.append({"label": label, "pass": None if ok is None else bool(ok), "pts": pts if ok else 0,
                       "max": pts, "note": note, "req": req})
    return add


def luk(rec, d, ctx, explain=False):
    """Martin Luk's pullback: strong mover dips to the 9-day EMA and reclaims it."""
    if len(d) < 260:
        return None
    c, h, l, v = d["Close"], d["High"], d["Low"], d["Volume"].fillna(0)
    e9, e21, e50 = ema(c, 9), ema(c, 21), ema(c, 50)
    adr = float(((h / l) - 1).rolling(20).mean().iloc[-1])
    run = max(c.iloc[-1] / c.iloc[-22] - 1, c.iloc[-1] / c.iloc[-64] - 1)
    stack = e9.iloc[-1] > e21.iloc[-1] > e50.iloc[-1]
    i0 = h.iloc[-250:].idxmax()
    seg = d.loc[i0:]
    tp = (seg["High"] + seg["Low"] + seg["Close"]) / 3
    av = float((tp * seg["Volume"]).sum() / max(seg["Volume"].sum(), 1))
    n = len(d)
    sig = None
    for j in (n - 1, n - 2):
        prev9 = e9.iloc[j - 1]
        rng = h.iloc[j] - l.iloc[j]
        touch = l.iloc[j] <= prev9 and l.iloc[j] >= prev9 - adr * c.iloc[j - 1]
        upper = rng > 0 and (c.iloc[j] - l.iloc[j]) / rng >= 0.4
        if touch and upper and c.iloc[j] >= e9.iloc[j]:
            sig = j
            break
    still = c.iloc[-1] >= e9.iloc[-1]
    mkt = ctx.get("index_ok")
    checks = []
    add = _check_adder(checks)
    add("Big mover: up 30%+ in 1 or 3 months", run >= 0.30, 2, f"{run * 100:+.0f}%", req=True)
    add("Volatile enough (avg daily range 5%+)", adr >= 0.05, 1, f"{adr * 100:.1f}%", req=True)
    add("EMAs stacked 9 > 21 > 50", stack, 1, req=True)
    add("Above anchored VWAP from 1-year high", c.iloc[-1] > av, 1, f"VWAP {fmt_num(av)}", req=True)
    add("Pulled back to 9-day EMA and bounced", sig is not None, 2,
        "" if sig is None else ("today" if sig == n - 1 else "yesterday"), req=True)
    add("Still above the 9-day EMA", still, 1, req=True)
    add("Market OK (index 9-day EMA above 21-day)", mkt, 1, "no index data" if mkt is None else "", req=mkt is not None)
    add("Very fast mover (up 50%+)", run >= 0.50, 1)
    rs = num(rec.get("rs_rank"))
    add("RS rank 90+", rs is not None and rs >= 90, 1, "no data" if rs is None else f"RS {rs:.0f}")
    ok = all(ch["pass"] for ch in checks if ch["req"])
    if not ok and not explain:
        return None
    stop = float(max(l.iloc[sig], c.iloc[-1] * 0.95)) if sig is not None else float(c.iloc[-1] * 0.95)
    return {"strategy": "luk", "checks": checks, "tag": "Pullback to 9 EMA" if ok else "Not set up",
            "tag_class": "z-buy" if ok else "z-ok",
            "plan": f"Stop at the bounce-day low {fmt_num(stop)} (max 5% risk). Sell on a daily close below the "
                    f"9-day EMA ({fmt_num(e9.iloc[-1])}).",
            "stop": stop, "required_ok": ok}


def kell(rec, d, explain=False):
    """Oliver Kell's Cycle of Price Action: wedge pop or EMA crossback."""
    if len(d) < 210:
        return None
    c, h, l, v = d["Close"], d["High"], d["Low"], d["Volume"].fillna(0)
    e10, e20 = ema(c, 10), ema(c, 20)
    s50, s200 = c.rolling(50).mean(), c.rolling(200).mean()
    n = len(d)
    best = None
    for j in (n - 1, n - 2):
        prior = slice(j - 10, j)
        wedge = {
            "Drifted below the 10-day EMA (wedge)": int((c.iloc[prior] < e10.iloc[prior]).sum()) >= 5,
            "Lower highs into the pop": h.iloc[j - 10:j - 5].max() > h.iloc[j - 5:j].max(),
            "Popped above recent highs and both EMAs": bool(c.iloc[j] > h.iloc[j - 5:j].max()
                                                          and c.iloc[j] > e10.iloc[j] and c.iloc[j] > e20.iloc[j]),
            "Volume 1.3x+ average": v.iloc[j] >= 1.3 * v.iloc[j - 20:j].mean(),
            "Longer trend OK (50-day rising or above 200-day)": bool(s50.iloc[j] > s50.iloc[j - 10] or c.iloc[j] > s200.iloc[j]),
        }
        cross = {
            "Uptrend: 10 EMA > 20 EMA > 50-day, 50-day rising": bool(e10.iloc[j] > e20.iloc[j] > s50.iloc[j]
                                                                    and s50.iloc[j] > s50.iloc[j - 10]),
            "Had run 8%+ above the 20-day EMA, then pulled back": bool((c.iloc[j - 15:j - 3] / e20.iloc[j - 15:j - 3]).max() >= 1.08),
            "Dipped to the 20-day EMA": l.iloc[j - 3:j + 1].min() <= e20.iloc[j] * 1.01,
            "Bounced: closed above the 10-day EMA and the prior day's high": bool(c.iloc[j] > e10.iloc[j]
                                                                                 and c.iloc[j] > h.iloc[j - 1]),
            "Not stretched (within 5% of the 20-day EMA)": c.iloc[j] <= e20.iloc[j] * 1.05,
        }
        for name, conds in (("Wedge pop", wedge), ("EMA crossback", cross)):
            score = sum(conds.values()) / len(conds)
            if best is None or score > best[0]:
                best = (score, name, conds, j)
        if best[0] == 1:
            break
    score, name, conds, j = best
    checks = []
    add = _check_adder(checks)
    for label, okc in conds.items():
        add(label, okc, 1, req=True)
    rs = num(rec.get("rs_rank"))
    add("Market leader (RS rank 80+)", rs is not None and rs >= 80, 1, "no data" if rs is None else f"RS {rs:.0f}",
        req=rs is not None)
    add("Above the 200-day MA", c.iloc[-1] > s200.iloc[-1], 1)
    ok = all(ch["pass"] for ch in checks if ch["req"]) and c.iloc[-1] >= e10.iloc[-1]
    if not ok and not explain:
        return None
    low = float(l.iloc[max(0, j - 3):j + 1].min())
    return {"strategy": "kell", "checks": checks, "tag": name if ok else f"No {name.lower()} yet",
            "tag_class": "z-buy" if ok else "z-ok",
            "plan": f"Stop under the {name.lower()} low {fmt_num(low)}. Sell on a daily close below the 20-day EMA "
                    f"({fmt_num(e20.iloc[-1])}).",
            "stop": low, "required_ok": ok}


# ---------------------------------------------------------------------------
# Strategies 8-10: value and quality (TradingView fundamentals)
# ---------------------------------------------------------------------------


def value_context(u: pd.DataFrame, market: str, bench_perf_y=None) -> dict:
    """Market-wide figures the value methods rank against."""
    ctx = {"bench_perf_y": bench_perf_y}
    pb = pd.to_numeric(u.get("price_book_fq"), errors="coerce")
    pb = pb[pb > 0]
    ctx["pb_cut"] = float(pb.quantile(0.20)) if len(pb) >= 20 else None
    m = u[~u["sector"].astype(str).isin(["Finance", "Utilities"])].copy()
    m = m[m["market_cap_basic"] >= 50e6]
    evebit = pd.to_numeric(m.get("enterprise_value_to_ebit_ttm"), errors="coerce")
    roic = pd.to_numeric(m.get("return_on_invested_capital"), errors="coerce")
    m = m.assign(ey=1 / evebit, roic=roic)
    m = m[(m["ey"] > 0) & m["roic"].notna()]
    ranks = {}
    if len(m) >= 20:
        combo = m["ey"].rank(ascending=False) + m["roic"].rank(ascending=False)
        order = combo.rank(method="first")
        ranks = dict(zip(m["ticker"], order.astype(int)))
    ctx["magic_rank"], ctx["magic_n"] = ranks, len(m)
    return ctx


def _above50(rec):
    c, s = num(rec.get("close")), num(rec.get("SMA50"))
    return None if c is None or s is None else c > s


def _timing(add, rec):
    a = _above50(rec)
    add("Price above its 50-day MA (momentum check)", a, 1, "no data" if a is None else "", req=a is not None)
    return a


def slater(rec, ctx, market, explain=False):
    eps, fwd = num(rec.get("earnings_per_share_diluted_ttm")), num(rec.get("earnings_per_share_forecast_next_fy"))
    pe_f = num(rec.get("price_earnings_forward_fy")) or num(rec.get("price_earnings_ttm"))
    growth = (fwd / eps - 1) if (eps and fwd and eps > 0 and fwd > 0) else None
    peg = (pe_f / (growth * 100)) if (pe_f and growth and growth > 0) else None
    roce = num(rec.get("return_on_capital_employed_fy"))
    if roce is None:
        roce = num(rec.get("return_on_invested_capital"))
    perf_y, bpy = num(rec.get("Perf.Y")), ctx.get("bench_perf_y")
    rel = (perf_y - bpy) if (perf_y is not None and bpy is not None) else perf_y
    cap = num(rec.get("market_cap_basic"))
    checks = []
    add = _check_adder(checks)
    add("Forecast EPS growth over 15%", growth is not None and growth > 0.15, 2,
        "no forecast" if growth is None else f"{growth * 100:+.0f}%", req=True)
    add("PEG under 0.75", peg is not None and peg < 0.75, 2, "n/a" if peg is None else f"PEG {peg:.2f}", req=True)
    add("Forward P/E under 20", pe_f is not None and 0 < pe_f < 20, 1, "no data" if pe_f is None else f"P/E {pe_f:.1f}", req=True)
    add("Return on capital over 12%", roce is not None and roce > 12, 1, "no data" if roce is None else f"{roce:.0f}%", req=True)
    add("Beating the market over 1 year", rel is not None and rel > 0, 1, "no data" if rel is None else f"{rel:+.0f} pts", req=True)
    add("Small company (20m to 1bn)", cap is not None and 20e6 <= cap <= 1e9, 1,
        "no data" if cap is None else fmt_cap(cap), req=True)
    _timing(add, rec)
    ok = all(ch["pass"] for ch in checks if ch["req"])
    if not ok and not explain:
        return None
    return {"strategy": "slater", "checks": checks, "tag": "Zulu buy" if ok else "Doesn't fit",
            "tag_class": "z-buy" if ok else "z-ok",
            "plan": "A growth-at-a-low-price holding. Review if forecasts are cut, the PEG rises above 1, or it "
                    "underperforms the market.",
            "stop": None, "required_ok": ok}


def piotroski(rec, ctx, explain=False):
    f = num(rec.get("piotroski_f_score_ttm"))
    pb = num(rec.get("price_book_fq"))
    cut = ctx.get("pb_cut")
    z = num(rec.get("altman_z_score_ttm"))
    checks = []
    add = _check_adder(checks)
    add("F-Score 8 or 9 (strong, improving finances)", f is not None and f >= 8, 3,
        "no data" if f is None else f"F-Score {f:.0f}/9", req=True)
    add("Cheap: lowest 20% price-to-book in its market", pb is not None and cut is not None and 0 < pb <= cut, 2,
        "no data" if pb is None or cut is None else f"P/B {pb:.2f} (cut-off {cut:.2f})", req=True)
    add("Low bankruptcy risk (Altman Z over 1.8)", z is not None and z > 1.8, 1, "no data" if z is None else f"Z {z:.1f}")
    _timing(add, rec)
    ok = all(ch["pass"] for ch in checks if ch["req"])
    if not ok and not explain:
        return None
    return {"strategy": "piotroski", "checks": checks, "tag": f"F-Score {f:.0f}" if ok and f else "Doesn't fit",
            "tag_class": "z-buy" if ok else "z-ok",
            "plan": "Piotroski held for a year and rebalanced. Review when new annual figures drop the score.",
            "stop": None, "required_ok": ok}


def magic(rec, ctx, explain=False):
    rank, total = ctx.get("magic_rank", {}).get(rec.get("ticker")), ctx.get("magic_n", 0)
    evebit, roic = num(rec.get("enterprise_value_to_ebit_ttm")), num(rec.get("return_on_invested_capital"))
    checks = []
    add = _check_adder(checks)
    add("Top 30 on Magic Formula rank", rank is not None and rank <= 30, 3,
        "not ranked (financial, utility, loss-making or no data)" if rank is None else f"#{rank} of {total}", req=True)
    add("High earnings yield (EBIT / EV)", evebit is not None and evebit > 0, 1,
        "no data" if evebit is None else f"{100 / evebit:.0f}%" if evebit > 0 else "negative")
    add("High return on capital", roic is not None and roic > 15, 1, "no data" if roic is None else f"{roic:.0f}%")
    _timing(add, rec)
    ok = all(ch["pass"] for ch in checks if ch["req"])
    if not ok and not explain:
        return None
    return {"strategy": "magic", "checks": checks, "tag": f"Magic #{rank}" if ok else "Doesn't fit",
            "tag_class": "z-buy" if ok else "z-ok",
            "plan": "Greenblatt held each pick for a year, then re-ranked. Expect long spells of lagging the market.",
            "stop": None, "required_ok": ok}


def value_prefilter(u: pd.DataFrame, ctx: dict, market: str) -> pd.DataFrame:
    """Value picks are chosen from fundamentals alone, so find them across the whole universe."""
    keep = []
    for rec in u.to_dict("records"):
        if _above50(rec) is False:
            continue
        if slater(rec, ctx, market) or piotroski(rec, ctx) or magic(rec, ctx):
            keep.append(rec["ysym"])
    return u[u["ysym"].isin(keep)]


# ---------------------------------------------------------------------------
# Strategy 5: simple Stage 2 (Weinstein's trend definition only)
# ---------------------------------------------------------------------------


def stage2_simple(rec, w, bench_w, explain=False):
    c = w["Close"]
    if len(w) < 45:
        return None
    ma10, ma30 = c.rolling(10).mean(), c.rolling(30).mean()
    rising = ma30 > ma30.shift(4)
    in_s2 = (c > ma30) & rising & (ma10 > ma30)
    if not bool(in_s2.iloc[-1]):
        if not explain:
            return None
        chk = []
        a = _check_adder(chk)
        a("Above a rising 30-week MA", bool(c.iloc[-1] > ma30.iloc[-1] and rising.iloc[-1]), 2, req=True)
        a("10-week MA above 30-week MA", bool(ma10.iloc[-1] > ma30.iloc[-1]), 1, req=True)
        return {"strategy": "stage2", "checks": chk, "tag": "Not in Stage 2", "tag_class": "z-ok",
                "plan": "", "stop": None, "required_ok": False}
    weeks = 0
    for v in reversed(in_s2.tolist()):
        if not v:
            break
        weeks += 1
    m = mansfield_rs(w, bench_w)
    mrs = num(m.iloc[-1]) if m is not None else None
    ext = float(c.iloc[-1] / ma30.iloc[-1] - 1)
    rs = num(rec.get("rs_rank"))
    checks = []

    def add(label, ok, pts, note="", req=False):
        checks.append({"label": label, "pass": bool(ok), "pts": pts if ok else 0, "max": pts, "note": note, "req": req})

    add("Above a rising 30-week MA", True, 2, f"{ext * 100:+.0f}% above it", req=True)
    add("10-week MA above 30-week MA", True, 1, req=True)
    add("Beating the market (Mansfield RS > 0)", mrs is not None and mrs > 0, 2,
        "no index data" if mrs is None else f"{mrs * 100:+.0f}%", req=True)
    add(f"Early Stage 2 (last {STAGE2_MAX_WEEKS} weeks)", weeks <= STAGE2_MAX_WEEKS, 2,
        f"{weeks} week{'s' if weeks != 1 else ''} in Stage 2", req=True)
    add("Not stretched", ext <= STAGE2_MAX_EXT, 1, f"within {STAGE2_MAX_EXT * 100:.0f}% of the 30-week MA", req=True)
    add("RS rank 70+", rs is not None and rs >= 70, 1, "no data" if rs is None else f"RS {rs:.0f}")
    ok = all(ch["pass"] for ch in checks if ch["req"])
    return {"strategy": "stage2", "checks": checks,
            "tag": f"Stage 2 · {weeks} wk{'s' if weeks != 1 else ''}", "tag_class": "z-buy" if ok else "z-ok",
            "plan": f"Sell rule: weekly close below the 30-week MA ({fmt_num(ma30.iloc[-1])}).",
            "stop": None, "required_ok": ok}


# ---------------------------------------------------------------------------
# Market check
# ---------------------------------------------------------------------------


def market_check(bench_sym, bench_w):
    if bench_w is None or len(bench_w) < 35:
        return {"state": "unknown", "text": "Index data unavailable this week.", "bench": bench_sym}
    c = bench_w["Close"]
    ma = c.rolling(30).mean()
    above = c.iloc[-1] > ma.iloc[-1]
    rising = ma.iloc[-1] > ma.iloc[-5]
    ch = (c.iloc[-1] / c.iloc[-5] - 1) * 100
    if above and rising:
        st, txt = "up", "Uptrend: index above a rising 30-week MA. Breakouts have the wind behind them."
    elif above:
        st, txt = "mixed", "Mixed: index above its 30-week MA but the MA is flat or falling. Be selective."
    else:
        st, txt = "down", "Downtrend: index below its 30-week MA. Most breakouts fail in weak markets, so go small or wait."
    return {"state": st, "text": txt, "bench": bench_sym, "change_4w": round(float(ch), 1)}


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def fmt_num(x):
    x = num(x)
    if x is None:
        return "-"
    return f"{x:,.2f}" if x < 1000 else f"{x:,.0f}"


def make_card(rec, res, w, market, insider, ysym):
    c = w["Close"]
    ma10, ma30 = c.rolling(10).mean(), c.rolling(30).mean()
    score = sum(ch["pts"] for ch in res["checks"])
    mx = sum(ch["max"] for ch in res["checks"])
    exch = str(rec["exchange"]).upper()
    tick = str(rec["ticker"])
    return {
        "strategy": res["strategy"], "market": market,
        "ticker": tick, "exchange": exch, "name": str(rec.get("description") or tick), "ysym": ysym,
        "price": num(rec.get("close")), "currency": str(rec.get("currency") or ""),
        "mcap": num(rec.get("market_cap_basic")), "sector": str(rec.get("sector") or ""),
        "industry": str(rec.get("industry") or ""), "rs_rank": num(rec.get("rs_rank")),
        "score": score, "max_score": mx, "tag": res["tag"], "tag_class": res["tag_class"],
        "plan": res["plan"], "checks": res["checks"],
        "stop": res.get("stop"), "stop_pct": res.get("stop_pct"),
        "dist_ma10": float(c.iloc[-1] / ma10.iloc[-1] - 1) if not pd.isna(ma10.iloc[-1]) else None,
        "closes": [r4(x) for x in c.iloc[-30:]],
        "ma10": [None if pd.isna(x) else r4(x) for x in ma10.iloc[-30:]],
        "ma30": [None if pd.isna(x) else r4(x) for x in ma30.iloc[-30:]],
        "insider": insider,
        "entry_close": float(c.iloc[-1]),
        "chart_url": f"https://www.tradingview.com/chart/?symbol={exch}:{tick}&interval=W",
        "insider_url": insider_link(market, tick),
    }


def market_context(u, market, bench_d):
    bpy = None
    idx_ok = None
    if bench_d is not None and len(bench_d) >= 260:
        bc = bench_d["Close"]
        bpy = float((bc.iloc[-1] / bc.iloc[-253] - 1) * 100)
        idx_ok = bool(ema(bc, 9).iloc[-1] > ema(bc, 21).iloc[-1])
    ctx = value_context(u, market, bpy) if u is not None and len(u) else {"bench_perf_y": bpy}
    ctx["index_ok"] = idx_ok
    return ctx


def analyse_stock(rec, d, w, bench_w, ins, m, ctx, explain=False):
    """Run every method on one stock. Returns {method: result or None}."""
    out = {}
    lo, hi = MARKETS[m]["stine_price"]
    eps = num(rec.get("earnings_per_share_diluted_ttm"))
    price = num(rec.get("close")) or 0
    gate = lo <= price <= hi and not (REQUIRE_PROFIT and eps is not None and eps <= 0)
    r = stine(rec, w, ins, m) if (gate or explain) else None
    if r is not None and not gate:
        r["checks"].insert(0, {"label": "Price under 15 and profitable", "pass": False, "pts": 0, "max": 0,
                               "note": "outside Stine's price range or loss-making", "req": True})
        r["required_ok"] = False
    out["stine"] = r
    out["weinstein"] = weinstein(rec, w, bench_w, explain)
    out["minervini"] = minervini(rec, d, explain)
    out["episodic"] = episodic(rec, d, explain)
    out["stage2"] = stage2_simple(rec, w, bench_w, explain)
    out["luk"] = luk(rec, d, ctx, explain)
    out["kell"] = kell(rec, d, explain)
    out["slater"] = slater(rec, ctx, m, explain)
    out["piotroski"] = piotroski(rec, ctx, explain)
    out["magic"] = magic(rec, ctx, explain)
    return out


def run_pipeline(fetch_tv, fetch_prices, fetch_insiders, today: dt.date, ledger: list[dict], prev=None,
                 record=True, fetch_uk_dealings=None) -> dict:
    results = {"generated": dt.datetime.combine(today, dt.datetime.now(dt.timezone.utc).time(), dt.timezone.utc)
               .isoformat(timespec="minutes"), "date": today.isoformat(), "markets": {}}
    print("Fetching US insider purchases...")
    us_ins = fetch_insiders()

    universes, cands = {}, {}
    for m in MARKETS:
        print(f"\n== {MARKETS[m]['label']} ==")
        try:
            u = prepare_universe(fetch_tv(m), m)
            universes[m] = u
            vc = value_prefilter(u, value_context(u, m), m)
            cands[m] = pd.concat([pick_candidates(u), vc]).drop_duplicates("ysym")
            print(f"  universe {len(u)}, candidates {len(cands[m])} ({len(vc)} from value screens)")
        except Exception as e:
            print(f"  scan failed: {e}")
            results["markets"][m] = {"label": MARKETS[m]["label"], "error": str(e)}

    # one price download for candidates, benchmarks and open track-record picks
    syms = [s for m in cands for s in cands[m]["ysym"]]
    syms += [b for m in MARKETS for b in MARKETS[m]["bench"]]
    syms += [p["ysym"] for p in ledger if needs_update(p, today)]
    syms += [p["bench"] for p in ledger if needs_update(p, today) and p.get("bench")]
    prices = fetch_prices(syms)

    benches = {}
    for m in MARKETS:
        for b in MARKETS[m]["bench"]:
            if b in prices and len(prices[b]) >= 200:
                benches[m] = b
                break

    for m, cand in cands.items():
        bsym = benches.get(m)
        bench_w = to_weekly(prices[bsym]) if bsym else None
        ctx = market_context(universes[m], m, prices.get(bsym) if bsym else None)
        lists = {k: [] for k in STRATEGIES}
        for rec in cand.to_dict("records"):
            d = prices.get(rec["ysym"])
            if d is None or len(d) < 120:
                continue
            w = to_weekly(d)
            ins = us_ins.get(str(rec["ticker"]).upper()) if m == "america" else None
            try:
                for k, r in analyse_stock(rec, d, w, bench_w, ins, m, ctx).items():
                    if r and r["required_ok"]:
                        lists[k].append(make_card(rec, r, w, m, ins, rec["ysym"]))
            except Exception as e:
                print(f"  {rec['ysym']}: analysis error {e}")
        mc = market_check(bsym, bench_w)
        held_back = {}
        for k in lists:
            # only stocks at a buy point right now, best first, max 10
            ripe = [x for x in lists[k] if x["tag_class"] == "z-buy"]
            if MARKET_FILTER and mc["state"] == "down" and k in ("weinstein", "minervini", "stage2", "kell"):
                held_back[k] = len(ripe)
                ripe = []
            if k == "magic":
                ripe.sort(key=lambda x: int(re.sub(r"\D", "", x["tag"]) or 999))
            else:
                ripe.sort(key=lambda x: (x["score"], x.get("rs_rank") or 0), reverse=True)
            lists[k] = ripe[:SHOW_PER_LIST]
        if m == "uk" and fetch_uk_dealings:
            ticks = sorted({x["ticker"] for L in lists.values() for x in L})
            try:
                apply_uk_dealings(lists, fetch_uk_dealings(ticks, today) if ticks else {})
            except Exception as e:
                print(f"  UK director dealings failed: {e}")
        # cross-reference which methods flagged each ticker (Minervini "trend only" doesn't count)
        flagged: dict[str, list] = {}
        for k, L in lists.items():
            for x in L:
                if not (k == "minervini" and x["tag"] == "Trend only") and k != "stage2":
                    flagged.setdefault(x["ticker"], []).append((k, x))
        for k, L in lists.items():
            for x in L:
                x["also"] = [STRATEGIES[kk]["short"] for kk, _ in flagged.get(x["ticker"], []) if kk != k]
        multi = []
        for tick, hits in flagged.items():
            if len(hits) < 2:
                continue
            best = max(hits, key=lambda h: h[1]["score"] / max(h[1]["max_score"], 1))[1]
            multi.append({**best, "strategy": best["strategy"], "count": len(hits),
                          "methods": [{"strategy": kk, "tag": x["tag"], "tag_class": x["tag_class"],
                                       "score": x["score"], "max_score": x["max_score"], "plan": x["plan"]}
                                      for kk, x in hits],
                          "avg_pct": float(np.mean([x["score"] / max(x["max_score"], 1) for _, x in hits]))})
        multi.sort(key=lambda e: (e["count"], e["avg_pct"]), reverse=True)
        results["markets"][m] = {
            "label": MARKETS[m]["label"], "universe": int(len(universes[m])), "checked": int(len(cand)),
            "market": mc, "bench": bsym, "held_back": held_back,
            "lists": lists, "multi": multi,
        }
        print(f"  " + ", ".join(f"{STRATEGIES[k]['short']} {len(v)}" for k, v in lists.items())
              + f", 2+ methods {len(multi)}")

    update_ledger(ledger, results, prices, today, prev, record)
    return results


# ---------------------------------------------------------------------------
# Track record
# ---------------------------------------------------------------------------


def needs_update(p, today):
    return p.get("final") is not True


def price_on_or_after(d: pd.DataFrame, when: pd.Timestamp):
    s = d.loc[d.index >= when, "Close"]
    return None if s.empty else float(s.iloc[0])


def price_at(d: pd.DataFrame, when: pd.Timestamp):
    s = d.loc[d.index <= when, "Close"]
    return None if s.empty else float(s.iloc[-1])


def evaluate_pick(p, d, bd, today):
    t0 = pd.Timestamp(p["date"])
    after = d.loc[d.index > t0]
    now = float(d["Close"].iloc[-1])
    p["last"] = now
    p["ret_now"] = now / p["entry"] - 1
    if bd is not None and p.get("bench_entry"):
        p["bench_now"] = float(bd["Close"].iloc[-1]) / p["bench_entry"] - 1
    for hz in HORIZONS:
        when = t0 + pd.Timedelta(weeks=hz)
        if pd.Timestamp(today) < when:
            continue
        px = price_at(d, when)
        if px is None:
            continue
        p[f"r{hz}"] = px / p["entry"] - 1
        if bd is not None and p.get("bench_entry"):
            bp = price_at(bd, when)
            if bp:
                p[f"b{hz}"] = bp / p["bench_entry"] - 1

    # sell rule
    exit_px, exit_date = None, None
    if p["strategy"] in ("luk", "kell"):
        e = ema(d["Close"], 9 if p["strategy"] == "luk" else 20)
        stop = p.get("stop")
        for ix, row in after.iterrows():
            if stop and row["Low"] <= stop:
                exit_px, exit_date = float(min(row["Open"], stop)), ix
                break
            if row["Close"] < e.loc[ix]:
                exit_px, exit_date = float(row["Close"]), ix
                break
    elif p["strategy"] in ("stine", "weinstein", "consensus", "stage2"):
        w = to_weekly(d)
        ma = w["Close"].rolling(10 if p["strategy"] == "stine" else 30).mean()
        wk = w.loc[w.index > t0]
        for ix, row in wk.iterrows():
            if not pd.isna(ma.loc[ix]) and row["Close"] < ma.loc[ix]:
                exit_px, exit_date = float(row["Close"]), ix
                break
    elif p["strategy"] not in VALUE_METHODS:
        stop = p.get("stop")
        if stop is None and p.get("stop_pct"):
            stop = p["entry"] * (1 - p["stop_pct"])
        if stop:
            for ix, row in after.iterrows():
                if row["Low"] <= stop:
                    exit_px, exit_date = float(min(row["Open"], stop)), ix
                    break
    if exit_px is not None:
        p["exit"] = exit_px
        p["exit_date"] = str(exit_date.date())
        p["ret_rule"] = exit_px / p["entry"] - 1
        if bd is not None and p.get("bench_entry"):
            bp = price_on_or_after(bd, exit_date)
            p["bench_rule"] = (bp / p["bench_entry"] - 1) if bp else p.get("bench_now")
    else:
        p.pop("exit", None), p.pop("exit_date", None)
        p["ret_rule"] = p["ret_now"]
        p["bench_rule"] = p.get("bench_now")
    if f"r{HORIZONS[-1]}" in p:
        p["final"] = True


def previous_keys(prev):
    """(method, market, ticker) for everything listed in last week's scan."""
    keys = set()
    for m, data in ((prev or {}).get("markets") or {}).items():
        for k, L in (data.get("lists") or {}).items():
            keys.update((k, m, x["ticker"]) for x in L)
        keys.update(("consensus", m, x["ticker"]) for x in data.get("multi") or [])
    return keys


def update_ledger(ledger, results, prices, today, prev=None, record=True):
    today_s = today.isoformat()
    if record:
        ledger[:] = [p for p in ledger if p["date"] != today_s]   # re-runs on the same day replace
    recent = {(p["strategy"], p["market"], p["ticker"]) for p in ledger
              if (today - dt.date.fromisoformat(p["date"])).days <= NEW_WINDOW_WEEKS * 7}
    last_week = previous_keys(prev)
    for m, data in results["markets"].items():
        if "lists" not in data:
            continue
        bsym = data.get("bench")
        bclose = float(prices[bsym]["Close"].iloc[-1]) if bsym in prices else None
        for k, L in data["lists"].items():
            for i, x in enumerate(L):
                key = (k, m, x["ticker"])
                x["new"] = key not in last_week
                # only record what you'd actually buy: buy-zone picks, once per 8 weeks
                if not record or i >= RECORD_PER_LIST or key in recent or x["tag_class"] != "z-buy":
                    continue
                if x["tag"] == "Possible takeover bid":
                    continue
                ledger.append({
                    "date": today_s, "market": m, "strategy": k, "ticker": x["ticker"],
                    "exchange": x["exchange"], "name": x["name"], "sector": x["sector"], "ysym": x["ysym"],
                    "entry": x["entry_close"], "bench": bsym, "bench_entry": bclose,
                    "score": x["score"], "max_score": x["max_score"], "tag": x["tag"],
                    "stop": x.get("stop"), "stop_pct": x.get("stop_pct"), "rules": RULES_VERSION,
                })
        for i, x in enumerate(data.get("multi", [])):
            key = ("consensus", m, x["ticker"])
            x["new"] = key not in last_week
            if not record or i >= RECORD_PER_LIST or key in recent or not any(h["tag_class"] == "z-buy" for h in x["methods"]):
                continue
            ledger.append({
                "date": today_s, "market": m, "strategy": "consensus", "ticker": x["ticker"],
                "exchange": x["exchange"], "name": x["name"], "sector": x["sector"], "ysym": x["ysym"],
                "entry": x["entry_close"], "bench": bsym, "bench_entry": bclose,
                "score": x["count"], "max_score": len(STRATEGIES),
                "tag": " + ".join(STRATEGIES[h["strategy"]]["short"] for h in x["methods"]),
                "stop": None, "stop_pct": None, "rules": RULES_VERSION,
            })
    for p in ledger:
        if p["date"] == today_s or not needs_update(p, today):
            continue
        d = prices.get(p["ysym"])
        if d is None or d.empty:
            continue
        bd = prices.get(p.get("bench")) if p.get("bench") else None
        try:
            evaluate_pick(p, d, bd, today)
        except Exception as e:
            print(f"  track record {p['ysym']}: {e}")


def current(ledger):
    return [p for p in ledger if p.get("rules", 1) == RULES_VERSION]


def summarise(ledger):
    out = {}
    ledger = current(ledger)
    for k in TRACKED:
        ps = [p for p in ledger if p["strategy"] == k]
        row = {"picks": len(ps)}
        live = [p for p in ps if "ret_now" in p]
        if live:
            ex = [p["ret_now"] - (p.get("bench_now") or 0) for p in live]
            exr = [p["ret_rule"] - (p.get("bench_rule") or 0) for p in live]
            row["so_far"] = {"n": len(live), "avg": float(np.mean([p["ret_now"] for p in live])),
                             "excess": float(np.mean(ex)), "beat": float(np.mean([e > 0 for e in ex])),
                             "rule_avg": float(np.mean([p["ret_rule"] for p in live])),
                             "rule_excess": float(np.mean(exr))}
        for hz in HORIZONS:
            done = [p for p in ps if f"r{hz}" in p]
            if done:
                ex = [p[f"r{hz}"] - (p.get(f"b{hz}") or 0) for p in done]
                row[f"h{hz}"] = {"n": len(done), "avg": float(np.mean([p[f"r{hz}"] for p in done])),
                                 "excess": float(np.mean(ex)), "beat": float(np.mean([e > 0 for e in ex]))}
        out[k] = row
    return out



# ---------------------------------------------------------------------------
# US politicians' disclosed buys (House periodic transaction reports)
# ---------------------------------------------------------------------------

UA = {"User-Agent": "Mozilla/5.0 (compatible; superstock-scanner; personal research)"}


def pol_match(first, last):
    first, last = (first or "").strip().lower(), (last or "").strip().lower()
    for f, l in POLITICIANS:
        if last == l.lower() and first[:1] == f[:1].lower():
            return f"{f} {l}"
    return None


def house_index(year):
    """Periodic transaction report filings for one year from the House Clerk's free annual file."""
    import requests, zipfile
    import xml.etree.ElementTree as ET

    r = requests.get(HOUSE_ZIP.format(year=year), headers=UA, timeout=90)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    xml_name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
    root = ET.fromstring(z.read(xml_name))
    out = []
    for m in root.iter("Member"):
        g = {c.tag: (c.text or "").strip() for c in m}
        if g.get("FilingType") != "P":
            continue
        who = pol_match(g.get("First"), g.get("Last"))
        if not who:
            continue
        try:
            filed = dt.datetime.strptime(g.get("FilingDate", ""), "%m/%d/%Y").date()
        except ValueError:
            continue
        out.append({"who": who, "state": g.get("StateDst", ""), "filed": filed.isoformat(),
                    "doc": g.get("DocID", ""), "year": int(g.get("Year") or year)})
    return out


AMOUNT = r"(?:\$[\d,]+\s*-\s*\$[\d,]+|Over\s+\$[\d,]+|\$[\d,]+)"
TRADE_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)\s*\[([A-Z]{2})\]\s*(P|S\s*\(partial\)|S|E)\s+"
                      r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(" + AMOUNT + ")")


def parse_ptr_text(text):
    """Pull trades out of the text of an electronically filed House PTR."""
    t = re.sub(r"\s+", " ", text.replace("\x00", ""))
    trades, prev_end = [], 0
    for m in TRADE_RE.finditer(t):
        seg = t[prev_end:m.start()]
        seg = re.split(r"Cap\.? Gains > \$200\??|F\s*S\s*:\s*(?:New|Amended)|Filing Status:\s*\w+", seg)[-1]
        parts = re.split(r"(?:^|\s)(?:SP|JT|DC)\s", seg)
        if len(parts) > 1:
            name = parts[-1]
        elif re.search(r"\bD\s*:|Description:", seg):
            name = re.split(r"\.\s+", seg)[-1]
        else:
            name = seg
        name = re.sub(r"\s*(gfedc|Yes|No)\s*$", "", name.strip()).strip(" -:")[-90:]
        trades.append({
            "ticker": m.group(1),
            "asset_type": {"ST": "Stock", "OP": "Option", "AB": "Bond", "EF": "Fund", "OT": "Other"}.get(m.group(2), m.group(2)),
            "type": "buy" if m.group(3) == "P" else ("exchange" if m.group(3) == "E" else "sell"),
            "traded": dt.datetime.strptime(m.group(4), "%m/%d/%Y").date().isoformat(),
            "amount": re.sub(r"\s+", " ", m.group(6)).replace(" - ", "–").replace("-", "–"),
            "asset": name[-90:],
        })
        prev_end = m.end()
    return trades


def ptr_text(year, doc):
    import requests

    r = requests.get(HOUSE_PTR.format(year=year, doc=doc), headers=UA, timeout=60)
    r.raise_for_status()
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            return "\n".join((pg.extract_text() or "") for pg in pdf.pages)
    except ImportError:
        from pypdf import PdfReader
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(r.content)).pages)


def live_politicians(today, seen: set):
    """New watch-list filings since last run -> (list of filings with parsed trades, all doc ids seen)."""
    years = {today.year} | ({today.year - 1} if today.month <= 2 else set())
    filings = []
    for y in sorted(years):
        try:
            filings += house_index(y)
        except Exception as e:
            print(f"  House index {y} unavailable: {e}")
    cutoff = (today - dt.timedelta(days=POL_DAYS)).isoformat()
    out = []
    for f in filings:
        if f["doc"] in seen or f["filed"] < cutoff:
            continue
        try:
            f["trades"] = parse_ptr_text(ptr_text(f["year"], f["doc"]))
            f["readable"] = bool(f["trades"])
        except Exception as e:
            print(f"  PTR {f['doc']} failed: {e}")
            f["trades"], f["readable"] = [], False
        f["url"] = HOUSE_PTR.format(year=f["year"], doc=f["doc"])
        out.append(f)
        time.sleep(0.5)
    print(f"  politicians: {len(out)} new filing(s) from the watch-list")
    return out


def update_politicians(state: dict, new_filings: list, today) -> tuple[dict, list]:
    """Merge new filings into saved state. Returns (state, newly reported buys to alert on)."""
    first_run = not state.get("seen")
    seen = set(state.get("seen", []))
    buys = state.get("buys", [])
    fresh = []
    for f in new_filings:
        seen.add(f["doc"])
        if not f["readable"]:
            item = {"who": f["who"], "filed": f["filed"], "url": f["url"], "unreadable": True, "doc": f["doc"]}
            buys.append(item)
            fresh.append(item)
            continue
        for t in f["trades"]:
            if t["type"] != "buy":
                continue
            item = {**t, "who": f["who"], "filed": f["filed"], "url": f["url"], "doc": f["doc"],
                    "lag": (dt.date.fromisoformat(f["filed"]) - dt.date.fromisoformat(t["traded"])).days}
            buys.append(item)
            fresh.append(item)
    cutoff = (today - dt.timedelta(days=POL_DAYS)).isoformat()
    buys = [b for b in buys if b["filed"] >= cutoff]
    buys.sort(key=lambda b: (b["filed"], b.get("traded", "")), reverse=True)
    state = {"seen": sorted(seen), "buys": buys, "updated": today.isoformat()}
    return state, ([] if first_run else fresh)


def pol_by_ticker(state):
    out = {}
    for b in (state or {}).get("buys", []):
        if b.get("ticker") and b.get("asset_type") in ("Stock", "Option"):
            out.setdefault(b["ticker"].upper(), []).append(b)
    return out


def politician_alerts(fresh, results=None):
    if not fresh:
        return
    listed = {}
    for data in ((results or {}).get("markets") or {}).values():
        for k, L in (data.get("lists") or {}).items():
            for x in L:
                listed.setdefault(x["ticker"].upper(), []).append(STRATEGIES[k]["short"])
    for b in fresh[:15]:
        if b.get("unreadable"):
            push(f"{b['who']} filed a trade report", "It couldn't be read automatically (scanned form). Tap to open it.",
                 3, "classical_building")
            continue
        when = dt.date.fromisoformat(b["traded"]).strftime("%d %b")
        also = listed.get(b["ticker"].upper())
        extra = f" Also on this week's {', '.join(also)} list." if also else ""
        push(f"{b['who']} bought {b['ticker']}",
             f"{b['asset_type']}: {b['asset']} · {b['amount']} · traded {when}, reported {b['lag']} days later.{extra}",
             4 if also else 3, "classical_building")


def politicians_html(state):
    intro = ('<p class="blurb">Purchases reported by the House members on your watch-list (the best estimated returns '
             'in Unusual Whales\' 2024 and 2025 Congress-trading reports). Members can report up to 45 days after trading, '
             'so these are <b>late signals</b>, not live ones. Senators aren\'t included because the Senate site blocks '
             'automated access. Edit the list in SETTINGS.</p>')
    names = ", ".join(f"{f} {l}" for f, l in POLITICIANS)
    intro += f'<p class="small">Watching: {E(names)}</p>'
    buys = (state or {}).get("buys") or []
    if not buys:
        return intro + f'<p class="empty">No purchases reported by the watch-list in the last {POL_DAYS} days.</p>'
    rows = []
    for b in buys[:60]:
        filed = dt.date.fromisoformat(b["filed"]).strftime("%d %b")
        if b.get("unreadable"):
            rows.append(f'<tr><td><b>{E(b["who"])}</b><br><small>filed {filed}</small></td>'
                        f'<td colspan="2"><a href="{E(b["url"])}" target="_blank" rel="noopener">Scanned report, open it ↗</a></td></tr>')
            continue
        traded = dt.date.fromisoformat(b["traded"]).strftime("%d %b")
        tick = E(b["ticker"])
        chart = f"https://www.tradingview.com/chart/?symbol={tick}&interval=W"
        rows.append(f'<tr><td><b>{E(b["who"])}</b><br><small>traded {traded} · filed {filed} ({b["lag"]}d)</small></td>'
                    f'<td><a href="{chart}" target="_blank" rel="noopener"><b>{tick}</b></a> <small>{E(b["asset_type"])}</small>'
                    f'<br><small>{E(b["asset"][:40])}</small></td>'
                    f'<td><small>{E(b["amount"])}</small><br><a href="{E(b["url"])}" target="_blank" rel="noopener"><small>report ↗</small></a></td></tr>')
    return intro + '<table class="picks"><tr><th>Who</th><th>Bought</th><th>Amount</th></tr>' + "".join(rows) + "</table>"


# ---------------------------------------------------------------------------
# UK director / PDMR dealings (RNS), for UK picks only
# ---------------------------------------------------------------------------

def _clean_html(h):
    h = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|td|th|li|h\d)>", "\n", h)
    t = html.unescape(re.sub(r"<[^>]+>", " ", h))
    return re.sub(r"[ \t\xa0]+", " ", t)


NOT_A_BUY = re.compile(r"\b(sale|sold|dispos|exercis|award|grant|vest|transfer|gift|lapse|share incentive|SIP\b|"
                       r"dividend reinvest|DRIP|partnership shares|matching shares|nil[- ]cost|LTIP|option)", re.I)
IS_BUY = re.compile(r"\b(purchas|acqui|bought|buy|subscri)", re.I)


def _parse_date(sn):
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(sn.strip().rstrip(".,"), fmt).date()
        except ValueError:
            pass
    return None


def gbp(sn):
    """'185.5p' -> 1.855, '£1.85' -> 1.85, 'GBP 1.85' -> 1.85. A bare number is ambiguous, so None."""
    sn = sn.strip()
    v = re.search(r"[\d.,]+", sn)
    if not v:
        return None
    x = float(v.group(0).replace(",", ""))
    if "£" in sn or "GBP" in sn.upper():
        return x
    if re.search(r"p\b|pence", sn, re.I):
        return x / 100
    return None


def parse_pdmr(text):
    """Read a Director/PDMR Shareholding announcement. Returns a dict for a purchase, else None."""
    t = re.sub(r"\s+", " ", text)
    nat = re.search(r"Nature of the transaction\s*:?\s*(.{0,160}?)(?:\s[c-f]\)|\sPrice\(s\)|\sPrice)", t, re.I)
    nature = nat.group(1).strip() if nat else ""
    basis = nature or t[:1500]
    if not IS_BUY.search(basis) or (nature and NOT_A_BUY.search(nature)):
        return None
    if not nature and NOT_A_BUY.search(t[:800]) and not re.search(r"\b(purchased|bought)\b", t[:1500], re.I):
        return None
    who = re.search(r"\bName\s*:?\s+(?:of natural person\s+)?([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){1,3})", t)
    role = re.search(r"Position\s*/\s*status\s*:?\s+(.{3,60}?)(?:\s[a-f]\)|\sInitial|\s3\b)", t, re.I)
    if not who:
        pm = re.search(r"\bthat\s+((?:Mr|Mrs|Ms|Dr)\.?\s+)?([A-Z][a-z]+(?:\s+[A-Z][A-Za-z'\-]+){1,2}),\s+(?:the\s+|a\s+|an\s+|our\s+)?"
                       r"([^,]{3,50}?),\s+(?:has\s+)?(?:purchased|bought|acquired|subscribed)", t)
        if pm:
            who, role = pm, pm
            name_s, role_s = pm.group(2), pm.group(3)
    when = re.search(r"Date of the transaction\s*:?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Z][a-z]+\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})", t, re.I)
    shares = price = None
    agg = re.search(r"Aggregated.{0,160}?volume[^0-9]{0,40}([\d,]{2,})", t, re.I)
    if agg:
        shares = int(agg.group(1).replace(",", ""))
        chunk = t[agg.start():agg.start() + 260]
        pr = re.search(r"(£\s*[\d.,]+|GBP\s*[\d.,]+|[\d.,]+\s*(?:p\b|pence|GBP))", chunk, re.I)
        price = gbp(pr.group(1)) if pr else None
    if shares is None:
        pv = re.search(r"Price\(s\)\s*(?:and|&)\s*volume\(s\).{0,80}?((?:£|GBP)?\s*[\d.]+\s*(?:p\b|pence|GBP)?)\s+([\d,]{3,})", t, re.I)
        if pv:
            price, shares = gbp(pv.group(1)), int(pv.group(2).replace(",", ""))
    if shares is None:
        fb = re.search(r"(?:purchased|bought|acquired|subscribed for)\s+(?:a total of\s+)?([\d,]{3,})\s+(?:new\s+)?(?:ordinary\s+)?shares"
                       r".{0,80}?(?:at|price of)\s+(?:an average (?:price )?of\s+)?((?:£)?[\d.]+\s*(?:p\b|pence)?)", t, re.I)
        if fb:
            shares, price = int(fb.group(1).replace(",", "")), gbp(fb.group(2))
    if who is not None and who.re.pattern.startswith("\\bthat"):
        nm, rl = who.group(2), who.group(3).strip()
    else:
        nm, rl = (who.group(1) if who else None), (role.group(1).strip() if role else None)
    return {"name": nm, "role": rl,
            "nature": nature[:80] or "purchase", "date": (_parse_date(when.group(1)).isoformat()
                                                         if when and _parse_date(when.group(1)) else None),
            "shares": shares, "price_gbp": price,
            "value_gbp": round(shares * price) if shares and price else None}


def live_uk_dealings(tickers, today):
    """Director purchases in the last 90 days for each UK ticker, from Investegate's RNS pages."""
    import requests

    out = {}
    cutoff = today - dt.timedelta(days=UK_DEALING_DAYS)
    for tk in tickers:
        sym = str(tk).rstrip(".").replace(".", "")
        try:
            r = requests.get(f"{INVESTEGATE}/company/{sym}", headers=UA, timeout=30)
            if r.status_code != 200:
                continue
            links = re.findall(r'<a[^>]+href="([^"]*/announcement/[^"]+)"[^>]*>(.*?)</a>', r.text, re.I | re.S)
            seen, items = set(), []
            for href, label in links:
                lab = re.sub(r"<[^>]+>", " ", label)
                if not re.search(r"director|pdmr", lab, re.I) or not re.search(r"shareholding|dealing|purchase|share", lab, re.I):
                    continue
                url = href if href.startswith("http") else INVESTEGATE + href
                if url in seen:
                    continue
                seen.add(url)
                if len(seen) > 6:
                    break
                a = requests.get(url, headers=UA, timeout=30)
                if a.status_code != 200:
                    continue
                text = _clean_html(a.text)
                pub = re.search(r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})", text)
                pub_d = _parse_date(pub.group(1)) if pub else None
                if pub_d and pub_d < cutoff:
                    break                      # announcements are newest first
                d = parse_pdmr(text)
                if d:
                    d["date"] = d["date"] or (pub_d.isoformat() if pub_d else None)
                    if d["date"] and dt.date.fromisoformat(d["date"]) < cutoff:
                        continue
                    d["url"] = url
                    items.append(d)
                time.sleep(0.4)
            if items:
                out[str(tk)] = items
        except Exception as e:
            print(f"  RNS {tk}: {e}")
        time.sleep(0.4)
    print(f"  RNS: director buys found for {len(out)} of {len(tickers)} UK picks")
    return out


def apply_uk_dealings(lists, dealings):
    for k, L in lists.items():
        for x in L:
            items = dealings.get(x["ticker"])
            if not items:
                continue
            vals = [i["value_gbp"] for i in items if i.get("value_gbp")]
            latest = max((i["date"] for i in items if i.get("date")), default="")
            x["insider"] = {"buys": len(items), "value": float(sum(vals)) if vals else None, "currency": "GBP",
                            "insiders": sorted({f'{i["name"]}{" (" + i["role"] + ")" if i.get("role") else ""}'
                                                for i in items if i.get("name")})[:4],
                            "latest": latest, "source": "RNS", "url": items[0]["url"]}
            for c in x["checks"]:
                if c["label"] == "Insider buying":
                    c.update(label="Director buying (90 days)", **{"pass": True}, pts=2, max=2,
                             note=f'{len(items)} buy(s)' + (f', £{sum(vals):,.0f}' if vals else ""))
            x["score"] = sum(c["pts"] for c in x["checks"])
            x["max_score"] = sum(c["max"] for c in x["checks"])
        L.sort(key=lambda y: (y["score"], y.get("rs_rank") or 0), reverse=True)


def uk_dealing_alerts(results, state: dict) -> dict:
    data = (results.get("markets") or {}).get("uk") or {}
    new_state = dict(state)
    sent = 0
    for k, L in (data.get("lists") or {}).items():
        for x in L:
            ins = x.get("insider")
            if not ins or ins.get("source") != "RNS":
                continue
            key = f'{x["ticker"]}:{ins.get("latest")}'
            if key in new_state:
                continue
            new_state[key] = "sent"
            if sent < 10:
                who = ", ".join(ins["insiders"][:2]) or "a director"
                val = f' (£{ins["value"]:,.0f})' if ins.get("value") else ""
                push(f'Director buying: {x["ticker"]} ({STRATEGIES[k]["short"]})',
                     f'{who} bought{val}, latest {ins.get("latest") or "recently"}. {x["name"]} is at a '
                     f'{STRATEGIES[k]["short"]} buy point: {x["tag"]}.', 4, "moneybag")
                sent += 1
    return new_state


# ---------------------------------------------------------------------------
# Evaluate a stock: any ticker you add, run through every method with reasons
# ---------------------------------------------------------------------------

MARKET_ALIASES = {"uk": "uk", "lse": "uk", "aim": "uk", "lon": "uk", "gb": "uk",
                  "us": "america", "usa": "america", "america": "america", "nasdaq": "america", "nyse": "america",
                  "amex": "america", "ca": "canada", "canada": "canada", "tsx": "canada", "tsxv": "canada",
                  "cse": "canada", "neo": "canada"}


def parse_symbols(text: str) -> list[tuple[str, str]]:
    """'KOO:uk, TSLA:us, LSE:AT.' -> [(ticker, market), ...]"""
    out = []
    for part in re.split(r"[,\s]+", (text or "").strip()):
        part = re.sub(r"[^A-Za-z0-9.:\-]", "", part)
        if not part:
            continue
        bits = part.split(":")
        if len(bits) == 2 and bits[0].lower() in MARKET_ALIASES and bits[1].lower() not in MARKET_ALIASES:
            bits = [bits[1], bits[0]]          # EXCHANGE:TICKER form
        tick = bits[0].upper()[:12]
        mkt = MARKET_ALIASES.get(bits[1].lower(), "uk") if len(bits) > 1 else "uk"
        if tick:
            out.append((tick, mkt))
    return out


def update_watchlist(watch: list, add: str, remove: str, today) -> list:
    keys = {(w["ticker"], w["market"]) for w in watch}
    for t, m in parse_symbols(add):
        if (t, m) not in keys:
            watch.append({"ticker": t, "market": m, "added": today.isoformat()})
            keys.add((t, m))
    drop = set(parse_symbols(remove))
    return [w for w in watch if (w["ticker"], w["market"]) not in drop]


def _verdict(r):
    if r is None:
        return "No data", "v-na"
    if r["required_ok"] and r["tag_class"] == "z-buy":
        return "Buy point now", "v-buy"
    if r["required_ok"]:
        return "Qualifies, not at a buy point", "v-wait"
    return "Doesn't qualify", "v-no"


def run_evaluations(watch, fetch_tv, fetch_prices, fetch_insiders, fetch_uk_dealings, today, fetch_tv_names=None):
    out = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "items": []}
    if not watch:
        return out
    by_m: dict[str, list] = {}
    for w in watch:
        by_m.setdefault(w["market"], []).append(w["ticker"])
    us_ins = fetch_insiders() if "america" in by_m and fetch_insiders else {}
    for m, ticks in by_m.items():
        try:
            u = prepare_universe(fetch_tv(m), m)
        except Exception as e:
            print(f"  evaluate: universe for {m} failed: {e}")
            u = None
        names = sorted({t for t in ticks} | {t + "." for t in ticks if not t.endswith(".")})
        try:
            raw = fetch_tv_names(m, names) if fetch_tv_names else fetch_tv(m)
        except Exception as e:
            print(f"  evaluate: lookup failed: {e}")
            raw = pd.DataFrame()
        rows = {}
        for rec in raw.to_dict("records") if len(raw) else []:
            rows[str(rec["name"]).upper().rstrip(".")] = rec
        recs = {}
        for t in ticks:
            rec = rows.get(t.rstrip("."))
            if rec is None:
                continue
            rec = dict(rec)
            rec["ticker"] = str(rec["name"])
            rec["ysym"] = yahoo_symbol(rec["exchange"], rec["name"])
            if u is not None and len(u):
                p3, p6, p12 = (num(rec.get(c)) for c in ("Perf.3M", "Perf.6M", "Perf.Y"))
                p6 = p6 if p6 is not None else p3
                p12 = p12 if p12 is not None else p6
                if p3 is not None:
                    comp_u = (0.4 * pd.to_numeric(u["Perf.3M"], errors="coerce")
                              + 0.3 * pd.to_numeric(u["Perf.6M"], errors="coerce").fillna(pd.to_numeric(u["Perf.3M"], errors="coerce"))
                              + 0.3 * pd.to_numeric(u["Perf.Y"], errors="coerce").fillna(pd.to_numeric(u["Perf.3M"], errors="coerce"))).dropna()
                    comp = 0.4 * p3 + 0.3 * p6 + 0.3 * p12
                    rec["rs_rank"] = round(float((comp_u < comp).mean()) * 98 + 1) if len(comp_u) else None
            recs[t] = rec
        syms = [r["ysym"] for r in recs.values()] + MARKETS[m]["bench"]
        prices = fetch_prices(syms)
        bsym = next((b for b in MARKETS[m]["bench"] if b in prices and len(prices[b]) >= 200), None)
        bench_w = to_weekly(prices[bsym]) if bsym else None
        ctx = market_context(u, m, prices.get(bsym) if bsym else None)
        dealings = {}
        if m == "uk" and fetch_uk_dealings and recs:
            try:
                dealings = fetch_uk_dealings([r["ticker"] for r in recs.values()], today)
            except Exception as e:
                print(f"  evaluate: RNS failed: {e}")
        for t in ticks:
            item = {"key": f"{t}:{m}", "ticker": t, "market": m, "updated": today.isoformat()}
            rec = recs.get(t)
            d = prices.get(rec["ysym"]) if rec else None
            if rec is None or d is None or len(d) < 60:
                item["error"] = (f"Couldn't find {t} on the {MARKETS[m]['label']} market." if rec is None
                                 else f"Found {t}, but couldn't get its price history.")
                out["items"].append(item)
                continue
            w = to_weekly(d)
            ins = us_ins.get(t) if m == "america" else None
            try:
                res = analyse_stock(rec, d, w, bench_w, ins, m, ctx, explain=True)
            except Exception as e:
                item["error"] = f"Analysis failed: {e}"
                out["items"].append(item)
                continue
            best = next((r for r in res.values() if r and r["required_ok"] and r["tag_class"] == "z-buy"), None)
            base = best or next((r for r in res.values() if r), None)
            card = make_card(rec, base, w, m, ins, rec["ysym"]) if base else {}
            if dealings.get(rec["ticker"]):
                tmp = {"x": [card]}
                apply_uk_dealings(tmp, dealings)
            methods = {}
            for k, r in res.items():
                verdict, vcls = _verdict(r)
                methods[k] = {"verdict": verdict, "vclass": vcls,
                              "tag": r["tag"] if r else "", "plan": r["plan"] if r else "",
                              "checks": r["checks"] if r else [],
                              "score": sum(c_["pts"] for c_ in r["checks"]) if r else 0,
                              "max_score": sum(c_["max"] for c_ in r["checks"]) if r else 0}
            item.update(card=card, methods=methods, market_state=market_check(bsym, bench_w))
            out["items"].append(item)
    return out


def evaluate_html(ev, demo=False):
    form = ("" if demo else
            '<div class="evform"><input id="ev-tick" type="text" autocomplete="off" autocapitalize="characters" '
            'placeholder="Ticker, e.g. KOO" aria-label="Ticker">'
            '<select id="ev-mkt" aria-label="Market"><option value="uk">UK</option><option value="america">USA</option>'
            '<option value="canada">Canada</option></select><button id="ev-go" type="button">Evaluate</button></div>'
            '<p class="small">Runs the stock through all ten methods and shows what passes, what fails and why. '
            'Takes about 2–3 minutes; the page updates itself. Stocks you add are re-checked at every weekend scan.</p>')
    items = (ev or {}).get("items") or []
    if not items:
        return form + '<p class="empty">No stocks added yet.</p>'
    order = {"v-buy": 0, "v-wait": 1, "v-no": 2, "v-na": 3}
    parts = [form]
    for it in sorted(items, key=lambda i: i.get("added", ""), reverse=True):
        rm = (f'<button class="ev-rm" type="button" data-key="{E(it["key"])}">Remove</button>' if not demo else "")
        if it.get("error"):
            parts.append(f'<div class="evcard"><div class="evhead"><b>{E(it["ticker"])}</b> '
                         f'<span class="ex">{E(MARKETS[it["market"]]["label"])}</span></div>'
                         f'<p class="empty">{E(it["error"])}</p>{rm}</div>')
            continue
        c = it["card"]
        ms = it["methods"]
        buys = [STRATEGIES[k]["short"] for k, v in ms.items() if v["vclass"] == "v-buy"]
        waits = [STRATEGIES[k]["short"] for k, v in ms.items() if v["vclass"] == "v-wait"]
        if buys:
            head = f'<div class="evsum v-buy">Buy point now: {E(", ".join(buys))}</div>'
        elif waits:
            head = f'<div class="evsum v-wait">Qualifies but not at a buy point: {E(", ".join(waits))}</div>'
        else:
            head = '<div class="evsum v-no">No method has it at a buy point right now.</div>'
        rows = []
        for k in sorted(ms, key=lambda k: order.get(ms[k]["vclass"], 9)):
            v = ms[k]
            checks = "".join(
                f'<li class="{"ok" if ch["pass"] else ("na" if ch["pass"] is None else "no")}">'
                f'<span>{E(ch["label"])}{" <em>(must pass)</em>" if ch.get("req") and not ch["pass"] else ""}</span>'
                f'<small>{E(ch["note"] or "")}</small></li>' for ch in v["checks"])
            plan = f'<div class="plan">{E(v["plan"])}</div>' if v["plan"] and v["vclass"] != "v-no" else ""
            rows.append(f'<details class="evm"><summary><span>{E(STRATEGIES[k]["label"])}</span>'
                        f'<span class="vb {v["vclass"]}">{E(v["verdict"])}</span></summary>'
                        f'<ul class="checks">{checks}</ul>{plan}</details>')
        ins = ""
        if c.get("insider"):
            i = c["insider"]
            cur = "£" if i.get("currency") == "GBP" else "$"
            val_s = f", {cur}{i['value']:,.0f}" if i.get("value") else ""
            who = "Director" if i.get("source") == "RNS" else "Insider"
            ins = f'<div class="meta">{who} buys (90 days): {i["buys"]}{val_s}</div>'
        buy = ""
        if not demo and _PAGE.get("date") and c.get("entry_close"):
            bc = dict(c)
            if not buys:
                bc["strategy"] = "other"
            buy = (f'<a class="buy" href="{E(gh_new_url(holding_path(it["market"], c["ticker"]), holding_template(bc, _PAGE["date"])))}" '
                   'target="_blank" rel="noopener">I bought this ↗</a>')
        mstate = it.get("market_state") or {}
        rs_s = f" · RS {c['rs_rank']:.0f}" if c.get("rs_rank") is not None else ""
        parts.append(
            f'<div class="evcard"><div class="evhead"><div><b>{E(c["ticker"])}</b> <span class="ex">{E(c["exchange"])}</span>'
            f'<div class="nm">{E(c["name"])}</div></div>{sparkline(c)}'
            f'<div class="px">{fmt_price(c["price"], c["currency"], c["exchange"])}</div></div>'
            f'{head}<div class="meta">Mkt cap {fmt_cap(c.get("mcap"))} · {E(c.get("sector") or "-")}'
            f'{rs_s}'
            f' · market {E(mstate.get("state", "?"))} · checked {E(it["updated"])}</div>{ins}'
            f'<div class="evms">{"".join(rows)}</div>'
            f'<div class="links"><a href="{E(c["chart_url"])}" target="_blank" rel="noopener">Weekly chart ↗</a>'
            f'<a href="{E(c["insider_url"])}" target="_blank" rel="noopener">Insider filings ↗</a></div>'
            f'<div class="evbtns">{buy}{rm}</div></div>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# Holdings: what you've bought, checked against each method's sell rule
# ---------------------------------------------------------------------------

STATUS_RANK = {"OK": 0, "WATCH": 1, "WARNING": 2, "SELL": 3, "ERROR": -1}


def holding_path(market, ticker):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(ticker)).strip("-") or "stock"
    return f"holdings/{market}-{slug}.json"


def holding_template(card, today):
    h = {
        "ticker": card["ticker"], "exchange": card["exchange"], "market": card["market"],
        "name": card["name"], "method": card["strategy"],
        "bought": today, "price": round(card["entry_close"], 4),
        "note": "Change price to what you paid (UK prices in pence). Delete this file when you sell.",
    }
    if card.get("stop") is not None:
        h["stop"] = round(card["stop"], 4)
    return json.dumps(h, indent=2)


def gh_new_url(path, content):
    from urllib.parse import quote
    return (f"https://github.com/{REPO}/new/{BRANCH}?filename={quote(path)}&value={quote(content)}")


def gh_edit_url(path):
    return f"https://github.com/{REPO}/edit/{BRANCH}/{path}"


def gh_delete_url(path):
    return f"https://github.com/{REPO}/delete/{BRANCH}/{path}"


def load_holdings(root: Path) -> list[dict]:
    out = []
    for f in sorted((root / "holdings").glob("*.json")):
        rel = f"holdings/{f.name}"
        try:
            h = json.loads(f.read_text(encoding="utf-8"))
            h["ticker"] = str(h["ticker"]).strip()
            h["market"] = str(h.get("market") or "america").strip().lower()
            if h["market"] not in MARKETS:
                raise ValueError(f"market must be one of {', '.join(MARKETS)}")
            h["exchange"] = str(h.get("exchange") or {"america": "NASDAQ", "canada": "TSX", "uk": "LSE"}[h["market"]]).upper()
            h["method"] = str(h.get("method") or "other").lower()
            if h["method"] not in STRATEGIES:
                h["method"] = "other"
            h["price"] = float(h["price"])
            h["bought"] = str(h.get("bought") or dt.date.today().isoformat())[:10]
            dt.date.fromisoformat(h["bought"])
            h["ysym"] = h.get("yahoo") or yahoo_symbol(h["exchange"], h["ticker"])
            h["file"] = rel
            out.append(h)
        except Exception as e:
            out.append({"file": rel, "ticker": f.stem, "market": "america", "error": f"Couldn't read this file: {e}"})
    return out


def check_holding(h, d, today):
    """Return status OK / WATCH / WARNING / SELL with a plain-English reason."""
    st = {"file": h["file"], "ticker": h["ticker"], "market": h["market"], "name": h.get("name") or h["ticker"],
          "method": h["method"], "paid": h["price"], "bought": h["bought"], "exchange": h["exchange"]}
    if d is None or len(d) < 40:
        st.update(status="ERROR", reason=f"No price data found for {h['ysym']}. Check the ticker and exchange.")
        return st
    c = d["Close"]
    last = float(c.iloc[-1])
    st.update(last=last, last_date=str(d.index[-1].date()), pl=last / h["price"] - 1)
    t0 = pd.Timestamp(h["bought"])
    m = h["method"]
    band = WARN_BAND.get(m, 0.05)
    if m in ("luk", "kell"):
        n = 9 if m == "luk" else 20
        e = ema(c, n)
        name = f"{n}-day EMA"
        st["level"], st["level_name"] = float(e.iloc[-1]), name
        since = d.loc[d.index > t0]
        stop = num(h.get("stop"))
        hit = since[since["Low"] <= stop] if stop else since.iloc[0:0]
        below = since[since["Close"] < e.loc[since.index]]
        if not hit.empty:
            st.update(status="SELL", reason=f"Hit the stop ({fmt_num(stop)}) on {hit.index[0].strftime('%d %b')}.")
        elif not below.empty:
            st.update(status="SELL", reason=f"Closed below the {name} on {below.index[0].strftime('%d %b')}. "
                                             "The method says sell.")
        elif last <= e.iloc[-1] * (1 + band):
            st.update(status="WARNING", reason=f"Within {band * 100:.0f}% of the {name} ({fmt_num(e.iloc[-1])}).")
        else:
            st.update(status="OK", reason=f"{(last / e.iloc[-1] - 1) * 100:.0f}% above the {name} ({fmt_num(e.iloc[-1])}).")
    elif m in ("minervini", "episodic"):
        stop = num(h.get("stop")) or h["price"] * (1 - MIN_STOP)
        st["level"], st["level_name"] = stop, ("gap-day low" if m == "episodic" else "8% stop")
        since = d.loc[d.index > t0]
        hit = since[since["Low"] <= stop]
        if not hit.empty:
            st.update(status="SELL", reason=f"Hit the {st['level_name']} ({fmt_num(stop)}) on "
                                             f"{hit.index[0].strftime('%d %b')}. The method says sell.")
        elif last <= stop * (1 + band):
            st.update(status="WARNING", reason=f"Within {band * 100:.0f}% of the {st['level_name']} ({fmt_num(stop)}).")
        else:
            st.update(status="OK", reason=f"{(last / stop - 1) * 100:.0f}% above the {st['level_name']}.")
    else:
        n = 10 if m == "stine" else 30
        name = f"{n}-week MA"
        w = to_weekly(d)
        ma = w["Close"].rolling(n).mean()
        complete = d.index[-1].weekday() == 4
        k = -1 if complete else -2
        wk_close, wk_ma, wk_date = float(w["Close"].iloc[k]), num(ma.iloc[k]), w.index[k]
        cur_ma = num(ma.iloc[-1])
        st["level"], st["level_name"] = cur_ma, name
        if cur_ma is None:
            st.update(status="ERROR", reason="Not enough price history to work out the moving average.")
        elif wk_ma is not None and wk_close < wk_ma and wk_date >= t0:
            st.update(status="SELL", reason=f"Week ending {wk_date.strftime('%d %b')} closed at {fmt_num(wk_close)}, "
                                             f"below the {name} ({fmt_num(wk_ma)}). The method says sell.")
        elif last < cur_ma:
            st.update(status="WARNING", reason=f"Trading below the {name} ({fmt_num(cur_ma)}). "
                                                f"Sell if the week closes below it.")
        elif last <= cur_ma * (1 + band):
            st.update(status="WATCH", reason=f"Within {band * 100:.0f}% of the {name} ({fmt_num(cur_ma)}).")
        else:
            st.update(status="OK", reason=f"{(last / cur_ma - 1) * 100:.0f}% above the {name} ({fmt_num(cur_ma)}).")
    return st


def choose_benches(prices):
    out = {}
    for m in MARKETS:
        for b in MARKETS[m]["bench"]:
            if b in prices and len(prices[b]) >= 200:
                out[m] = b
                break
    return out


def run_holdings(holdings, fetch_prices, today):
    good = [h for h in holdings if "error" not in h]
    syms = [h["ysym"] for h in good] + [b for m in MARKETS for b in MARKETS[m]["bench"]]
    prices = fetch_prices(syms) if syms else {}
    benches = choose_benches(prices)
    markets = {m: market_check(b, to_weekly(prices[b])) for m, b in benches.items()}
    rows = []
    for h in holdings:
        if "error" in h:
            rows.append({"file": h["file"], "ticker": h["ticker"], "market": h["market"], "method": "other",
                         "status": "ERROR", "reason": h["error"]})
            continue
        try:
            rows.append(check_holding(h, prices.get(h["ysym"]), today))
        except Exception as e:
            rows.append({"file": h["file"], "ticker": h["ticker"], "market": h["market"], "method": h["method"],
                         "status": "ERROR", "reason": f"Check failed: {e}"})
    rows.sort(key=lambda r: STATUS_RANK.get(r["status"], 0), reverse=True)
    return {"date": today.isoformat(), "holdings": rows, "markets": markets}


# ---------------------------------------------------------------------------
# Pre-close check: about an hour before each market closes, hold or sell?
# ---------------------------------------------------------------------------

CLOSE_TIMES = {"uk": ("Europe/London", 16, 30), "america": ("America/New_York", 16, 0),
               "canada": ("America/New_York", 16, 0)}
WEEKLY_RULES = ("stine", "weinstein", "stage2", "other", "slater", "piotroski", "magic")


def minutes_to_close(market, now_utc=None):
    from zoneinfo import ZoneInfo
    tz, hh, mm = CLOSE_TIMES[market]
    now = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(tz))
    if now.weekday() >= 5:
        return None, now
    close = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return (close - now).total_seconds() / 60, now


def preclose_advice(st, method, is_friday, has_today):
    """Turn a holding's status (computed with today's live price) into hold / sell advice."""
    if not has_today:
        return "HOLD", "Hold", "No trading data for today yet (holiday or data delay), so nothing new to act on."
    status = st.get("status")
    weekly = method in WEEKLY_RULES
    if status == "SELL":
        reason = st.get("reason", "")
        if weekly and "Week ending" in reason:
            if not is_friday:
                return "SELL", "Sell", reason + " (last week's close)"
            reason = reason.replace("closed at", "is trading at").replace(" The method says sell.", "")
            return "SELL", "Sell before the close", reason + ". It's Friday, so if it closes here the method says sell."
        return "SELL", "Sell before the close", reason
    if status == "WARNING":
        if weekly and not is_friday:
            return ("CONSIDER", "Hold for now, watch closely",
                    st["reason"] + " This method only sells on a weekly close, so it's Friday's close that counts.")
        if weekly and is_friday:
            return "SELL", "Sell before the close", st["reason"] + " It's Friday, so today's close is the weekly close."
        return "CONSIDER", "Consider selling", st["reason"]
    if status == "WATCH":
        return "HOLD", "Hold, but it's near the sell level", st["reason"]
    if status == "ERROR":
        return "CONSIDER", "Check it yourself", st.get("reason", "")
    return "HOLD", "Hold", st.get("reason", "")


def run_preclose(holdings, fetch_prices, markets, now_utc=None):
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    hs = [h for h in holdings if "error" not in h and h["market"] in markets]
    hv = run_holdings(hs, fetch_prices, now_utc.date())
    out = {}
    for r in hv["holdings"]:
        h = next(x for x in hs if x["file"] == r["file"])
        mins, local = minutes_to_close(r["market"], now_utc)
        has_today = r.get("last_date") == local.date().isoformat()
        action, label, why = preclose_advice(r, h["method"], local.weekday() == 4, has_today)
        mstate = (hv["markets"].get(r["market"]) or {}).get("state")
        if mstate == "down" and action == "HOLD":
            why += " Note: this market is in a downtrend, so keep stops tight."
        r["preclose"] = {"time": local.strftime("%a %H:%M"), "action": action, "label": label, "why": why}
        out[r["file"]] = r
    return hv, out


def preclose_alerts(hv, markets):
    for m in markets:
        rows = [r for r in hv["holdings"] if r["market"] == m and r.get("preclose")]
        if not rows:
            continue
        sells = [r for r in rows if r["preclose"]["action"] == "SELL"]
        cons = [r for r in rows if r["preclose"]["action"] == "CONSIDER"]
        lines = []
        for r in sorted(rows, key=lambda r: {"SELL": 0, "CONSIDER": 1, "HOLD": 2}[r["preclose"]["action"]]):
            pl = f" ({pct(r['pl'])})" if r.get("pl") is not None else ""
            lines.append(f"{r['ticker']}{pl}: {r['preclose']['label']}. {r['preclose']['why']}")
        head = (f"SELL {', '.join(r['ticker'] for r in sells)}" if sells else
                f"Watch {', '.join(r['ticker'] for r in cons)}" if cons else "All hold")
        push(f"{MARKETS[m]['label']} pre-close check: {head}", "\n\n".join(lines),
             5 if sells else 4 if cons else 2, "rotating_light" if sells else "hourglass")


# ---------------------------------------------------------------------------
# Phone alerts via ntfy (free app, no account)
# ---------------------------------------------------------------------------


def push(title, message, priority=3, tags=""):
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        print(f"[alert not sent, NTFY_TOPIC not set] {title}: {message}")
        return False
    import requests

    try:
        r = requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"), timeout=20, headers={
            "Title": title.encode("ascii", "ignore").decode(), "Priority": str(priority),
            "Tags": tags, "Click": SITE})
        r.raise_for_status()
        print(f"[alert sent] {title}")
        return True
    except Exception as e:
        print(f"[alert failed] {title}: {e}")
        return False


def send_alerts(hv, state: dict, digest: bool) -> dict:
    new_state = {}
    held_markets = {r["market"] for r in hv["holdings"]}
    for r in hv["holdings"]:
        key = r["file"]
        prev = state.get(key)
        cur = r["status"]
        new_state[key] = cur
        if f"watchping:{key}" in state:
            new_state[f"watchping:{key}"] = state[f"watchping:{key}"]
        label = f"{r['ticker']} ({MARKETS[r['market']]['label']})"
        if prev is None:
            if cur != "ERROR":
                push(f"Now watching {label}", f"{STRATEGIES.get(r['method'], {}).get('short', 'Default')} sell rule. "
                     f"Currently {cur}: {r['reason']}", 2, "eyes")
            else:
                push(f"Problem with {label}", r["reason"], 3, "warning")
            continue
        if cur == prev:
            continue
        if cur == "WATCH" and prev == "OK":
            # avoid repeat pings when a price hovers around the watch band
            last_ping = state.get(f"watchping:{key}")
            if last_ping and (dt.date.fromisoformat(hv["date"]) - dt.date.fromisoformat(last_ping)).days < 7:
                new_state[f"watchping:{key}"] = last_ping
                continue
            new_state[f"watchping:{key}"] = hv["date"]
        if STATUS_RANK.get(cur, 0) > STATUS_RANK.get(prev, 0):
            pri, tag = {"SELL": (5, "rotating_light"), "WARNING": (4, "warning"), "WATCH": (3, "eyes")}.get(cur, (3, ""))
            push(f"{cur}: {label}", f"{r['reason']} Now {fmt_num(r.get('last'))}, {pct(r.get('pl'))} since you bought.", pri, tag)
        elif cur == "ERROR":
            push(f"Problem with {label}", r["reason"], 3, "warning")
        elif prev in ("WARNING", "SELL") and cur != "ERROR":
            push(f"Recovered to {cur}: {label}", r["reason"], 2, "white_check_mark")
    for m, mc in hv["markets"].items():
        key = f"market:{m}"
        new_state[key] = mc["state"]
        prev = state.get(key)
        if m not in held_markets or prev is None or prev == mc["state"]:
            continue
        if mc["state"] == "down":
            push(f"{MARKETS[m]['label']} market turned down", mc["text"] + " Consider tightening stops on your "
                 f"{MARKETS[m]['label']} holdings.", 4, "chart_with_downwards_trend")
        elif mc["state"] == "up":
            push(f"{MARKETS[m]['label']} market back in an uptrend", mc["text"], 2, "chart_with_upwards_trend")
    if digest:
        rows = hv["holdings"]
        if rows:
            counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("SELL", "WARNING", "WATCH", "OK", "ERROR")}
            head = ", ".join(f"{v} {k.lower()}" for k, v in counts.items() if v)
            lines = [f"{r['ticker']}: {r['status']} {pct(r.get('pl')) if r.get('pl') is not None else ''}" for r in rows]
            push(f"Holdings check: {head}", "\n".join(lines), 3 if not counts["SELL"] else 4, "clipboard")
        else:
            push("Alerts are working", "No holdings yet. Tap 'I bought this' on a stock card to start tracking one.", 2, "white_check_mark")
    return new_state


def preclose_line(r):
    pc = r.get("preclose")
    if not pc:
        return ""
    cls = {"SELL": "neg", "CONSIDER": "warnc", "HOLD": "pos"}.get(pc["action"], "")
    return (f'<div class="pcl">Pre-close check {E(pc["time"])}: <b class="{cls}">{E(pc["label"])}</b>'
            f'<br><small>{E(pc["why"])}</small></div>')


def holdings_html(hv, demo=False):
    add_tpl = json.dumps({"ticker": "ABC", "exchange": "LSE", "market": "uk", "method": "weinstein",
                          "bought": dt.date.today().isoformat(), "price": 100.0,
                          "note": "market: america, canada or uk. method: stine, weinstein, minervini, episodic or other. UK prices in pence."},
                         indent=2)
    add = (f'<a class="btn" href="{E(gh_new_url("holdings/new-stock.json", add_tpl))}" target="_blank" rel="noopener">'
           'Add a stock by hand ↗</a>')
    intro = ('<p class="blurb">Stocks you\'ve told the bot you bought. It checks each one against its method\'s sell rule '
             'every weekday evening and every Saturday, and sends a phone alert when anything changes. '
             'To add one, tap <b>I bought this</b> on its card. When you sell, tap <b>I sold it</b>.</p>')
    if not hv or not hv.get("holdings"):
        return intro + '<p class="empty">No holdings yet.</p>' + add
    parts = [intro]
    held = {r["market"] for r in hv["holdings"]}
    for m in MARKETS:
        if m in held and m in hv["markets"]:
            parts.append(f'<div class="mk">{E(MARKETS[m]["label"])} market</div>' + banner(hv["markets"][m]))
    cls = {"SELL": "h-sell", "WARNING": "h-warn", "WATCH": "h-watch", "OK": "h-ok", "ERROR": "h-err"}
    for r in hv["holdings"]:
        meth = STRATEGIES.get(r["method"], {}).get("short", "Default (30-week MA)")
        money = ""
        if r.get("last") is not None:
            pl = r["pl"]
            money = (f'<div class="hrow"><span>Paid {fmt_num(r["paid"])} on {dt.date.fromisoformat(r["bought"]).strftime("%d %b")}</span>'
                     f'<b class="{"pos" if pl >= 0 else "neg"}">{fmt_num(r["last"])} ({pct(pl)})</b></div>')
        lvl = ""
        if r.get("level"):
            gap = "" if r.get("last") is None else " (" + pct(r["level"] / r["last"] - 1, 0) + ")"
            lvl = (f'<div class="hrow"><span>Sell level: {E(r["level_name"])}</span><b>{fmt_num(r["level"])}{gap}</b></div>')
        links = ""
        if not demo:
            links = (f'<div class="links"><a href="{E(gh_delete_url(r["file"]))}" target="_blank" rel="noopener">I sold it ↗</a>'
                     f'<a href="{E(gh_edit_url(r["file"]))}" target="_blank" rel="noopener">Edit ↗</a></div>')
        parts.append(f'<div class="hold {cls.get(r["status"], "")}"><div class="hhead"><div><b>{E(r["ticker"])}</b> '
                     f'<span class="ex">{E(MARKETS[r["market"]]["label"])} · {E(meth)}</span></div>'
                     f'<span class="hst">{E(r["status"])}</span></div>'
                     f'<div class="hreason">{E(r["reason"])}</div>{preclose_line(r)}{money}{lvl}{links}</div>')
    parts.append(add)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Web page
# ---------------------------------------------------------------------------

E = html.escape


def fmt_price(x, currency, exchange):
    if x is None:
        return "-"
    if currency == "GBX":
        return f"{x:,.1f}p" if x < 1000 else f"{x:,.0f}p"
    sym = {"USD": "$", "CAD": "C$", "GBP": "£"}.get(currency, "")
    return f"{sym}{x:,.2f}" if x < 1000 else f"{sym}{x:,.0f}"


def fmt_cap(m):
    if m is None:
        return "-"
    return f"{m / 1e9:.2f}bn" if m >= 1e9 else f"{m / 1e6:.0f}m"


def pct(x, dp=1, sign=True):
    if x is None:
        return "-"
    return f"{x * 100:+.{dp}f}%" if sign else f"{x * 100:.{dp}f}%"


def sparkline(r):
    vals = [x for x in r["closes"] + r["ma10"] + r["ma30"] if x is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1
    W, H = 120, 36

    def path(series):
        pts = [(i * W / max(len(series) - 1, 1), H - (v - lo) / span * H) for i, v in enumerate(series) if v is not None]
        return " ".join(f"{'M' if j == 0 else 'L'}{x:.1f},{y:.1f}" for j, (x, y) in enumerate(pts))

    return (f'<svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-hidden="true">'
            f'<path d="{path(r["ma30"])}" class="s-ma30"/><path d="{path(r["ma10"])}" class="s-ma10"/>'
            f'<path d="{path(r["closes"])}" class="s-px"/></svg>')


REFRESH_HTML = """
<div class="refresh">
  <button id="btn-scan" type="button">↻ Run a new scan</button>
  <button id="btn-hold" type="button" class="ghost">Check my stocks</button>
</div>
<div id="rstatus" class="rstatus" hidden></div>
<details id="rsetup" class="rsetup">
  <summary>Set up the refresh buttons (one time)</summary>
  <ol>
    <li>Open <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">GitHub: new fine-grained token ↗</a>.</li>
    <li>Name it <b>scanner refresh</b>. Choose the longest expiry offered.</li>
    <li>Under <b>Repository access</b>, choose <b>Only select repositories</b> and pick <b>__REPONAME__</b>.</li>
    <li>Under <b>Permissions → Repository permissions</b>, set <b>Actions</b> to <b>Read and write</b>. Leave everything else as it is.</li>
    <li>Tap <b>Generate token</b>, copy it, and paste it here:</li>
  </ol>
  <div class="tokrow"><input id="tok" type="password" autocomplete="off" placeholder="github_pat_..." aria-label="GitHub token">
  <button id="tok-save" type="button">Save</button></div>
  <p class="small">The key is saved only in this browser on this phone. It can only start and check this scanner's runs, nothing else.
  <a href="#" id="tok-forget">Forget the saved key</a></p>
</details>
"""

REFRESH_JS = """
(function(){
 const REPO='__REPO__', GEN='__GEN__', EVGEN='__EVGEN__', API='https://api.github.com/repos/'+REPO+'/actions/workflows/';
 const $=id=>document.getElementById(id), st=$('rstatus');
 const get=k=>{try{return localStorage.getItem(k)}catch(e){return null}};
 const put=(k,v)=>{try{v===null?localStorage.removeItem(k):localStorage.setItem(k,v)}catch(e){}};
 const hdr=()=>({'Authorization':'Bearer '+get('gh_token'),'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'});
 function say(msg,cls){st.hidden=false;st.className='rstatus '+(cls||'');st.innerHTML=msg;}
 if(get('gh_token')) $('rsetup').hidden=true;
 $('tok-save').onclick=async()=>{
   const t=$('tok').value.trim(); if(!t){say('Paste the token first.','bad');return;}
   put('gh_token',t); say('Checking the key…');
   try{const r=await fetch(API+'weekly-scan.yml',{headers:hdr()});
     if(r.ok){say('Key saved. The buttons are ready.','good');$('tok').value='';$('rsetup').open=false;$('rsetup').hidden=true;}
     else{put('gh_token',null);say('GitHub didn\\'t accept that key ('+r.status+'). Check it has <b>Actions: Read and write</b> on this repo.','bad');}
   }catch(e){put('gh_token',null);say('Couldn\\'t reach GitHub. Check your connection.','bad');}
 };
 $('tok-forget').onclick=e=>{e.preventDefault();put('gh_token',null);put('gh_pending',null);$('rsetup').hidden=false;say('Saved key removed.');};
 async function start(wf,label,inputs){
   if(!get('gh_token')){$('rsetup').hidden=false;$('rsetup').open=true;$('rsetup').scrollIntoView({behavior:'smooth'});return;}
   say('Starting '+label+'…');
   try{
     const body={ref:'main'}; if(inputs) body.inputs=inputs;
     const r=await fetch(API+wf+'/dispatches',{method:'POST',headers:hdr(),body:JSON.stringify(body)});
     if(r.status===204){put('gh_pending',JSON.stringify({wf,label,t:Date.now()}));poll();}
     else if(r.status===401||r.status===403||r.status===404){say('GitHub refused ('+r.status+'). The key may have expired. <a href="#" onclick="document.getElementById(\\'rsetup\\').hidden=false;document.getElementById(\\'rsetup\\').open=true;return false">Set it up again</a>.','bad');}
     else say('Couldn\\'t start it ('+r.status+'). Try again in a minute.','bad');
   }catch(e){say('Couldn\\'t reach GitHub. Check your connection.','bad');}
 }
 async function poll(){
   const p=JSON.parse(get('gh_pending')||'null'); if(!p) return;
   const mins=Math.round((Date.now()-p.t)/60000);
   if(Date.now()-p.t>90*60000){put('gh_pending',null);return;}
   try{
     const r=await fetch(API+p.wf+'/runs?per_page=5&event=workflow_dispatch',{headers:hdr()});
     const j=await r.json(); const run=(j.workflow_runs||[]).find(x=>Date.parse(x.created_at)>=p.t-120000);
     if(!run){say(p.label+' requested. Waiting for GitHub to pick it up…');}
     else if(run.status!=='completed'){
       say(p.label+' running ('+mins+' min so far). '+(p.wf==='weekly-scan.yml'?'A full scan takes about 15–20 minutes.':'This takes about 2–3 minutes.')+' You can leave this page; it carries on.');}
     else if(run.conclusion==='success'){
       put('gh_pending',null); say('Finished. Updating the page…','good');
       waitForPage(p.wf); return;}
     else{put('gh_pending',null);say(p.label+' failed. <a href="'+run.html_url+'" target="_blank" rel="noopener">See what went wrong ↗</a>','bad');return;}
   }catch(e){say('Lost contact with GitHub; retrying…');}
   setTimeout(poll,20000);
 }
 async function waitForPage(wf){
   const file=wf==='weekly-scan.yml'?'data/latest.json':(wf==='evaluate.yml'?'data/evaluations.json':null);
   const was=wf==='weekly-scan.yml'?GEN:EVGEN;
   for(let i=0;i<12;i++){
     await new Promise(r=>setTimeout(r,15000));
     if(!file && i>=4) break;
     try{if(file){const j=await (await fetch(file+'?t='+Date.now(),{cache:'no-store'})).json();
       if(j.generated!==was) break;}}catch(e){}
   }
   location.replace(location.pathname+'?t='+Date.now());
 }
 $('btn-scan').onclick=()=>start('weekly-scan.yml','New scan');
 $('btn-hold').onclick=()=>start('daily-check.yml','Holdings check');
 const go=$('ev-go');
 if(go){
   const run=()=>{const t=($('ev-tick').value||'').trim().toUpperCase().replace(/^[A-Z]+:/,'');
     if(!/^[A-Z0-9.-]{1,12}$/.test(t)){say('Type a ticker such as KOO, TSLA or AT.','bad');return;}
     put('tab','evaluate'); window.scrollTo({top:0,behavior:'smooth'});
     start('evaluate.yml','Evaluation of '+t,{add:t+':'+$('ev-mkt').value,remove:''});};
   go.onclick=run; $('ev-tick').addEventListener('keydown',e=>{if(e.key==='Enter')run();});
 }
 document.querySelectorAll('.ev-rm').forEach(b=>b.onclick=()=>{put('tab','evaluate');window.scrollTo({top:0,behavior:'smooth'});
   start('evaluate.yml','Removing '+b.dataset.key.split(':')[0],{add:'',remove:b.dataset.key});});
 if(get('gh_pending')) poll();
})();
"""


_PAGE: dict = {}


def card_html(r, show_market=False):
    checks = "".join(
        f'<li class="{"ok" if c["pass"] else ("na" if c["pass"] is None else "no")}">'
        f'<span>{E(c["label"])}</span><small>{E(c["note"])}</small></li>' for c in r["checks"])
    p = r["score"] / r["max_score"] if r["max_score"] else 0
    new = '<span class="new">NEW</span>' if r.get("new") else ""
    if r.get("also"):
        new += f'<span class="multi" title="Picked by {1 + len(r["also"])} methods">×{1 + len(r["also"])}</span>'
    also = (f'<div class="also">Also flagged by: {E(", ".join(r["also"]))}</div>' if r.get("also") else "")
    ins = ""
    if r.get("insider"):
        i = r["insider"]
        cur = "£" if i.get("currency") == "GBP" else "$"
        val = f', {cur}{i["value"]:,.0f}' if i.get("value") else ""
        who = f' · {E(", ".join(i.get("insiders") or []))}' if i.get("source") == "RNS" and i.get("insiders") else ""
        src = (f' <a href="{E(i["url"])}" target="_blank" rel="noopener">RNS ↗</a>' if i.get("url") else "")
        ins = f'<div class="meta">{"Director" if i.get("source") == "RNS" else "Insider"} buys (90 days): {i["buys"]}{val}{who}{src}</div>'
    pol = _PAGE.get("pol", {}).get(r["ticker"].upper()) if r.get("market") == "america" else None
    if pol:
        new += '<span class="pol" title="Bought by a watched politician">🏛</span>'
        p0 = pol[0]
        ins += (f'<div class="meta">🏛 {E(p0["who"])} bought {E(p0["amount"])} on '
                f'{dt.date.fromisoformat(p0["traded"]).strftime("%d %b")} (reported {dt.date.fromisoformat(p0["filed"]).strftime("%d %b")})'
                f'{f" + {len(pol) - 1} more" if len(pol) > 1 else ""}</div>')
    mk = f' · {E(MARKETS[r["market"]]["label"])}' if show_market else ""
    strat = f'<span class="strat">{E(STRATEGIES[r["strategy"]]["short"])}</span>' if show_market else ""
    rs = f' · RS {r["rs_rank"]:.0f}' if r.get("rs_rank") is not None else ""
    buy = ""
    if _PAGE.get("date") and r.get("entry_close") and not _PAGE.get("demo"):
        path = holding_path(r["market"], r["ticker"])
        buy = (f'<a class="buy" href="{E(gh_new_url(path, holding_template(r, _PAGE["date"])))}" '
               f'target="_blank" rel="noopener">I bought this ↗</a>')
    return f"""
<details class="card">
  <summary>
    <div class="score" style="--p:{p:.2f}"><b>{r['score']}</b><small>/{r['max_score']}</small></div>
    <div class="who"><div class="tick">{E(r['ticker'])} <span class="ex">{E(r['exchange'])}</span>{new}{strat}</div>
      <div class="nm">{E(r['name'])}</div></div>
    {sparkline(r)}
    <div class="px"><div>{fmt_price(r['price'], r['currency'], r['exchange'])}</div><span class="zone {r['tag_class']}">{E(r['tag'])}</span></div>
  </summary>
  <div class="body">
    <ul class="checks">{checks}</ul>
    <div class="plan">{E(r['plan'])}</div>
    <div class="meta">Mkt cap {fmt_cap(r['mcap'])} · {E(r['sector'] or '-')}{rs}{mk}</div>
    {ins}{also}
    <div class="links"><a href="{E(r['chart_url'])}" target="_blank" rel="noopener">Weekly chart ↗</a>
      <a href="{E(r['insider_url'])}" target="_blank" rel="noopener">Insider filings ↗</a></div>
    {buy}
  </div>
</details>"""


def multi_html(e):
    rows = "".join(
        f'<div class="kv"><span>{E(STRATEGIES[h["strategy"]]["short"])} <small>{h["score"]}/{h["max_score"]}</small></span>'
        f'<b class="zone {h["tag_class"]}">{E(h["tag"])}</b></div>' for h in e["methods"])
    plans = "".join(f'<li><b>{E(STRATEGIES[h["strategy"]]["short"])}:</b> {E(h["plan"])}</li>' for h in e["methods"])
    new = '<span class="new">NEW</span>' if e.get("new") else ""
    buy = ""
    if _PAGE.get("date") and e.get("entry_close") and not _PAGE.get("demo"):
        path = holding_path(e["market"], e["ticker"])
        buy = (f'<a class="buy" href="{E(gh_new_url(path, holding_template(e, _PAGE["date"])))}" '
               f'target="_blank" rel="noopener">I bought this ↗</a>')
    return f"""
<details class="card mcard">
  <summary>
    <div class="mcount"><b>{e['count']}</b><small>methods</small></div>
    <div class="who"><div class="tick">{E(e['ticker'])} <span class="ex">{E(e['exchange'])}</span>{new}</div>
      <div class="nm">{E(e['name'])}</div></div>
    {sparkline(e)}
    <div class="px"><div>{fmt_price(e['price'], e['currency'], e['exchange'])}</div></div>
  </summary>
  <div class="body">
    <div class="mrows">{rows}</div>
    <ul class="plans">{plans}</ul>
    <div class="meta">Mkt cap {fmt_cap(e['mcap'])} · {E(e['sector'] or '-')}{f" · RS {e['rs_rank']:.0f}" if e.get('rs_rank') is not None else ""}</div>
    <div class="links"><a href="{E(e['chart_url'])}" target="_blank" rel="noopener">Weekly chart ↗</a>
      <a href="{E(e['insider_url'])}" target="_blank" rel="noopener">Insider filings ↗</a></div>
    {buy}
  </div>
</details>"""


def multi_section(entries):
    body = "".join(multi_html(e) for e in entries) if entries else '<p class="empty">No overlap this week.</p>'
    new = sum(1 for e in entries if e.get("new"))
    return (f'<details class="sec"{" open" if entries else ""}><summary><span class="st">{E(CONSENSUS["label"])}</span>'
            f'<span class="cnt">{len(entries)}{f" · {new} new" if new else ""}</span></summary>'
            f'<p class="blurb">{E(CONSENSUS["blurb"])} Each method\'s own label is shown; '
            f'if one says "Extended" it\'s still worth waiting.</p>{body}</details>')


def banner(mc):
    if not mc:
        return ""
    cls = {"up": "b-up", "mixed": "b-mixed", "down": "b-down"}.get(mc["state"], "b-mixed")
    extra = f' <small>({E(str(mc.get("bench")))}, {mc["change_4w"]:+.1f}% in 4 wks)</small>' if mc.get("change_4w") is not None else ""
    return f'<div class="banner {cls}">{E(mc["text"])}{extra}</div>'


def strategy_section(k, L, open_=True, show_market=False, held=0):
    s = STRATEGIES[k]
    buy = [r for r in L if r["tag_class"] == "z-buy"]
    if buy:
        body = "".join(card_html(r, show_market) for r in buy)
    elif held:
        body = (f'<p class="empty">{held} stock(s) reached a buy point, but held back because the market is in a '
                'downtrend and this method says not to start new positions then.</p>')
    else:
        body = '<p class="empty">Nothing at a buy point right now.</p>'
    new = sum(1 for r in buy if r.get("new"))
    newtxt = f' · {new} new' if new else ""
    return (f'<details class="sec"{" open" if open_ else ""}><summary><span class="st">{E(s["label"])}</span>'
            f'<span class="cnt">{len(buy)} ready{newtxt}</span></summary><p class="blurb">{E(s["blurb"])}</p>{body}</details>')


def track_html(ledger, summ, today):
    old = len(ledger) - len(current(ledger))
    ledger = current(ledger)
    if not ledger:
        return ('<p class="empty">The track record starts with the next weekend scan '
                f'(current rules since {dt.date.fromisoformat(RULES_SINCE).strftime("%d %b")}).</p>')
    parts = ['<p class="blurb">Each weekend scan, the stocks at a buy point are recorded at that '
             'Friday\'s close and compared with the market\'s index. "Buy and hold" ignores the sell rules; '
             '"With sell rules" exits when the method says to.</p>']
    if old:
        parts.append(f'<p class="blurb">Counting picks under the current rules (since '
                     f'{dt.date.fromisoformat(RULES_SINCE).strftime("%d %b")}). {old} earlier picks are kept in the data '
                     'but left out here.</p>')
    first4 = min(dt.date.fromisoformat(p["date"]) for p in ledger) + dt.timedelta(weeks=4)
    if not any("r4" in p for p in ledger):
        parts.append(f'<p class="note">First 4-week results arrive around {first4.strftime("%d %b")}. '
                     'Give it at least 3 months before drawing conclusions.</p>')
    for k, s in TRACKED.items():
        row = summ.get(k, {})
        sf = row.get("so_far")
        if sf:
            big = (f'<div class="big {"pos" if sf["excess"] >= 0 else "neg"}">{pct(sf["excess"])}</div>'
                   f'<div class="bigl">average vs index so far ({sf["n"]} picks)</div>')
            stats = [("Buy and hold", f'{pct(sf["avg"])} · beat index {pct(sf["beat"], 0, False)}'),
                     ("With sell rules", f'{pct(sf["rule_avg"])} · {pct(sf["rule_excess"])} vs index')]
        else:
            big = '<div class="big">-</div><div class="bigl">no results yet</div>'
            stats = []
        for hz in HORIZONS:
            h = row.get(f"h{hz}")
            stats.append((f"After {hz} weeks", f'{pct(h["excess"])} vs index · beat {pct(h["beat"], 0, False)} (n={h["n"]})'
                          if h else "waiting"))
        rows = "".join(f'<div class="kv"><span>{E(a)}</span><b>{E(b)}</b></div>' for a, b in stats)
        parts.append(f'<div class="tcard"><div class="th">{E(s["label"])} <small>{row.get("picks", 0)} picks</small></div>'
                     f'{big}{rows}</div>')
    recent = sorted(ledger, key=lambda p: (p["date"], p["strategy"]), reverse=True)[:40]
    items = []
    for p in recent:
        r = p.get("ret_now")
        b = p.get("bench_now")
        ex = None if r is None else r - (b or 0)
        status = (f'Sold {dt.date.fromisoformat(p["exit_date"]).strftime("%d %b")}' if p.get("exit_date")
                  else ("Holding" if r is not None else "Just added"))
        cls = "pos" if (ex or 0) >= 0 else "neg"
        items.append(f'<tr><td>{dt.date.fromisoformat(p["date"]).strftime("%d %b")}</td>'
                     f'<td><b>{E(p["ticker"])}</b><br><small>{E(TRACKED.get(p["strategy"], {}).get("short", p["strategy"]))} · '
                     f'{E(MARKETS[p["market"]]["label"])}</small></td>'
                     f'<td class="{cls}">{pct(r) if r is not None else "-"}<br><small>{pct(ex) + " vs idx" if ex is not None else ""}</small></td>'
                     f'<td><small>{E(status)}</small></td></tr>')
    parts.append('<h3>Latest 40 picks</h3><table class="picks"><tr><th>Date</th><th>Pick</th><th>Return</th><th>Status</th></tr>'
                 + "".join(items) + "</table>")
    return "".join(parts)


CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1b1d21;--mute:#6b6f76;--line:#e4e2dd;--good:#1f8a5b;--bad:#b4472e;--warn:#b07a12;--acc:#2f5bd3;--na:#8b8f96;
--bup:#e5f3ec;--bmix:#fbf1dc;--bdown:#f8e3dd}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a20;--ink:#e8e9ec;--mute:#9298a2;--line:#262a32;--good:#4cc38a;--bad:#e2795f;--warn:#e0ad46;--acc:#7c9cff;--na:#6d727b;
--bup:#13261d;--bmix:#2a2312;--bdown:#2c1813}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:0 16px 40px}
header,nav,main,.legend,footer{max-width:760px;margin-left:auto;margin-right:auto}
header{padding:22px 0 6px}h1{font-size:22px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--mute);font-size:13px}.sub a{color:inherit}.demo{background:var(--warn);color:#000;padding:8px 12px;border-radius:8px;margin-top:10px;font-weight:600}
nav{position:sticky;top:0;background:var(--bg);display:flex;gap:6px;padding:8px 0;z-index:2;overflow-x:auto;scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
nav button{flex:1 0 auto;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:8px 12px;border-radius:10px;font:inherit;font-size:14px;font-weight:600;white-space:nowrap}
nav button.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
section{display:none}section.on{display:block}
.banner{border-radius:10px;padding:10px 12px;margin:6px 0 10px;font-size:14px;font-weight:500}
.banner small{color:var(--mute);font-weight:400}.b-up{background:var(--bup)}.b-mixed{background:var(--bmix)}.b-down{background:var(--bdown)}
.stats{color:var(--mute);font-size:12px;margin:4px 2px 8px}
.sec{margin:10px 0 4px}.sec>summary{list-style:none;display:flex;justify-content:space-between;align-items:baseline;padding:8px 2px;cursor:pointer;border-bottom:2px solid var(--line)}
.sec>summary::-webkit-details-marker{display:none}.st{font-weight:700;font-size:16px}.st::before{content:"▸ ";color:var(--mute)}
.sec[open]>summary .st::before{content:"▾ "}.cnt{color:var(--mute);font-size:13px}
.blurb{color:var(--mute);font-size:13px;margin:6px 2px 10px}.empty{color:var(--mute);font-size:14px;margin:8px 2px}
.note{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:8px;overflow:hidden}
.card>summary{list-style:none;display:flex;align-items:center;gap:12px;padding:12px;cursor:pointer}.card>summary::-webkit-details-marker{display:none}
.score{width:44px;height:44px;border-radius:50%;flex:none;display:grid;place-content:center;text-align:center;
 background:conic-gradient(var(--good) calc(var(--p)*360deg),var(--line) 0);position:relative}
.score::before{content:"";position:absolute;inset:4px;border-radius:50%;background:var(--card)}
.score b,.score small{position:relative;line-height:1}.score b{font-size:16px}.score small{font-size:10px;color:var(--mute)}
.who{flex:1;min-width:0}.tick{font-weight:700}.ex{font-weight:500;font-size:11px;color:var(--mute)}
.new{display:inline-block;margin-left:6px;font-size:10px;font-weight:700;background:var(--acc);color:#fff;border-radius:4px;padding:1px 5px;vertical-align:2px}
.multi{display:inline-block;margin-left:6px;font-size:10px;font-weight:700;background:var(--good);color:#fff;border-radius:4px;padding:1px 5px;vertical-align:2px}
.mcard{border-color:var(--good)}
.pol{margin-left:6px;font-size:12px;vertical-align:1px}
.evform{display:flex;gap:6px;margin:8px 0}.evform input{flex:1;min-width:0;padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--ink);font:inherit;text-transform:uppercase}
.evform select{padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--ink);font:inherit}
.evform button{border:0;border-radius:10px;padding:10px 14px;background:var(--acc);color:#fff;font:inherit;font-weight:700}
.evcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin:10px 0}
.evhead{display:flex;align-items:center;gap:12px}.evhead>div:first-child{flex:1;min-width:0}
.evsum{margin:10px 0 6px;padding:8px 10px;border-radius:8px;font-weight:600;font-size:14px}
.evsum.v-buy{background:var(--bup)}.evsum.v-wait{background:var(--bmix)}.evsum.v-no{background:var(--bg);color:var(--mute)}
.evms{margin:8px 0}.evm{border-top:1px dashed var(--line)}.evm>summary{list-style:none;display:flex;justify-content:space-between;gap:8px;padding:8px 0;cursor:pointer;font-size:14px}
.evm>summary::-webkit-details-marker{display:none}.vb{font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;border:1px solid currentColor;white-space:nowrap;align-self:center}
.vb.v-buy{color:var(--good)}.vb.v-wait{color:var(--warn)}.vb.v-no,.vb.v-na{color:var(--mute)}
.checks em{color:var(--bad);font-style:normal;font-size:11px}
.evbtns{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.ev-rm{margin-top:10px;border:1px solid var(--line);background:var(--bg);color:var(--mute);border-radius:8px;padding:8px 12px;font:inherit;font-size:14px}
.mcount{width:44px;height:44px;border-radius:50%;flex:none;display:grid;place-content:center;text-align:center;background:var(--good);color:#fff;line-height:1}
.mcount b{font-size:18px}.mcount small{font-size:7px;text-transform:uppercase}
.mrows{margin:10px 0}.mrows .zone{margin:0}.kv small{color:var(--mute)}
.plans{margin:6px 0 4px;padding-left:18px;font-size:13px}.plans li{margin:3px 0}
.strat{display:inline-block;margin-left:6px;font-size:10px;font-weight:600;border:1px solid var(--line);color:var(--mute);border-radius:4px;padding:0 5px;vertical-align:2px}
.nm{color:var(--mute);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.spark{width:90px;height:32px;flex:none}.spark path{fill:none;stroke-width:1.5;vector-effect:non-scaling-stroke}
.s-px{stroke:var(--ink)}.s-ma10{stroke:var(--acc)}.s-ma30{stroke:var(--warn)}
.px{text-align:right;font-weight:600;flex:none;max-width:40%}.zone{display:inline-block;font-size:11px;font-weight:600;padding:2px 7px;border-radius:99px;margin-top:3px;border:1px solid currentColor}
.z-buy{color:var(--good)}.z-ext{color:var(--bad)}.z-ok{color:var(--mute)}.z-low{color:var(--warn)}
.body{padding:0 12px 12px;border-top:1px solid var(--line)}
.checks{list-style:none;margin:10px 0;padding:0}.checks li{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px dashed var(--line)}
.checks li span::before{display:inline-block;width:18px;font-weight:700}.checks li.ok span::before{content:"✓";color:var(--good)}
.checks li.no span::before{content:"✗";color:var(--bad)}.checks li.na span::before{content:"?";color:var(--na)}
.checks small{color:var(--mute);text-align:right}
.plan{font-size:13px;margin:8px 0;padding:8px 10px;border-left:3px solid var(--acc);background:var(--bg);border-radius:0 6px 6px 0}
.meta,.also{color:var(--mute);font-size:12px;margin:6px 0}.links{display:flex;gap:16px}.links a{color:var(--acc);font-weight:600;text-decoration:none}
.tcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:10px}
.th{font-weight:700}.th small{color:var(--mute);font-weight:400}.big{font-size:28px;font-weight:700;margin-top:6px}.bigl{color:var(--mute);font-size:12px;margin-bottom:6px}
.pos{color:var(--good)}.neg{color:var(--bad)}
.kv{display:flex;justify-content:space-between;gap:10px;font-size:13px;padding:4px 0;border-top:1px dashed var(--line)}.kv span{color:var(--mute)}.kv b{font-weight:500;text-align:right}
h3{font-size:16px;margin:18px 2px 8px}
table.picks{width:100%;border-collapse:collapse;font-size:13px;background:var(--card);border-radius:12px;overflow:hidden}
.picks th{text-align:left;color:var(--mute);font-weight:500;padding:8px}.picks td{padding:8px;border-top:1px solid var(--line);vertical-align:top}
.picks small{color:var(--mute)}.picks a{color:var(--acc);text-decoration:none}
.refresh{display:flex;gap:8px;margin-top:12px}
.refresh button{flex:1;border:0;border-radius:10px;padding:11px 10px;font:inherit;font-weight:700;background:var(--acc);color:#fff}
.refresh button.ghost{background:var(--card);color:var(--ink);border:1px solid var(--line)}
.rstatus{margin-top:8px;padding:10px 12px;border-radius:10px;background:var(--card);border:1px solid var(--line);font-size:14px}
.rstatus.good{border-color:var(--good)}.rstatus.bad{border-color:var(--bad)}.rstatus a{color:var(--acc)}
.rsetup{margin-top:8px;font-size:14px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px}
.rsetup summary{cursor:pointer;font-weight:600;color:var(--acc)}.rsetup ol{padding-left:20px;margin:8px 0}.rsetup li{margin:4px 0}.rsetup a{color:var(--acc)}
.tokrow{display:flex;gap:6px}.tokrow input{flex:1;min-width:0;padding:9px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink);font:inherit}
.tokrow button{border:0;border-radius:8px;padding:9px 14px;background:var(--ink);color:var(--bg);font:inherit;font-weight:700}
.small{font-size:12px;color:var(--mute)}.small a{color:var(--mute)}
.sub{margin:6px 0 10px}.sub>summary{cursor:pointer;color:var(--mute);font-size:14px;font-weight:600;padding:6px 2px}
.mk{font-size:15px;font-weight:700;margin:16px 2px 4px;color:var(--mute)}
.legend{color:var(--mute);font-size:12px;margin-top:18px;line-height:1.6}
.legend i{display:inline-block;width:14px;height:2px;vertical-align:middle;margin-right:4px}
footer{margin-top:20px;color:var(--mute);font-size:12px}
.buy,.btn{display:inline-block;margin-top:10px;padding:8px 12px;border-radius:8px;background:var(--ink);color:var(--bg);font-weight:600;text-decoration:none;font-size:14px}
.btn{margin-top:12px}
.hold{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--line);border-radius:12px;padding:12px;margin-bottom:8px}
.hhead{display:flex;justify-content:space-between;align-items:center;gap:8px}.hst{font-size:12px;font-weight:700;padding:2px 8px;border-radius:99px;border:1px solid currentColor}
.hreason{margin:8px 0;font-size:14px}.hrow{display:flex;justify-content:space-between;gap:10px;font-size:13px;padding:4px 0;border-top:1px dashed var(--line)}.hrow span{color:var(--mute)}
.pcl{margin:6px 0 8px;padding:8px 10px;border-radius:8px;background:var(--bg);font-size:14px}.pcl small{color:var(--mute)}.warnc{color:var(--warn)}
.h-sell{border-left-color:var(--bad)}.h-sell .hst{color:var(--bad)}.h-warn{border-left-color:var(--warn)}.h-warn .hst{color:var(--warn)}
.h-watch .hst{color:var(--acc)}.h-watch{border-left-color:var(--acc)}.h-ok{border-left-color:var(--good)}.h-ok .hst{color:var(--good)}.h-err .hst{color:var(--mute)}
@media (max-width:420px){.spark{display:none}}
"""


def render_page(results, ledger, summ, demo=False, hv=None, pol=None, ev=None):
    _PAGE.clear()
    _PAGE.update(date=results["date"], demo=demo, pol=pol_by_ticker(pol))
    tabs, secs = [], []
    first = True
    for m, data in results["markets"].items():
        if "lists" not in data:
            body = f'<p class="empty">Scan failed this week: {E(str(data.get("error", ""))[:200])}</p>'
            n = 0
        else:
            L = data["lists"]
            n = sum(1 for v in L.values() for r in v if r["tag_class"] == "z-buy")
            body = banner(data.get("market"))
            body += (f'<p class="stats">{data["universe"]} stocks screened · {data["checked"]} charts analysed</p>')
            body += multi_section(data.get("multi", []))
            body += "".join(strategy_section(k, L[k], held=(data.get("held_back") or {}).get(k, 0)) for k in STRATEGIES)
        tabs.append((m, f"{data['label']} ({n})"))
        secs.append((m, body))

    # Resources tab: every pick in mining / oil and gas sectors
    res_body = ('<p class="blurb">All picks from every method in mining and oil &amp; gas, across the three markets. '
                'Weinstein and Episodic pivots don\'t need earnings, so they can catch explorers.</p>')
    rn = 0
    for m, data in results["markets"].items():
        if "lists" not in data:
            continue
        seen, cards = set(), []
        for k in ("episodic", "weinstein", "stage2", "kell", "luk", "stine", "minervini", "slater", "piotroski", "magic"):
            for r in data["lists"][k]:
                if r["tag"] == "Trend only":
                    continue
                if r["sector"] in RESOURCE_SECTORS and (k, r["ticker"]) not in seen:
                    seen.add((k, r["ticker"]))
                    cards.append(r)
        if cards:
            rn += len(cards)
            res_body += f'<div class="mk">{E(data["label"])}</div>' + "".join(card_html(r, True) for r in cards)
    if not rn:
        res_body += '<p class="empty">No resource stocks flagged this week.</p>'
    tabs.append(("resources", f"Resources ({rn})"))
    secs.append(("resources", res_body))
    en = len((ev or {}).get("items") or [])
    tabs.append(("evaluate", f"Evaluate ({en})"))
    secs.append(("evaluate", evaluate_html(ev, demo)))
    pn = len((pol or {}).get("buys") or [])
    tabs.append(("politics", f"Politicians ({pn})"))
    secs.append(("politics", politicians_html(pol)))
    tabs.append(("track", "Track record"))
    secs.append(("track", track_html(ledger, summ, results["date"])))
    hold_rows = (hv or {}).get("holdings") or []
    alerts = sum(1 for r in hold_rows if r["status"] in ("SELL", "WARNING"))
    mine = ("mine", f"My stocks ({len(hold_rows)})" + (" ⚠" if alerts else ""))
    if hold_rows:
        tabs.insert(0, mine)
        secs.insert(0, ("mine", holdings_html(hv, demo)))
    else:
        tabs.append(mine)
        secs.append(("mine", holdings_html(hv, demo)))

    nav = "".join(f'<button class="{"on" if i == 0 else ""}" data-t="{k}">{E(lbl)}</button>' for i, (k, lbl) in enumerate(tabs))
    main = "".join(f'<section id="{k}" class="{"on" if i == 0 else ""}">{b}</section>' for i, (k, b) in enumerate(secs))
    gen = dt.datetime.fromisoformat(results["generated"]).strftime("%a %d %b %Y, %H:%M UTC")
    demo_b = '<div class="demo">SAMPLE DATA: simulated stocks to test the layout</div>' if demo else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Superstock Scanner</title>
<link rel="manifest" href="manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="theme-color" content="#0f1115">
<style>{CSS}</style></head><body>
<header><h1>Superstock Scanner</h1>
<div class="sub">Ten methods · updated {gen} · <a href="history/">past weeks</a></div>{demo_b}
{"" if demo else REFRESH_HTML.replace("__REPONAME__", E(REPO.split("/")[-1]))}</header>
<nav>{nav}</nav>
<main>{main}</main>
<div class="legend">Chart: <i style="background:var(--ink)"></i>price <i style="background:var(--acc)"></i>10-week MA
 <i style="background:var(--warn)"></i>30-week MA · last 30 weeks. Only stocks at a buy point right now are listed, up to 10 per method. <b>NEW</b> = not on that method's list last time. <b>×2</b>, <b>×3</b> = picked by that many methods.
 Tap a stock for its checklist, entry and sell rule. Canada and UK insider buys aren't scored automatically, so use the filings link.</div>
<footer>Free, unofficial data from TradingView, Yahoo Finance and OpenInsider. It can be delayed or wrong, so check everything before acting.
A research shortlist, not financial advice.</footer>
<script>
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{{
 document.querySelectorAll('nav button,section').forEach(x=>x.classList.remove('on'));
 b.classList.add('on');document.getElementById(b.dataset.t).classList.add('on');
 try{{localStorage.setItem('tab',b.dataset.t)}}catch(e){{}}}});
try{{const t=localStorage.getItem('tab');t&&document.querySelector(`nav [data-t="${{t}}"]`)?.click()}}catch(e){{}}
</script>
<script>{"" if demo else REFRESH_JS.replace("__REPO__", REPO).replace("__GEN__", results["generated"]).replace("__EVGEN__", (ev or {}).get("generated", ""))}</script></body></html>"""


def write_outputs(results, ledger, docs: Path, demo=False, hv=None, pol=None, ev=None):
    (docs / "data").mkdir(parents=True, exist_ok=True)
    (docs / "history").mkdir(parents=True, exist_ok=True)
    summ = summarise(ledger)
    page = render_page(results, ledger, summ, demo, hv, pol, ev)
    (docs / "index.html").write_text(page, encoding="utf-8")
    if hv is not None:
        (docs / "data" / "holdings.json").write_text(json.dumps(hv, indent=1, default=jdefault), encoding="utf-8")
    slim = json.loads(json.dumps(results, default=jdefault))
    (docs / "data" / "latest.json").write_text(json.dumps(slim, indent=1), encoding="utf-8")
    (docs / "data" / "picks.json").write_text(json.dumps(ledger, indent=1, default=jdefault), encoding="utf-8")
    (docs / "data" / "summary.json").write_text(json.dumps(summ, indent=1, default=jdefault), encoding="utf-8")
    day = results["date"]
    (docs / "history" / f"{day}.html").write_text(
        page.replace('href="history/"', 'href="./"').replace('href="manifest.json"', 'href="../manifest.json"'),
        encoding="utf-8")
    (docs / "data" / f"{day}.json").write_text(json.dumps(slim), encoding="utf-8")
    pages = sorted((docs / "history").glob("20*.html"), reverse=True)
    links = "".join(f'<li><a href="{p.name}">{p.stem}</a></li>' for p in pages) or "<li>No past weeks yet.</li>"
    (docs / "history" / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Past weeks</title><body style="font:16px -apple-system,sans-serif;padding:16px">'
        f'<h1>Past weeks</h1><p><a href="../">Latest</a></p><ul>{links}</ul>', encoding="utf-8")
    (docs / "manifest.json").write_text(json.dumps({
        "name": "Superstock Scanner", "short_name": "Superstocks", "start_url": "./",
        "display": "standalone", "background_color": "#0f1115", "theme_color": "#0f1115"}), encoding="utf-8")
    (docs / ".nojekyll").write_text("")
    return summ


# ---------------------------------------------------------------------------
# Email (optional)
# ---------------------------------------------------------------------------


def email_html(results, summ):
    site = os.getenv("SITE_URL", "")
    parts = [f"<h2>Superstock Scanner: {results['date']}</h2>"]
    if site:
        parts.append(f'<p><a href="{E(site)}">Open the full page</a></p>')
    for data in results["markets"].values():
        parts.append(f"<h3>{E(data['label'])}</h3>")
        if "lists" not in data:
            parts.append("<p>Scan failed.</p>")
            continue
        if data.get("market"):
            parts.append(f"<p><i>{E(data['market']['text'])}</i></p>")
        if data.get("multi"):
            parts.append(f"<b>{E(CONSENSUS['label'])}</b><ul>")
            for e in data["multi"][:5]:
                parts.append(f'<li><a href="{E(e["chart_url"])}">{E(e["ticker"])}</a> {E(e["name"][:30])}: '
                             + ", ".join(f'{E(STRATEGIES[h["strategy"]]["short"])} ({E(h["tag"])})' for h in e["methods"])
                             + (" <b>NEW</b>" if e.get("new") else "") + "</li>")
            parts.append("</ul>")
        for k, s in STRATEGIES.items():
            L = data["lists"][k][:5]
            if not L:
                continue
            parts.append(f"<b>{E(s['label'])}</b><ul>")
            for r in L:
                parts.append(f'<li><a href="{E(r["chart_url"])}">{E(r["ticker"])}</a> {E(r["name"][:30])}: '
                             f'{r["score"]}/{r["max_score"]}, {E(r["tag"])}{" <b>NEW</b>" if r.get("new") else ""}</li>')
            parts.append("</ul>")
    parts.append("<h3>Track record (average vs index so far)</h3><ul>")
    for k, s in TRACKED.items():
        sf = summ.get(k, {}).get("so_far")
        parts.append(f"<li>{E(s['label'])}: {pct(sf['excess']) + ' over ' + str(sf['n']) + ' picks' if sf else 'no results yet'}</li>")
    parts.append("</ul><p style='color:#888;font-size:12px'>Research shortlist from free data. Not financial advice.</p>")
    return "".join(parts)


def send_email(results, summ):
    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"), os.getenv("EMAIL_TO")
    if not (user and pw and to):
        print("Email not configured; skipping.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Superstock Scanner: {results['date']}"
    msg["From"], msg["To"] = user, to
    msg.attach(MIMEText(email_html(results, summ), "html"))
    with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465"))) as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    print(f"Email sent to {to}")


# ---------------------------------------------------------------------------
# Demo: simulated market so the whole pipeline can be tested offline
# ---------------------------------------------------------------------------


class Simulator:
    """Makes ~2.7 years of daily prices for made-up stocks with known patterns."""

    PATTERNS = ["breakout", "stage2", "vcp", "gap", "downtrend", "chop", "uptrend"]

    def __init__(self, seed=1, per_market=70):
        rng = np.random.default_rng(seed)
        self.days = pd.bdate_range(end=pd.Timestamp("2026-10-16"), periods=700)
        self.stocks = {m: [] for m in MARKETS}
        self.prices = {}
        exch = {"america": "NASDAQ", "canada": "TSXV", "uk": "LSE"}
        cur = {"america": "USD", "canada": "CAD", "uk": "GBX"}
        sectors = ["Technology Services", "Energy Minerals", "Non-Energy Minerals", "Health Technology",
                   "Producer Manufacturing", "Retail Trade"]
        for m in MARKETS:
            for k in range(per_market):
                pat = self.PATTERNS[k % len(self.PATTERNS)]
                tick = f"{m[:2].upper()}{k:02d}"
                base = rng.uniform(3, 12) * (100 if m == "uk" else 1)
                event = int(rng.integers(420, 690))
                close = self._path(pat, base, event, rng)
                vol = rng.lognormal(12, 0.3, len(self.days))
                if pat in ("breakout", "stage2", "gap"):
                    vol[event:event + 5] *= rng.uniform(4, 9)
                if pat == "vcp":
                    vol[-12:] *= 0.5
                opn = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.004, len(close)))
                if pat == "gap":
                    opn[event] = close[event - 1] * 1.18
                    close[event:] = np.maximum(close[event:], opn[event] * 0.98)
                hi = np.maximum(opn, close) * (1 + np.abs(rng.normal(0, 0.012, len(close))))
                lo = np.minimum(opn, close) * (1 - np.abs(rng.normal(0, 0.012, len(close))))
                sym = yahoo_symbol(exch[m], tick)
                self.prices[sym] = pd.DataFrame({"Open": opn, "High": hi, "Low": lo, "Close": close, "Volume": vol},
                                                index=self.days)
                self.stocks[m].append({
                    "name": tick, "description": f"Sample {pat.title()} Co {k}", "exchange": exch[m],
                    "currency": cur[m], "type": "stock", "sector": sectors[k % len(sectors)], "industry": "",
                    "typespecs": ["common"], "market_cap_basic": float(rng.uniform(5e7, 8e8)),
                    "price_earnings_ttm": float(rng.choice([np.nan, 9, 14, 35])),
                    "earnings_per_share_diluted_ttm": float(rng.choice([np.nan, 0.4, 0.8, -0.2])),
                    "earnings_per_share_diluted_yoy_growth_fq": float(rng.choice([np.nan, 10, 45, 120])),
                    "float_shares_outstanding": float(rng.choice([np.nan, 12e6, 50e6])),
                    "piotroski_f_score_ttm": float(rng.integers(2, 10)),
                    "price_book_fq": float(rng.uniform(0.4, 5)),
                    "enterprise_value_to_ebit_ttm": float(rng.choice([-5, 4, 6, 9, 14, 25])),
                    "return_on_invested_capital": float(rng.uniform(-5, 40)),
                    "return_on_capital_employed_fy": float(rng.uniform(-5, 40)),
                    "price_earnings_forward_fy": float(rng.uniform(5, 30)),
                    "earnings_per_share_forecast_next_fy": float(rng.choice([0.5, 0.9, 1.4])),
                    "altman_z_score_ttm": float(rng.uniform(0.5, 5)),
                })
        for m, syms in (("america", ["IWM"]), ("canada", ["XCS.TO"]), ("uk", ["^FTAI", "^FTMC"])):
            for i, s in enumerate(syms):
                drift = np.linspace(0, 0.25 if m != "uk" else -0.05, len(self.days))
                c = 100 * np.exp(drift + np.cumsum(rng.normal(0, 0.008, len(self.days))))
                self.prices[s] = pd.DataFrame({"Open": c, "High": c * 1.005, "Low": c * 0.995, "Close": c,
                                               "Volume": 1e7}, index=self.days)
        self.asof = self.days[-1]

    def _path(self, pat, base, ev, rng):
        n = len(self.days)
        noise = rng.normal(0, 0.012, n)
        trend = np.zeros(n)
        if pat == "breakout":
            trend[: ev - 80] = 0.0012
            trend[ev:ev + 4] = 0.04
            trend[ev + 4:] = 0.002
        elif pat == "stage2":
            trend[: ev - 150] = -0.001
            trend[ev:ev + 3] = 0.035
            trend[ev + 3:] = 0.0025
            noise[ev - 150:ev] *= 0.6
        elif pat == "vcp":
            trend[:] = 0.0016
            trend[-70:] = 0.0
            noise[-70:-40] *= 1.4
            noise[-40:-15] *= 0.7
            noise[-15:] *= 0.25
        elif pat == "gap":
            trend[: ev] = 0.0002
            trend[ev:] = 0.001
        elif pat == "downtrend":
            trend[:] = -0.0015
        elif pat == "uptrend":
            trend[:] = 0.0015
        c = base * np.exp(np.cumsum(trend + noise))
        if pat == "gap":
            c[ev:] *= 1.2
        return c

    def tv(self, market):
        rows = []
        for s in self.stocks[market]:
            d = self.prices[yahoo_symbol(s["exchange"], s["name"])].loc[:self.asof]
            c = d["Close"]

            def perf(k):
                return float((c.iloc[-1] / c.iloc[-k - 1] - 1) * 100) if len(c) > k else np.nan

            rows.append({**s, "close": float(c.iloc[-1]), "volume": float(d["Volume"].iloc[-1]),
                         "average_volume_10d_calc": float(d["Volume"].iloc[-10:].mean()),
                         "relative_volume_10d_calc": float(d["Volume"].iloc[-1] / d["Volume"].iloc[-10:].mean()),
                         "SMA50": float(c.iloc[-50:].mean()), "SMA150": float(c.iloc[-150:].mean()),
                         "SMA200": float(c.iloc[-200:].mean()), "price_52_week_high": float(d["High"].iloc[-252:].max()),
                         "Perf.W": perf(5), "Perf.3M": perf(63), "Perf.6M": perf(126), "Perf.Y": perf(252)})
        return pd.DataFrame(rows)

    def fetch_prices(self, syms):
        out = {}
        for s in set(syms):
            if s in self.prices:
                d = self.prices[s].loc[:self.asof].iloc[-504:]
                out[s] = d
        return out

    def uk_dealings(self, ticks, today):
        return {t: [{"name": "Jane Example", "role": "CEO", "nature": "Purchase of ordinary shares",
                     "date": today.isoformat(), "shares": 20000, "price_gbp": 1.5, "value_gbp": 30000,
                     "url": "https://example.com"}] for t in ticks[:1]}

    def insiders(self):
        return {"AM00": {"buys": 2, "value": 250000.0, "insiders": [], "latest": ""},
                "AM07": {"buys": 1, "value": 90000.0, "insiders": [], "latest": ""}}


def run_demo(weeks: int, docs: Path):
    sim = Simulator()
    if docs.exists():
        shutil.rmtree(docs)
    ledger: list[dict] = []
    fridays = [d for d in sim.days if d.weekday() == 4][-weeks:]
    res = None
    for fri in fridays:
        sim.asof = fri
        res = run_pipeline(sim.tv, sim.fetch_prices, sim.insiders, fri.date(), ledger, res,
                           fetch_uk_dealings=sim.uk_dealings)
        res["demo"] = True
        summ = write_outputs(res, ledger, docs, demo=True)
    return res, ledger, summ


# ---------------------------------------------------------------------------


def carry_preclose(hv, docs: Path):
    """Keep today's pre-close advice on the page when the evening check rewrites the holdings."""
    old = load_json(docs / "data" / "holdings.json", None) or {}
    keep = {r["file"]: r.get("preclose") for r in old.get("holdings", []) if r.get("preclose")}
    for r in (hv or {}).get("holdings", []):
        if r["file"] in keep and not r.get("preclose"):
            r["preclose"] = keep[r["file"]]
    return hv


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def do_alerts(hv, docs: Path, scheduled_digest: bool):
    manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    sp = docs / "data" / "alert_state.json"
    state = load_json(sp, {})
    digest = manual or (scheduled_digest and bool(hv["holdings"]))
    new_state = send_alerts(hv, state, digest)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(new_state, indent=1), encoding="utf-8")


def refresh_politicians(docs: Path, today):
    sp = docs / "data" / "politicians.json"
    state = load_json(sp, {})
    try:
        new = live_politicians(today, set(state.get("seen", [])))
        state, fresh = update_politicians(state, new, today)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        return state, fresh
    except Exception as e:
        print(f"  politicians check failed: {e}")
        return state, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="simulated data, written to demo_docs/")
    ap.add_argument("--holdings", action="store_true", help="only check holdings and send alerts (daily run)")
    ap.add_argument("--weeks", type=int, default=30, help="weeks to simulate in demo mode")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--evaluate", action="store_true", help="evaluate the stocks on your Evaluate list")
    ap.add_argument("--preclose", action="store_true", help="hold/sell check about an hour before the close")
    ap.add_argument("--markets", default="", help="with --preclose: force these markets (uk,america,canada)")
    ap.add_argument("--add", default="", help="with --evaluate: tickers to add, e.g. 'KOO:uk, TSLA:us'")
    ap.add_argument("--remove", default="", help="with --evaluate: tickers to remove")
    a = ap.parse_args()
    if a.demo:
        run_demo(a.weeks, ROOT / "demo_docs")
        print(f"\nWrote {ROOT / 'demo_docs' / 'index.html'}")
        return
    docs = ROOT / "docs"
    ledger = load_json(docs / "data" / "picks.json", [])
    today = dt.datetime.now(dt.timezone.utc).date()
    holdings = load_holdings(ROOT)
    print(f"{len(holdings)} holding(s) found")
    wp = docs / "data" / "watchlist.json"
    watch = load_json(wp, [])
    if a.evaluate:
        watch = update_watchlist(watch, a.add, a.remove, today)
        wp.parent.mkdir(parents=True, exist_ok=True)
        wp.write_text(json.dumps(watch, indent=1), encoding="utf-8")
        ev = run_evaluations(watch, live_tv, live_prices, live_insiders, live_uk_dealings, today, live_tv)
        for it in ev["items"]:
            it["added"] = next((w["added"] for w in watch if f'{w["ticker"]}:{w["market"]}' == it["key"]), "")
            print(f'  {it["key"]}: {it.get("error") or ", ".join(k for k, v in it["methods"].items() if v["vclass"] == "v-buy") or "no buy point"}')
        (docs / "data" / "evaluations.json").write_text(json.dumps(ev, indent=1, default=jdefault), encoding="utf-8")
        res = load_json(docs / "data" / "latest.json", None)
        if res and "markets" in res:
            page = render_page(res, ledger, summarise(ledger), False, load_json(docs / "data" / "holdings.json", None),
                               load_json(docs / "data" / "politicians.json", {}), ev)
            (docs / "index.html").write_text(page, encoding="utf-8")
        return
    ev_saved = load_json(docs / "data" / "evaluations.json", None)
    if a.preclose:
        sp = docs / "data" / "preclose_state.json"
        state = load_json(sp, {})
        now_utc = dt.datetime.now(dt.timezone.utc)
        if a.markets.strip():
            due = [m for m in re.split(r"[,\s]+", a.markets.strip().lower()) if m in MARKETS]
        else:
            due = []
            for m in MARKETS:
                mins, local = minutes_to_close(m, now_utc)
                if mins is not None and 10 <= mins <= 80 and state.get(m) != local.date().isoformat():
                    due.append(m)
        due = [m for m in due if any(h.get("market") == m and "error" not in h for h in holdings)]
        if not due:
            print("Pre-close: nothing due (outside the window, already done today, or no holdings there).")
            return
        print(f"Pre-close check for: {', '.join(due)}")
        hv_pc, rows = run_preclose(holdings, live_prices, due, now_utc)
        for r in hv_pc["holdings"]:
            print(f"  {r['ticker']}: {r['preclose']['label']} - {r['preclose']['why']}")
        preclose_alerts(hv_pc, due)
        for m in due:
            state[m] = minutes_to_close(m, now_utc)[1].date().isoformat()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        hv = load_json(docs / "data" / "holdings.json", None) or {"holdings": [], "markets": {}}
        for r in hv["holdings"]:
            if r["file"] in rows:
                r["preclose"] = rows[r["file"]]["preclose"]
        known = {r["file"] for r in hv["holdings"]}
        hv["holdings"] += [r for f, r in rows.items() if f not in known]
        (docs / "data" / "holdings.json").write_text(json.dumps(hv, indent=1, default=jdefault), encoding="utf-8")
        res = load_json(docs / "data" / "latest.json", None)
        if res and "markets" in res:
            page = render_page(res, ledger, summarise(ledger), False, hv, load_json(docs / "data" / "politicians.json", {}),
                               ev_saved)
            (docs / "index.html").write_text(page, encoding="utf-8")
        return
    if a.holdings:
        hv = carry_preclose(run_holdings(holdings, live_prices, today), docs)
        for r in hv["holdings"]:
            print(f"  {r['ticker']}: {r['status']} - {r['reason']}")
        pol, fresh = refresh_politicians(docs, today)
        res = load_json(docs / "data" / "latest.json", None)
        politician_alerts(fresh, res)
        if res and "markets" in res:
            page = render_page(res, ledger, summarise(ledger), False, hv, pol, ev_saved)
            (docs / "index.html").write_text(page, encoding="utf-8")
            (docs / "data" / "holdings.json").write_text(json.dumps(hv, indent=1, default=jdefault), encoding="utf-8")
        do_alerts(hv, docs, scheduled_digest=False)
        return
    # Backup schedules: skip if this weekend's scan already ran (unless started by hand)
    prev = load_json(docs / "data" / "latest.json", {})
    manual = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not manual and today.weekday() in (5, 6) and prev.get("date"):
        last = dt.date.fromisoformat(prev["date"])
        if last.weekday() in (5, 6) and (today - last).days <= 1:
            print(f"This weekend's scan already ran on {last}; nothing to do.")
            return
    # only weekend scans (Friday closes) go into the track record; weekday refreshes just update the page
    res = run_pipeline(live_tv, live_prices, live_insiders, today, ledger, prev, record=today.weekday() in (5, 6),
                       fetch_uk_dealings=live_uk_dealings)
    hv = run_holdings(holdings, live_prices, today)
    pol, fresh = refresh_politicians(docs, today)
    ev = ev_saved
    if watch:
        try:
            ev = run_evaluations(watch, live_tv, live_prices, live_insiders, live_uk_dealings, today, live_tv)
            for it in ev["items"]:
                it["added"] = next((w["added"] for w in watch if f'{w["ticker"]}:{w["market"]}' == it["key"]), "")
            (docs / "data" / "evaluations.json").write_text(json.dumps(ev, indent=1, default=jdefault), encoding="utf-8")
        except Exception as e:
            print(f"  evaluations refresh failed: {e}")
    summ = write_outputs(res, ledger, docs, hv=hv, pol=pol, ev=ev)
    print(f"\nWrote {docs / 'index.html'}  ({len(ledger)} picks in track record)")
    do_alerts(hv, docs, scheduled_digest=True)
    politician_alerts(fresh, res)
    rp = docs / "data" / "rns_alerted.json"
    rp.write_text(json.dumps(uk_dealing_alerts(res, load_json(rp, {})), indent=1), encoding="utf-8")
    if not a.no_email:
        try:
            send_email(res, summ)
        except Exception as e:
            print(f"Email failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
