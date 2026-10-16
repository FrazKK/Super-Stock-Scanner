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
        "min_avg_value": 300_000,              # average daily traded value, USD
        "exchanges": {"NASDAQ", "NYSE", "AMEX", "NYSE ARCA", "NYSEARCA"},
        "bench": ["IWM", "SPY"],               # small caps first, fallback
    },
    "canada": {
        "label": "Canada",
        "universe_price_min": 0.10,
        "stine_price": (0.50, 15.0),           # CAD
        "mcap": (10e6, 1.5e9),
        "min_avg_value": 50_000,
        "exchanges": {"TSX", "TSXV", "CSE", "NEO"},
        "bench": ["XCS.TO", "XIC.TO", "^GSPTSE"],
    },
    "uk": {
        "label": "UK",
        "universe_price_min": 1.0,             # pence
        "stine_price": (5.0, 1500.0),          # pence (5p to 15)
        "mcap": (5e6, 1.0e9),
        "min_avg_value": 20_000,               # GBP
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
}

RESOURCE_SECTORS = {"Energy Minerals", "Non-Energy Minerals"}

REQUIRE_PROFIT = True          # Stine only: drop loss-makers when EPS data exists
MAX_CANDIDATES = 400           # per market, how many get full price analysis
SHOW_PER_LIST = 20             # cards per strategy per market
RECORD_PER_LIST = 10           # how many of those go into the track record
                               # (Minervini "trend only" names are shown but not recorded)
NEW_WINDOW_WEEKS = 8           # a pick is "NEW" if not picked in this many weeks
WATCH_SHOW = 10

# Stine
STINE_BASE_WEEKS = 12
STINE_BASE_MAX_DEPTH = 0.40
STINE_BREAKOUT_LOOKBACK = 8    # breakout must be within the last 8 weeks
STINE_VOL_MULT = 2.5
STINE_STEEP = 0.03             # 30-wk MA up 3%+ in 4 weeks
STINE_MIN_SCORE = 6
EPS_GROWTH_MIN = 25.0
PE_MAX = 20.0
SMALL_FLOAT = 30e6

# Weinstein
WEIN_LOOKBACK = 8
WEIN_VOL_MULT = 2.0

# Minervini
MIN_RS = 70
MIN_STOP = 0.08

# Episodic pivot
EP_GAP = 0.10
EP_VOL_MULT = 3.0
EP_MIN_MOVE = 0.08

HORIZONS = [4, 12, 26]         # weeks, for the track record

ROOT = Path(__file__).resolve().parent

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


def live_tv(market: str) -> pd.DataFrame:
    from tradingview_screener import Query, col

    cfg = MARKETS[market]
    cols = TV_CORE + TV_OPTIONAL
    for _ in range(len(TV_OPTIONAL) + 1):
        try:
            q = (Query().set_markets(market).select(*cols)
                 .where(col("type") == "stock",
                        col("market_cap_basic").between(*cfg["mcap"]),
                        col("average_volume_10d_calc") > 5000)
                 .limit(6000))
            total, df = q.get_scanner_data()
            print(f"  [{market}] TradingView returned {len(df)} of {total}")
            for c in TV_CORE + TV_OPTIONAL:
                if c not in df.columns:
                    df[c] = np.nan
            return df
        except Exception as e:
            bad = [b for b in re.findall(r'[Uu]nknown field "?([\w.|]+)"?', str(e))
                   if b in cols and b not in TV_CORE]
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
    add("Base breakout on big volume", bo, 2,
        f"{bo_vol:.1f}x volume, {bo_back} wk ago" if bo else "no breakout in 8 weeks", req=True)
    add("Steep 30-week MA", slope >= STINE_STEEP, 1, f"{slope * 100:+.1f}% in 4 wks")
    add("Biggest weekly volume in 6 months", record_vol, 1)
    add("Tight base", depth <= STINE_BASE_MAX_DEPTH, 1, f"{depth * 100:.0f}% range")
    eg, pe, flt = (num(rec.get(k)) for k in ("earnings_per_share_diluted_yoy_growth_fq",
                                              "price_earnings_ttm", "float_shares_outstanding"))
    add("EPS growth > 25%", eg is not None and eg >= EPS_GROWTH_MIN, 1, "no data" if eg is None else f"{eg:+.0f}% YoY")
    add("Low P/E", pe is not None and 0 < pe <= PE_MAX, 1, "no data" if pe is None else f"P/E {pe:.1f}")
    add("Small float", flt is not None and flt <= SMALL_FLOAT, 1, "no data" if flt is None else f"{flt / 1e6:.1f}M shares")
    rs = num(rec.get("rs_rank"))
    add("Market leader (RS rank 80+)", rs is not None and rs >= 80, 1, "no data" if rs is None else f"RS {rs:.0f}")
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
            "stop": None, "required_ok": trend and bo, "trend_ok": trend, "dist_ma10": dist}


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


