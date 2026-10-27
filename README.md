# Superstock Scanner v2

A free weekly scan of US, Canadian and UK stocks. It runs four breakout methods side by side and keeps a track record, so you can see which method actually works before risking money on it. Every Saturday morning GitHub runs the scan and updates your page: https://frazkk.github.io/Super-Stock-Scanner/

## The ten methods (each author's own rules)

Only stocks at a **buy point right now** are listed, up to **10 per method per market**. Nothing to watch, nothing already gone.

| Method | Must pass | Listed only when (buy point) | Sell rule |
|---|---|---|---|
| **Stine superstocks** | Price under 15, profitable, above a rising 30-week MA, broke out of a 12-week base in the last 8 weeks on 3x+ weekly volume (biggest in 6 months), low float (under 50M shares, or a small company if float is unknown) | Pulled back to the 10-week MA ("magic line") | Weekly close below the 10-week MA |
| **Weinstein Stage 2** | Breakout above resistance on 2x+ volume from a flat Stage 1 base, above a flat or rising 30-week MA, beating the market (Mansfield RS > 0) | Breakout in the last 2 weeks and within 10% of the breakout level, or a pullback to the breakout level | Weekly close below the 30-week MA |
| **Minervini trend + VCP** | All 8 Trend Template rules, including RS rank 70+ | Breaking out of the pivot on volume, no more than 5% above it | 8% stop below entry |
| **Episodic pivots** | Gapped up 10%+ on 3x+ volume, still holding the gap | Gap in the last 3 trading days, not run on more than 10% since | Below the gap-day low |
| **Simple Stage 2** | Above a rising 30-week MA, 10-week MA above the 30-week, beating the market (Mansfield RS > 0). No volume or base rules | Entered Stage 2 in the last 8 weeks and no more than 15% above the 30-week MA | Weekly close below the 30-week MA |
| **Luk pullback** (Martin Luk, 2025 US Investing Champion, record +969%) | Up 30%+ in 1 or 3 months, average daily range 5%+, EMAs stacked 9 > 21 > 50, above the anchored VWAP from its 1-year high, market's 9-day EMA above its 21-day | Dipped to the 9-day EMA today or yesterday and closed back above it, in the upper part of the day's range | Stop at the bounce-day low (max 5%); daily close below the 9-day EMA |
| **Kell wedge pop / EMA crossback** (Oliver Kell, 2020 US Investing Champion, +941%) | RS rank 80+. Wedge pop: drifted below the 10-day EMA with lower highs, then popped above recent highs and both EMAs on 1.3x volume. Crossback: uptrend (10 EMA > 20 EMA > rising 50-day) that ran 8%+ above the 20-day EMA, dipped to it and bounced | Either pattern today or yesterday, still above the 10-day EMA | Stop under the pattern low; daily close below the 20-day EMA |
| **Slater Zulu** (Jim Slater; Stockopedia's version has returned about 23% a year since 2012) | Forecast EPS growth over 15%, PEG under 0.75, forward P/E under 20, return on capital over 12%, beating the market over a year, market cap 20m to 1bn | Price above its 50-day MA | None (Slater reviewed on forecast cuts); tracked as buy and hold |
| **Piotroski F-Score** (academic study: high scorers beat low scorers by 23 points a year, best in small caps) | F-Score 8 or 9, and in the cheapest 20% by price-to-book in its market | Price above its 50-day MA | None (annual rebalance); tracked as buy and hold |
| **Greenblatt Magic Formula** | Top 30 in its market when ranked on earnings yield (EBIT/EV) plus return on capital, excluding financials and utilities, market cap 50m+ | Price above its 50-day MA | None (hold a year); tracked as buy and hold |

The 50-day MA check on the three value methods is our addition, so you aren't shown cheap stocks that are still falling. Their sell rules aren't the authors': none of the three used stops.


**Picked by 2+ methods** appears at the top of each market tab. It lists stocks that more than one method has at a buy point, with each method's plan. Simple Stage 2 doesn't count towards this, because it overlaps Weinstein by design. A green **×2** or **×3** badge on a card shows the same thing. These overlap picks are tracked as their own line in the track record, using the 30-week MA sell rule.

**Market downtrends:** Weinstein and Minervini both advise against new buys when the market is in a downtrend. By default the page still lists their picks under a red banner. Set `MARKET_FILTER = True` in SETTINGS to hide them instead.

A minimum daily trading value (US $300k, Canada C$75k, UK £30k) keeps out stocks too thin to trade. That's data hygiene rather than a method rule. All thresholds are in SETTINGS.

## Refresh from the page

The **Run a new scan** and **Check my stocks** buttons at the top of the page start the scans without going to GitHub. A new scan takes about 15–20 minutes, and the page reloads itself when it's done. You can leave the page meanwhile.

The first time, you'll need to give it a GitHub key. Follow **Set up the refresh buttons** on the page: create a fine-grained token limited to this repo with **Actions: Read and write** only, then paste it in. It's stored only in that phone's browser. When the key expires, the page will ask for a new one.

Only weekend scans (Friday closes) go into the track record. A weekday refresh updates the page but doesn't add picks.

## US politicians' buys

The **Politicians** tab and a phone alert show purchases reported by a watch-list of House members. The list is the members with the best estimated returns in Unusual Whales' 2024 and 2025 Congress-trading reports: Pelosi, Rouzer, Crenshaw, Davidson, Norcross, Sewell, Steil, LaLota, Guest, McClintock and Evans. You can edit `POLITICIANS` in SETTINGS.

- The data comes from the House Clerk's free official disclosure files and is checked every weekday evening and at the weekend scan.
- You get one alert per new purchase. It's marked more urgently if the same stock is also on one of this week's method lists. A 🏛 badge appears on any US card a watched member has bought in the last 90 days.
- **Late signals:** members can report up to 45 days after trading.
- **No senators:** the Senate disclosure site blocks automated access from servers.
- **Scanned forms:** a few members file on paper. Those can't be read automatically, so you get a "filed a trade report" alert with a link instead.
- **No backlog of alerts:** the first run quietly records what's already been filed. Alerts start with the next new filing.

## UK director buying

For UK stocks that reach a buy point under the rules above, the scan reads their recent RNS "Director/PDMR Shareholding" announcements (via Investegate). It counts only genuine purchases, not awards, options, share-scheme or dividend-reinvestment shares, or sales. Director buys in the last 90 days are shown on the card with name, role and value, and earn Stine's insider-buying points. You get a phone alert when a ready UK pick has director buying.

## Opening surge alert

About 30 minutes after each market opens (08:30 UK; 10:00 New York for US and Canada), the bot looks for stocks that:

- have **already traded more than a normal full day's volume** (their 10-day average). The free data is about 15 minutes behind, so this covers the first 15 minutes of trading;
- are **trending up**: price above the 20-day MA, 20-day above the 50-day, and 50-day above the 200-day, and up today;
- have **positive-looking news in the last 3 days** (RNS for UK stocks, Yahoo Finance news for all three markets). Headlines are sorted by keywords, so read them yourself.

Also checked at the same time:

- **Placings, fundraises, warnings, delays and similar** rule a stock out. Volume from those is usually bad news.
- **Holding gains**: price above the open and in the top half of today's range, so not fading.
- **Up 20%+ already**: flagged as chasing risk.
- **Near a 52-week high**: flagged as a breakout.
- **Minimum traded value so far** (UK £25k, US $250k, Canada C$50k), so one trade in a thin stock can't trigger it.
- **Cross-checks**: whether the stock is on one of this week's method lists, and whether you own it.

You get one phone alert per market and the results appear on an **Opening movers** tab. Stocks with the volume and trend but no clear good news are listed underneath for reference.

**On-time alerts (recommended, free):** GitHub's own timer is often late, so let cron-job.org start the check at the exact time.

1. Create a free account at https://cron-job.org.
2. Create a GitHub fine-grained token for this repo with **Actions: Read and write** (the same kind you made for the page buttons; you can make a second one).
3. In cron-job.org, create a cron job:
   - **URL:** `https://api.github.com/repos/FrazKK/Super-Stock-Scanner/actions/workflows/opening.yml/dispatches`
   - **Schedule:** Monday to Friday at 08:30, time zone Europe/London
   - **Advanced:** request method **POST**, request body `{"ref":"main"}`, and headers `Authorization: Bearer YOUR_TOKEN`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`
4. Clone the job and change the schedule to 10:00, time zone America/New_York, for the US and Canada.

The workflow still runs on GitHub's own timer as a backup. It only acts once per market per day, so you won't get duplicates. To test now: Actions, Opening surge check, Run workflow, and enter `uk` (or `america`).

## Pre-close check (hold or sell?)

About an hour before each market closes (UK 16:30, US and Canada 16:00 New York time), the bot checks the stocks you've marked as bought in that market. It uses today's live price (delayed about 15 minutes) and sends one phone alert per market:

- **Sell before the close**: if the stock closed here, its method would say sell. For example, a Luk stock below its 9-day EMA, or a Weinstein stock below its 30-week MA on a Friday.
- **Hold for now, watch closely** / **Consider selling**: it's below or near the sell level, but the rule isn't broken yet. Weekly-rule methods only sell on Friday's close.
- **Hold**: nothing to do.

The advice also appears on each card in the **My stocks** tab. It runs from `.github/workflows/preclose.yml` and `preclose_due.py`. GitHub can delay scheduled runs, so the workflow tries every 15 minutes through that hour and acts once per market per day. To run it immediately: Actions, Pre-close check, Run workflow.

## Evaluate a stock

The **Evaluate** tab lets you check any stock yourself. Type its ticker, choose UK, USA or Canada, and tap **Evaluate**. About 2–3 minutes later the page shows the stock against all ten methods. For each method it gives a verdict (**Buy point now**, **Qualifies, not at a buy point** or **Doesn't qualify**), and you can tap it to see every check, which ones failed, and that method's entry and sell plan. UK stocks also show recent director buying.

- Stocks stay on the tab and are re-checked at every weekend scan. Tap **Remove** to take one off.
- The button uses the same GitHub key as the refresh buttons. If you haven't set that up, the page asks you to.
- This runs through the **Evaluate stocks** workflow (`.github/workflows/evaluate.yml`). You can also run it from GitHub: Actions, Evaluate stocks, Run workflow, then type e.g. `KOO:uk, TSLA:us`.

## What's on the page

- **My stocks tab**: your holdings and their sell-rule status (see below).
- **USA / Canada / UK tabs**: the market-trend banner, picks by 2+ methods, then each method's ready-to-buy picks. A **NEW** badge marks stocks that weren't on that method's list last time.
- **Resources tab**: every ready pick in mining and oil & gas across all three markets.
- **Track record tab**: every weekend scan, the ready picks are recorded at the Friday close. Their returns are then measured after 4, 12 and 26 weeks against the market's index (Russell 2000 for the US, TSX SmallCap for Canada, FTSE AIM All-Share for the UK, with fallbacks). Results are shown both as buy and hold and with each method's sell rule. Only picks made under the current rules (since 18 Oct) are counted.

Give the track record at least 3 months before drawing conclusions. A handful of picks proves nothing.

## Tuning

All thresholds are in the **SETTINGS** block at the top of `screener.py`.

## Running and maintenance

- It runs every Saturday at about 08:17 UK time. To run it now, go to **Actions**, then **Weekly superstock scan**, then **Run workflow**.
- The track record lives in `docs/data/picks.json`. Don't delete it, or the scorecard starts again.
- If a market shows "Scan failed", a free data source changed or blocked the request. Open the failed run in **Actions** and send the error to Claude.

## My stocks and sell alerts

Tell the bot what you've bought, and it checks each holding against its method's sell rule every weekday evening (about 22:40 UK time) and every Saturday. It sends a push alert to your phone when anything changes.

**Adding a holding:** tap **I bought this** on a stock's card. GitHub opens with a small file already filled in. Change `price` to what you actually paid (UK prices in pence), then tap **Commit changes**. To add a stock the scanner didn't find, use **Add a stock by hand** at the bottom of the My stocks tab.

**When you sell:** go to the My stocks tab, tap **I sold it**, then **Commit changes**.

**What it checks:**

| Method | Sell signal | Early warning |
|---|---|---|
| Stine | Weekly close below the 10-week MA | Trading below it midweek, or within 3% |
| Weinstein, and "other" | Weekly close below the 30-week MA | Trading below it midweek, or within 5% |
| Minervini | Price touches 8% below what you paid | Within 3% of that |
| Episodic | Price touches the gap-day low | Within 3% of that |

It also alerts you if the market of a stock you hold turns down (its index drops below the 30-week MA). You get one alert when a status changes, not a daily repeat. On Saturdays you get a summary of everything you hold.

**Setting up phone alerts (free, about 3 minutes):**

1. Install the **ntfy** app from the Play Store or App Store.
2. In the app, tap **+** and subscribe to the private channel name you were given. Treat it like a password.
3. In this repo go to **Settings**, then **Secrets and variables**, then **Actions**, then **New repository secret**. Name it `NTFY_TOPIC` and paste the same channel name as the value.
4. Test it: go to **Actions**, then **Daily holdings check**, then **Run workflow**. An alert should arrive within a few minutes.

**Notes:**

- This repo is public, so anyone with its link could see which stocks you hold and the prices you paid. No quantities or amounts are stored.
- GitHub's scheduled runs can be delayed, so treat these alerts as a prompt to check. For a hard stop, set a stop order with your broker too.

## Optional weekly email

Add these repository secrets (**Settings**, **Secrets and variables**, **Actions**): `SMTP_USER` (your Gmail address), `SMTP_PASS` (a Gmail app password from myaccount.google.com/apppasswords), `EMAIL_TO`, and `SITE_URL` (your page link).

## Known limits

- **Free data is unofficial.** It can have gaps, especially fundamentals for AIM stocks. Missing data scores 0 rather than excluding a stock.
- **Insider buying is automatic for the US only.** For Canada and the UK, use the filings link on each card.
- **Track-record returns** use closing prices with no costs or spread. Small caps often have wide spreads, so real returns would be lower.
- **Not financial advice.** It's a shortlist for your own research.

## Test without live data

`python screener.py --demo` simulates 30 weeks of made-up stocks into `demo_docs/`.
