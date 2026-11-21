#!/usr/bin/env python3
"""SDC Briefly: collects watch-industry news from the sources in sources.txt, ranks it and
publishes the public page.

Standard-library Python plus curl. Commands:
    python3 briefly.py daily     fetch, rank, publish the site and write source-health.md
    python3 briefly.py collect   fetch and rank only
    python3 briefly.py publish   rebuild the site from the database
    python3 briefly.py health    rewrite source-health.md
"""
import datetime as dt
import email.utils
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
IN_CLOUD = bool(os.environ.get("GITHUB_ACTIONS"))
DB = os.path.join(HERE, "briefly.db")
SOURCES = os.path.join(HERE, "sources.txt")
CONFIG = os.path.join(HERE, "config.txt")
LOG = os.path.join(HERE, "last-run.txt")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

WATCH_WORDS = re.compile(
    r"\b(watch(es|maker|makers|making)?|wristwatch\w*|horlog\w*|montres?|uhren\w*|uhrmacher\w*|"
    r"orolog\w*|chronograph\w*|tourbillon|rolex|patek|audemars|richemont|swatch|omega|cartier|"
    r"breitling|tudor|hublot|tag heuer|breguet|vacheron|jaeger|lange|journe|bucherer|"
    r"chrono24|watchfinder|baselworld|gphg)\b", re.I)

# Search results must be about watches as objects, not the verb "to watch".
STRONG = re.compile(
    r"\b(watches|wristwatch\w*|watchmak\w*|watch (brand|maker|industry|dealer|collector|auction|market|retailer)s?|"
    r"timepieces?|horlog\w*|montres?|uhren\w*|uhrmacher\w*|orolog\w*|rolex|patek|audemars|richemont|"
    r"swatch group|omega|cartier|breitling|tudor|hublot|tag heuer|breguet|vacheron|jaeger|lange|journe|"
    r"bucherer|chrono24|watchfinder|gphg|richard mille)\b", re.I)
JUNK = re.compile(r"^\W*watch\s*[:|–—-]|apple watch|smart ?watch|galaxy watch|pixel watch|watch (live|the video|party|now)|"
                  r"first watch|night.?s watch|neighbou?rhood watch|storm watch|watch list|to watch\b", re.I)

# Words that push an item up or down when no AI key is set.
UP = [(r"lawsuit|sues?|sued|court|tribunal|ruling|trademark|counterfeit|seiz|fraud|"
       r"proc[eè]s|justice|contrefa|plainte|gericht|klage", 3, "law"),
      (r"acqui|takeover|stake|merger|rachat|rach[eè]te|bankrupt|faillite|insolv|layoff|job cuts|licenci|"
       r"ch[oô]mage|kurzarbeit|restructur|ceo|appoint|nomm[eé]|steps down|resign|d[eé]mission", 3, "business"),
      (r"exports?|exportations|tariff|douane|results|revenue|sales|chiffre d'affaires|umsatz|profit|"
       r"shares|ipo|invest|funding|price (rise|increase|hike)|hausse des prix", 2, "business"),
      (r"theft|robber|stolen|heist|burglar|vol de|cambriol", 1, "crime"),
      (r"auction|ench[eè]res|hammer|record|phillips|christie|sotheby", 1, "auction"),
      (r"independent|ind[eé]pendant|atelier|workshop|apprentic|[eé]cole|school|museum|mus[eé]e|"
       r"archive|history|histoire|interview|profile", 1, "feature")]
DOWN = [(r"hands[- ]on|review|introducing|first look|new release|unveils?|launches|debuts|"
         r"best .* watches|top \d+|buying guide|gift guide|deal|discount|giveaway|sponsored|"
         r"wrist check|podcast|photo report", -2)]
EMOJI = {"law": "⚖️", "business": "💰", "auction": "🔨", "feature": "📖", "swiss": "🇨🇭",
         "release": "⌚", "crime": "🚨", "other": "📰"}