def weinstein(rec, w, bench_w):
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
        return None
    last = c.iloc[-1]
    above = bool(last > ma30.iloc[-1])
    slope = float(ma30.iloc[-1] / ma30.iloc[-5] - 1)
    m = mansfield_rs(w, bench_w)
    mrs = num(m.iloc[-1]) if m is not None else None
    rs = num(rec.get("rs_rank"))

    checks = []

    def add(label, ok, pts, note="", req=False):
        checks.append({"label": label, "pass": bool(ok), "pts": pts if ok else 0, "max": pts, "note": note, "req": req})

    add("Breakout above resistance on volume", True, 2, f"{found['vx']:.1f}x volume, {found['back']} wk ago", req=True)
    add("Came out of a flat Stage 1 base", found["flat"] and found["basing"], 1, req=True)
    add("Still above 30-week MA, MA not falling", above and slope >= -0.005, 1, f"MA {slope * 100:+.1f}% in 4 wks", req=True)
    add("Beating the market (Mansfield RS > 0)", mrs is not None and mrs > 0, 2,
        "no index data" if mrs is None else f"{mrs * 100:+.0f}%")
    add("30-week MA rising", slope > 0.005, 1)
    add("Breakout volume 3x+", found["vx"] >= 3, 1)
    add("Not extended (within 15% of breakout)", last <= found["res"] * 1.15, 1,
        f"{(last / found['res'] - 1) * 100:+.0f}% vs breakout")
    add("RS rank 70+", rs is not None and rs >= 70, 1, "no data" if rs is None else f"RS {rs:.0f}")

    if last > ma30.iloc[-1] * 1.25:
        tag, cls = "Extended, wait", "z-ext"
    elif found["back"] <= 1:
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


def minervini(rec, d):
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
    add("RS rank 70+", rs is not None and rs >= MIN_RS, 1, "no data" if rs is None else f"RS {rs:.0f}", req=True)
    if not all(ch["pass"] for ch in checks):
        return None

    def depth(x):
        return float((x["High"].max() - x["Low"].min()) / x["High"].max())

    da, db, dc = depth(d.iloc[-65:-35]), depth(d.iloc[-35:-15]), depth(d.iloc[-15:])
    pivot0 = float(d["High"].iloc[-25:-5].max())
    # each pullback clearly smaller than the last, final one tight, price near the pivot
    vcp = db <= 0.75 * da and dc <= 0.75 * db and dc <= 0.10 and last >= pivot0 * 0.92
    dry = v.iloc[-10:].mean() < 0.8 * v.iloc[-60:-10].mean()
    pivot = float(h.iloc[-25:-5].max())
    avg50 = v.iloc[-55:-5].mean()
    base_ok = depth(d.iloc[-25:-5]) <= 0.15          # a real consolidation before the pivot
    brk = bool(base_ok and (c.iloc[-5:] > pivot).any() and v.iloc[-5:].max() >= 1.4 * avg50 and last > pivot * 0.97)
    eg = num(rec.get("earnings_per_share_diluted_yoy_growth_fq"))

    add("Volatility contraction (VCP)", vcp, 2, f"pullbacks {da * 100:.0f}% > {db * 100:.0f}% > {dc * 100:.0f}%")
    add("Volume drying up", dry, 1)
    add("Pivot breakout on volume", brk, 2, f"pivot {fmt_num(pivot)}")
    add("EPS growth > 25%", eg is not None and eg >= EPS_GROWTH_MIN, 1, "no data" if eg is None else f"{eg:+.0f}% YoY")

    if brk and last > pivot * 1.10:
        tag, cls = "Extended from pivot", "z-ext"
    elif brk:
        tag, cls = "Breaking out", "z-buy"
    elif vcp:
        tag, cls = "VCP forming", "z-ok"
    else:
        tag, cls = "Trend only", "z-ok"
    return {"strategy": "minervini", "checks": checks, "tag": tag, "tag_class": cls,
            "plan": f"Buy point: above the pivot {fmt_num(pivot)}. Sell rule: 8% stop below your entry.",
            "stop_pct": MIN_STOP, "required_ok": True}


# ---------------------------------------------------------------------------
# Strategy 4: Episodic pivot
# ---------------------------------------------------------------------------


