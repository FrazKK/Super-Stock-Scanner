"""Collect US company news releases into one file for the morning "strong risers" check.

Runs on GitHub Actions every ~10 minutes. Each run reads the previous news.json, pulls the latest items from
the newswires and from SEC EDGAR (8-K / 6-K filings, which every US-listed company files for material news),
keeps only items that name a Trading 212 US ticker, drops routine noise, and writes the merged file back.

Polling often is what gives full coverage: short RSS feeds never get the chance to roll over between runs, and
the PR Newswire list pages are paged back until they reach items already collected.

Usage: python news_collect.py --prev prev.json --out out/news.json
Env:   SEC_CONTACT  "Your Name your@email"  (SEC asks automated clients to identify themselves; EDGAR is skipped without it)
"""

import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests

NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
KEEP_DAYS = 4
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0 Safari/537.36 superstock-scanner"}

RSS_FEEDS = {
    "prnewswire-rss": "https://www.prnewswire.com/rss/news-releases-list.rss",
    "globenewswire-public": "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
    "globenewswire-health": "https://www.globenewswire.com/RssFeed/industry/4000-Health%20Care/feedTitle/GlobeNewswire%20-%20Industry%20News%20on%20Health%20Care",
    "globenewswire-energy": "https://www.globenewswire.com/RssFeed/industry/1-Energy/feedTitle/GlobeNewswire%20-%20Industry%20News%20on%20Energy",
    "globenewswire-materials": "https://www.globenewswire.com/RssFeed/industry/1000-Basic%20Materials/feedTitle/GlobeNewswire%20-%20Industry%20News%20on%20Basic%20Materials",
    "globenewswire-tech": "https://www.globenewswire.com/RssFeed/industry/9000-Technology/feedTitle/GlobeNewswire%20-%20Industry%20News%20on%20Technology",
    "globenewswire-industrials": "https://www.globenewswire.com/RssFeed/industry/2000-Industrials/feedTitle/GlobeNewswire%20-%20Industry%20News%20on%20Industrials",
    "businesswire": "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRWA==",
    "accessnewswire": "https://www.accessnewswire.com/newsroom/rss",
}
PRN_LIST = "https://www.prnewswire.com/news-releases/news-releases-list/?page={page}&pagesize=100"
# Wires that block GitHub's servers (GlobeNewswire 403, ACCESS 404, Newsfile bot check) are read through Google
# News instead: a "site:" search for the last day returns up to ~100 of each wire's releases. Several searches per
# wire widen the net; results are de-duplicated by headline.
GOOGLE_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
GOOGLE_WIRES = {
    "globenewswire": ("globenewswire.com", ["", "Nasdaq", "NYSE", "announces", "results", "FDA OR trial",
                                            "contract OR order OR award", "drilling OR resource OR production"]),
    "accessnewswire": ("accessnewswire.com", ["", "Nasdaq OR NYSE OR OTC", "announces"]),
    "newsfile": ("newsfilecorp.com", ["", "Nasdaq OR NYSE", "announces", "drilling OR assay OR resource"]),
}
NEWSFILE_LIST = "https://www.newsfilecorp.com/news/business?pg={page}"
EDGAR_CURRENT = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&company=&dateb=&owner=include"
                 "&start={start}&count=100&output=atom")
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"

EXCH = r"(?:NASDAQ|Nasdaq|NYSE\s+American|NYSE\s+MKT|NYSE\s+Arca|NYSE|NYSEAMERICAN|NYSEAmerican)"
TICKER_RE = re.compile(EXCH + r"(?:\s*(?:GS|GM|CM|Global Select Market|Global Market|Capital Market))?"
                       r"\s*[:\-]\s*\$?([A-Z][A-Z0-9]{0,5}(?:[./][A-Z])?)\b")