# ---------------------------------------------------------------- basics
def config():
    c = {"anthropic_api_key": "", "model": "claude-haiku-5-5"}
    if os.path.exists(CONFIG):
        for line in open(CONFIG, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                c[k.strip()] = v.strip()
    if os.environ.get("ANTHROPIC_API_KEY"):
        c["anthropic_api_key"] = os.environ["ANTHROPIC_API_KEY"]
    return c


def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS items(
        id INTEGER PRIMARY KEY, url TEXT UNIQUE, title TEXT, title_en TEXT, excerpt TEXT,
        source TEXT, kind TEXT, published TEXT, found TEXT, score REAL, tag TEXT, why TEXT,
        emoji TEXT, picked INTEGER DEFAULT 0, used INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0,
        caption TEXT)""")
    cols = [r[1] for r in con.execute("PRAGMA table_info(items)")]
    for c in ("excerpt_en", "lang", "dup_of", "ranked", "reviewed", "mine", "mine_hidden"):
        if c not in cols:
            con.execute("ALTER TABLE items ADD COLUMN %s TEXT" % c)
    con.execute("CREATE TABLE IF NOT EXISTS feeds(site TEXT PRIMARY KEY, feed TEXT, status TEXT)")
    return con


def fetch(url, timeout=25):
    """Return page text, or '' on failure. curl first, urllib as backup."""
    try:
        out = subprocess.run(["curl", "-sL", "--compressed", "-m", str(timeout), "-A", UA, url],
                             capture_output=True, timeout=timeout + 5)
        if out.returncode == 0 and out.stdout:
            return out.stdout.decode("utf-8", "replace")
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    except Exception:
        return ""


def read_sources():
    out = []
    for line in open(SOURCES, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            out.append({"name": parts[0], "addr": parts[1], "kind": parts[2].lower()})
    return out


def text_of(s):
    s = re.sub(r"(?is)<(script|style|figure|figcaption)[^>]*>.*?</\1>", " ", s or "")
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def excerpt_from(raw):
    """First one or two sentences of the source's own words. Nothing is rewritten."""
    t = text_of(raw)
    t = re.sub(r"\s*(The post .{0,200}? (appeared|first appeared) .*|Continue reading.*|Read more.*|"
               r"\[(…|\.\.\.|&hellip;)\].*|This article .{0,80} appeared first.*)$", "", t, flags=re.I).strip()
    if len(t) < 40:
        return ""
    sentences = re.split(r"(?<=[.!?»”\"])\s+(?=[A-ZÀ-ÝÉ«“\"])", t)
    out = sentences[0]
    if len(out) < 110 and len(sentences) > 1 and len(out) + len(sentences[1]) < 300:
        out += " " + sentences[1]
    if len(out) > 300:
        out = out[:297].rsplit(" ", 1)[0] + "…"
    elif t.endswith(("…", "...")) and out == t:
        out = out.rstrip(". …") + "…"
    return out


def page_standfirst(url):
    """(subheading, published date) from the article page: og:description, else meta description,
    else first paragraph. Nothing is rewritten."""
    page = fetch(url, 8)
    if not page:
        return "", None
    d = re.search(r'(?:article:published_time|datePublished)["\']?[^>]{0,40}?(\d{4}-\d\d-\d\d[T\d:+.Z-]*)', page)
    when = to_date(d.group(1)) if d else None
    for pat in (r'<meta[^>]+property=["\']og:description["\'][^>]+content="([^"]{40,})"',
                r"<meta[^>]+property=[\"']og:description[\"'][^>]+content='([^']{40,})'",
                r'<meta[^>]+content="([^"]{40,})"[^>]+property=["\']og:description["\']',
                r'<meta[^>]+name=["\']description["\'][^>]+content="([^"]{40,})"',
                r"<meta[^>]+name=[\"']description[\"'][^>]+content='([^']{40,})'"):
        m = re.search(pat, page, re.I)
        if m:
            return excerpt_from(m.group(1)), when
    body = re.search(r"(?is)<article.*?</article>", page)
    for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", body.group(0) if body else page):
        e = excerpt_from(p)
        if len(e) > 80:
            return e, when
    return "", when


def listing(addr):
    """Headline links from a site's own front or news page, for sites with no feed."""
    raw = fetch(addr, 12)
    host, seen, out = host_of(addr), set(), []
    for m in re.finditer(r"(?is)<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", raw or ""):
        u = urllib.parse.urljoin(addr, html.unescape(m.group(1)))
        p = urllib.parse.urlparse(u)
        t = text_of(m.group(2))
        last = [x for x in p.path.split("/") if x]
        if (host_of(u) != host or not 28 <= len(t) <= 200 or p.path in seen or not last
                or len(last[-1].split("-")) < 3):
            continue
        seen.add(p.path)
        out.append({"title": t, "link": u, "desc": "", "date": "", "publisher": ""})
    return out[:25]


# ---------------------------------------------------------------- feeds
def find_feed(addr, cached=None):
    """Return (feed address, feed text). Tries the address, then the page's own feed link, then 4 guesses."""
    if cached:
        body = fetch(cached, 12)
        if looks_like_feed(body):
            return cached, body
    body = fetch(addr, 12)
    if looks_like_feed(body):
        return addr, body
    base = addr if addr.endswith("/") else addr + "/"
    guesses = []
    for m in re.finditer(r"<link[^>]+>", body or "", re.I):
        tag = m.group(0)
        if re.search(r"application/(rss|atom)\+xml", tag, re.I) and "comments" not in tag.lower():
            h = re.search(r'href=["\']([^"\']+)["\']', tag)
            if h:
                guesses.append(urllib.parse.urljoin(base, html.unescape(h.group(1))))
    root = "{0.scheme}://{0.netloc}/".format(urllib.parse.urlparse(addr))
    guesses = guesses[:2] + [root + "feed/", root + "rss.xml", root + "feed.xml", root + "rss"]
    for g in list(dict.fromkeys(guesses))[:6]:
        body = fetch(g, 8)
        if looks_like_feed(body):
            return g, body
    return None, None


def looks_like_feed(body):
    head = (body or "")[:3000].lower()
    return "<rss" in head or "<feed" in head or "<rdf:rdf" in head


def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(body):
    body = re.sub(r"^[^<]+", "", body)
    try:
        root = ET.fromstring(body.encode("utf-8"))
    except ET.ParseError:
        try:
            root = ET.fromstring(re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-f]+);)", "&amp;",
                                        body, flags=re.I).encode("utf-8"))
        except ET.ParseError:
            return parse_broken(body)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        d = {"title": "", "link": "", "desc": "", "date": "", "publisher": ""}
        for c in el:
            n, txt = local(c.tag), (c.text or "").strip()
            if n == "title":
                d["title"] = text_of(txt)
            elif n == "link":
                href = c.attrib.get("href")
                if href and c.attrib.get("rel", "alternate") == "alternate":
                    d["link"] = href
                elif txt and not d["link"]:
                    d["link"] = txt
            elif n in ("description", "summary"):
                d["desc"] = txt or d["desc"]
            elif n in ("encoded", "content") and not d["desc"]:
                d["desc"] = txt or "".join(c.itertext())
            elif n in ("pubdate", "published", "date") or (n == "updated" and not d["date"]):
                d["date"] = txt
            elif n == "source" and txt:
                d["publisher"] = txt
        if d["title"] and d["link"]:
            items.append(d)
    return items