def episodic(rec, d):
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
    if not best:
        return None
    last = c.iloc[-1]
    if last < best["low"]:
        return None
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
    return {"strategy": "episodic", "checks": checks, "tag": tag, "tag_class": cls,
            "plan": f"Check the news behind the gap. Sell rule: below the gap-day low ({fmt_num(best['low'])}).",
            "stop": best["low"], "required_ok": True, "gap_date": str(d.index[j].date())}


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


def run_pipeline(fetch_tv, fetch_prices, fetch_insiders, today: dt.date, ledger: list[dict]) -> dict:
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
            cands[m] = pick_candidates(u)
            print(f"  universe {len(u)}, candidates {len(cands[m])}")
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
        lists = {k: [] for k in STRATEGIES}
        watch = []
        lo, hi = MARKETS[m]["stine_price"]
        for rec in cand.to_dict("records"):
            d = prices.get(rec["ysym"])
            if d is None or len(d) < 120:
                continue
            w = to_weekly(d)
            ins = us_ins.get(str(rec["ticker"]).upper()) if m == "america" else None
            try:
                # Stine
                eps = num(rec.get("earnings_per_share_diluted_ttm"))
                if lo <= (num(rec.get("close")) or 0) <= hi and not (REQUIRE_PROFIT and eps is not None and eps <= 0):
                    r = stine(rec, w, ins, m)
                    if r:
                        card = make_card(rec, r, w, m, ins, rec["ysym"])
                        if r["required_ok"] and card["score"] >= STINE_MIN_SCORE:
                            lists["stine"].append(card)
                        elif r["trend_ok"] and not r["required_ok"] and card["score"] >= STINE_MIN_SCORE - 2:
                            watch.append(card)
                for fn, args in ((weinstein, (rec, w, bench_w)), (minervini, (rec, d)), (episodic, (rec, d))):
                    r = fn(*args)
                    if r and r["required_ok"]:
                        lists[r["strategy"]].append(make_card(rec, r, w, m, ins, rec["ysym"]))
            except Exception as e:
                print(f"  {rec['ysym']}: analysis error {e}")
        for k in lists:
            lists[k].sort(key=lambda x: (x["score"], x.get("rs_rank") or 0), reverse=True)
            lists[k] = lists[k][:SHOW_PER_LIST]
        watch.sort(key=lambda x: x["score"], reverse=True)
        # cross-reference which methods flagged each ticker
        flagged = {}
        for k, L in lists.items():
            for x in L:
                flagged.setdefault(x["ticker"], []).append(STRATEGIES[k]["short"])
        for k, L in lists.items():
            for x in L:
                x["also"] = [s for s in flagged[x["ticker"]] if s != STRATEGIES[k]["short"]]
        results["markets"][m] = {
            "label": MARKETS[m]["label"], "universe": int(len(universes[m])), "checked": int(len(cand)),
            "market": market_check(bsym, bench_w), "bench": bsym,
            "lists": lists, "watch": watch[:WATCH_SHOW],
        }
        print(f"  " + ", ".join(f"{STRATEGIES[k]['short']} {len(v)}" for k, v in lists.items()))

    update_ledger(ledger, results, prices, today)
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
    if p["strategy"] in ("stine", "weinstein"):
        w = to_weekly(d)
        ma = w["Close"].rolling(10 if p["strategy"] == "stine" else 30).mean()
        wk = w.loc[w.index > t0]
        for ix, row in wk.iterrows():
            if not pd.isna(ma.loc[ix]) and row["Close"] < ma.loc[ix]:
                exit_px, exit_date = float(row["Close"]), ix
                break
    else:
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