# Routine items Fraz never wants (step 3 of the routine). Matched against the headline.
SKIP_RE = re.compile(
    r"class action|shareholder alert|investor alert|securities fraud|investigation|law firm|rosen law|pomerantz|"
    r"levi & korsinsky|bragar|faruqi|robbins llp|kessler topaz|glancy|schall law|bronstein|portnoy|"
    r"to participate in|to present at|to host|fireside chat|webcast|conference call|investor conference|"
    r"to report .*results|to announce .*results|results (?:date|on)|earnings call|announces date|"
    r"dividend|declares .*distribution|monthly distribution|inducement|stock options?|\brsus?\b|annual (?:general )?meeting|\bagm\b|form 8\.3|"
    r"appoints|names .* (?:ceo|cfo|chief|president|director)|joins (?:the )?board|"
    r"reverse (?:stock |share )?split|nasdaq (?:notice|notification)|deficiency|minimum bid", re.I)

TAKEOVER_RE = re.compile(r"to be acquired|definitive (?:merger )?agreement to be acquired|enters into .*merger|"
                         r"tender offer|go(?:ing)?[- ]private|unsolicited (?:proposal|offer)|takeover|"
                         r"receives? .*(?:proposal|offer) to acquire|agrees? to be acquired|merger agreement", re.I)

# 8-K items that carry price-moving news. Filings with only other items (5.02 people, 5.07 votes, 9.01 exhibits) are dropped.
EDGAR_ITEMS = {"1.01": "material agreement", "2.01": "acquisition/disposal", "2.02": "results",
               "3.02": "unregistered share sale", "7.01": "Reg FD release", "8.01": "other events"}

NAME_STOP = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "llc", "lp",
             "l.p", "sa", "nv", "ag", "se", "holdings", "holding", "group", "the", "class", "a", "adr", "ads", "trust"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def norm_name(s):
    words = re.sub(r"[^a-z0-9& ]+", " ", s.lower()).split()
    return " ".join(w for w in words if w not in NAME_STOP)


