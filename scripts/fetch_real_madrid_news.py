"""
Real Madrid News Fetcher — 改善版
主な改善点:
  1. Claude API (claude-haiku) で記事を実際に日本語要約・翻訳
  2. 記事重要度スコアリング（移籍・負傷・試合結果を優先）
  3. published 空でもソース優先度で補完するソート
  4. X テキストを日本語タイトルで生成
  5. note.md のコメントも動的生成
  6. 除外ロジック強化（URL / タイトル両方でチェック）
  7. ANTHROPIC_API_KEY 未設定でもフォールバック動作
"""

import json
import os
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

# ─────────────────────────────────────────────
# 定数
# ─────────────────────────────────────────────
JST = timezone(timedelta(hours=9))
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-haiku-4-5-20251001"  # 軽量・高速

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0 Safari/537.36"
    )
}

# ─────────────────────────────────────────────
# キーワード定義
# ─────────────────────────────────────────────
INCLUDE_KEYWORDS = [
    "real madrid", "madridista", "los blancos", "rmcf",
    "bernabéu", "bernabeu",
    "ancelotti", "carlo ancelotti", "florentino", "florentino perez",
    "arbeloa", "alvaro arbeloa",
    "courtois", "lunin",
    "carvajal", "lucas vazquez", "rudiger", "rüdiger",
    "militao", "éder militão", "alaba", "mendy", "fran garcia", "huijsen",
    "bellingham", "camavinga", "tchouameni", "modric", "kroos",
    "valverde", "arda guler", "guler", "ceballos",
    "vinicius", "vinicius jr", "vini jr", "rodrygo",
    "mbappe", "mbappé", "endrick", "brahim", "joselu",
    "nico paz", "latasa",
    "el clasico", "clásico", "champions league", "la liga",
]

# タイトル・URL 両方でチェックする除外ワード
EXCLUDE_TITLE_KEYWORDS = [
    "real sociedad", "atletico madrid", "atléti", "barcelona",
    "girona", "osasuna", "sevilla", "villarreal",
    "athletic club", "athletic bilbao",
]

# 重要トピック（スコアアップ）
HIGH_PRIORITY_KEYWORDS = [
    "transfer", "signing", "injury", "injured", "ruled out",
    "contract", "sacked", "fired", "resign", "appointed",
    "win", "victory", "defeat", "draw", "goal", "hat-trick",
    "press conference", "official",
    "移籍", "負傷", "優勝", "得点",
]

# ソース優先度（高いほど優先）
SOURCE_PRIORITY = {
    "Real Madrid Official": 10,
    "Managing Madrid": 8,
    "Football España": 7,
    "AS": 6,
    "OneFootball": 5,
    "Sky Sports": 4,
    "ESPN": 3,
    "LaLiga": 3,
    "NewsNow": 2,
    "Football España Home": 6,
}


# ─────────────────────────────────────────────
# データクラス
# ─────────────────────────────────────────────
@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    published: Optional[str] = None
    summary: str = ""
    score: int = 0          # 内部スコア（出力には含めない）


# ─────────────────────────────────────────────
# ユーティリティ
# ─────────────────────────────────────────────
def now_jst() -> datetime:
    return datetime.now(timezone.utc).astimezone(JST)


def clean_text(text: str) -> str:
    text = BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


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


