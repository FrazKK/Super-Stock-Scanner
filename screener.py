"""
Superstock Scanner
==================
A free weekly screen for US, Canadian and UK stocks, loosely based on Jesse
Stine's "Insider Buy Superstocks" checklist.

Pipeline
  1. Broad screen   - TradingView's public scanner (free, unofficial) for each
                      market: price, trend, size, fundamentals.
  2. Weekly charts  - Yahoo Finance weekly bars (free, via yfinance) for the
                      survivors: 10/30-week MAs, MA slope, base tightness,
                      breakout volume, distance from the "magic line".
  3. Insider buys   - US: OpenInsider (SEC Form 4 purchases, last 90 days).
                      Canada / UK: a link to check by hand.
  4. Output         - docs/index.html (phone-friendly page), docs/data/*.json,
                      dated archive pages, and an optional email.

Run:  python screener.py            (live data)
      python screener.py --demo     (made-up data, to preview the page)

Not financial advice. Free data sources are unofficial and can have gaps.
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
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# SETTINGS - change these to tune the screen
# ---------------------------------------------------------------------------

MARKETS = {
    # TradingView market name: settings
    "america": {
        "label": "USA",
        "price_min": 2.0, "price_max": 15.0,        # USD
        "mcap_min": 20e6, "mcap_max": 1.0e9,        # USD
        "min_avg_volume": 50_000,
        "exchanges": {"NASDAQ", "NYSE", "AMEX", "NYSE ARCA", "NYSEARCA"},
    },
    "canada": {
        "label": "Canada",
        "price_min": 0.50, "price_max": 15.0,       # CAD
        "mcap_min": 15e6, "mcap_max": 1.0e9,        # CAD
        "min_avg_volume": 30_000,
        "exchanges": {"TSX", "TSXV", "CSE", "NEO"},
    },
    "uk": {
        "label": "UK",
        # LSE/AIM prices are quoted in pence on TradingView
        "price_min": 5.0, "price_max": 1500.0,      # pence (5p to 15)
        "mcap_min": 10e6, "mcap_max": 750e6,        # GBP
        "min_avg_volume": 20_000,
        "exchanges": {"LSE", "LSIN"},
    },
}

# Exclude loss-making companies when earnings data exists. Stine's method
# relies on real, growing earnings. Companies with no data are kept and flagged.
REQUIRE_PROFIT = True

MAX_CHART_CHECKS_PER_MARKET = 200   # how many survivors get weekly-chart analysis
SHOW_PER_MARKET = 30                # rows shown on the page per market
MIN_SCORE_TO_SHOW = 4

# Weekly-chart rules
BASE_WEEKS = 12            # length of the base before a breakout
BASE_MAX_DEPTH = 0.40      # base high/low range must be under 40% to count as "tight"
BREAKOUT_LOOKBACK = 3      # breakout must have happened in the last N weeks
BREAKOUT_VOL_MULT = 2.5    # breakout week volume vs prior 10-week average
STEEP_MA30_4WK = 0.03      # 30-week MA up at least 3% over 4 weeks = "steep"
BUY_ZONE_MAX = 0.08        # within 8% above the 10-week MA = near the magic line
EXTENDED_MIN = 0.25        # over 25% above the 10-week MA = extended, don't chase

# Fundamentals
EPS_GROWTH_MIN = 25.0      # % quarterly YoY
PE_MAX = 20.0
SMALL_FLOAT = 30e6         # shares

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"

# ---------------------------------------------------------------------------
# 1. Broad screen (TradingView)
# ---------------------------------------------------------------------------

CORE_COLS = [
    "name", "description", "exchange", "close", "currency", "volume",
    "average_volume_10d_calc", "relative_volume_10d_calc",
    "SMA50", "SMA200", "price_52_week_high", "market_cap_basic", "type",
]
OPTIONAL_COLS = [
    "SMA150", "price_earnings_ttm", "earnings_per_share_diluted_ttm",
    "earnings_per_share_diluted_yoy_growth_fq", "total_revenue_yoy_growth_fq",
    "float_shares_outstanding", "sector", "industry", "typespecs",
]

# Not operating companies: SPACs, funds, trusts, REITs, BDCs, units, preferreds
EXCLUDE_NAME = re.compile(
    r"\b(acquisition|spac|blank check|merger corp|investment company|fund|trust|reit|etf|etn|income|bdc|"
    r"finance corp|capital corp|investment corp|lending|units?|warrants?|rights?|"
    r"preferred|pref|notes?|debentures?|split corp|royalty)\b", re.I)
EXCLUDE_TYPESPEC = {"reit", "etf", "etn", "fund", "closedend", "closed-end", "spac", "unit",
                    "preferred", "trust", "right", "warrant"}
EXCLUDE_INDUSTRY = re.compile(r"(real estate investment trusts|investment trusts|closed end|"
                              r"financial conglomerates|investment managers)", re.I)


def tv_scan(market: str) -> pd.DataFrame:
    from tradingview_screener import Query, col

    cfg = MARKETS[market]
    cols = CORE_COLS + OPTIONAL_COLS
    for attempt in range(len(OPTIONAL_COLS) + 1):
        try:
            q = (
                Query()
                .set_markets(market)
                .select(*cols)
                .where(
                    col("type") == "stock",
                    col("market_cap_basic").between(cfg["mcap_min"], cfg["mcap_max"]),
                    col("average_volume_10d_calc") > cfg["min_avg_volume"],
                    col("close") > col("SMA50"),
                )
                .limit(5000)
            )
            total, df = q.get_scanner_data()
            print(f"  [{market}] TradingView returned {len(df)} of {total}")
            for c in cols:
                if c not in df.columns:
                    df[c] = np.nan
            return df
        except Exception as e:  # drop a field TradingView doesn't recognise, retry
            msg = str(e)
            bad = re.findall(r'[Uu]nknown field "?([\w.|]+)"?', msg)
            bad = [b for b in bad if b in cols and b not in CORE_COLS]
            if not bad:
                raise
            print(f"  [{market}] dropping unsupported field(s): {bad}")
            cols = [c for c in cols if c not in bad]
    raise RuntimeError("TradingView scan failed")


def is_excluded(row) -> bool:
    name = str(row.get("description") or "")
    tick = str(row.get("name") or "")
    if EXCLUDE_NAME.search(name) or EXCLUDE_INDUSTRY.search(str(row.get("industry") or "")):
        return True
    if re.search(r"\.(UN|U|WT|PR\.?\w*|DB)$", tick, re.I):
        return True
    # SPAC shells: finance-sector "corps" parked near their $10 trust value
    price = row.get("close")
    if (str(row.get("sector") or "") == "Finance" and isinstance(price, (int, float))
            and 9.5 <= price <= 11.5 and str(row.get("currency") or "") == "USD"
            and re.search(r"(\b(I{1,3}|IV|VI{0,3}|\d+)\b|capital\d|treasury|financial ii)", name, re.I)):
        return True
    specs = row.get("typespecs")
    if isinstance(specs, (list, tuple, np.ndarray)) and {str(x).lower() for x in specs} & EXCLUDE_TYPESPEC:
        return True
    return False


def first_pass(df: pd.DataFrame, market: str) -> pd.DataFrame:
    cfg = MARKETS[market]
    d = df.copy()
    d = d[d["exchange"].astype(str).str.upper().isin(cfg["exchanges"]) | (len(cfg["exchanges"]) == 0)]
    d = d[d["close"].between(cfg["price_min"], cfg["price_max"])]
    d = d[~d.apply(is_excluded, axis=1)]
    slow = d["SMA150"].where(d["SMA150"].notna(), d["SMA200"])
    d = d[(d["close"] > slow) & (d["SMA50"] > slow)]
    d = d[d["close"] >= 0.75 * d["price_52_week_high"]]
    if REQUIRE_PROFIT:
        eps = d["earnings_per_share_diluted_ttm"]
        d = d[eps.isna() | (eps > 0)]
    d = d.sort_values("relative_volume_10d_calc", ascending=False)
    return d.head(MAX_CHART_CHECKS_PER_MARKET)


# ---------------------------------------------------------------------------
# 2. Weekly chart analysis (Yahoo Finance)
# ---------------------------------------------------------------------------

YAHOO_SUFFIX = {"TSX": ".TO", "TSXV": ".V", "CSE": ".CN", "NEO": ".NE", "LSE": ".L", "LSIN": ".L"}


def yahoo_symbol(exchange: str, name: str) -> str:
    # e.g. BRK.B -> BRK-B, BT.A -> BT-A.L, ABC (TSXV) -> ABC.V
    sym = str(name).strip().rstrip(".").replace(".", "-")
    return sym + YAHOO_SUFFIX.get(exchange.upper(), "")


def weekly_bars(symbols: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i + 50]
        try:
            raw = yf.download(chunk, period="2y", interval="1wk", group_by="ticker",
                              auto_adjust=True, progress=False, threads=True)
        except Exception as e:
            print(f"  yfinance error: {e}")
            continue
        for s in chunk:
            try:
                d = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
                d = d.dropna(subset=["Close"])
                if len(d) >= 40:
                    out[s] = d
            except Exception:
                pass
        time.sleep(1)
    return out


def analyse_weekly(w: pd.DataFrame) -> dict:
    c, h, l, v = w["Close"], w["High"], w["Low"], w["Volume"].fillna(0)
    ma10, ma30 = c.rolling(10).mean(), c.rolling(30).mean()
    last = c.iloc[-1]
    r: dict = {}
    r["above_ma30"] = bool(last > ma30.iloc[-1])
    r["ma10_over_ma30"] = bool(ma10.iloc[-1] > ma30.iloc[-1])
    r["ma30_slope_4w"] = float(ma30.iloc[-1] / ma30.iloc[-5] - 1)
    r["ma30_rising"] = r["ma30_slope_4w"] > 0
    r["ma30_steep"] = r["ma30_slope_4w"] >= STEEP_MA30_4WK
    r["dist_ma10"] = float(last / ma10.iloc[-1] - 1)

    # Breakout from a base on big volume in the last few weeks
    r["breakout"] = False
    r["breakout_weeks_ago"] = None
    r["breakout_vol_x"] = None
    r["base_depth"] = None
    n = len(w)
    for back in range(BREAKOUT_LOOKBACK):
        i = n - 1 - back
        if i - BASE_WEEKS - 10 < 0:
            break
        base_hi = h.iloc[i - BASE_WEEKS:i].max()
        base_lo = l.iloc[i - BASE_WEEKS:i].min()
        avg_vol = v.iloc[i - 10:i].mean()
        vol_x = v.iloc[i] / avg_vol if avg_vol > 0 else 0
        if c.iloc[i] > base_hi and vol_x >= BREAKOUT_VOL_MULT:
            r.update(breakout=True, breakout_weeks_ago=back, breakout_vol_x=round(float(vol_x), 1),
                     base_depth=float(base_hi / base_lo - 1))
            break
    if r["base_depth"] is None:
        r["base_depth"] = float(h.iloc[-BASE_WEEKS - 1:-1].max() / l.iloc[-BASE_WEEKS - 1:-1].min() - 1)
    r["tight_base"] = r["base_depth"] <= BASE_MAX_DEPTH
    r["record_volume"] = bool(v.iloc[-BREAKOUT_LOOKBACK:].max() >= v.iloc[-26:].max())
    r["weekly_closes"] = [round(float(x), 4) for x in c.iloc[-30:]]
    r["ma10_series"] = [None if pd.isna(x) else round(float(x), 4) for x in ma10.iloc[-30:]]
    r["ma30_series"] = [None if pd.isna(x) else round(float(x), 4) for x in ma30.iloc[-30:]]
    return r


# ---------------------------------------------------------------------------
# 3. Insider buying
# ---------------------------------------------------------------------------

OPENINSIDER_URL = (
    "http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd=90&fdr=&td=0&tdr=&fdlyl=&fdlyh="
    "&daysago=&xp=1&vl=10&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999&grp=0&nfl=&nfh=&nil=&nih="
    "&nol=&noh=&v2l=&v2h=&oc2l=&oc2h=&sortcol=0&cnt=5000&page=1"
)


def us_insider_buys() -> dict[str, dict]:
    """Open-market insider purchases (Form 4, code P) in the last 90 days, by ticker."""
    import requests

    try:
        resp = requests.get(OPENINSIDER_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0 superstock-scanner"})
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text), attrs={"class": "tinytable"})
    except Exception as e:
        print(f"  OpenInsider unavailable: {e}")
        return {}
    t = tables[0]
    t.columns = [str(c).replace("\xa0", " ").strip() for c in t.columns]
    t["value"] = pd.to_numeric(t["Value"].astype(str).str.replace(r"[^\d.]", "", regex=True), errors="coerce")
    out: dict[str, dict] = {}
    for tick, g in t.groupby("Ticker"):
        out[str(tick).strip().upper()] = {
            "buys": int(len(g)),
            "value": float(g["value"].sum()),
            "insiders": sorted({str(x) for x in g.get("Insider Name", [])})[:4],
            "latest": str(g.get("Trade Date", pd.Series([""])).max()),
        }
    print(f"  OpenInsider: {len(out)} US tickers with insider purchases in 90 days")
    return out


def insider_link(market: str, exchange: str, ticker: str) -> str:
    if market == "america":
        return f"http://openinsider.com/{ticker}"
    if market == "canada":
        return f"https://www.canadianinsider.com/node/7?menu_tickersearch={ticker}"
    return f"https://www.investegate.co.uk/company/{ticker}"


# ---------------------------------------------------------------------------
# 4. Scoring
# ---------------------------------------------------------------------------

def num(x):
    try:
        x = float(x)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def score_row(row: dict, wk: dict, ins: dict | None, market: str) -> dict:
    checks = []

    def add(label, passed, pts, note=""):
        checks.append({"label": label, "pass": passed, "pts": pts if passed else 0, "note": note})

    add("Above rising 30-week MA", wk["above_ma30"] and wk["ma30_rising"] and wk["ma10_over_ma30"], 2)
    add("Steep 30-week MA", wk["ma30_steep"], 1, f"{wk['ma30_slope_4w']*100:+.1f}% in 4 wks")
    bo_note = (f"{wk['breakout_vol_x']}x volume, {wk['breakout_weeks_ago']} wk ago"
               if wk["breakout"] else "no recent volume breakout")
    add("Base breakout on big volume", wk["breakout"], 2, bo_note)
    add("Biggest weekly volume in 6 months", wk["record_volume"], 1)
    add("Tight base", wk["tight_base"], 1, f"{wk['base_depth']*100:.0f}% range")

    eg = num(row.get("earnings_per_share_diluted_yoy_growth_fq"))
    pe = num(row.get("price_earnings_ttm"))
    flt = num(row.get("float_shares_outstanding"))
    add("EPS growth > 25%", eg is not None and eg >= EPS_GROWTH_MIN, 1,
        "no data" if eg is None else f"{eg:+.0f}% YoY")
    add("Low P/E", pe is not None and 0 < pe <= PE_MAX, 1, "no data" if pe is None else f"P/E {pe:.1f}")
    add("Small float", flt is not None and flt <= SMALL_FLOAT, 1,
        "no data" if flt is None else f"{flt/1e6:.1f}M shares")
    if market == "america":
        add("Insider buying (90 days)", bool(ins), 2,
            f"{ins['buys']} buy(s), ${ins['value']:,.0f}" if ins else "none found")
    else:
        checks.append({"label": "Insider buying", "pass": None, "pts": 0, "note": "check link"})

    score = sum(c["pts"] for c in checks)
    max_score = sum((2 if c["label"].startswith(("Above", "Base", "Insider")) else 1)
                    for c in checks if c["pass"] is not None)

    d = wk["dist_ma10"]
    if d < 0:
        zone = "Below 10-wk MA"
    elif d <= BUY_ZONE_MAX:
        zone = "Near magic line"
    elif d >= EXTENDED_MIN:
        zone = "Extended"
    else:
        zone = "Above 10-wk MA"
    return {"score": score, "max_score": max_score, "checks": checks, "zone": zone}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_live() -> dict:
    results = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"), "markets": {}}
    print("Fetching US insider purchases...")
    us_ins = us_insider_buys()
    for market, cfg in MARKETS.items():
        print(f"\n== {cfg['label']} ==")
        try:
            df = tv_scan(market)
        except Exception as e:
            print(f"  scan failed: {e}")
            results["markets"][market] = {"label": cfg["label"], "error": str(e), "rows": []}
            continue
        cand = first_pass(df, market)
        print(f"  {len(cand)} pass the first filter")
        cand = cand.assign(ysym=[yahoo_symbol(e, n) for e, n in zip(cand["exchange"], cand["name"])])
        bars = weekly_bars(cand["ysym"].tolist())
        print(f"  weekly data for {len(bars)}")
        rows = []
        for rec in cand.to_dict("records"):
            w = bars.get(rec["ysym"])
            if w is None:
                continue
            try:
                wk = analyse_weekly(w)
            except Exception:
                continue
            if not wk["above_ma30"]:
                continue
            tick = str(rec["name"]).upper()
            ins = us_ins.get(tick) if market == "america" else None
            s = score_row(rec, wk, ins, market)
            rows.append(build_row(rec, wk, s, ins, market))
        rows.sort(key=lambda r: (r["score"], r["breakout"]), reverse=True)
        results["markets"][market] = {"label": cfg["label"], "scanned": int(len(df)),
                                      "checked": int(len(bars)), "rows": rows}
        print(f"  {len(rows)} scored; top score {rows[0]['score'] if rows else '-'}")
    return results


def build_row(rec, wk, s, ins, market) -> dict:
    exch = str(rec["exchange"]).upper()
    tick = str(rec["name"])
    cur = str(rec.get("currency") or "")
    return {
        "ticker": tick, "exchange": exch, "name": str(rec.get("description") or tick),
        "price": num(rec["close"]), "currency": cur,
        "mcap": num(rec.get("market_cap_basic")),
        "sector": str(rec.get("sector") or ""), "industry": str(rec.get("industry") or ""),
        "score": s["score"], "max_score": s["max_score"], "zone": s["zone"],
        "checks": s["checks"], "breakout": wk["breakout"],
        "dist_ma10": wk["dist_ma10"],
        "closes": wk["weekly_closes"], "ma10": wk["ma10_series"], "ma30": wk["ma30_series"],
        "insider": ins,
        "chart_url": f"https://www.tradingview.com/chart/?symbol={exch}:{tick}&interval=W",
        "insider_url": insider_link(market, exch, tick),
    }


def run_demo() -> dict:
    """Made-up data so you can preview the page. NOT real stocks."""
    rng = np.random.default_rng(7)
    results = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
               "demo": True, "markets": {}}
    for market, cfg in MARKETS.items():
        rows = []
        for k in range(12):
            n = 104
            base = np.concatenate([np.linspace(1, 1.6, 50), 1.6 + rng.normal(0, .04, 44),
                                   np.linspace(1.65, 2.3 + rng.random(), 10)])
            close = base * (3 + rng.random() * 5) * (1 + rng.normal(0, .02, n))
            w = pd.DataFrame({"Close": close, "High": close * 1.04, "Low": close * .96,
                              "Volume": np.r_[rng.integers(1e5, 3e5, n - 3), rng.integers(8e5, 2e6, 3)]})
            wk = analyse_weekly(w)
            rec = {"name": f"DEMO{k+1}", "description": f"Sample Company {k+1}", "exchange": list(cfg["exchanges"])[0],
                   "close": close[-1] * (100 if market == "uk" else 1), "currency": "GBX" if market == "uk" else "",
                   "market_cap_basic": rng.uniform(5e7, 6e8),
                   "earnings_per_share_diluted_yoy_growth_fq": rng.choice([np.nan, 15, 60, 140]),
                   "price_earnings_ttm": rng.choice([np.nan, 9, 14, 32]),
                   "float_shares_outstanding": rng.choice([np.nan, 12e6, 45e6]), "sector": "Sample"}
            ins = {"buys": 2, "value": 250000.0, "insiders": [], "latest": ""} if (market == "america" and k % 3 == 0) else None
            rows.append(build_row(rec, wk, score_row(rec, wk, ins, market), ins, market))
        rows.sort(key=lambda r: r["score"], reverse=True)
        results["markets"][market] = {"label": cfg["label"], "scanned": 0, "checked": 12, "rows": rows}
    return results


# ---------------------------------------------------------------------------
# Output: web page
# ---------------------------------------------------------------------------

def fmt_price(r):
    p = r["price"]
    if p is None:
        return "-"
    if r["currency"] == "GBX":
        return f"{p:,.1f}p"
    sym = {"USD": "$", "CAD": "C$", "GBP": "£"}.get(r["currency"], "$" if r["exchange"] in ("NASDAQ", "NYSE", "AMEX") else "")
    return f"{sym}{p:,.2f}"


def fmt_cap(r):
    m = r["mcap"]
    if m is None:
        return "-"
    return f"{m/1e9:.2f}bn" if m >= 1e9 else f"{m/1e6:.0f}m"


def sparkline(r) -> str:
    vals = [x for x in r["closes"] + [v for v in r["ma10"] + r["ma30"] if v is not None] if x is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1
    W, H = 120, 36

    def path(series):
        pts = [(i * W / (len(series) - 1), H - (v - lo) / span * H) for i, v in enumerate(series) if v is not None]
        return " ".join(f"{'M' if j == 0 else 'L'}{x:.1f},{y:.1f}" for j, (x, y) in enumerate(pts))

    return (f'<svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-hidden="true">'
            f'<path d="{path(r["ma30"])}" class="s-ma30"/><path d="{path(r["ma10"])}" class="s-ma10"/>'
            f'<path d="{path(r["closes"])}" class="s-px"/></svg>')


ZONE_CLASS = {"Near magic line": "z-buy", "Extended": "z-ext", "Above 10-wk MA": "z-ok", "Below 10-wk MA": "z-low"}


def card(r) -> str:
    e = html.escape
    checks = "".join(
        f'<li class="{"ok" if c["pass"] else ("na" if c["pass"] is None else "no")}">'
        f'<span>{e(c["label"])}</span><small>{e(c["note"])}</small></li>'
        for c in r["checks"]
    )
    pct = r["score"] / r["max_score"] if r["max_score"] else 0
    return f"""
