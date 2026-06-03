#!/usr/bin/env python3
"""
Always-on market-news monitor (cloud version) for GitHub Actions.
Fetches NewsData.io across all topics, filters to last 2 days (3 at the
07:00 Amman morning run), de-dupes via state.json, ranks by market impact,
builds docs/index.html (GitHub Pages), and pushes a phone alert via ntfy.sh.

Repo Secrets used:
  NEWSDATA_API_KEY  your NewsData key (pub_...)
  NTFY_TOPIC        your private ntfy topic name
  NTFY_SERVER       optional, default https://ntfy.sh
"""
import os, json, html, re, datetime as dt
from datetime import timezone, timedelta
import urllib.parse
import requests

try:
    from zoneinfo import ZoneInfo
    AMMAN = ZoneInfo("Asia/Amman")
except Exception:
    AMMAN = timezone(timedelta(hours=3))

API_KEY = os.environ.get("NEWSDATA_API_KEY", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
STATE_PATH = os.path.join(HERE, "state.json")
os.makedirs(DOCS, exist_ok=True)

now = dt.datetime.now(AMMAN)
now_utc = dt.datetime.now(timezone.utc)
IS_MORNING = (now.hour == 7)
WINDOW_HOURS = 72 if IS_MORNING else 48

TOPICS = [
    ("Politics / Geopolitics", "politics,world", 'tariff OR sanctions OR election OR "central bank" OR war'),
    ("Stocks / Markets", "business", 'stocks OR "Federal Reserve" OR earnings OR inflation OR rates'),
    ("Commodities", "business,world", 'oil OR crude OR gold OR OPEC OR copper OR gas'),
    ("Technology", "technology", 'AI OR chip OR semiconductor OR Nvidia OR cloud'),
    ("EV", "business,technology", '"electric vehicle" OR EV OR Tesla OR BYD OR Rivian'),
    ("Batteries", "business,technology", 'battery OR lithium OR "energy storage" OR cathode'),
    ("Solar / Renewables", "business,technology", 'solar OR photovoltaic OR renewable OR "clean energy"'),
]

HIGH = {
    "federal reserve": 6, "fed": 4, "central bank": 6, "interest rate": 6, "rate decision": 7,
    "rate cut": 6, "rate hike": 6, "inflation": 5, "cpi": 5, "jobs report": 5, "payroll": 5,
    "tariff": 6, "tariffs": 6, "sanction": 6, "sanctions": 6, "opec": 6, "crude": 4,
    "oil price": 5, "hormuz": 7, "war": 5, "ceasefire": 5, "earnings": 4, "guidance": 4,
    "merger": 5, "acquisition": 5, "ipo": 4, "downgrade": 4, "upgrade": 3, "ban": 4,
    "regulation": 3, "default": 4, "recession": 6, "stimulus": 4, "gdp": 4, "boe": 5, "ecb": 5,
}
MED = {
    "stock": 2, "stocks": 2, "shares": 2, "revenue": 2, "forecast": 2, "lithium": 3,
    "battery": 2, "ev": 2, "tesla": 3, "byd": 3, "nvidia": 3, "solar": 2, "chip": 2,
    "semiconductor": 3, "data center": 2, "subsidy": 3, "factory": 2, "supply": 2,
}
UPCOMING_HINTS = ["to meet", "will meet", "expected to", "scheduled", "next week",
                  "upcoming", "ahead of", "set to", "this week", "to decide", "to report",
                  "forecast", "to launch", "by year end", "by year-end", "to vote", "plans to"]


def score_article(a):
    text = " ".join([
        (a.get("title") or ""), (a.get("description") or ""),
        " ".join(a.get("keywords") or []), " ".join(a.get("category") or []),
    ]).lower()
    s = 0
    for k, w in HIGH.items():
        if re.search(r"\b" + re.escape(k) + r"\b", text):
            s += w
    for k, w in MED.items():
        if re.search(r"\b" + re.escape(k) + r"\b", text):
            s += w
    sp = a.get("source_priority")
    if isinstance(sp, int):
        if sp <= 3000:
            s += 4
        elif sp <= 20000:
            s += 2
        elif sp <= 100000:
            s += 1
    return s


def importance(s):
    if s >= 8:
        return "High"
    if s >= 3:
        return "Medium"
    return "Low"


def is_upcoming(a):
    text = ((a.get("title") or "") + " " + (a.get("description") or "")).lower()
    return any(h in text for h in UPCOMING_HINTS)


def parse_pub(s):
    try:
        return dt.datetime.strptime((s or "").strip()[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def fetch():
    out = []
    seen_ids = set()
    for label, cat, q in TOPICS:
        params = {"apikey": API_KEY, "language": "en", "size": 10,
                  "removeduplicate": 1, "category": cat, "q": q}
        url = "https://newsdata.io/api/1/latest?" + urllib.parse.urlencode(params)
        try:
            data = requests.get(url, timeout=30).json()
        except Exception as e:
            print("fetch error for", label, e)
            continue
        if data.get("status") != "success":
            print("api non-success for", label, str(data)[:160])
            continue
        for a in data.get("results", []):
            aid = a.get("article_id") or a.get("link")
            if not aid or aid in seen_ids or a.get("duplicate") is True:
                continue
            pub = parse_pub(a.get("pubDate"))
            if not pub or (now_utc - pub) > timedelta(hours=WINDOW_HOURS):
                continue
            seen_ids.add(aid)
            a["_topic"] = label
            a["_score"] = score_article(a)
            a["_imp"] = importance(a["_score"])
            a["_upcoming"] = is_upcoming(a)
            a["_pub"] = pub
            out.append(a)
    out.sort(key=lambda x: (x["_score"], x["_pub"]), reverse=True)
    return out


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": {}}


def save_state(state):
    cutoff = now - timedelta(days=3)
    pruned = {}
    for k, v in state.get("seen", {}).items():
        try:
            t = dt.datetime.fromisoformat(v)
            if t.tzinfo is None:
                t = t.replace(tzinfo=AMMAN)
            if t >= cutoff:
                pruned[k] = v
        except Exception:
            pruned[k] = v
    state["seen"] = pruned
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


CSS = (
    ":root{color-scheme:light}*{box-sizing:border-box}"
    "body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f6f7f9;color:#1a2027}"
    ".wrap{max-width:760px;margin:0 auto;padding:18px 14px 60px}"
    "h1{font-size:20px;margin:0 0 2px;color:#0f172a}"
    ".sub{color:#64748b;font-size:13px;margin-bottom:16px}"
    ".card{display:flex;gap:12px;background:#fff;border:1px solid #e5e8ec;border-radius:14px;padding:14px;margin-bottom:12px;box-shadow:0 1px 2px rgba(16,24,40,.04)}"
    ".rank{font-size:22px;font-weight:700;color:#2563eb;min-width:26px;text-align:center}"
    ".body{flex:1;min-width:0}.meta{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:6px}"
    ".badge{color:#fff;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px}"
    ".badge.new{background:#16a34a}.chip{background:#eef1f5;color:#475569;font-size:11px;padding:2px 8px;border-radius:999px}"
    ".when{color:#94a3b8;font-size:12px;margin-left:auto}"
    "a.title{color:#0f172a;font-weight:600;font-size:16px;text-decoration:none;display:block;line-height:1.35}"
    "a.title:hover{text-decoration:underline;color:#1d4ed8}"
    ".why{color:#475569;font-size:13.5px;margin:6px 0 8px;line-height:1.45}"
    ".src{color:#94a3b8;font-size:12px}.src a{color:#2563eb;text-decoration:none}"
    ".foot{color:#94a3b8;font-size:12px;text-align:center;margin-top:18px}"
)
IMP_COLOR = {"High": "#e02424", "Medium": "#d97706", "Low": "#6b7280"}


def blurb(a):
    d = (a.get("description") or "").strip()
    return (d[:217] + "...") if len(d) > 220 else d


def build_html(top, new_ids):
    cards = []
    for i, a in enumerate(top, 1):
        imp = a["_imp"]
        color = IMP_COLOR.get(imp, "#6b7280")
        when = ("UPCOMING" if a["_upcoming"] else a["_pub"].astimezone(AMMAN).strftime("%b %d, %H:%M"))
        nb = '<span class="badge new">NEW</span>' if (a.get("article_id") in new_ids) else ""
        url = html.escape(a.get("link") or "#")
        src = a.get("source_name") or a.get("source_id") or ""
        cards.append(
            '<div class="card"><div class="rank">' + str(i) + '</div><div class="body"><div class="meta">'
            '<span class="badge" style="background:' + color + '">' + imp + '</span>'
            '<span class="chip">' + html.escape(a["_topic"]) + '</span>' + nb +
            '<span class="when">' + when + '</span></div>'
            '<a class="title" href="' + url + '" target="_blank" rel="noopener">' + html.escape(a.get("title") or "") + '</a>'
            '<div class="why">' + html.escape(blurb(a)) + '</div>'
            '<div class="src">' + html.escape(src) +
            ' &middot; <a href="' + url + '" target="_blank" rel="noopener">Open</a></div></div></div>'
        )
    head = "Morning recap (last 3 days)" if IS_MORNING else "Last 2 days &amp; upcoming"
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="refresh" content="900">'
        '<title>Market News - Top 10</title><style>' + CSS + '</style></head><body><div class="wrap">'
        '<h1>Market News - Top ' + str(len(top)) + '</h1>'
        '<div class="sub">' + head + ' &middot; Updated ' + now.strftime("%A %b %d, %Y - %H:%M") +
        ' (Amman / Jordan time) &middot; auto-refreshed hourly</div>' +
        ("".join(cards) if cards else '<div class="sub">No items right now.</div>') +
        '<div class="foot">Politics &middot; Stocks &middot; Commodities &middot; Tech &middot; EV &middot; Batteries &middot; Solar &nbsp;|&nbsp; Source: NewsData.io</div>'
        '</div></body></html>'
    )
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(doc)


def notify(title, body, tags="newspaper", priority="default"):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set; skipping push. Message:\n", title, "\n", body)
        return
    try:
        requests.post(NTFY_SERVER + "/" + NTFY_TOPIC, data=body.encode("utf-8"),
                      headers={"Title": title, "Tags": tags, "Priority": priority}, timeout=20)
        print("ntfy sent.")
    except Exception as e:
        print("ntfy error:", e)


def main():
    if not API_KEY:
        raise SystemExit("NEWSDATA_API_KEY is not set.")
    arts = fetch()
    state = load_state()
    seen = state.setdefault("seen", {})
    top = arts[:10]
    new_ids = {a.get("article_id") for a in arts if a.get("article_id") and a.get("article_id") not in seen}
    for a in top:
        aid = a.get("article_id")
        if aid:
            seen[aid] = now.isoformat()
    build_html(top, new_ids)
    save_state(state)
    stamp = now.strftime("%b %d, %H:%M")
    if IS_MORNING:
        title = "Good morning - Market briefing (" + now.strftime("%b %d") + ")"
        lines = [str(i) + ") [" + a["_imp"] + "] " + (a.get("title") or "")[:80] for i, a in enumerate(top[:5], 1)]
        up = [a for a in top if a["_upcoming"]]
        if up:
            lines.append("Upcoming: " + (up[0].get("title") or "")[:80])
        notify(title, "\n".join(lines) if lines else "No major items in the last 3 days.", tags="sunrise", priority="high")
    else:
        new_top = [a for a in top if a.get("article_id") in new_ids]
        if not new_top:
            print("No new items this hour; page refreshed, no push.")
            return
        title = "Market update - " + str(len(new_top)) + " new (" + stamp + " Amman)"
        lines = [str(i) + ") [" + a["_imp"] + "] " + (a.get("title") or "")[:80] for i, a in enumerate(new_top[:3], 1)]
        notify(title, "\n".join(lines), tags="newspaper",
               priority=("high" if any(a["_imp"] == "High" for a in new_top) else "default"))


if __name__ == "__main__":
    main()