def trim_text(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def resolve_google_news_url(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        return r.url
    except Exception:
        return url


# ─────────────────────────────────────────────
# フィルタリング
# ─────────────────────────────────────────────
def is_relevant(title: str, summary: str = "") -> bool:
    hay = f"{title} {summary}".lower()

    # 除外チェック（タイトルのみ）
    title_lower = title.lower()
    if any(ex in title_lower for ex in EXCLUDE_TITLE_KEYWORDS):
        return False

    return any(k in hay for k in INCLUDE_KEYWORDS)


def calc_priority_score(item: NewsItem) -> int:
    """記事の重要度スコアを計算"""
    hay = f"{item.title} {item.summary}".lower()
    score = SOURCE_PRIORITY.get(item.source, 1)

    # 高優先キーワードでボーナス
    for kw in HIGH_PRIORITY_KEYWORDS:
        if kw in hay:
            score += 3

    # 公式サイトは信頼性ボーナス
    if "realmadrid.com" in item.link:
        score += 2

    return score


# ─────────────────────────────────────────────
# 記事本文取得
# ─────────────────────────────────────────────
def fetch_article_text(url: str, max_paragraphs: int = 6) -> str:
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


# ─────────────────────────────────────────────
# Claude API による要約・翻訳
# ─────────────────────────────────────────────
def call_claude(prompt: str, max_tokens: int = 400) -> str:
    """Claude Haiku API を呼び出して結果を返す"""
    if not ANTHROPIC_API_KEY:
        return ""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()
    except Exception as e:
        print(f"[WARN] Claude API call failed: {e}")
        return ""


def summarize_with_claude(title: str, article_text: str) -> Tuple[str, str]:
    """
    Claude で英語要約と日本語要約を生成。
    戻り値: (英語要約, 日本語要約)
    """
    if not article_text:
        return ("", "記事の詳細はリンク先で確認してください。")

    prompt = f"""You are a sports news assistant specializing in Real Madrid FC.

Article title: {title}
Article text: {article_text[:1500]}

Please respond with EXACTLY this JSON format (no markdown, no extra text):
{{
  "en": "English summary in 1-2 sentences, max 150 chars, factual and specific",
  "ja": "日本語要約を1〜2文で、150文字以内。具体的な内容を含めること"
}}"""

    result = call_claude(prompt, max_tokens=300)

    try:
        # JSON を抽出
        match = re.search(r'\{.*?\}', result, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            en = parsed.get("en", "").strip()
            ja = parsed.get("ja", "").strip()
            if en and ja:
                return (trim_text(en, 150), trim_text(ja, 150))
    except Exception:
        pass

    # パースに失敗した場合は result をそのまま日本語要約として使う
    if result:
        return ("", trim_text(result, 150))

    return ("", "記事の詳細はリンク先で確認してください。")


def translate_title_with_claude(title: str) -> str:
    """タイトルを日本語に翻訳（Claude使用）"""
    prompt = f"""Translate this Real Madrid news headline to natural Japanese. 
Respond with ONLY the Japanese translation, no explanation, no quotes.

Headline: {title}"""

    result = call_claude(prompt, max_tokens=100)
    return result if result else title


def generate_comment_with_claude(items: List[NewsItem]) -> str:
    """今日のニュース全体への総括コメントを生成"""
    titles = "\n".join(f"- {item.title}" for item in items[:10])
    prompt = f"""You are a Real Madrid news curator writing for Japanese fans.

Today's top Real Madrid news headlines:
{titles}

Write a 2-3 sentence overall comment in Japanese about today's Real Madrid news landscape.
Be specific about the main themes (e.g., injuries, transfers, match results, manager situation).
Respond with ONLY the comment, no preamble."""

    result = call_claude(prompt, max_tokens=200)
    return result if result else "本日もレアル・マドリードの最新情報をお届けします。引き続きチームの動向に注目していきましょう。"


# ─────────────────────────────────────────────
# フォールバック要約（Claude API なし）
# ─────────────────────────────────────────────
FALLBACK_JA_PATTERNS = [
    (["injury", "injured", "ruled out", "fitness"], "負傷・コンディション情報。選手の回復状況と今後の起用に注目。"),
    (["transfer", "signing", "deal", "contract"], "移籍・契約に関する情報。クラブの補強動向として押さえておきたい内容。"),
    (["press conference", "arbeloa", "manager", "coach"], "監督のプレスカンファレンス発言。チームの現状と今後の方針が語られている。"),
    (["win", "victory", "goal", "match result"], "試合結果に関するレポート。得点者やパフォーマンスの詳細が含まれる。"),
    (["draw", "equaliser", "equalizer"], "引き分けの結果に関する分析。勝ち点獲得機会を逃した背景を解説している。"),
    (["defeat", "loss", "lost"], "敗戦に関する振り返り。課題の整理と次戦への展望に注目。"),
    (["mbappe", "mbappé"], "エムバペに関する最新情報。チームへの影響とプレー状況が取り上げられている。"),
    (["vinicius", "vini"], "ヴィニシウスに関する最新情報。パフォーマンスや契約状況が話題となっている。"),
    (["bellingham"], "ベリンガムに関するニュース。チームの中心選手としての活躍と現況が報じられている。"),
    (["bernabeu", "bernabéu"], "サンティアゴ・ベルナベウに関する情報。スタジアムやクラブ施設の最新動向。"),
]


def fallback_ja_summary(title: str, en_text: str) -> str:
    hay = f"{title} {en_text}".lower()
    for keywords, summary in FALLBACK_JA_PATTERNS:
        if any(k in hay for k in keywords):
            return summary
    return "レアル・マドリードに関する最新情報。詳細はリンク先の記事でご確認ください。"


def build_summaries(item: NewsItem) -> Tuple[str, str]:
    """英語・日本語の要約を返す（Claude優先、フォールバックあり）"""
    base_text = clean_text(item.summary) if item.summary else ""

    if len(base_text) < 60:
        article_text = fetch_article_text(item.link)
        if article_text:
            base_text = article_text

    if ANTHROPIC_API_KEY:
        return summarize_with_claude(item.title, base_text)
    else:
        # API キーなし: 英語は先頭文のみ、日本語はパターンマッチ
        en = trim_text(base_text, 150) if base_text else "See link for details."
        ja = fallback_ja_summary(item.title, base_text)
        return (en, ja)


def get_ja_title(item: NewsItem) -> str:
    """日本語タイトルを取得（Claude優先）"""
    if ANTHROPIC_API_KEY:
        return translate_title_with_claude(item.title)
    return item.title  # フォールバック: 英語のまま


# ─────────────────────────────────────────────
# 重複排除・ソート
# ─────────────────────────────────────────────
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
    """
    スコア優先 → published日時 → ソース優先度の複合ソート
    published が空でもスコアで順位付けできる
    """
    for item in items:
        item.score = calc_priority_score(item)

    def sort_key(item: NewsItem):
        dt = parse_dt(item.published) or datetime(2000, 1, 1, tzinfo=timezone.utc)
        return (item.score, dt)

    return sorted(items, key=sort_key, reverse=True)


# ─────────────────────────────────────────────
# 多様性を確保した5件選択
# ─────────────────────────────────────────────
BAD_EXACT_URLS = {
    "https://www.managingmadrid.com",
    "https://www.football-espana.net",
    "https://en.as.com/soccer",
    "https://www.skysports.com/la-liga",
    "https://onefootball.com/en/competition/laliga-10",
    "https://www.laliga.com/laliga-easports",
    "https://www.newsnow.co.uk/h/?search=La%2BLiga&lang=a",
}

BAD_TITLE_PATTERNS = [
    "real madrid cf: news",
    "real madrid transfer news & rumors",
    "real madrid cf: champions league",
    "a real madrid community",
    "la liga news",
    "real madrid news",
    "more real madrid news",
    "atletico madrid news",
    "more atletico madrid news",
    "real madrid transfer news",
    "atletico madrid transfer news",
]

MANAGING_MADRID_CATEGORY_PATHS = [
    "/real-madrid-cf-news",
    "/real-madrid-cf-transfer-talk",
    "/real-madrid-cf-champions-league",
]


def normalize_url(url: str) -> str:
    return url.lower().strip().rstrip("/").replace("http://", "https://")


def get_domain(url: str) -> str:
    u = normalize_url(url).replace("https://", "")
    domain = u.split("/")[0]
    return domain[4:] if domain.startswith("www.") else domain


def is_bad_item(item: NewsItem) -> bool:
    link = normalize_url(item.link)
    title = item.title.lower()

    if link in {normalize_url(u) for u in BAD_EXACT_URLS}:
        return True

    if "managingmadrid.com/" in link:
        if any(path in link for path in MANAGING_MADRID_CATEGORY_PATHS) and "/20" not in link:
            return True

    if any(p in title for p in BAD_TITLE_PATTERNS):
        return True

    # ジャンク: 著者ページ・タグページ
    if re.search(r"/author/|/tag/|/category/|/page/", link):
        return True

    return False


def get_topic_group(item: NewsItem) -> str:
    link = normalize_url(item.link)
    for path in MANAGING_MADRID_CATEGORY_PATHS:
        if path in link:
            return f"managingmadrid:{path}"
    simplified = re.sub(r"[^a-z0-9]+", "", item.title.lower())
    return f"title:{simplified[:80]}"


def pick_diverse_items(items: List[NewsItem], limit: int = 5, max_per_domain: int = 2) -> List[NewsItem]:
    filtered = [item for item in items if not is_bad_item(item)]

    picked: List[NewsItem] = []
    domain_counts: dict = {}
    used_groups: set = set()

    for item in filtered:
        domain = get_domain(item.link)
        group = get_topic_group(item)
        if domain_counts.get(domain, 0) >= max_per_domain:
            continue
        if group in used_groups:
            continue
        picked.append(item)
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        used_groups.add(group)
        if len(picked) >= limit:
            return picked

    # 2周目: ドメイン制限を緩める
    for item in filtered:
        if item in picked:
            continue
        group = get_topic_group(item)
        if group in used_groups:
            continue
        picked.append(item)
        used_groups.add(group)
        if len(picked) >= limit:
            break

    return picked


# ─────────────────────────────────────────────
# フェッチ関数
# ─────────────────────────────────────────────
def fetch_realmadrid_official(limit: int = 12) -> List[NewsItem]:
    url = "https://www.realmadrid.com/en-US/news"
    html = get_html(url)
    soup = BeautifulSoup(html, "lxml")
    items: List[NewsItem] = []

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        text = clean_text(a.get_text(" ", strip=True))
        if not href or not text or "/news/" not in href or len(text) < 12:
            continue
        link = ("https://www.realmadrid.com" + href) if href.startswith("/") else href
        if not is_relevant(text, "") and "real madrid" not in link.lower():
            continue
        items.append(NewsItem(title=text, link=link, source="Real Madrid Official"))

    return dedupe_items(items)[:limit]


def fetch_managing_madrid(limit: int = 12) -> List[NewsItem]:
    url = "https://www.managingmadrid.com/"
    html = get_html(url)
    soup = BeautifulSoup(html, "lxml")
    items: List[NewsItem] = []

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        text = clean_text(a.get_text(" ", strip=True))
        if not href or not text or len(text) < 12:
            continue
        if "managingmadrid.com" not in href and not href.startswith("/"):
            continue
        link = ("https://www.managingmadrid.com" + href) if href.startswith("/") else href
        if link.rstrip("/") == "https://www.managingmadrid.com":
            continue
        if any(p in link.lower() for p in MANAGING_MADRID_CATEGORY_PATHS):
            continue
        if not is_relevant(text, ""):
            continue
        items.append(NewsItem(title=text, link=link, source="Managing Madrid"))

    return dedupe_items(items)[:limit]


def fetch_football_espana(limit: int = 12) -> List[NewsItem]:
    url = "https://www.football-espana.net/category/la-liga/real-madrid"
    html = get_html(url)
    soup = BeautifulSoup(html, "lxml")
    items: List[NewsItem] = []

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        text = clean_text(a.get_text(" ", strip=True))
        if not href or not text or len(text) < 12:
            continue
        if "football-espana.net" not in href and not href.startswith("/"):
            continue
        link = ("https://www.football-espana.net" + href) if href.startswith("/") else href
        if link.rstrip("/") == "https://www.football-espana.net":
            continue
        # /author/, /category/ ページを除外
        if re.search(r"/author/|/category/", link):
            continue
        if not is_relevant(text, ""):
            continue
        items.append(NewsItem(title=text, link=link, source="Football España"))

    return dedupe_items(items)[:limit]


def fetch_extra_sites() -> List[NewsItem]:
    sources = [
        ("Football España Home", "https://www.football-espana.net/", "https://www.football-espana.net"),
        ("AS", "https://en.as.com/soccer/", "https://en.as.com"),
        ("OneFootball", "https://onefootball.com/en/competition/laliga-10", "https://onefootball.com"),
        ("Sky Sports", "https://www.skysports.com/la-liga", "https://www.skysports.com"),
    ]

    extra_kw = [
        "madrid", "real madrid", "bellingham", "vinicius", "rodrygo",
        "mbappe", "mbappé", "valverde", "courtois", "ancelotti", "arbeloa",
        "camavinga", "tchouameni", "modric", "guler", "endrick", "bernabeu",
    ]

    items: List[NewsItem] = []

    for label, url, base in sources:
        try:
            html = get_html(url)
            soup = BeautifulSoup(html, "lxml")
            count = 0
            for a in soup.select("a[href]"):
                href = a.get("href", "").strip()
                text = clean_text(a.get_text(" ", strip=True))
                if not href or not text or len(text) < 10:
                    continue
                link = (base + href) if href.startswith("/") else href
                if not link.startswith("http"):
                    continue
                if re.search(r"/author/|/category/|/tag/|/page/", link):
                    continue
                if any(k in text.lower() for k in extra_kw):
                    if is_relevant(text, ""):
                        items.append(NewsItem(title=text, link=link, source=label))
                        count += 1
            print(f"[INFO] {label}: {count} items")
        except Exception as e:
            print(f"[WARN] {label} failed: {e}")

    return dedupe_items(items)[:40]


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

        items.append(NewsItem(
            title=title, link=link, source=label,
            published=published, summary=summary,
        ))
    return items


def collect_all_items() -> List[NewsItem]:
    all_items: List[NewsItem] = []

    for name, fn in [
        ("realmadrid_official", fetch_realmadrid_official),
        ("managing_madrid", fetch_managing_madrid),
        ("football_espana", fetch_football_espana),
    ]:
        try:
            items = fn()
            all_items.extend(items)
            print(f"[INFO] {name}: {len(items)} items")
            time.sleep(1)
        except Exception as e:
            print(f"[WARN] {name} failed: {e}")

    try:
        extra = fetch_extra_sites()
        all_items.extend(extra)
        time.sleep(1)
    except Exception as e:
        print(f"[WARN] extra sites failed: {e}")

    for label, query in [
        ("Google News / Real Madrid", "Real Madrid"),
        ("Google News / Managing Madrid", "Real Madrid site:managingmadrid.com"),
    ]:
        try:
            items = fetch_google_news_rss(query, label=label, limit=10)
            all_items.extend(items)
            time.sleep(1)
        except Exception as e:
            print(f"[WARN] RSS {label} failed: {e}")

    all_items = dedupe_items(all_items)
    all_items = [i for i in all_items if "news.google.com" not in i.link]
    all_items = sort_items(all_items)

    src_summary = {}
    for i in all_items:
        src_summary[i.source] = src_summary.get(i.source, 0) + 1
    print("[INFO] source counts:", src_summary)

    return all_items


# ─────────────────────────────────────────────
# 出力ビルダー
# ─────────────────────────────────────────────
NUMBERS = ["①", "②", "③", "④", "⑤"]


def build_note_md(items: List[NewsItem]) -> str:
    date_str = now_jst().strftime("%Y-%m-%d")
    lines = [f"📰 レアル・マドリードニュースまとめ（{date_str}）", ""]

    if not items:
        lines += [
            "本日は有力な更新を取得できませんでした。",
            "",
            "🧾 今日のまとめ",
            "",
            "今日は大きな更新が少ない一日でした。次回の更新をお待ちください。",
        ]
        return "\n".join(lines)

    top_items = pick_diverse_items(items, 5)

    for i, item in enumerate(top_items):
        print(f"[INFO] Building summary for: {item.title}")
        en_summary, ja_summary = build_summaries(item)
        ja_title = get_ja_title(item) if ANTHROPIC_API_KEY else item.title
        time.sleep(0.5)  # API レート制限対策

        lines += [
            f"{NUMBERS[i]} {item.title}",
            f"🇯🇵 {ja_title}",
            "",
            "🔗 リンク",
            item.link,
            "",
            "📝 要約（英語）",
            en_summary if en_summary else "See link for details.",
            "",
            "📝 要約（日本語）",
            ja_summary,
            "",
        ]

    # 総括コメント（Claude使用）
    comment = generate_comment_with_claude(top_items) if ANTHROPIC_API_KEY else \
        "本日もレアル・マドリードの最新情報をお届けしました。引き続きチームの動向に注目していきましょう。"

    lines += [
        "🧾 今日のまとめ",
        "",
        comment,
    ]
    return "\n".join(lines)


def build_x_text(items: List[NewsItem]) -> str:
    if not items:
        return "【レアル・マドリード】本日は主要ニュースを確認できませんでした。#RealMadrid"

    top_items = pick_diverse_items(items, 1)
    if not top_items:
        return "【レアル・マドリード】本日は主要ニュースを確認できませんでした。#RealMadrid"

    top = top_items[0]

    # 日本語タイトルを取得
    if ANTHROPIC_API_KEY:
        ja_title = translate_title_with_claude(top.title)
    else:
        ja_title = top.title

    # X は 280 文字制限。URL(23) + ハッシュタグ(12) + 冒頭(8) = 43 文字分確保
    title_trimmed = trim_text(ja_title, 230 - len(top.link))

    return f"【レアル速報】{title_trimmed}\n{top.link}\n#RealMadrid #レアルマドリード"


def build_json(items: List[NewsItem]) -> str:
    # score フィールドは出力に含めない
    def item_to_dict(item: NewsItem) -> dict:
        d = asdict(item)
        d.pop("score", None)
        return d

    payload = {
        "generated_at_jst": now_jst().isoformat(),
        "count": len(items),
        "items": [item_to_dict(i) for i in items],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────
# エントリポイント
# ─────────────────────────────────────────────
def main():
    if not ANTHROPIC_API_KEY:
        print("[WARN] ANTHROPIC_API_KEY not set. Running in fallback mode (no AI summarization).")

    items = collect_all_items()

    (OUTPUT_DIR / "note.md").write_text(build_note_md(items), encoding="utf-8")
    (OUTPUT_DIR / "x.txt").write_text(build_x_text(items), encoding="utf-8")
    (OUTPUT_DIR / "news.json").write_text(build_json(items), encoding="utf-8")

    print(f"done: {len(items)} items")


if __name__ == "__main__":
    main()