<details class="card">
  <summary>
    <div class="score" style="--p:{pct:.2f}"><b>{r['score']}</b><small>/{r['max_score']}</small></div>
    <div class="who"><div class="tick">{e(r['ticker'])} <span class="ex">{e(r['exchange'])}</span></div>
      <div class="nm">{e(r['name'])}</div></div>
    {sparkline(r)}
    <div class="px"><div>{fmt_price(r)}</div><span class="zone {ZONE_CLASS.get(r['zone'],'')}">{e(r['zone'])}</span></div>
  </summary>
  <div class="body">
    <ul class="checks">{checks}</ul>
    <div class="meta">Mkt cap {fmt_cap(r)} · {e(r['sector'] or '-')} · {r['dist_ma10']*100:+.0f}% vs 10-wk MA</div>
    <div class="links"><a href="{e(r['chart_url'])}" target="_blank" rel="noopener">Weekly chart ↗</a>
      <a href="{e(r['insider_url'])}" target="_blank" rel="noopener">Insider filings ↗</a></div>
  </div>
</details>"""


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Superstock Scanner</title>
<link rel="manifest" href="manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0f1115">
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1b1d21;--mute:#6b6f76;--line:#e4e2dd;--good:#1f8a5b;--bad:#b4472e;--warn:#b07a12;--acc:#2f5bd3;--na:#8b8f96}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a20;--ink:#e8e9ec;--mute:#9298a2;--line:#262a32;--good:#4cc38a;--bad:#e2795f;--warn:#e0ad46;--acc:#7c9cff;--na:#6d727b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:0 16px 40px}
header{max-width:760px;margin:0 auto;padding:22px 0 8px}h1{font-size:22px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--mute);font-size:13px}.demo{background:var(--warn);color:#000;padding:8px 12px;border-radius:8px;margin-top:10px;font-weight:600}
nav{position:sticky;top:0;background:var(--bg);display:flex;gap:6px;max-width:760px;margin:10px auto 0;padding:8px 0;z-index:2}
nav button{flex:1;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:9px;border-radius:10px;font:inherit;font-weight:600}
nav button.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
main{max-width:760px;margin:0 auto}section{display:none}section.on{display:block}
.stats{color:var(--mute);font-size:12px;margin:6px 2px 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:8px;overflow:hidden}
summary{list-style:none;display:flex;align-items:center;gap:12px;padding:12px;cursor:pointer}summary::-webkit-details-marker{display:none}
.score{width:44px;height:44px;border-radius:50%;flex:none;display:grid;place-content:center;text-align:center;
 background:conic-gradient(var(--good) calc(var(--p)*360deg),var(--line) 0);position:relative}
.score::before{content:"";position:absolute;inset:4px;border-radius:50%;background:var(--card)}
.score b,.score small{position:relative;line-height:1}.score b{font-size:16px}.score small{font-size:10px;color:var(--mute)}
.who{flex:1;min-width:0}.tick{font-weight:700}.ex{font-weight:500;font-size:11px;color:var(--mute)}
.nm{color:var(--mute);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.spark{width:90px;height:32px;flex:none}.spark path{fill:none;stroke-width:1.5;vector-effect:non-scaling-stroke}
.s-px{stroke:var(--ink)}.s-ma10{stroke:var(--acc)}.s-ma30{stroke:var(--warn)}
.px{text-align:right;font-weight:600;flex:none}.zone{display:inline-block;font-size:11px;font-weight:600;padding:2px 7px;border-radius:99px;margin-top:3px;border:1px solid currentColor}
.z-buy{color:var(--good)}.z-ext{color:var(--bad)}.z-ok{color:var(--mute)}.z-low{color:var(--warn)}
.body{padding:0 12px 12px;border-top:1px solid var(--line)}
.checks{list-style:none;margin:10px 0;padding:0}.checks li{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px dashed var(--line)}
.checks li span::before{display:inline-block;width:18px;font-weight:700}.checks li.ok span::before{content:"✓";color:var(--good)}
.checks li.no span::before{content:"✗";color:var(--bad)}.checks li.na span::before{content:"?";color:var(--na)}
.checks small{color:var(--mute);text-align:right}
.meta{color:var(--mute);font-size:12px;margin:6px 0}.links{display:flex;gap:16px}.links a{color:var(--acc);font-weight:600;text-decoration:none}
.legend{color:var(--mute);font-size:12px;max-width:760px;margin:18px auto 0;line-height:1.6}
.legend i{display:inline-block;width:14px;height:2px;vertical-align:middle;margin-right:4px}
footer{max-width:760px;margin:20px auto 0;color:var(--mute);font-size:12px}
@media (max-width:420px){.spark{display:none}}
</style></head><body>
<header><h1>Superstock Scanner</h1>
<div class="sub">Weekly screen · updated __GENERATED__ · <a href="history/" style="color:inherit">past weeks</a></div>__DEMO__</header>
<nav>__TABS__</nav>
<main>__SECTIONS__</main>
<div class="legend">Chart: <i style="background:var(--ink)"></i>price <i style="background:var(--acc)"></i>10-week MA (magic line)
 <i style="background:var(--warn)"></i>30-week MA · last 30 weeks.<br>
<b>Near magic line</b> = within 8% of the 10-week MA (Stine's low-risk entry). <b>Extended</b> = 25%+ above it; wait for a pullback.
Canada and UK insider buys aren't scored automatically, so tap the filings link.</div>
<footer>Free, unofficial data from TradingView, Yahoo Finance and OpenInsider. It can be delayed or wrong; check everything before acting.
This is a research shortlist, not financial advice.</footer>
<script>
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('nav button,section').forEach(x=>x.classList.remove('on'));
 b.classList.add('on');document.getElementById(b.dataset.t).classList.add('on');
 try{localStorage.setItem('tab',b.dataset.t)}catch(e){}});
try{const t=localStorage.getItem('tab');t&&document.querySelector(`nav [data-t="${t}"]`)?.click()}catch(e){}
</script></body></html>"""