def parse_broken(body):
    """Salvage items from a feed that is cut off or not valid XML."""
    items = []
    for blk in re.findall(r"(?is)<(?:item|entry)\b.*?(?=<(?:item|entry)\b|</channel>|</feed>|$)", body):
        def tag(name):
            m = re.search(r"(?is)<%s\b[^>]*>(.*?)</%s>" % (name, name), blk)
            v = (m.group(1) if m else "").strip()
            return re.sub(r"(?s)^<!\[CDATA\[(.*)\]\]>$", r"\1", v).strip()
        link = tag("link")
        if not link:
            m = re.search(r'(?is)<link\b[^>]*href=["\']([^"\']+)["\']', blk)
            link = m.group(1) if m else ""
        title = text_of(tag("title"))
        if title and link:
            items.append({"title": title, "link": html.unescape(link), "desc": tag("description") or tag("summary")
                          or tag("content"), "date": tag("pubDate") or tag("published") or tag("updated"),
                          "publisher": ""})
    return items


def to_date(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def clean_url(u):
    u = html.unescape(u.strip())
    if "bing.com/news/apiclick" in u:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        u = q.get("url", [u])[0]
    p = urllib.parse.urlparse(u)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
         if not k.lower().startswith(("utm_", "fbclid", "mc_", "ref"))]
    return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q), fragment=""))


def rule_score(title, excerpt, kind, source):
    text = (title + " " + excerpt).lower()
    score = {"watch": 3.0, "general": 5.0, "search": 5.0}.get(kind, 4.0)
    tag = "other"
    for pat, pts, t in UP:
        if re.search(pat, text):
            score += pts
            if tag == "other":
                tag = t
    for pat, pts in DOWN:
        if re.search(pat, text):
            score += pts
            if tag == "other":
                tag = "release"
    if tag in ("other", "feature") and re.search(r"swissinfo|temps|arcinfo|rts|24 heures|bilan|agefi|"
                                                 r"horlog|uhren", (source + " " + text).lower()):
        tag = "swiss" if tag == "other" else tag
    return max(1.0, min(10.0, score)), tag


# ---------------------------------------------------------------- collect
STATUS = {"running": False, "text": "Idle."}


def say(msg, verbose):
    STATUS["text"] = msg
    if verbose:
        print(msg, flush=True)


BING = "https://www.bing.com/news/search?format=rss&qft=sortbydate%3d%221%22&q="


def host_of(u):
    return urllib.parse.urlparse(u).netloc.lower().replace("www.", "")


def pull(s, cached):
    """Return (source, feed address or None, feed text, how). how = feed / search / news-search."""
    try:
        if s["kind"] == "search":
            import time, random
            feed = BING + urllib.parse.quote_plus(s["addr"])
            for attempt in range(3):          # the search service turns away bursts; wait and try again
                body = fetch(feed, 12)
                if looks_like_feed(body):
                    break
                time.sleep(4 + random.random() * 6)
            return s, feed, body, "search"
        feed, body = find_feed(s["addr"], cached.get(s["addr"]))
        if feed:
            return s, feed, body, "feed"
        # No feed: read the headline links off the site's own page.
        items = listing(s["addr"])
        if len(items) >= 3:
            return s, "page", items, "page"
        # Site blocks robots altogether: ask the news search for that site only.
        host = host_of(s["addr"])
        terms = [""] if s["kind"] == "watch" else [" watches", " horlogerie", " montres", " Uhren"]
        bodies = [fetch(BING + urllib.parse.quote_plus("site:" + host + t), 12) for t in terms]
        items = []
        for b in bodies:
            if looks_like_feed(b):
                items += [i for i in parse_feed(b) if host_of(clean_url(i["link"])).endswith(host)]
        if items:
            return s, "news-search", items, "news-search"
        return s, None, None, "failed"
    except Exception:
        return s, None, None, "failed"


LOCK = os.path.join(HERE, "fetching.lock")


def network_up(wait=240):
    """True once the internet answers."""
    import time
    end = time.time() + wait
    while True:
        for probe in ("https://www.bing.com/", "https://www.google.com/"):
            if fetch(probe, 8):
                return True
        if time.time() > end:
            return False
        time.sleep(20)


