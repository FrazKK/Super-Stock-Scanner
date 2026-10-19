# Superstock Scanner v2

A free weekly scan of US, Canadian and UK stocks. It runs four breakout methods side by side and keeps a track record, so you can see which method actually works before risking money on it. Every Saturday morning GitHub runs the scan and updates your page: https://frazkk.github.io/Super-Stock-Scanner/

## The four methods (each author's own rules)

Only stocks at a **buy point right now** are listed, up to **10 per method per market**. Nothing to watch, nothing already gone.

| Method | Must pass | Listed only when (buy point) | Sell rule |
|---|---|---|---|
| **Stine superstocks** | Price under 15, profitable, above a rising 30-week MA, broke out of a 12-week base in the last 8 weeks on 3x+ weekly volume (biggest in 6 months), low float (under 50M shares, or a small company if float is unknown) | Pulled back to the 10-week MA ("magic line") | Weekly close below the 10-week MA |
| **Weinstein Stage 2** | Breakout above resistance on 2x+ volume from a flat Stage 1 base, above a flat or rising 30-week MA, beating the market (Mansfield RS > 0) | Breakout in the last 2 weeks and within 10% of the breakout level, or a pullback to the breakout level | Weekly close below the 30-week MA |
| **Minervini trend + VCP** | All 8 Trend Template rules, including RS rank 70+ | Breaking out of the pivot on volume, no more than 5% above it | 8% stop below entry |
| **Episodic pivots** | Gapped up 10%+ on 3x+ volume, still holding the gap | Gap in the last 3 trading days, not run on more than 10% since | Below the gap-day low |

**Picked by 2+ methods** appears at the top of each market tab. It lists stocks that more than one method has at a buy point, with each method's plan. A green **×2** or **×3** badge on a card shows the same thing. These overlap picks are tracked as their own line in the track record, using the 30-week MA sell rule.

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