def render_page(results: dict) -> str:
    tabs, secs = [], []
    for i, (m, data) in enumerate(results["markets"].items()):
        rows = [r for r in data["rows"] if r["score"] >= MIN_SCORE_TO_SHOW][:SHOW_PER_MARKET]
        on = " on" if i == 0 else ""
        tabs.append(f'<button class="{on.strip()}" data-t="{m}">{html.escape(data["label"])} ({len(rows)})</button>')
        if data.get("error"):
            body = f'<p class="stats">Scan failed this week: {html.escape(data["error"][:200])}</p>'
        elif not rows:
            body = '<p class="stats">Nothing met the bar this week.</p>'
        else:
            body = (f'<p class="stats">{data.get("scanned", 0)} screened · {data.get("checked", 0)} charts checked · '
                    f'showing score ≥ {MIN_SCORE_TO_SHOW}</p>' + "".join(card(r) for r in rows))
        secs.append(f'<section id="{m}" class="{on.strip()}">{body}</section>')
    gen = dt.datetime.fromisoformat(results["generated"]).strftime("%a %d %b %Y, %H:%M UTC")
    demo = '<div class="demo">SAMPLE DATA: made-up stocks to preview the layout</div>' if results.get("demo") else ""
    return (PAGE.replace("__GENERATED__", gen).replace("__DEMO__", demo)
            .replace("__TABS__", "".join(tabs)).replace("__SECTIONS__", "".join(secs)))