def is_newsletter(s):
    """Substack blocks cloud servers, so Substack newsletters are skipped."""
    return "substack.com" in s["addr"] or "substack" in s["name"].lower()


def collect(verbose=True):
    import time
    if STATUS["running"]:
        return "Already fetching."
    if os.path.exists(LOCK) \
            and time.time() - os.path.getmtime(LOCK) < 1800:
        return "Another fetch is already running."
    STATUS["running"] = True
    open(LOCK, "w").write(str(os.getpid()))
    try:
        say("Waiting for the internet...", verbose)
        if not network_up():
            STATUS["text"] = "No internet connection; will try again later."
            if verbose:
                print(STATUS["text"])
            return STATUS["text"]
        return _collect(verbose, only=lambda s: not is_newsletter(s))
    finally:
        STATUS["running"] = False
        try:
            os.remove(LOCK)
        except OSError:
            pass


def _collect(verbose, only=None, note=""):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    con = db()
    now = dt.datetime.now(dt.timezone.utc)
    first_run = con.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    cutoff = now - dt.timedelta(days=7 if first_run else 4)
    cached = {r["site"]: r["feed"] for r in con.execute("SELECT site,feed FROM feeds WHERE status='ok'")}
    sources = [s for s in read_sources() if only is None or only(s)]
    report, new_ids, need_page, n = [], [], [], 0
    with ThreadPoolExecutor(16) as pool:
        jobs = [pool.submit(pull, s, cached) for s in sources]
        for job in as_completed(jobs):
            s, feed, body, how = job.result()
            n += 1
            parsed = body if how in ("news-search", "page") else (parse_feed(body) if looks_like_feed(body) else None)
            if not feed or parsed is None:
                if s["kind"] != "search":
                    con.execute("INSERT OR REPLACE INTO feeds VALUES(?,?,?)", (s["addr"], feed or "", "failed"))
                report.append("FAILED   %s  (%s)" % (s["name"], s["addr"]))
            else:
                if how == "feed":
                    con.execute("INSERT OR REPLACE INTO feeds VALUES(?,?,?)", (s["addr"], feed, "ok"))
                added = 0
                for it in parsed:
                    url = clean_url(it["link"])
                    when = to_date(it["date"]) or now
                    if when < cutoff or con.execute("SELECT 1 FROM items WHERE url=?", (url,)).fetchone():
                        continue
                    ex = excerpt_from(it["desc"])
                    blob = it["title"] + " " + ex
                    # broad outlets and searches: keep only stories clearly about watches as objects
                    if JUNK.search(it["title"]) or (s["kind"] != "watch" and not STRONG.search(blob)):
                        continue
                    source = it["publisher"] if how == "search" and it["publisher"] else s["name"]
                    score, tag = rule_score(it["title"], ex, s["kind"], source)
                    cur = con.execute(
                        "INSERT OR IGNORE INTO items(url,title,excerpt,source,kind,published,found,score,tag,emoji)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (url, it["title"], ex, source, s["kind"], when.isoformat(), now.isoformat(),
                         score, tag, EMOJI.get(tag, "📰")))
                    if cur.rowcount:
                        added += 1
                        new_ids.append(cur.lastrowid)
                        if how != "feed" or len(ex) < 60 or ex.endswith("…"):
                            need_page.append((score, cur.lastrowid, url))
                report.append("ok %4d  %s%s" % (added, s["name"], {"news-search": "  (via news search)", "page": "  (read from its page)"}.get(how, "")))
            con.commit()
            say("[%d/%d] %s" % (n, len(sources), report[-1]), verbose)
        # second pass: the article's own subheading, for items whose feed gave little or nothing
        need_page = sorted(need_page, reverse=True)[:200]
        jobs = {pool.submit(page_standfirst, u): i for _, i, u in need_page}
        for k, job in enumerate(as_completed(jobs), 1):
            try:
                ex, when = job.result()
            except Exception:
                ex, when = "", None
            if ex:
                con.execute("UPDATE items SET excerpt=? WHERE id=?", (ex, jobs[job]))
            if when:
                con.execute("UPDATE items SET published=?, hidden=CASE WHEN ?<? THEN 1 ELSE hidden END WHERE id=?",
                            (when.isoformat(), when.isoformat(), cutoff.isoformat(), jobs[job]))
            if k % 20 == 0 or k == len(jobs):
                say("Reading subheadings %d/%d" % (k, len(jobs)), verbose)
        con.commit()
    junk = [str(r["id"]) for r in con.execute("SELECT id,title,excerpt FROM items WHERE kind='search'")
            if JUNK.search(r["title"]) or not STRONG.search(r["title"] + " " + (r["excerpt"] or ""))]
    if junk:
        con.execute("UPDATE items SET hidden=1 WHERE picked=0 AND id IN (%s)" % ",".join(junk))
    translate_missing(con, verbose)
    dedupe(con)
    say("Ranking...", verbose)
    ai_note = ai_score(con, new_ids)
    note = (note + " " + ai_note).strip()
    con.commit()
    failed = [r for r in report if r.startswith("FAILED")]
    summary = "%s  %d new items from %d of %d sources. %s\n" % (
        dt.datetime.now().strftime("%Y-%m-%d %H:%M"), len(new_ids), len(report) - len(failed), len(report), note)
    with open(LOG, "w", encoding="utf-8") as f:
        f.write(summary + "\n".join(sorted(report)) + "\n")
    say(summary.strip(), verbose)
    return summary


