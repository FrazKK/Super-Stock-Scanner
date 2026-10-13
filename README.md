# Superstock Scanner

A free weekly screen of US, Canadian and UK stocks based on Jesse Stine's "Insider Buy Superstocks" checklist. Every Saturday morning GitHub runs the scan for you and updates a web page you can open on your phone. It can also email you a summary.

**Cost: £0.** It uses a free GitHub account, TradingView's public screener, Yahoo Finance and OpenInsider.

## What it checks

| Check | Points |
|---|---|
| Price above a rising 30-week MA, 10-week MA above the 30-week | 2 |
| Steep 30-week MA (up 3%+ in 4 weeks) | 1 |
| Breakout from a 12-week base on 2.5x+ normal weekly volume (last 3 weeks) | 2 |
| Biggest weekly volume in 6 months | 1 |
| Tight base (under 40% high-to-low range) | 1 |
| EPS growth over 25% | 1 |
| P/E under 20 | 1 |
| Float under 30M shares | 1 |
| Insider buying in the last 90 days (US only, automatic) | 2 |

Each stock is also labelled by where it sits relative to the 10-week MA (Stine's "magic line"): **Near magic line** (low-risk entry zone), **Extended** (wait for a pullback), and so on.

Before scoring, stocks must pass these filters: price US $2–15, Canada C$0.50–15, UK 5p–1500p; market cap roughly 10m–1bn; decent volume; and above their 50-day and 150-day averages. Loss-making companies are excluded when earnings data exists.

All the thresholds are in the **SETTINGS** block at the top of `screener.py`.

## One-time setup (about 10 minutes, all in a web browser)

1. **Create a GitHub account** at github.com if you don't have one.
2. **Create a new repository**: click **+** then **New repository**. Name it `superstock-scanner` and set it to **Public**. (Free GitHub Pages needs a public repo. The page only shows screen results, nothing personal.) Click **Create repository**.
3. **Upload the files**: on the new repo page click **uploading an existing file**, then drag in everything from the unzipped folder, **including the `.github` folder**. Click **Commit changes**.
   - If your computer hides the `.github` folder, create the workflow by hand instead: **Add file**, then **Create new file**, name it `.github/workflows/weekly-scan.yml`, and paste the contents in.
4. **Let the scan save its results**: go to **Settings**, **Actions**, **General**. Under *Workflow permissions* choose **Read and write permissions**, then **Save**.
5. **Run it once now**: go to the **Actions** tab, choose **Weekly superstock scan**, then **Run workflow**. It takes about 5–15 minutes.
6. **Turn on the web page**: go to **Settings**, **Pages**. Under *Source* pick **Deploy from a branch**, set the branch to `main` and the folder to `/docs`, then **Save**. After a minute your page is at
   `https://YOUR-USERNAME.github.io/superstock-scanner/`
7. **Put it on your phone's home screen**: open that link on your phone. In Chrome on Android, tap the menu then **Add to Home screen**. It opens like an app.

## Optional: weekly email

Gmail needs an *app password* rather than your normal one.

1. Turn on 2-Step Verification for your Google account, then create an app password at myaccount.google.com/apppasswords.
2. In the repo go to **Settings**, **Secrets and variables**, **Actions**, **New repository secret**, and add:
   - `SMTP_USER`: your Gmail address
   - `SMTP_PASS`: the 16-character app password
   - `EMAIL_TO`: where to send it (can be the same address)
   - `SITE_URL`: your Pages link from step 6, so the email links to it

Secrets are encrypted and stay hidden even though the repo is public.

## Day-to-day

- It runs by itself every Saturday at about 08:17 UK time. Press **Run workflow** for an extra run.
- Past weeks are kept under **past weeks** on the page, so you can see how picks played out. That's how you'll prove (or disprove) the method.
- If a week shows "Scan failed", one of the free sources changed or blocked the request. Open the failed run in the **Actions** tab to see the error. Claude Code can usually fix it in a few minutes.

## Known limits

- **Free data is unofficial.** TradingView and Yahoo can change without notice, and small-cap fundamentals (especially AIM) are often missing. Missing data shows as "no data" and scores 0 rather than excluding the stock.
- **Insider buys are automatic for the US only.** For Canada (SEDI) and the UK (RNS director dealings) each stock has a link to check by hand.
- Market cap limits use each market's own currency, so they're approximate.
- **Not financial advice.** It's a shortlist for your own research.

## Preview without live data

`python screener.py --demo` builds the page with made-up stocks so you can see the layout.