def write_outputs(results: dict) -> Path:
    (DOCS / "data").mkdir(parents=True, exist_ok=True)
    (DOCS / "history").mkdir(parents=True, exist_ok=True)
    page = render_page(results)
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    (DOCS / "data" / "latest.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    if not results.get("demo"):
        day = results["generated"][:10]
        (DOCS / "history" / f"{day}.html").write_text(page.replace('href="history/"', 'href="./"')
                                                      .replace('href="manifest.json"', 'href="../manifest.json"'),
                                                      encoding="utf-8")
        (DOCS / "data" / f"{day}.json").write_text(json.dumps(results, default=str), encoding="utf-8")
    pages = sorted((DOCS / "history").glob("20*.html"), reverse=True)
    links = "".join(f'<li><a href="{p.name}">{p.stem}</a></li>' for p in pages) or "<li>No past weeks yet.</li>"
    (DOCS / "history" / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Past weeks</title><body style="font:16px -apple-system,sans-serif;padding:16px">'
        f'<h1>Past weeks</h1><p><a href="../">Latest</a></p><ul>{links}</ul>', encoding="utf-8")
    (DOCS / "manifest.json").write_text(json.dumps({
        "name": "Superstock Scanner", "short_name": "Superstocks", "start_url": "./",
        "display": "standalone", "background_color": "#0f1115", "theme_color": "#0f1115",
    }), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("")
    return DOCS / "index.html"


# ---------------------------------------------------------------------------
# Output: email (optional)
# ---------------------------------------------------------------------------

def send_email(results: dict):
    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"), os.getenv("EMAIL_TO")
    if not (user and pw and to):
        print("Email not configured; skipping.")
        return
    site = os.getenv("SITE_URL", "")
    parts = [f"<h2>Superstock Scanner: {results['generated'][:10]}</h2>"]
    if site:
        parts.append(f'<p><a href="{html.escape(site)}">Open the full page</a></p>')
    for data in results["markets"].values():
        rows = [r for r in data["rows"] if r["score"] >= MIN_SCORE_TO_SHOW][:10]
        parts.append(f"<h3>{html.escape(data['label'])}</h3>")
        if not rows:
            parts.append("<p>Nothing met the bar.</p>")
            continue
        parts.append('<table cellpadding="5" style="border-collapse:collapse;font:14px sans-serif">'
                     "<tr><th align=left>Ticker</th><th>Score</th><th align=right>Price</th><th align=left>Zone</th></tr>")
        for r in rows:
            parts.append(f'<tr><td><a href="{html.escape(r["chart_url"])}">{html.escape(r["ticker"])}</a> '
                         f'<small>{html.escape(r["name"][:28])}</small></td><td align=center>{r["score"]}/{r["max_score"]}</td>'
                         f'<td align=right>{fmt_price(r)}</td><td>{html.escape(r["zone"])}</td></tr>')
        parts.append("</table>")
    parts.append("<p style='color:#888;font-size:12px'>Research shortlist from free data. Not financial advice.</p>")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Superstock Scanner: {results['generated'][:10]}"
    msg["From"], msg["To"] = user, to
    msg.attach(MIMEText("".join(parts), "html"))
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    with smtplib.SMTP_SSL(host, int(os.getenv("SMTP_PORT", "465"))) as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    print(f"Email sent to {to}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="use made-up data to preview the page")
    ap.add_argument("--no-email", action="store_true")
    a = ap.parse_args()
    res = run_demo() if a.demo else run_live()
    out = write_outputs(res)
    print(f"\nWrote {out}")
    if not a.demo and not a.no_email:
        try:
            send_email(res)
        except Exception as e:
            print(f"Email failed: {e}", file=sys.stderr)