FOREIGN = set("le la les des du une un est et pour dans sur avec aux qui que par au ses son sa ce cette "
              "der die das und für mit von den dem ein eine ist im auf zu nicht sich bei nach wird "
              "il lo gli di che per con del della nel sono è el los las por una en al se su".split())
ENGLISH = set("the and of to in for with on is at by from as its his her has have that this a an are was "
              "it new how why what will be you your their".split())


def is_foreign(text):
    if re.search(r"[\u0400-\u04ff\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af\u0600-\u06ff]", text):
        return True
    words = re.findall(r"[a-zà-ÿ']+", text.lower())
    f = sum(w in FOREIGN for w in words)
    e = sum(w in ENGLISH for w in words)
    return f >= 2 and f > e


def translate(text):
    """Machine translation to English through Google's public endpoint. '' on failure."""
    raw = fetch("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=en&dt=t&q="
                + urllib.parse.quote(text[:1500]), 12)
    try:
        return "".join(seg[0] for seg in json.loads(raw)[0] if seg and seg[0]).strip()
    except Exception:
        return ""


def translate_missing(con, verbose):
    from concurrent.futures import ThreadPoolExecutor
    rows = con.execute("SELECT id,title,excerpt FROM items WHERE lang IS NULL").fetchall()
    todo = []
    for r in rows:
        if is_foreign(r["title"] + " " + (r["excerpt"] or "")):
            todo.append(r)
        else:
            con.execute("UPDATE items SET lang='en' WHERE id=?", (r["id"],))
    if todo:
        say("Translating %d foreign items into English..." % len(todo), verbose)

    def one(r):
        return r["id"], translate(r["title"]), translate(r["excerpt"]) if r["excerpt"] else ""
    with ThreadPoolExecutor(6) as pool:
        for i, t, e in pool.map(one, todo):
            if t:
                con.execute("UPDATE items SET lang='foreign', title_en=?, excerpt_en=? WHERE id=?", (t, e, i))
    con.commit()


STOP = set("the a an and of to in for with on at by from as its is are was after over into amid new says "
           "le la les des du de un une et en au aux pour sur der die das und für mit von".split())


def words(r):
    return set(w for w in re.findall(r"[a-z0-9à-ÿ]+", (r["title_en"] or r["title"]).lower())
               if len(w) > 2 and w not in STOP)