def update_ledger(ledger, results, prices, today):
    today_s = today.isoformat()
    ledger[:] = [p for p in ledger if p["date"] != today_s]       # re-runs on the same day replace
    recent = {(p["strategy"], p["market"], p["ticker"]) for p in ledger
              if (today - dt.date.fromisoformat(p["date"])).days <= NEW_WINDOW_WEEKS * 7}
    for m, data in results["markets"].items():
        if "lists" not in data:
            continue
        bsym = data.get("bench")
        bclose = float(prices[bsym]["Close"].iloc[-1]) if bsym in prices else None
        for k, L in data["lists"].items():
            for i, x in enumerate(L):
                key = (k, m, x["ticker"])
                x["new"] = key not in recent and not (k == "minervini" and x["tag"] == "Trend only")
                if i >= RECORD_PER_LIST or not x["new"]:
                    continue
                if x["tag"] == "Possible takeover bid":
                    continue
                if k == "minervini" and x["tag"] == "Trend only":
                    continue          # Minervini only buys at a VCP or pivot, so don't score trend-only names
                ledger.append({
                    "date": today_s, "market": m, "strategy": k, "ticker": x["ticker"],
                    "exchange": x["exchange"], "name": x["name"], "sector": x["sector"], "ysym": x["ysym"],
                    "entry": x["entry_close"], "bench": bsym, "bench_entry": bclose,
                    "score": x["score"], "max_score": x["max_score"], "tag": x["tag"],
                    "stop": x.get("stop"), "stop_pct": x.get("stop_pct"),
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


def summarise(ledger):
    out = {}
    for k in STRATEGIES:
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


def card_html(r, show_market=False):
    checks = "".join(
        f'<li class="{"ok" if c["pass"] else ("na" if c["pass"] is None else "no")}">'
        f'<span>{E(c["label"])}</span><small>{E(c["note"])}</small></li>' for c in r["checks"])
    p = r["score"] / r["max_score"] if r["max_score"] else 0
    new = '<span class="new">NEW</span>' if r.get("new") else ""
    also = (f'<div class="also">Also flagged by: {E(", ".join(r["also"]))}</div>' if r.get("also") else "")
    ins = ""
    if r.get("insider"):
        i = r["insider"]
        ins = f'<div class="meta">Insider buys (90 days): {i["buys"]}, ${i["value"]:,.0f}</div>'
    mk = f' · {E(MARKETS[r["market"]]["label"])}' if show_market else ""
    strat = f'<span class="strat">{E(STRATEGIES[r["strategy"]]["short"])}</span>' if show_market else ""
    rs = f' · RS {r["rs_rank"]:.0f}' if r.get("rs_rank") is not None else ""
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
  </div>
</details>"""


def banner(mc):
    if not mc:
        return ""
    cls = {"up": "b-up", "mixed": "b-mixed", "down": "b-down"}.get(mc["state"], "b-mixed")
    extra = f' <small>({E(str(mc.get("bench")))}, {mc["change_4w"]:+.1f}% in 4 wks)</small>' if mc.get("change_4w") is not None else ""
    return f'<div class="banner {cls}">{E(mc["text"])}{extra}</div>'


def strategy_section(k, L, open_=True, show_market=False):
    s = STRATEGIES[k]
    body = "".join(card_html(r, show_market) for r in L) if L else '<p class="empty">Nothing this week.</p>'
    new = sum(1 for r in L if r.get("new"))
    newtxt = f' · {new} new' if new else ""
    return (f'<details class="sec"{" open" if open_ else ""}><summary><span class="st">{E(s["label"])}</span>'
            f'<span class="cnt">{len(L)}{newtxt}</span></summary><p class="blurb">{E(s["blurb"])}</p>{body}</details>')


def track_html(ledger, summ, today):
    if not ledger:
        return '<p class="empty">The track record starts with this week\'s picks.</p>'
    parts = ['<p class="blurb">Each week the top 10 new picks per method and market are recorded at that '
             'Friday\'s close and compared with the market\'s index. "Buy and hold" ignores the sell rules; '
             '"With sell rules" exits when the method says to.</p>']
    first4 = min(dt.date.fromisoformat(p["date"]) for p in ledger) + dt.timedelta(weeks=4)
    if not any("r4" in p for p in ledger):
        parts.append(f'<p class="note">First 4-week results arrive around {first4.strftime("%d %b")}. '
                     'Give it at least 3 months before drawing conclusions.</p>')
    for k, s in STRATEGIES.items():
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
                     f'<td><b>{E(p["ticker"])}</b><br><small>{E(STRATEGIES[p["strategy"]]["short"])} · '
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
.picks small{color:var(--mute)}
.mk{font-size:15px;font-weight:700;margin:16px 2px 4px;color:var(--mute)}
.legend{color:var(--mute);font-size:12px;margin-top:18px;line-height:1.6}
.legend i{display:inline-block;width:14px;height:2px;vertical-align:middle;margin-right:4px}
footer{margin-top:20px;color:var(--mute);font-size:12px}
@media (max-width:420px){.spark{display:none}}
"""


def render_page(results, ledger, summ, demo=False):
    tabs, secs = [], []
    first = True
    for m, data in results["markets"].items():
        if "lists" not in data:
            body = f'<p class="empty">Scan failed this week: {E(str(data.get("error", ""))[:200])}</p>'
            n = 0
        else:
            L = data["lists"]
            n = sum(len(v) for v in L.values())
            body = banner(data.get("market"))
            body += (f'<p class="stats">{data["universe"]} stocks screened · {data["checked"]} charts analysed</p>')
            body += "".join(strategy_section(k, L[k]) for k in STRATEGIES)
            if data.get("watch"):
                body += ('<details class="sec"><summary><span class="st">Watching</span>'
                         f'<span class="cnt">{len(data["watch"])}</span></summary>'
                         '<p class="blurb">Strong Stine candidates in an uptrend that haven\'t broken out on '
                         'volume yet. Not recorded in the track record.</p>'
                         + "".join(card_html(r) for r in data["watch"]) + "</details>")
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
        for k in ("episodic", "weinstein", "stine", "minervini"):
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
    tabs.append(("track", "Track record"))
    secs.append(("track", track_html(ledger, summ, results["date"])))

    nav = "".join(f'<button class="{"on" if i == 0 else ""}" data-t="{k}">{E(lbl)}</button>' for i, (k, lbl) in enumerate(tabs))
    main = "".join(f'<section id="{k}" class="{"on" if i == 0 else ""}">{b}</section>' for i, (k, b) in enumerate(secs))
    gen = dt.datetime.fromisoformat(results["generated"]).strftime("%a %d %b %Y")
    demo_b = '<div class="demo">SAMPLE DATA: simulated stocks to test the layout</div>' if demo else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Superstock Scanner</title>
<link rel="manifest" href="manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="theme-color" content="#0f1115">
<style>{CSS}</style></head><body>
<header><h1>Superstock Scanner</h1>
<div class="sub">Four breakout methods, scanned weekly · updated {gen} · <a href="history/">past weeks</a></div>{demo_b}</header>
<nav>{nav}</nav>
<main>{main}</main>
<div class="legend">Chart: <i style="background:var(--ink)"></i>price <i style="background:var(--acc)"></i>10-week MA
 <i style="background:var(--warn)"></i>30-week MA · last 30 weeks. <b>NEW</b> = not flagged by that method in the last 8 weeks.
 Tap a stock for its checklist, entry and sell rule. Canada and UK insider buys aren't scored automatically, so use the filings link.</div>
<footer>Free, unofficial data from TradingView, Yahoo Finance and OpenInsider. It can be delayed or wrong, so check everything before acting.
A research shortlist, not financial advice.</footer>
<script>
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{{
 document.querySelectorAll('nav button,section').forEach(x=>x.classList.remove('on'));
 b.classList.add('on');document.getElementById(b.dataset.t).classList.add('on');
 try{{localStorage.setItem('tab',b.dataset.t)}}catch(e){{}}}});
try{{const t=localStorage.getItem('tab');t&&document.querySelector(`nav [data-t="${{t}}"]`)?.click()}}catch(e){{}}
</script></body></html>"""


def write_outputs(results, ledger, docs: Path, demo=False):
    (docs / "data").mkdir(parents=True, exist_ok=True)
    (docs / "history").mkdir(parents=True, exist_ok=True)
    summ = summarise(ledger)
    page = render_page(results, ledger, summ, demo)
    (docs / "index.html").write_text(page, encoding="utf-8")
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
    for k, s in STRATEGIES.items():
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

    def insiders(self):
        return {"AM00": {"buys": 2, "value": 250000.0, "insiders": [], "latest": ""},
                "AM07": {"buys": 1, "value": 90000.0, "insiders": [], "latest": ""}}


def run_demo(weeks: int, docs: Path):
    sim = Simulator()
    if docs.exists():
        shutil.rmtree(docs)
    ledger: list[dict] = []
    fridays = [d for d in sim.days if d.weekday() == 4][-weeks:]
    for fri in fridays:
        sim.asof = fri
        res = run_pipeline(sim.tv, sim.fetch_prices, sim.insiders, fri.date(), ledger)
        res["demo"] = True
        summ = write_outputs(res, ledger, docs, demo=True)
    return res, ledger, summ


# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="simulated data, written to demo_docs/")
    ap.add_argument("--weeks", type=int, default=30, help="weeks to simulate in demo mode")
    ap.add_argument("--no-email", action="store_true")
    a = ap.parse_args()
    if a.demo:
        run_demo(a.weeks, ROOT / "demo_docs")
        print(f"\nWrote {ROOT / 'demo_docs' / 'index.html'}")
        return
    docs = ROOT / "docs"
    lp = docs / "data" / "picks.json"
    ledger = json.loads(lp.read_text()) if lp.exists() else []
    today = dt.datetime.now(dt.timezone.utc).date()
    res = run_pipeline(live_tv, live_prices, live_insiders, today, ledger)
    summ = write_outputs(res, ledger, docs)
    print(f"\nWrote {docs / 'index.html'}  ({len(ledger)} picks in track record)")
    if not a.no_email:
        try:
            send_email(res, summ)
        except Exception as e:
            print(f"Email failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
