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

KEYWORDS = [
    "real madrid", "madridista", "los blancos", "rmcf",
    "bernabéu", "bernabeu",
    "ancelotti", "carlo ancelotti", "florentino", "florentino perez",
    "courtois", "lunin",
    "carvajal", "lucas vazquez", "vazquez", "rudiger", "rüdiger",
    "militao", "éder militão", "alaba", "mendy", "fran garcia",
    "bellingham", "camavinga", "tchouameni", "modric", "kroos",
    "valverde", "arda guler", "guler", "ceballos",
    "vinicius", "vinicius jr", "vini jr", "rodrygo", "mbappe",
    "endrick", "brahim", "joselu",
    "nico paz", "latasa", "marvel",
    "real madrid vs", "madrid derby", "el clasico", "ucl",
    "champions league", "la liga",
]

EXCLUDE_KEYWORDS = [
    "real sociedad",
    "betis",
    "sevilla",
    "girona",
    "osasuna",
    "barcelona femeni",
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
        dt = dtparser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def is_relevant(title: str, summary: str = "") -> bool:
    hay = f"{title} {summary}".lower()

    if any(ex in hay for ex in EXCLUDE_KEYWORDS):
        return False

    return any(k in hay for k in KEYWORDS)


def normalize_title(title: str) -> str:
    title = title.lower()

    title = re.sub(r"\b(real madrid|report|rumor|rumour|official|update|news)\b", "", title)
    title = re.sub(r"\s*-\s*(managing madrid|football españa|football espana|real madrid).*?$", "", title)
    title = re.sub(r"[^a-z0-9]+", "", title)

    return title


def dedupe_items(items: List[NewsItem]) -> List[NewsItem]:
    seen_titles = set()
    seen_links = set()
    out = []

    for item in items:
        link_key = item.link.lower().strip().rstrip("/")
        title_key = normalize_title(item.title)

        if link_key in seen_links:
            continue

        if title_key and title_key in seen_titles:
            continue

        seen_links.add(link_key)
        if title_key:
            seen_titles.add(title_key)

        out.append(item)

    return out


def filter_recent_items(items: List[NewsItem], hours: int = 24) -> List[NewsItem]:
    now = now_jst()
    recent = []

    for item in items:
        dt = parse_dt(item.published)

        if not dt:
            recent.append(item)
            continue

        dt_jst = dt.astimezone(JST)
        if now - dt_jst <= timedelta(hours=hours):
            recent.append(item)

    return recent


def sort_items(items: List[NewsItem]) -> List[NewsItem]:
    def sort_key(item: NewsItem):
        dt = parse_dt(item.published) or datetime(1970, 1, 1, tzinfo=timezone.utc)

        score = 0
        if "realmadrid.com" in item.link:
            score -= 1
        if "managingmadrid.com" in item.link:
            score -= 0.5

        return (dt, score)

    return sorted(items, key=sort_key, reverse=True)


def trim_summary(text: str, max_len: int = 180) -> str:
    text = clean_text(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def fetch_article_text(url: str, max_paragraphs: int = 5) -> str:
    try:
        html = get_html(url, timeout=20)
        soup = BeautifulSoup(html, "lxml")

        for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
            tag.decompose()

        paragraphs = []
        for p in soup.select("p"):
            text = clean_text(p.get_text(" ", strip=True))
            if len(text) < 40:
                continue
            paragraphs.append(text)

        return " ".join(paragraphs[:max_paragraphs])
    except Exception as e:
        print(f"[WARN] fetch_article_text failed: {url} / {e}")
        return ""


def generate_summary(item: NewsItem, max_len: int = 230) -> str:
    base_text = clean_text(item.summary) if item.summary else ""

    if len(base_text) < 60:
        article_text = fetch_article_text(item.link)
        if article_text:
            base_text = article_text

    if not base_text:
        return "記事の詳細はリンク先で確認してください。"

    sentences = re.split(r"(?<=[。.!?])\s+", base_text)
    picked = []

    for s in sentences:
        s = clean_text(s)
        if len(s) < 20:
            continue
        picked.append(s)
        if len(" ".join(picked)) >= max_len:
            break

    result = " ".join(picked)

    if len(result) > max_len:
        result = result[: max_len - 1].rstrip() + "…"

    return result


def translate_title_simple(title: str) -> str:
    t = clean_text(title)

    rules = [
        ("New Bernabéu", "新ベルナベウに関するニュース"),
        ("Bernabéu", "ベルナベウに関するニュース"),
        ("training", "トレーニングに関するニュース"),
        ("Training", "トレーニングに関するニュース"),
        ("Valverde", "バルベルデに関するニュース"),
        ("Courtois", "クルトワに関するニュース"),
        ("Florentino Pérez", "フロレンティーノ・ペレス会長に関するニュース"),
        ("match", "試合に関するニュース"),
        ("Match", "試合に関するニュース"),
        ("injury", "負傷に関するニュース"),
        ("Injury", "負傷に関するニュース"),
        ("medical", "負傷・コンディションに関するニュース"),
        ("Medical", "負傷・コンディションに関するニュース"),
        ("transfer", "移籍に関するニュース"),
        ("Transfer", "移籍に関するニュース"),
        ("Champions League", "チャンピオンズリーグに関するニュース"),
        ("Endrick", "エンドリッキに関するニュース"),
        ("Vinicius", "ヴィニシウスに関するニュース"),
        ("Mbappe", "エムバペに関するニュース"),
        ("Bellingham", "ベリンガムに関するニュース"),
        ("Rodrygo", "ロドリゴに関するニュース"),
        ("Güler", "ギュレルに関するニュース"),
        ("Guler", "ギュレルに関するニュース"),
    ]

    for en, ja in rules:
        if en in t:
            return ja

    return "レアル・マドリード関連ニュース"


def translate_summary_simple(title: str, en_summary: str) -> str:
    text = clean_text(en_summary)
    tl = clean_text(title).lower()
    sl = text.lower()

    if "bernabeu" in tl or "bernabéu" in tl:
        return "ベルナベウに関する話題で、スタジアムの機能やクラブの将来性に注目が集まっている。収益面やブランド価値にも関わるテーマであり、今後のレアル・マドリードの成長戦略を考えるうえでも重要なニュース。"

    if "training" in tl or "train" in tl:
        return "チームは次戦に向けて調整を進めており、コンディションや戦術面の確認が主なポイントになっている。主力選手の状態や起用法にも関わるため、試合前の流れを把握するうえで押さえておきたい内容。"

    if "injury" in tl or "medical" in tl:
        return "負傷やコンディションに関する更新で、今後の起用や復帰時期にも注目したい内容。シーズン終盤や重要な試合が続く時期ほど、選手層やローテーションに大きく影響する可能性がある。"

    if "transfer" in tl or "rumor" in tl or "rumour" in tl:
        return "移籍や補強に関する話題で、今後のチーム編成や市場での動きに関わる内容として注目したい。若手の去就や主力選手の契約状況は、来季のレアル・マドリードの戦い方にも影響しそうだ。"

    if "match" in tl or "preview" in tl or "derby" in tl:
        return "試合に向けた見どころや状況を整理した内容で、チーム状態を把握するうえで押さえておきたい。相手との力関係だけでなく、選手起用や直近の流れも結果を左右するポイントになりそうだ。"

    if "champions league" in tl or "ucl" in tl:
        return "チャンピオンズリーグに関する話題で、試合内容やチームの戦い方を確認するうえで重要な内容。レアル・マドリードにとって欧州での結果はクラブ評価に直結するため、注目度の高いニュース。"

    if "real madrid" in sl:
        return "レアル・マドリードに関する重要トピックで、チームやクラブの動きを追ううえで確認しておきたい内容。選手の状態、監督方針、移籍市場の動きなど、今後の流れを読む材料になりそうだ。"

    return "この記事ではレアル・マドリードに関する主要な話題が扱われており、今後の動向を追ううえでも注目したい。チーム状況やクラブの判断を知る材料として、引き続きチェックしておきたい内容。"


def build_japanese_summary(item: NewsItem, max_len: int = 230) -> str:
    en_summary = generate_summary(item, max_len)
    return translate_summary_simple(item.title, en_summary)


def resolve_google_news_url(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        return r.url
    except Exception:
        return url


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

        if "news.google.com" in link:
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

        low_link = link.lower().rstrip("/")

        if low_link == "https://www.managingmadrid.com":
            continue

        category_paths = [
            "managingmadrid.com/real-madrid-cf-news",
            "managingmadrid.com/real-madrid-cf-transfer-talk",
            "managingmadrid.com/real-madrid-cf-champions-league",
        ]

        if any(path in low_link for path in category_paths) and "/20" not in low_link:
            continue

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

        if link.rstrip("/") == "https://www.football-espana.net":
            continue

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

    extra_keywords = [
        "madrid", "real madrid", "bellingham", "vinicius", "vini", "rodrygo",
        "mbappe", "valverde", "courtois", "ancelotti", "florentino",
        "camavinga", "tchouameni", "modric", "guler", "endrick", "bernabeu",
    ]

    bad_exact_urls = {
        "https://www.managingmadrid.com",
        "https://www.football-espana.net",
        "https://en.as.com/soccer",
        "https://www.skysports.com/la-liga",
        "https://onefootball.com/en/competition/laliga-10",
        "https://www.laliga.com/laliga-easports",
        "https://www.newsnow.co.uk/h/?search=La%2BLiga&lang=a",
    }

    items: List[NewsItem] = []

    for label, url, base in sources:
        try:
            html = get_html(url)
            soup = BeautifulSoup(html, "lxml")

            source_count = 0

            for a in soup.select("a[href]"):
                href = a.get("href", "").strip()
                text = clean_text(a.get_text(" ", strip=True))

                if not href or not text:
                    continue
                if len(text) < 10:
                    continue

                if href.startswith("/"):
                    link = base + href
                else:
                    link = href

                if link.rstrip("/") in bad_exact_urls:
                    continue

                if not link.startswith("http"):
                    continue

                hay = text.lower()
                if not any(k in hay for k in extra_keywords):
                    continue

                items.append(
                    NewsItem(
                        title=text,
                        link=link,
                        source=label,
                        published="",
                        summary="",
                    )
                )
                source_count += 1

            print(f"[INFO] {label}: {source_count} items")

        except Exception as e:
            print(f"[WARN] {label} failed: {e}")

    return dedupe_items(items)[:40]


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
        ("Google News / Real Madrid", "Real Madrid when:1d"),
        ("Google News / Managing Madrid", "Real Madrid site:managingmadrid.com when:1d"),
        ("Google News / Football España", "Real Madrid site:football-espana.net when:1d"),
        ("Google News / AS", "Real Madrid site:en.as.com when:1d"),
        ("Google News / Sky Sports", "Real Madrid site:skysports.com when:1d"),
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

    recent_items = filter_recent_items(all_items, 24)

    if len(recent_items) >= 3:
        all_items = recent_items
    else:
        print("[WARN] recent items are few. fallback to all collected items.")

    all_items = sort_items(all_items)

    source_summary = {}
    for item in all_items:
        source_summary[item.source] = source_summary.get(item.source, 0) + 1

    print("[INFO] source counts:", source_summary)

    return all_items


def pick_diverse_items(items: List[NewsItem], limit: int = 5, max_per_domain: int = 2) -> List[NewsItem]:
    bad_exact_urls = {
        "https://www.managingmadrid.com",
        "https://www.football-espana.net",
        "https://en.as.com/soccer",
        "https://www.skysports.com/la-liga",
        "https://onefootball.com/en/competition/laliga-10",
        "https://www.laliga.com/laliga-easports",
        "https://www.newsnow.co.uk/h/?search=La%2BLiga&lang=a",
    }

    def normalize_url(url: str) -> str:
        url = url.lower().strip().rstrip("/")
        url = url.replace("http://", "https://")
        return url

    def get_domain(url: str) -> str:
        u = normalize_url(url)
        u = u.replace("https://", "")
        domain = u.split("/")[0]
        if domain.startswith("www."):
            domain = domain[4:]
        return domain

    def get_topic_group(item: NewsItem) -> str:
        title = normalize_title(item.title)
        return f"title:{title[:80]}"

    def is_bad_item(item: NewsItem) -> bool:
        link = normalize_url(item.link)
        title = item.title.lower()

        if link in {normalize_url(u) for u in bad_exact_urls}:
            return True

        managing_categories = [
            "/real-madrid-cf-news",
            "/real-madrid-cf-transfer-talk",
            "/real-madrid-cf-champions-league",
        ]

        if "managingmadrid.com/" in link:
            if any(path in link for path in managing_categories) and "/20" not in link:
                return True

        bad_title_patterns = [
            "real madrid cf: news",
            "real madrid transfer news & rumors",
            "real madrid transfer news & rumours",
            "real madrid cf: champions league",
            "a real madrid community",
            "la liga news",
        ]

        if any(p in title for p in bad_title_patterns):
            return True

        return False

    filtered = [item for item in items if not is_bad_item(item)]
    filtered = sort_items(filtered)

    picked = []
    domain_counts = {}
    used_groups = set()

    for item in filtered:
        domain = get_domain(item.link)
        topic_group = get_topic_group(item)

        if domain_counts.get(domain, 0) >= max_per_domain:
            continue
        if topic_group in used_groups:
            continue

        picked.append(item)
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        used_groups.add(topic_group)

        if len(picked) >= limit:
            return picked

    for item in filtered:
        if item in picked:
            continue

        topic_group = get_topic_group(item)
        if topic_group in used_groups:
            continue

        picked.append(item)
        used_groups.add(topic_group)

        if len(picked) >= limit:
            break

    return picked


def build_note_md(items: List[NewsItem]) -> str:
    date_str = now_jst().strftime("%Y-%m-%d")
    lines = [
        f"📰 レアル・マドリードニュースまとめ（{date_str}）",
        "",
    ]

    if not items:
        lines += [
            "本日は有力な更新を取得できませんでした。",
            "",
            "🧾 記事全体のコメント",
            "",
            "今日は大きな更新が少ない一日でした。主要メディアを中心に引き続き確認していきます。",
        ]
        return "\n".join(lines)

    top_items = pick_diverse_items(items, 5)
    number_map = ["①", "②", "③", "④", "⑤"]

    for i, item in enumerate(top_items):
        ja_summary = build_japanese_summary(item, 230)
        ja_title = translate_title_simple(item.title)

        lines += [
            f"{number_map[i]} **{item.title}**",
            f"**{ja_title}**",
            "",
            "🔗 リンク",
            item.link,
            "",
            "📝 要約（日本語）",
            ja_summary,
            "",
        ]

    lines += [
        "🧾 記事全体のコメント",
        "",
        "今日もレアル・マドリード関連の動きは多く、チーム状況・選手評価・クラブの方向性まで幅広く追う必要がある一日。公式発表だけでなく、専門メディアやリーガ全体の視点も合わせて見ることで、今後の流れがより立体的に見えてくる。",
    ]

    return "\n".join(lines)


def build_x_text(items: List[NewsItem]) -> str:
    if not items:
        return "⚽レアル・マドリード最新ニュース\n\n本日は主要ニュースを確認できませんでした。\n\n▼noteで詳細\n\n#レアルマドリード #realmadrid #laliga"

    top_items = pick_diverse_items(items, 4)

    bullets = []
    for item in top_items:
        title = trim_summary(item.title, 34)
        bullets.append(f"・{title}")

    return (
        "⚽レアル・マドリード最新ニュース\n\n"
        + "\n".join(bullets)
        + "\n\n"
        + "移籍、負傷、チーム状況まで要チェック。\n"
        + "今のレアルは大きな分岐点にいる。\n\n"
        + "▼noteで詳細\n\n"
        + "#レアルマドリード #realmadrid #laliga"
    )


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

KEYWORDS = [
    "real madrid", "madridista", "los blancos", "rmcf",
    "bernabéu", "bernabeu",
    "ancelotti", "carlo ancelotti", "florentino", "florentino perez",
    "courtois", "lunin",
    "carvajal", "lucas vazquez", "vazquez", "rudiger", "rüdiger",
    "militao", "éder militão", "alaba", "mendy", "fran garcia",
    "bellingham", "camavinga", "tchouameni", "modric", "kroos",
    "valverde", "arda guler", "guler", "ceballos",
    "vinicius", "vinicius jr", "vini jr", "rodrygo", "mbappe",
    "endrick", "brahim", "joselu",
    "nico paz", "latasa", "marvel",
    "real madrid vs", "madrid derby", "el clasico", "ucl",
    "champions league", "la liga",
]

EXCLUDE_KEYWORDS = [
    "real sociedad",
    "betis",
    "sevilla",
    "girona",
    "osasuna",
    "barcelona femeni",
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
        dt = dtparser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def is_relevant(title: str, summary: str = "") -> bool:
    hay = f"{title} {summary}".lower()

    if any(ex in hay for ex in EXCLUDE_KEYWORDS):
        return False

    return any(k in hay for k in KEYWORDS)


def normalize_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r"\s*-\s*(managing madrid|football españa|real madrid).*?$", "", title)
    title = re.sub(r"[^a-z0-9]+", "", title)
    return title


def dedupe_items(items: List[NewsItem]) -> List[NewsItem]:
    seen_titles = set()
    seen_links = set()
    out = []

    for item in items:
        link_key = item.link.lower().strip().rstrip("/")
        title_key = normalize_title(item.title)

        if link_key in seen_links:
            continue

        if title_key and title_key in seen_titles:
            continue

        seen_links.add(link_key)
        if title_key:
            seen_titles.add(title_key)

        out.append(item)

    return out


def filter_recent_items(items: List[NewsItem], hours: int = 24) -> List[NewsItem]:
    now = now_jst()
    recent = []

    for item in items:
        dt = parse_dt(item.published)
        if not dt:
            continue

        dt_jst = dt.astimezone(JST)
        if now - dt_jst <= timedelta(hours=hours):
            recent.append(item)

    return recent


def sort_items(items: List[NewsItem]) -> List[NewsItem]:
    def sort_key(item: NewsItem):
        dt = parse_dt(item.published) or datetime(1970, 1, 1, tzinfo=timezone.utc)

        score = 0
        if "realmadrid.com" in item.link:
            score -= 1
        if "managingmadrid.com" in item.link:
            score -= 0.5

        return (dt, score)

    return sorted(items, key=sort_key, reverse=True)


def trim_summary(text: str, max_len: int = 180) -> str:
    text = clean_text(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def fetch_article_text(url: str, max_paragraphs: int = 5) -> str:
    try:
        html = get_html(url, timeout=20)
        soup = BeautifulSoup(html, "lxml")

        for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
            tag.decompose()

        paragraphs = []
        for p in soup.select("p"):
            text = clean_text(p.get_text(" ", strip=True))
            if len(text) < 40:
                continue
            paragraphs.append(text)

        return " ".join(paragraphs[:max_paragraphs])
    except Exception as e:
        print(f"[WARN] fetch_article_text failed: {url} / {e}")
        return ""


def generate_summary(item: NewsItem, max_len: int = 230) -> str:
    base_text = clean_text(item.summary) if item.summary else ""

    if len(base_text) < 60:
        article_text = fetch_article_text(item.link)
        if article_text:
            base_text = article_text

    if not base_text:
        return "記事の詳細はリンク先で確認してください。"

    sentences = re.split(r"(?<=[。.!?])\s+", base_text)
    picked = []

    for s in sentences:
        s = clean_text(s)
        if len(s) < 20:
            continue
        picked.append(s)
        if len(" ".join(picked)) >= max_len:
            break

    result = " ".join(picked)

    if len(result) > max_len:
        result = result[: max_len - 1].rstrip() + "…"

    return result


def translate_title_simple(title: str) -> str:
    t = clean_text(title)

    rules = [
        ("New Bernabéu", "新ベルナベウに関するニュース"),
        ("Bernabéu", "ベルナベウに関するニュース"),
        ("training", "トレーニングに関するニュース"),
        ("Training", "トレーニングに関するニュース"),
        ("Valverde", "バルベルデに関するニュース"),
        ("Courtois", "クルトワに関するニュース"),
        ("Florentino Pérez", "フロレンティーノ・ペレス会長に関するニュース"),
        ("match", "試合に関するニュース"),
        ("Match", "試合に関するニュース"),
        ("injury", "負傷に関するニュース"),
        ("Injury", "負傷に関するニュース"),
        ("medical", "負傷・コンディションに関するニュース"),
        ("Medical", "負傷・コンディションに関するニュース"),
        ("transfer", "移籍に関するニュース"),
        ("Transfer", "移籍に関するニュース"),
        ("Champions League", "チャンピオンズリーグに関するニュース"),
        ("Endrick", "エンドリッキに関するニュース"),
        ("Vinicius", "ヴィニシウスに関するニュース"),
        ("Mbappe", "エムバペに関するニュース"),
        ("Bellingham", "ベリンガムに関するニュース"),
        ("Rodrygo", "ロドリゴに関するニュース"),
        ("Güler", "ギュレルに関するニュース"),
        ("Guler", "ギュレルに関するニュース"),
    ]

    for en, ja in rules:
        if en in t:
            return ja

    return "レアル・マドリード関連ニュース"


def translate_summary_simple(title: str, en_summary: str) -> str:
    text = clean_text(en_summary)
    tl = clean_text(title).lower()
    sl = text.lower()

    if "bernabeu" in tl or "bernabéu" in tl:
        return "ベルナベウに関する話題で、スタジアムの機能やクラブの将来性に注目が集まっている。収益面やブランド価値にも関わるテーマであり、今後のレアル・マドリードの成長戦略を考えるうえでも重要なニュース。"

    if "training" in tl or "train" in tl:
        return "チームは次戦に向けて調整を進めており、コンディションや戦術面の確認が主なポイントになっている。主力選手の状態や起用法にも関わるため、試合前の流れを把握するうえで押さえておきたい内容。"

    if "injury" in tl or "medical" in tl:
        return "負傷やコンディションに関する更新で、今後の起用や復帰時期にも注目したい内容。シーズン終盤や重要な試合が続く時期ほど、選手層やローテーションに大きく影響する可能性がある。"

    if "transfer" in tl or "rumor" in tl or "rumour" in tl:
        return "移籍や補強に関する話題で、今後のチーム編成や市場での動きに関わる内容として注目したい。若手の去就や主力選手の契約状況は、来季のレアル・マドリードの戦い方にも影響しそうだ。"

    if "match" in tl or "preview" in tl or "derby" in tl:
        return "試合に向けた見どころや状況を整理した内容で、チーム状態を把握するうえで押さえておきたい。相手との力関係だけでなく、選手起用や直近の流れも結果を左右するポイントになりそうだ。"

    if "champions league" in tl or "ucl" in tl:
        return "チャンピオンズリーグに関する話題で、試合内容やチームの戦い方を確認するうえで重要な内容。レアル・マドリードにとって欧州での結果はクラブ評価に直結するため、注目度の高いニュース。"

    if "real madrid" in sl:
        return "レアル・マドリードに関する重要トピックで、チームやクラブの動きを追ううえで確認しておきたい内容。選手の状態、監督方針、移籍市場の動きなど、今後の流れを読む材料になりそうだ。"

    return "この記事ではレアル・マドリードに関する主要な話題が扱われており、今後の動向を追ううえでも注目したい。チーム状況やクラブの判断を知る材料として、引き続きチェックしておきたい内容。"


def build_japanese_summary(item: NewsItem, max_len: int = 230) -> str:
    en_summary = generate_summary(item, max_len)
    return translate_summary_simple(item.title, en_summary)


def resolve_google_news_url(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        return r.url
    except Exception:
        return url


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

        if "news.google.com" in link:
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

        low_link = link.lower().rstrip("/")

        if low_link == "https://www.managingmadrid.com":
            continue

        category_paths = [
            "managingmadrid.com/real-madrid-cf-news",
            "managingmadrid.com/real-madrid-cf-transfer-talk",
            "managingmadrid.com/real-madrid-cf-champions-league",
        ]

        if any(path in low_link for path in category_paths) and "/20" not in low_link:
            continue

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

        if link.rstrip("/") == "https://www.football-espana.net":
            continue

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

    extra_keywords = [
        "madrid", "real madrid", "bellingham", "vinicius", "vini", "rodrygo",
        "mbappe", "valverde", "courtois", "ancelotti", "florentino",
        "camavinga", "tchouameni", "modric", "guler", "endrick", "bernabeu",
    ]

    bad_exact_urls = {
        "https://www.managingmadrid.com",
        "https://www.football-espana.net",
        "https://en.as.com/soccer",
        "https://www.skysports.com/la-liga",
        "https://onefootball.com/en/competition/laliga-10",
        "https://www.laliga.com/laliga-easports",
        "https://www.newsnow.co.uk/h/?search=La%2BLiga&lang=a",
    }

    items: List[NewsItem] = []

    for label, url, base in sources:
        try:
            html = get_html(url)
            soup = BeautifulSoup(html, "lxml")

            source_count = 0

            for a in soup.select("a[href]"):
                href = a.get("href", "").strip()
                text = clean_text(a.get_text(" ", strip=True))

                if not href or not text:
                    continue
                if len(text) < 10:
                    continue

                if href.startswith("/"):
                    link = base + href
                else:
                    link = href

                if link.rstrip("/") in bad_exact_urls:
                    continue

                if not link.startswith("http"):
                    continue

                hay = text.lower()
                if not any(k in hay for k in extra_keywords):
                    continue

                items.append(
                    NewsItem(
                        title=text,
                        link=link,
                        source=label,
                        published="",
                        summary="",
                    )
                )
                source_count += 1

            print(f"[INFO] {label}: {source_count} items")

        except Exception as e:
            print(f"[WARN] {label} failed: {e}")

    return dedupe_items(items)[:40]


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
        ("Google News / Real Madrid", "Real Madrid when:1d"),
        ("Google News / Managing Madrid", "Real Madrid site:managingmadrid.com when:1d"),
        ("Google News / Football España", "Real Madrid site:football-espana.net when:1d"),
        ("Google News / AS", "Real Madrid site:en.as.com when:1d"),
        ("Google News / Sky Sports", "Real Madrid site:skysports.com when:1d"),
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

    recent_items = filter_recent_items(all_items, 24)

    if len(recent_items) >= 3:
        all_items = recent_items
    else:
        print("[WARN] recent items are few. fallback to all collected items.")

    all_items = sort_items(all_items)

    source_summary = {}
    for item in all_items:
        source_summary[item.source] = source_summary.get(item.source, 0) + 1

    print("[INFO] source counts:", source_summary)

    return all_items


def pick_diverse_items(items: List[NewsItem], limit: int = 5, max_per_domain: int = 2) -> List[NewsItem]:
    bad_exact_urls = {
        "https://www.managingmadrid.com",
        "https://www.football-espana.net",
        "https://en.as.com/soccer",
        "https://www.skysports.com/la-liga",
        "https://onefootball.com/en/competition/laliga-10",
        "https://www.laliga.com/laliga-easports",
        "https://www.newsnow.co.uk/h/?search=La%2BLiga&lang=a",
    }

    def normalize_url(url: str) -> str:
        url = url.lower().strip().rstrip("/")
        url = url.replace("http://", "https://")
        return url

    def get_domain(url: str) -> str:
        u = normalize_url(url)
        u = u.replace("https://", "")
        domain = u.split("/")[0]
        if domain.startswith("www."):
            domain = domain[4:]
        return domain

    def get_topic_group(item: NewsItem) -> str:
        title = item.title.lower()
        simplified_title = re.sub(r"[^a-z0-9]+", "", title)
        return f"title:{simplified_title[:80]}"

    def is_bad_item(item: NewsItem) -> bool:
        link = normalize_url(item.link)
        title = item.title.lower()

        if link in {normalize_url(u) for u in bad_exact_urls}:
            return True

        managing_categories = [
            "/real-madrid-cf-news",
            "/real-madrid-cf-transfer-talk",
            "/real-madrid-cf-champions-league",
        ]

        if "managingmadrid.com/" in link:
            if any(path in link for path in managing_categories) and "/20" not in link:
                return True

        bad_title_patterns = [
            "real madrid cf: news",
            "real madrid transfer news & rumors",
            "real madrid cf: champions league",
            "a real madrid community",
            "la liga news",
        ]

        if any(p in title for p in bad_title_patterns):
            return True

        return False

    filtered = [item for item in items if not is_bad_item(item)]
    filtered = sort_items(filtered)

    picked = []
    domain_counts = {}
    used_groups = set()

    for item in filtered:
        domain = get_domain(item.link)
        topic_group = get_topic_group(item)

        if domain_counts.get(domain, 0) >= max_per_domain:
            continue
        if topic_group in used_groups:
            continue

        picked.append(item)
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        used_groups.add(topic_group)

        if len(picked) >= limit:
            return picked

    for item in filtered:
        if item in picked:
            continue

        topic_group = get_topic_group(item)
        if topic_group in used_groups:
            continue

        picked.append(item)
        used_groups.add(topic_group)

        if len(picked) >= limit:
            break

    return picked


def build_note_md(items: List[NewsItem]) -> str:
    date_str = now_jst().strftime("%Y-%m-%d")
    lines = [
        f"📰 レアル・マドリードニュースまとめ（{date_str}）",
        "",
    ]

    if not items:
        lines += [
            "本日は有力な更新を取得できませんでした。",
            "",
            "🧾 記事全体のコメント",
            "",
            "今日は大きな更新が少ない一日でした。主要メディアを中心に引き続き確認していきます。",
        ]
        return "\n".join(lines)

    top_items = pick_diverse_items(items, 5)
    number_map = ["①", "②", "③", "④", "⑤"]

    for i, item in enumerate(top_items):
        ja_summary = build_japanese_summary(item, 230)
        ja_title = translate_title_simple(item.title)

        lines += [
            f"{number_map[i]} **{item.title}**",
            f"**{ja_title}**",
            "",
            "🔗 リンク",
            item.link,
            "",
            "📝 要約（日本語）",
            ja_summary,
            "",
        ]

    lines += [
        "🧾 記事全体のコメント",
        "",
        "今日もレアル・マドリード関連の動きは多く、チーム状況・選手評価・クラブの方向性まで幅広く追う必要がある一日。公式発表だけでなく、専門メディアやリーガ全体の視点も合わせて見ることで、今後の流れがより立体的に見えてくる。",
    ]

    return "\n".join(lines)


def build_x_text(items: List[NewsItem]) -> str:
    if not items:
        return "⚽レアル・マドリード最新ニュース\n\n本日は主要ニュースを確認できませんでした。\n\n▼noteで詳細\n\n#レアルマドリード #realmadrid #laliga"

    top_items = pick_diverse_items(items, 4)

    bullets = []
    for item in top_items:
        title = trim_summary(item.title, 34)
        bullets.append(f"・{title}")

    return (
        "⚽レアル・マドリード最新ニュース\n\n"
        + "\n".join(bullets)
        + "\n\n"
        + "移籍、負傷、チーム状況まで要チェック。\n"
        + "今のレアルは大きな分岐点にいる。\n\n"
        + "▼noteで詳細\n\n"
        + "#レアルマドリード #realmadrid #laliga"
    )


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