def dedupe(con):
    """Mark later reports of the same story as duplicates of the best one."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=6)).isoformat()
    rows = con.execute("SELECT * FROM items WHERE found>=? AND hidden=0 AND dup_of IS NULL "
                       "ORDER BY (kind='search'), score DESC, published", (since,)).fetchall()
    keep = []
    for r in rows:
        w = words(r)
        for k, kw in keep:
            both = len(w & kw)
            if both >= 3 and both / max(1, len(w | kw)) >= 0.4:
                if not r["picked"]:
                    con.execute("UPDATE items SET dup_of=?, hidden=1 WHERE id=?", (k, r["id"]))
                break
        else:
            keep.append((r["id"], w))
    con.commit()


AI_BRIEF = """You rank watch news for SDC Briefly, a public daily page read by serious watch collectors and
people who work in the watch trade. Its edge is industry news they will not find on the big watch blogs.
Score highest (7-10): business results and strategy, ownership changes, people moves, legal, regulatory
and counterfeit cases, export and market data, retail and secondary-market news, auction records and
oddities, Swiss and European local press about the industry, and anything strange or niche a watch nerd
would not have seen. Score in the middle (5-6): important launches that collectors will talk about (a new
reference from Rolex, Patek Philippe, AP, Lange and peers, or a major independent's new or first watch),
thoughtful features, history and interviews. Score low (1-4): routine new releases, hands-on reviews,
listicles, buying and gift guides, deals, sponsored posts, celebrity wrist-spotting.
For each item return score (1-10, 10 = must read), tag (one of: law, business, auction, feature,
swiss, release, crime, other), why (max 8 words), and hide (true when the item is not really about
watches or the watch industry, the headline is broken or meaningless, or it is an advert or shop listing).
Only when the item is not in English, also return title_en and excerpt_en: faithful English translations
of the title and the excerpt. Reply with a JSON array only:
[{"id": 1, "score": 7, "tag": "law", "why": "...", "hide": false, "title_en": "", "excerpt_en": ""}]"""

DEDUPE_BRIEF = """Below are news headlines, one per line as: id | source | headline. Several lines can
report the same news story (the same event), often in different words or translated from other
languages. Group the lines that report the same story. Reply with a JSON array of groups, each a list of
ids, only for groups with two or more ids, for example [[12, 40, 41], [7, 9]]. Reply [] if none."""


def ai_call(system, content, max_tokens=8000):
    c = config()
    payload = json.dumps({"model": c["model"], "max_tokens": max_tokens, "system": system,
                          "messages": [{"role": "user", "content": content}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=payload, headers={
        "x-api-key": c["anthropic_api_key"], "anthropic-version": "2023-06-01",
        "content-type": "application/json"})
    reply = json.loads(urllib.request.urlopen(req, timeout=180).read())
    text = "".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text")
    if not text:
        raise ValueError("no text in reply: %s" % json.dumps(reply)[:300])
    return text


def ai_score(con, ids):
    if not config().get("anthropic_api_key"):
        return "No AI key set, so items were ranked by keyword rules."
    # also anything from the last three weeks that has never been read by AI or by hand
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=21)).isoformat()
    ids = list(dict.fromkeys(list(ids) + [r[0] for r in con.execute(
        "SELECT id FROM items WHERE ranked IS NULL AND hidden=0 AND dup_of IS NULL AND published>=?", (since,))]))
    done, err = 0, ""
    batches = [ids[i:i + 25] for i in range(0, len(ids), 25)]
    while batches:
        batch = batches.pop(0)
        rows = con.execute("SELECT id,title,excerpt,source FROM items WHERE id IN (%s)"
                           % ",".join(map(str, batch))).fetchall()
        listing = "\n".join("%d | %s | %s | %s" % (r["id"], r["source"], r["title"], (r["excerpt"] or "")[:300])
                            for r in rows)
        try:
            txt = ai_call(AI_BRIEF, listing)
            for r in json.loads(txt[txt.index("["):txt.rindex("]") + 1]):
                tag = r.get("tag", "other")
                con.execute("UPDATE items SET ranked=1,score=COALESCE(mine,?),tag=?,why=?,emoji=?,"
                            "hidden=CASE WHEN ? AND picked=0 THEN 1 ELSE hidden END WHERE id=?",
                            (float(r["score"]), tag, r.get("why", ""), EMOJI.get(tag, "📰"),
                             1 if r.get("hide") else 0, int(r["id"])))
                if r.get("title_en"):
                    con.execute("UPDATE items SET lang='foreign',title_en=?,excerpt_en=? WHERE id=?",
                                (r["title_en"], r.get("excerpt_en", ""), int(r["id"])))
                done += 1
            con.commit()
        except Exception as e:
            if len(batch) > 5:   # try again in two smaller halves
                batches += [batch[:len(batch) // 2], batch[len(batch) // 2:]]
            else:
                err = " Some items could not be ranked (%s); they will be tried again next run." % e
    merged = ai_dedupe(con)
    return "AI ranked %d items and merged %d duplicates.%s" % (done, merged, err)


def ai_dedupe(con):
    """Ask the AI which visible stories from the last 8 days are the same news; keep the best of each."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)).isoformat()
    rows = con.execute("SELECT id,source,title,title_en,score,kind FROM items WHERE hidden=0 AND dup_of IS NULL "
                       "AND published>=? ORDER BY id", (since,)).fetchall()
    if len(rows) < 2:
        return 0
    by = {r["id"]: r for r in rows}
    try:
        txt = ai_call(DEDUPE_BRIEF, "\n".join("%d | %s | %s" % (r["id"], r["source"], r["title_en"] or r["title"])
                                               for r in rows), 4000)
        groups = json.loads(txt[txt.index("["):txt.rindex("]") + 1])
    except Exception as e:
        print("Duplicate check failed: %s" % e)
        return 0
    merged = 0
    for g in groups:
        g = [int(x) for x in g if int(x) in by]
        if len(g) < 2:
            continue
        keep = sorted(g, key=lambda x: (-(by[x]["score"] or 0), by[x]["kind"] == "search", x))[0]
        for x in g:
            if x != keep:
                con.execute("UPDATE items SET dup_of=?, hidden=1 WHERE id=? AND picked=0", (keep, x))
                merged += 1
    con.commit()
    return merged


SITE_DAYS = 21


SITE_URL = "https://briefly.screwdowncrown.com"


def _esc(t):
    return html.escape(str(t or ""), quote=True)


def _story_rows(con, since=None, until=None, min_score=0):
    q = "SELECT * FROM items WHERE hidden=0 AND dup_of IS NULL"
    args = []
    if since:
        q += " AND published>=?"; args.append(since)
    if until:
        q += " AND published<?"; args.append(until)
    q += " AND score>=? ORDER BY published DESC"; args.append(min_score)
    rows = con.execute(q, args).fetchall()
    also = {}
    for r in con.execute("SELECT dup_of, source FROM items WHERE dup_of IS NOT NULL"):
        also.setdefault(int(r["dup_of"]), set()).add(r["source"])
    return [{"t": r["title_en"] or r["title"], "u": r["url"], "s": r["source"], "d": r["published"][:10],
             "n": round(r["score"] or 0), "g": r["tag"] or "", "x": 1 if r["title_en"] else 0,
             "a": sorted(also.get(r["id"], set()) - {r["source"]})[:4]} for r in rows]


TAG = "utm_source=screwdowncrown.com&utm_medium=referral&utm_campaign=sdc-briefly"


def tagged(u):
    """The outbound link with SDC's tag added, so publishers see where the reader came from.
    Links that already carry tracking tags of their own are left alone."""
    if "utm_" in u:
        return u
    base, _, frag = u.partition("#")
    return base + ("&" if "?" in base else "?") + TAG + ("#" + frag if frag else "")


def _item_html(i):
    meta = _esc(i["s"]) + (" · translated headline" if i["x"] else "") + \
        (" · also: " + _esc(", ".join(i["a"])) if i["a"] else "")
    return ('<div class="it"><div class="sc %s" title="Newsworthiness score out of 10">%d</div><div>'
            '<a href="%s" target="_blank" rel="noopener">%s</a><div class="meta">%s</div></div></div>'
            % ("hi" if i["n"] >= 7 else "", i["n"], _esc(tagged(i["u"])), _esc(i["t"]), meta))


def _day_label(d):
    return dt.date.fromisoformat(d).strftime("%A %-d %B %Y")


def _list_html(items):
    days = {}
    for i in items:
        days.setdefault(i["d"], []).append(i)
    out = []
    for d in sorted(days, reverse=True):
        out.append('<h2>%s</h2>' % _day_label(d))
        out += [_item_html(i) for i in sorted(days[d], key=lambda x: -x["n"])]
    return "\n".join(out)


def _analytics():
    p = os.path.join(HERE, "analytics-token.txt")
    tok = open(p, encoding="utf-8").read().strip() if os.path.exists(p) else ""
    if not re.fullmatch(r"[0-9a-f]{32}", tok):
        return ""
    return ("<!-- Cloudflare Web Analytics --><script type='module' "
            "src='https://static.cloudflareinsights.com/beacon.min.js' "
            "data-cf-beacon='{\"token\": \"%s\"}'></script><!-- End Cloudflare Web Analytics -->" % tok)


def publish(out_dir=None):
    """Write the public site: the front page (headlines in the HTML so search engines can read them),
    a page per day, an archive index, a sitemap and robots.txt."""
    out_dir = out_dir or os.path.join(HERE, "site")
    os.makedirs(os.path.join(out_dir, "day"), exist_ok=True)
    con = db()
    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=SITE_DAYS)).isoformat()
    data = _story_rows(con, since=since)
    stamp = now.strftime("%d %b %Y, %H:%M UTC")
    top = sorted([i for i in data if i["d"] >= (now - dt.timedelta(days=2)).date().isoformat()],
                 key=lambda x: -x["n"])[:10]
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebSite", "name": "SDC Briefly", "url": SITE_URL + "/",
         "publisher": {"@type": "Organization", "name": "ScrewDownCrown", "url": "https://www.screwdowncrown.com"}},
        {"@type": "ItemList", "name": "Today's top watch industry news", "itemListElement": [
            {"@type": "ListItem", "position": k + 1, "url": i["u"], "name": i["t"]} for k, i in enumerate(top)]}]}
    page = open(os.path.join(HERE, "public.html"), encoding="utf-8").read()
    page = page.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    page = page.replace("__LIST__", _list_html([i for i in data if i["n"] >= 5]))
    page = page.replace("__JSONLD__", json.dumps(ld, ensure_ascii=False).replace("</", "<\\/"))
    page = page.replace("__ANALYTICS__", _analytics())
    page = page.replace("__UPDATED__", stamp)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)

    # a page per day with stories (everything in the database, scored 5 or more)
    allitems = _story_rows(con, min_score=5)
    by_day = {}
    for i in allitems:
        by_day.setdefault(i["d"], []).append(i)
    days = sorted(by_day, reverse=True)
    shell = open(os.path.join(HERE, "day.html"), encoding="utf-8").read()
    for k, d in enumerate(days):
        newer = '<a href="/day/%s.html">← %s</a>' % (days[k - 1], _day_label(days[k - 1])) if k > 0 else ""
        older = '<a href="/day/%s.html">%s →</a>' % (days[k + 1], _day_label(days[k + 1])) if k + 1 < len(days) else ""
        best = sorted(by_day[d], key=lambda x: -x["n"])
        desc = "Watch industry news for %s: %s" % (_day_label(d), "; ".join(i["t"] for i in best[:3]))
        p = (shell.replace("__TITLE__", _esc("Watch industry news, %s · SDC Briefly" % _day_label(d)))
             .replace("__DESC__", _esc(desc[:300])).replace("__URL__", "%s/day/%s.html" % (SITE_URL, d))
             .replace("__HEADING__", _esc(_day_label(d)))
             .replace("__LIST__", "\n".join(_item_html(i) for i in best))
             .replace("__NAV__", '<div class="nav"><span>%s</span><span>%s</span></div>' % (newer, older))
             .replace("__ANALYTICS__", _analytics()))
        with open(os.path.join(out_dir, "day", d + ".html"), "w", encoding="utf-8") as f:
            f.write(p)
    links = "\n".join('<div class="it"><div><a href="/day/%s.html">%s</a><div class="meta">%d stories · top: %s</div></div></div>'
                      % (d, _day_label(d), len(by_day[d]), _esc(max(by_day[d], key=lambda x: x["n"])["t"]))
                      for d in days)
    p = (shell.replace("__TITLE__", "Watch industry news archive · SDC Briefly")
         .replace("__DESC__", "Every day's watch industry news collected by SDC Briefly, day by day.")
         .replace("__URL__", SITE_URL + "/archive.html").replace("__HEADING__", "Archive, day by day")
         .replace("__LIST__", links).replace("__NAV__", "").replace("__ANALYTICS__", _analytics()))
    with open(os.path.join(out_dir, "archive.html"), "w", encoding="utf-8") as f:
        f.write(p)

    # sitemap and robots.txt
    sm = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
          "<url><loc>%s/</loc><lastmod>%s</lastmod><changefreq>hourly</changefreq></url>" % (SITE_URL, now.date()),
          "<url><loc>%s/archive.html</loc><lastmod>%s</lastmod></url>" % (SITE_URL, now.date())]
    sm += ["<url><loc>%s/day/%s.html</loc><lastmod>%s</lastmod></url>" % (SITE_URL, d, d) for d in days]
    sm.append("</urlset>")
    with open(os.path.join(out_dir, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(sm))
    with open(os.path.join(out_dir, "robots.txt"), "w", encoding="utf-8") as f:
        # AI training crawlers take the text and send no readers back: blocked.
        # Search engines and AI answer bots (which cite and link) stay allowed.
        train = ["GPTBot", "CCBot", "Google-Extended", "ClaudeBot", "anthropic-ai",
                 "Bytespider", "meta-externalagent", "Applebot-Extended", "cohere-ai"]
        rules = "".join("User-agent: %s\nDisallow: /\n\n" % b for b in train)
        f.write(rules + "User-agent: *\nAllow: /\n\nSitemap: %s/sitemap.xml\n" % SITE_URL)
    print("Published %d headlines, %d day pages and sitemap to %s" % (len(data), len(days), out_dir))


def health():
    """source-health.md: how each source is doing, and outlets the searches keep finding that are not
    on the list yet. Read by the weekly source scout."""
    con = db()
    now = dt.datetime.now(dt.timezone.utc)
    d30 = (now - dt.timedelta(days=30)).isoformat()
    failed = set()
    if os.path.exists(LOG):
        failed = {m.group(1).strip() for m in re.finditer(r"^FAILED\s+(.+?)\s{2}\(", open(LOG, encoding="utf-8").read(), re.M)}
    known_hosts = {host_of(s["addr"]) for s in read_sources() if s["kind"] != "search"}
    out = ["# Source health", "", "Updated %s. Counts are stories found in the last 30 days." % now.strftime("%d %b %Y %H:%M UTC"),
           "", "## Sources on the list", "",
           "| Source | Kind | Stories (30 days) | Scored 6+ | Newest story | Last run |", "|---|---|---|---|---|---|"]
    rows = []
    for s in read_sources():
        q = con.execute("SELECT COUNT(*), SUM(score>=6), MAX(published) FROM items WHERE published>=? AND "
                        + ("kind='search' AND url IN (SELECT url FROM items WHERE kind='search') AND 0=1" if False else
                           "source=?"), (d30, s["name"])).fetchone() if s["kind"] != "search" else \
            con.execute("SELECT COUNT(*), SUM(score>=6), MAX(published) FROM items WHERE published>=? AND kind='search'",
                        (d30,)).fetchone()
        if s["kind"] == "search":
            continue
        newest = con.execute("SELECT MAX(published) FROM items WHERE source=?", (s["name"],)).fetchone()[0]
        rows.append((q[0] or 0, s, q[1] or 0, (newest or "")[:10]))
    for n, s, good, newest in sorted(rows, key=lambda r: (r[0], r[2])):
        out.append("| %s | %s | %d | %d | %s | %s |" % (s["name"], s["kind"], n, good, newest or "never",
                                                       "skipped (Substack blocks cloud servers)" if is_newsletter(s)
                                                       else "FAILED" if s["name"] in failed else "ok"))
    out += ["", "## Outlets the news searches found that are not on the list", "",
            "Stories that came in through the search phrases, grouped by website. High counts of good stories "
            "suggest the site is worth adding as a source of its own.", "",
            "| Website | Outlet name | Stories (30 days) | Scored 6+ | Average score | Example headline |",
            "|---|---|---|---|---|---|"]
    by = {}
    for r in con.execute("SELECT * FROM items WHERE kind='search' AND published>=? AND dup_of IS NULL", (d30,)):
        h = host_of(r["url"])
        if not h or h in known_hosts or "msn.com" in h or "yahoo.com" in h:
            continue
        by.setdefault(h, []).append(r)
    cands = sorted(by.items(), key=lambda kv: (-sum((x["score"] or 0) >= 6 for x in kv[1]), -len(kv[1])))[:40]
    for h, its in cands:
        best = max(its, key=lambda x: x["score"] or 0)
        out.append("| %s | %s | %d | %d | %.1f | %s |" % (
            h, best["source"], len(its), sum((x["score"] or 0) >= 6 for x in its),
            sum(x["score"] or 0 for x in its) / len(its), (best["title_en"] or best["title"]).replace("|", "-")[:90]))
    with open(os.path.join(HERE, "source-health.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("Wrote source-health.md")


def daily():
    """What the cloud runs each day: fetch, then publish, then report on the sources."""
    collect()
    publish()
    health()


if __name__ == "__main__":
    cmds = {"daily": daily, "collect": collect, "publish": publish, "health": health}
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in cmds:
        sys.exit("usage: python3 briefly.py daily|collect|publish|health")
    cmds[cmd]()
