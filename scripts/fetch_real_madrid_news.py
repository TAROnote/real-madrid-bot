import json
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

JST = timezone(timedelta(hours=9))
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0 Safari/537.36"
    )
}

def resolve_google_news_url(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        return r.url
    except Exception:
        return url
KEYWORDS = [
    "real madrid",
    "madridista",
    "bernabéu",
    "bernabeu",
    "vinicius",
    "bellingham",
    "mbappe",
    "rodrygo",
    "valverde",
    "courtois",
    "ancelotti",
    "xabi alonso",
]

@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    published: Optional[str] = None
    summary: str = ""

def now_jst() -> datetime:
    return datetime.now(timezone.utc).astimezone(JST)

def clean_text(text: str) -> str:
    text = BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def get_html(url: str, timeout: int = 20) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text

def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return dtparser.parse(value)
    except Exception:
        return None

def is_relevant(title: str, summary: str = "") -> bool:
    hay = f"{title} {summary}".lower()
    return any(k in hay for k in KEYWORDS)

def dedupe_items(items: List[NewsItem]) -> List[NewsItem]:
    seen = set()
    out = []
    for item in items:
        key = re.sub(r"[^a-z0-9]+", "", item.title.lower())
        if len(key) < 10:
            key = item.link
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out

def sort_items(items: List[NewsItem]) -> List[NewsItem]:
    def sort_key(item: NewsItem):
        dt = parse_dt(item.published)
        return dt or datetime(1970, 1, 1, tzinfo=timezone.utc)
    return sorted(items, key=sort_key, reverse=True)

def trim_summary(text: str, max_len: int = 180) -> str:
    text = clean_text(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"

def fetch_google_news_rss(query: str, label: str, limit: int = 10) -> List[NewsItem]:
    url = (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    feed = feedparser.parse(url)
    items: List[NewsItem] = []

    for entry in feed.entries[:limit]:
        title = clean_text(entry.get("title", ""))
        raw_link = entry.get("link", "")
        link = resolve_google_news_url(raw_link)
        summary = clean_text(entry.get("summary", ""))
        published = entry.get("published", "") or entry.get("updated", "")

        if not title or not link:
            continue
        if not is_relevant(title, summary):
            continue

        items.append(
            NewsItem(
                title=title,
                link=link,
                source=label,
                published=published,
                summary=summary,
            )
        )
    return items

def fetch_realmadrid_official(limit: int = 12) -> List[NewsItem]:
    url = "https://www.realmadrid.com/en-US/news"
    html = get_html(url)
    soup = BeautifulSoup(html, "lxml")
    items: List[NewsItem] = []

    # 汎用的にリンクを拾う
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        text = clean_text(a.get_text(" ", strip=True))

        if not href or not text:
            continue
        if "/news/" not in href:
            continue
        if len(text) < 12:
            continue

        if href.startswith("/"):
            link = "https://www.realmadrid.com" + href
        elif href.startswith("http"):
            link = href
        else:
            continue

        if not is_relevant(text, "") and "real madrid" not in link.lower():
            continue

        items.append(
            NewsItem(
                title=text,
                link=link,
                source="Real Madrid Official",
                published="",
                summary="",
            )
        )

    return dedupe_items(items)[:limit]

def fetch_managing_madrid(limit: int = 12) -> List[NewsItem]:
    url = "https://www.managingmadrid.com/"
    html = get_html(url)
    soup = BeautifulSoup(html, "lxml")
    items: List[NewsItem] = []

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        text = clean_text(a.get_text(" ", strip=True))

        if not href or not text:
            continue
        if "managingmadrid.com" not in href and not href.startswith("/"):
            continue
        if len(text) < 12:
            continue

        if href.startswith("/"):
            link = "https://www.managingmadrid.com" + href
        else:
            link = href

        if not is_relevant(text, ""):
            continue

        items.append(
            NewsItem(
                title=text,
                link=link,
                source="Managing Madrid",
                published="",
                summary="",
            )
        )

    return dedupe_items(items)[:limit]

def fetch_football_espana(limit: int = 12) -> List[NewsItem]:
    url = "https://www.football-espana.net/category/la-liga/real-madrid"
    html = get_html(url)
    soup = BeautifulSoup(html, "lxml")
    items: List[NewsItem] = []

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        text = clean_text(a.get_text(" ", strip=True))

        if not href or not text:
            continue
        if "football-espana.net" not in href and not href.startswith("/"):
            continue
        if len(text) < 12:
            continue

        if href.startswith("/"):
            link = "https://www.football-espana.net" + href
        else:
            link = href

        if not is_relevant(text, ""):
            continue

        items.append(
            NewsItem(
                title=text,
                link=link,
                source="Football España",
                published="",
                summary="",
            )
        )

    return dedupe_items(items)[:limit]

def fetch_extra_sites() -> List[NewsItem]:
    sources = [
        ("LaLiga", "https://www.laliga.com/laliga-easports", "https://www.laliga.com"),
        ("Football España Home", "https://www.football-espana.net/", "https://www.football-espana.net"),
        ("AS", "https://en.as.com/soccer/", "https://en.as.com"),
        ("OneFootball", "https://onefootball.com/en/competition/laliga-10", "https://onefootball.com"),
        ("ESPN", "https://www.espn.com/soccer/league/_/name/esp.1", "https://www.espn.com"),
        ("Sky Sports", "https://www.skysports.com/la-liga", "https://www.skysports.com"),
        ("NewsNow", "https://www.newsnow.co.uk/h/?search=La%2BLiga&lang=a", "https://www.newsnow.co.uk"),
    ]

    items: List[NewsItem] = []

    for label, url, base in sources:
        try:
            html = get_html(url)
            soup = BeautifulSoup(html, "lxml")

            for a in soup.select("a[href]"):
                href = a.get("href", "").strip()
                text = clean_text(a.get_text(" ", strip=True))

                if not href or not text:
                    continue
                if len(text) < 15:
                    continue

                if href.startswith("/"):
                    link = base + href
                else:
                    link = href

                if not link.startswith("http"):
                    continue

                if not is_relevant(text, ""):
                    continue

                items.append(
                    NewsItem(
                        title=text,
                        link=link,
                        source=label,
                    )
                )

        except Exception as e:
            print(f"[WARN] {label} failed: {e}")

    return dedupe_items(items)[:20]

def collect_all_items() -> List[NewsItem]:
    all_items: List[NewsItem] = []

    fetchers = [
        ("realmadrid_official", fetch_realmadrid_official),
        ("managing_madrid", fetch_managing_madrid),
        ("football_espana", fetch_football_espana),
    ]

    for name, fn in fetchers:
        try:
            items = fn()
            all_items.extend(items)
            time.sleep(1)
        except Exception as e:
            print(f"[WARN] {name} failed: {e}")

    try:
    extra_items = fetch_extra_sites()
    all_items.extend(extra_items)
    time.sleep(1)
except Exception as e:
    print(f"[WARN] extra sites failed: {e}")

    rss_queries = [
        ("Google News / Real Madrid", "Real Madrid"),
        ("Google News / Managing Madrid", "Real Madrid site:managingmadrid.com"),
        ("Google News / Football España", "Real Madrid site:football-espana.net"),
    ]

    for label, query in rss_queries:
        try:
            items = fetch_google_news_rss(query, label=label)
            all_items.extend(items)
            time.sleep(1)
        except Exception as e:
            print(f"[WARN] RSS {label} failed: {e}")

    all_items = dedupe_items(all_items)
    all_items = [item for item in all_items if "news.google.com" not in item.link]
    all_items = sort_items(all_items)
    return all_items[:15]

def build_note_md(items: List[NewsItem]) -> str:
    date_str = now_jst().strftime("%Y-%m-%d")
    lines = [
        f"# レアル・マドリードニュースまとめ（{date_str}）",
        "",
        "今日のレアル・マドリード関連ニュースを自動収集して整理しました。",
        "",
    ]

    if not items:
        lines += [
            "本日は有力な更新を取得できませんでした。",
            "",
            "ソース確認:",
            "- Real Madrid Official",
            "- Managing Madrid",
            "- Football España",
            "- Google News RSS",
        ]
        return "\n".join(lines)

    top_items = items[:5]

    for i, item in enumerate(top_items, start=1):
        lines += [
            f"## {i}. {item.title}",
            f"**媒体**: {item.source}",
        ]
        if item.published:
            lines.append(f"**公開日時**: {item.published}")
        if item.summary:
            lines.append(f"**要点**: {trim_summary(item.summary, 220)}")
        lines += [
            f"**リンク**: {item.link}",
            "",
        ]

    lines += [
        "---",
        "",
        "## 一言まとめ",
        "今日は公式発表・専門メディア・ニュース集約の3方向から主要トピックを確認。",
        "投稿前に、最上位1〜2本だけ本文を開いて内容確認すると精度が上がります。",
    ]
    return "\n".join(lines)

def build_x_text(items: List[NewsItem]) -> str:
    if not items:
        return "【レアル・マドリード】本日は主要ニュースを確認できませんでした。#RealMadrid"

    top = items[0]
    title = trim_summary(top.title, 85)
    source = top.source
    return f"【レアル・マドリード速報】{title} ({source}) {top.link} #RealMadrid"

def build_json(items: List[NewsItem]) -> str:
    payload = {
        "generated_at_jst": now_jst().isoformat(),
        "count": len(items),
        "items": [asdict(i) for i in items],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)

def main():
    items = collect_all_items()

    (OUTPUT_DIR / "note.md").write_text(build_note_md(items), encoding="utf-8")
    (OUTPUT_DIR / "x.txt").write_text(build_x_text(items), encoding="utf-8")
    (OUTPUT_DIR / "news.json").write_text(build_json(items), encoding="utf-8")

    print(f"done: {len(items)} items")

if __name__ == "__main__":
    main()