def iso(d):
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_any_time(s, today_ny=None):
    """RSS/Atom/listing timestamps -> aware UTC datetime, or None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d and d.tzinfo:
            return d.astimezone(UTC)
    except (TypeError, ValueError):
        pass
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo:
            return d.astimezone(UTC)
    except ValueError:
        pass
    s2 = re.sub(r"\s+", " ", s.replace(",", "")).replace(" ET", "").replace(" EST", "").replace(" EDT", "")
    for fmt in ("%b %d %Y %H:%M", "%B %d %Y %I:%M %p", "%b %d %Y %I:%M %p", "%B %d %Y %H:%M"):
        try:
            return dt.datetime.strptime(s2, fmt).replace(tzinfo=NY).astimezone(UTC)
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?: ?([AP]M))?", s2)
    if m and today_ny:
        h, mi = int(m.group(1)), int(m.group(2))
        if m.group(3) == "PM" and h < 12:
            h += 12
        if m.group(3) == "AM" and h == 12:
            h = 0
        return dt.datetime.combine(today_ny, dt.time(h, mi), NY).astimezone(UTC)
    return None


class Resolver:
    """Finds the US tickers an item is about and checks them against the Trading 212 list."""

    def __init__(self, t212, sec_rows):
        self.t212 = {t.upper().replace("/", ".") for t in t212}
        self.by_cik, self.by_name = {}, {}
        for r in sec_rows:
            tk = r["ticker"].upper().replace("-", ".")
            self.by_cik.setdefault(int(r["cik_str"]), tk)
            n = norm_name(r["title"])
            if n:
                self.by_name.setdefault(n, tk)

    def in_t212(self, tk):
        tk = tk.upper().replace("/", ".")
        return tk in self.t212 or tk.replace(".", "") in self.t212 or tk.replace(".", "/") in self.t212

    def from_text(self, text):
        out = []
        for tk in TICKER_RE.findall(text or ""):
            tk = tk.replace("/", ".")
            if tk not in out:
                out.append(tk)
        return out

    def from_name(self, title):
        """Match the first 1-5 words of a headline against SEC company names ("Opera Announces ..." -> OPRA)."""
        words = re.sub(r"[^A-Za-z0-9& ]+", " ", re.sub(r"['\u2019]s\b", "", title or "")).split()
        for k in range(min(5, len(words)), 0, -1):
            n = norm_name(" ".join(words[:k]))
            if n and (" " in n or len(n) >= 5) and n in self.by_name:
                return [self.by_name[n]]
        return []


def get(url, headers=None, tries=2):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers or BROWSER_UA, timeout=30)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)[:200]
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(last)


def text_of(el, tag):
    for child in el:
        if child.tag.split("}")[-1] == tag:
            return "".join(child.itertext()).strip()
    return ""


def parse_rss(xml_text, source):
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    items = []
    for it in root.iter():
        if it.tag.split("}")[-1] not in ("item", "entry"):
            continue
        title = html.unescape(text_of(it, "title"))
        link = text_of(it, "link")
        if not link:
            for child in it:
                if child.tag.split("}")[-1] == "link" and child.get("href"):
                    link = child.get("href")
        when = parse_any_time(text_of(it, "pubDate") or text_of(it, "updated") or text_of(it, "published"))
        blob = " ".join(html.unescape("".join(c.itertext())) for c in it)  # includes categories / descriptions
        blob = re.sub(r"<[^>]+>", " ", blob)
        if title and link and when:
            items.append({"source": source, "title": title, "url": link.strip(), "time": when, "text": blob[:3000]})
    return items


def parse_prn_list(page_html, today_ny):
    """PR Newswire list page: each release is a link to /news-releases/...-NNNNNNNNN.html with a time near it."""
    items, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="(/news-releases/[^"]+-\d{6,}\.html)"[^>]*>(.*?)</a>', page_html, re.S):
        href, inner = m.group(1), m.group(2)
        if href in seen:
            continue
        window = page_html[max(0, m.start() - 600): m.end() + 1500]
        t = re.search(r"((?:[A-Z][a-z]{2} \d{1,2}, \d{4}, )?\d{1,2}:\d{2} ET)", inner) or \
            re.search(r"((?:[A-Z][a-z]{2} \d{1,2}, \d{4}, )?\d{1,2}:\d{2} ET)", window)
        title = re.sub(r"<small>.*?</small>", " ", inner, flags=re.S)
        title = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", title))).strip()
        when = parse_any_time(t.group(1), today_ny) if t else None
        if not title or len(title) < 15 or not when:
            continue
        seen.add(href)
        # the summary is usually inside the link itself; only look after the link up to the NEXT release, or a
        # neighbouring release's "(NASDAQ: XYZ)" gets attached to this one
        after = page_html[m.end(): m.end() + 2500]
        nxt = re.search(r'href="/news-releases/[^"]+-\d{6,}\.html"', after)
        after = after[: nxt.start()] if nxt else after
        teaser = re.search(r"<p[^>]*>(.*?)</p>", inner, re.S) or re.search(r"<p[^>]*>(.*?)</p>", after, re.S)
        text = html.unescape(re.sub(r"<[^>]+>", " ", teaser.group(1))) if teaser else ""
        items.append({"source": "prnewswire", "title": title, "url": "https://www.prnewswire.com" + href,
                      "time": when, "text": text[:1500]})
    return items


def parse_newsfile_list(page_html):
    items, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="((?:https://www\.newsfilecorp\.com)?/release/\d+/[^"]+)"[^>]*>(.*?)</a>',
                         page_html, re.S):
        href = m.group(1)
        if href in seen:
            continue
        title = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2)))).strip()
        if len(title) < 15:
            continue
        window = html.unescape(re.sub(r"<[^>]+>", " ", page_html[max(0, m.start() - 1500): m.end() + 2500]))
        t = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4},? \d{1,2}:\d{2} [AP]M)", window)
        when = parse_any_time(t.group(1)) if t else None
        if not when:
            continue
        seen.add(href)
        url = href if href.startswith("http") else "https://www.newsfilecorp.com" + href
        items.append({"source": "newsfile", "title": title, "url": url, "time": when, "text": window[:2000]})
    return items


def parse_edgar(xml_text, resolver):
    items = []
    for it in parse_rss(xml_text, "sec-edgar"):
        m = re.match(r"(8-K|6-K)(?:/A)?\s+-\s+(.*?)\s+\((\d{10})\)", it["title"])
        if not m:
            continue
        form, company, cik = m.group(1), m.group(2), int(m.group(3))
        found = re.findall(r"Item (\d\.\d\d)", it["text"])
        keep = [i for i in found if i in EDGAR_ITEMS]
        if form == "8-K" and not keep:
            continue
        tk = resolver.by_cik.get(cik)
        if not tk:
            continue
        label = ", ".join(f"{i} {EDGAR_ITEMS[i]}" for i in keep) or "6-K report"
        items.append({"source": "sec-edgar", "title": f"{form} {company}: {label}", "url": it["url"],
                      "time": it["time"], "text": "", "tickers": [tk], "form": form, "items": keep})
    return items


def classify(item, resolver):
    if "tickers" not in item:
        item["tickers"] = resolver.from_text(item["title"] + " " + item.get("text", "")) or resolver.from_name(item["title"])
    item["t212"] = [t for t in item["tickers"] if resolver.in_t212(t)]
    flags = []
    if SKIP_RE.search(item["title"]):
        flags.append("routine")
    if TAKEOVER_RE.search(item["title"]):
        flags.append("takeover")
    if re.search(r"offering|private placement|registered direct|at-the-market|warrant", item["title"], re.I):
        flags.append("financing")
    item["flags"] = flags
    return item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prev", default="prev.json")
    ap.add_argument("--out", default="out/news.json")
    ap.add_argument("--t212", default="docs/data/t212_tickers.json")
    ap.add_argument("--max-prn-pages", type=int, default=40)
    args = ap.parse_args()

    now = dt.datetime.now(UTC)
    today_ny = now.astimezone(NY).date()
    cutoff = now - dt.timedelta(days=KEEP_DAYS)

    prev = {"items": [], "sources": {}}
    if os.path.exists(args.prev):
        try:
            prev = json.load(open(args.prev))
        except ValueError:
            log("previous news.json unreadable; starting fresh")
    known = {i["url"] for i in prev.get("items", [])}
    last_prn = max((i["time"] for i in prev.get("items", []) if i["source"] == "prnewswire"), default=None)
    last_prn = dt.datetime.fromisoformat(last_prn.replace("Z", "+00:00")) if last_prn else now - dt.timedelta(hours=40)

    t212 = json.load(open(args.t212)).get("us", [])
    contact = os.environ.get("SEC_CONTACT", "").strip()
    sec_rows = []
    if contact:
        try:
            sec_rows = list(json.loads(get(SEC_TICKERS, {"User-Agent": contact})).values())
        except Exception as e:  # noqa: BLE001
            log("SEC ticker map failed:", e)
    resolver = Resolver(t212, sec_rows)

    health, raw = {}, []

    def note(name, got, err=None):
        times = [i["time"] for i in got]
        health[name] = {"ok": err is None, "error": err, "fetched": len(got),
                        "newest": iso(max(times)) if times else None, "oldest": iso(min(times)) if times else None,
                        "checked": iso(now)}

    for name, url in RSS_FEEDS.items():
        try:
            got = parse_rss(get(url), name.split("-")[0])
            note(name, got)
            raw += got
        except Exception as e:  # noqa: BLE001
            note(name, [], str(e)[:200])

    # Blocked wires via Google News (see GOOGLE_WIRES)
    from urllib.parse import quote
    seen_titles = {re.sub(r"\W+", " ", i["title"]).strip().lower() for i in prev.get("items", [])}
    seen_titles |= {re.sub(r"\W+", " ", i["title"]).strip().lower() for i in raw}
    for wire, (site, extras) in GOOGLE_WIRES.items():
        got_g, err = [], None
        for extra in extras:
            q = quote(f"site:{site} {extra} when:1d".replace("  ", " "))
            try:
                items = parse_rss(get(GOOGLE_RSS.format(q=q)), wire)
            except Exception as e:  # noqa: BLE001
                err = f"{extra or 'all'}: {e}"[:200]
                continue
            for it in items:
                # Google appends " - GlobeNewswire" etc. to the headline
                it["title"] = re.sub(r"\s+-\s+[^-]{2,40}$", "", it["title"]).strip()
                key = re.sub(r"\W+", " ", it["title"]).strip().lower()
                if key and key not in seen_titles:
                    seen_titles.add(key)
                    it["via"] = "google-news"
                    got_g.append(it)
            time.sleep(1)
        note(f"google-{wire}", got_g, err if not got_g else None)
        raw += got_g

    # PR Newswire: page back until we overlap what we already have (or 40 h back on the first run).
    got_prn, err = [], None
    for page in range(1, args.max_prn_pages + 1):
        try:
            got = parse_prn_list(get(PRN_LIST.format(page=page)), today_ny)
        except Exception as e:  # noqa: BLE001
            err = f"page {page}: {e}"[:200]
            break
        if not got:
            err = f"page {page}: no items parsed (layout change?)"
            break
        got_prn += got
        if min(i["time"] for i in got) < last_prn - dt.timedelta(minutes=30):
            break
        time.sleep(1)
    note("prnewswire-list", got_prn, err)
    raw += got_prn

    got_nf, err = [], None
    for page in range(1, 9):
        try:
            got = parse_newsfile_list(get(NEWSFILE_LIST.format(page=page)))
        except Exception as e:  # noqa: BLE001
            err = f"page {page}: {e}"[:200]
            break
        if not got:
            err = None if page > 1 else "no items parsed (layout change?)"
            break
        got_nf += got
        if all(i["url"] in known for i in got) or min(i["time"] for i in got) < now - dt.timedelta(hours=40):
            break
        time.sleep(1)
    note("newsfile", got_nf, err)
    raw += got_nf

    if contact:
        for form in ("8-K", "6-K"):
            got_ed, err = [], None
            for start in (0, 100, 200, 300):
                try:
                    xml_text = get(EDGAR_CURRENT.format(form=form, start=start), {"User-Agent": contact})
                    got = parse_edgar(xml_text, resolver)
                except Exception as e:  # noqa: BLE001
                    err = str(e)[:200]
                    break
                got_ed += got
                entries = parse_rss(xml_text, "x")
                if not entries or all(i["url"] in known for i in got) or \
                        min(e["time"] for e in entries) < now - dt.timedelta(hours=40):
                    break
                time.sleep(0.5)
            note(f"sec-edgar-{form}", got_ed, err)
            raw += got_ed
    else:
        health["sec-edgar"] = {"ok": False, "error": "SEC_CONTACT secret not set", "checked": iso(now)}

    # Merge with previous items, keep only Trading 212 names, drop old ones.
    merged = {i["url"]: i for i in prev.get("items", [])}
    added = 0
    for it in raw:
        if it["url"] in merged or it["time"] < cutoff:
            continue
        classify(it, resolver)
        if not it["t212"]:
            continue
        it["time"] = iso(it["time"])
        it["first_seen"] = iso(now)
        it.pop("text", None)
        merged[it["url"]] = it
        added += 1
    items = sorted((i for i in merged.values() if i["time"] >= iso(cutoff)), key=lambda i: i["time"], reverse=True)

    sources = dict(prev.get("sources", {}))
    sources.update(health)
    out = {"updated": iso(now), "keep_days": KEEP_DAYS, "count": len(items), "sources": sources,
           "note": "Only items naming a Trading 212 US ticker. flags: routine / takeover / financing. "
                   "time = release time (UTC); first_seen = when the collector picked it up.",
           "items": items}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1)
    log(f"added {added}, total {len(items)}")
    for k, v in health.items():
        log(f"  {k}: {'ok' if v.get('ok') else 'FAIL'} fetched={v.get('fetched')} err={v.get('error')}")


if __name__ == "__main__":
    main()
