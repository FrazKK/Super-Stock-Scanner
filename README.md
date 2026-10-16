# Superstock Scanner v2

A free weekly scan of US, Canadian and UK stocks. It runs four breakout methods side by side and keeps a track record, so you can see which method actually works before risking money on it. Every Saturday morning GitHub runs the scan and updates your page: https://frazkk.github.io/Super-Stock-Scanner/

## The four methods

| Method | Looks for | Needs earnings? | Sell rule used in the track record |
|---|---|---|---|
| **Stine superstocks** | Cheap, small, profitable stock breaking out of a 12-week base on 2.5x+ weekly volume, above a rising 30-week MA, within the last 8 weeks | Yes | Weekly close below the 10-week MA |
| **Weinstein Stage 2** | Breakout above resistance on 2x+ volume after a flat "Stage 1" base, still above the 30-week MA | No | Weekly close below the 30-week MA |
| **Minervini trend + VCP** | Passes all 8 Trend Template rules (incl. RS rank 70+), then a volatility contraction and a pivot breakout | Preferred | 8% stop below entry |
| **Episodic pivots** | Gapped up 10%+ on 3x+ volume this week and still holding the gap | No | Below the gap-day low |

Each stock card shows its checklist, the entry idea, the sell rule and links to the weekly chart and insider filings.

## What's on the page

- **USA / Canada / UK tabs**: a market-trend banner (is the index above a rising 30-week MA?), then each method's picks. A **NEW** badge marks stocks that method hasn't flagged in the last 8 weeks. "Watching" lists strong Stine candidates that haven't broken out yet.
- **Resources tab**: every pick in mining and oil & gas across all three markets.
- **Track record tab**: each week the top 10 new picks per method and market are recorded at the Friday close. Their returns are then measured after 4, 12 and 26 weeks against the market's index (Russell 2000 for the US, TSX SmallCap for Canada, FTSE AIM All-Share for the UK, with fallbacks). Results are shown both as buy and hold and with each method's sell rule. Minervini "trend only" names are shown but not recorded, because Minervini only buys at a VCP or pivot.

Give the track record at least 3 months before drawing conclusions. A handful of picks proves nothing.

## Tuning

All thresholds are in the **SETTINGS** block at the top of `screener.py`.

## Running and maintenance

- It runs every Saturday at about 08:17 UK time. To run it now, go to **Actions**, then **Weekly superstock scan**, then **Run workflow**.
- The track record lives in `docs/data/picks.json`. Don't delete it, or the scorecard starts again.
- If a market shows "Scan failed", a free data source changed or blocked the request. Open the failed run in **Actions** and send the error to Claude.

## Optional weekly email

Add these repository secrets (**Settings**, **Secrets and variables**, **Actions**): `SMTP_USER` (your Gmail address), `SMTP_PASS` (a Gmail app password from myaccount.google.com/apppasswords), `EMAIL_TO`, and `SITE_URL` (your page link).

## Known limits

- **Free data is unofficial.** It can have gaps, especially fundamentals for AIM stocks. Missing data scores 0 rather than excluding a stock.
- **Insider buying is automatic for the US only.** For Canada and the UK, use the filings link on each card.
- **Track-record returns** use closing prices with no costs or spread. Small caps often have wide spreads, so real returns would be lower.
- **Not financial advice.** It's a shortlist for your own research.

## Test without live data

`python screener.py --demo` simulates 30 weeks of made-up stocks into `demo_docs/`.
