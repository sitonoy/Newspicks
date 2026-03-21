#!/usr/bin/env python3
"""
Newspick — GitHub Models API 版
毎日 AI 関連ニュースを RSS/arXiv から収集し、GitHub Models で和訳・要約・分類、
Notion にトグル形式で転記する。外部ライブラリ不要。

起動（即時実行）: python scripts/newspick.py --now
起動（デーモン）: python scripts/newspick.py
"""

import datetime
import email.utils
import json
import logging
import os
import signal
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

# ── パス ──────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ENV_FILE   = Path.home() / ".config" / "newspick" / ".env"
LOG_FILE   = SCRIPT_DIR / "newspick.log"
PID_FILE   = SCRIPT_DIR / "newspick.pid"

# ── .env 読み込み ─────────────────────────────────────────────────
def _load_env(path: Path) -> None:
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

_load_env(ENV_FILE)

# ── 設定 ──────────────────────────────────────────────────────────
AI_API_TOKEN       = os.environ.get("AI_API_TOKEN", "").strip()
NOTION_API_KEY     = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
NOTION_VERSION     = "2022-06-28"
SCHEDULE_TIME      = os.environ.get("SCHEDULE_TIME", "08:30")
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "30"))
AI_MODEL           = os.environ.get("AI_MODEL", "gpt-4o-mini")
AI_ENDPOINT        = os.environ.get("AI_ENDPOINT", "https://models.inference.ai.azure.com/chat/completions")

# JST タイムゾーン
_JST = datetime.timezone(datetime.timedelta(hours=9))

# ── ロギング ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("newspick")

# ── バリデーション ─────────────────────────────────────────────────
def _validate() -> bool:
    missing = [k for k, v in {
        "AI_API_TOKEN":       AI_API_TOKEN,
        "NOTION_API_KEY":     NOTION_API_KEY,
        "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
    }.items() if not v]
    if missing:
        log.error(f"未設定の必須項目: {', '.join(missing)}")
        return False
    return True

# ── HTTP ユーティリティ ────────────────────────────────────────────
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Newspick/3.0)"}

def _get(url: str, timeout: int = 20) -> str:
    req = Request(url, headers=_HEADERS)
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt < 2:
                log.warning(f"リトライ {attempt+1}/3 [{url}]: {e}")
                time.sleep(2)
            else:
                log.error(f"取得失敗 [{url}]: {e}")
                return ""

# ── 日付パーサー ──────────────────────────────────────────────────
def _parse_date(date_str: str) -> datetime.date | None:
    """RSS の各種日付形式を JST の date に変換"""
    if not date_str:
        return None
    # RFC 2822 (例: Thu, 20 Mar 2026 03:00:00 +0000)
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        return dt.astimezone(_JST).date()
    except Exception:
        pass
    # ISO 8601 (例: 2026-03-20T03:00:00Z)
    try:
        dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.astimezone(_JST).date()
    except Exception:
        pass
    return None

def filter_24h(articles: list[dict]) -> list[dict]:
    """過去24時間以内の記事のみ返す。なければ全件返す"""
    cutoff = datetime.datetime.now(_JST) - datetime.timedelta(hours=24)
    recent = []
    for a in articles:
        d = _parse_date(a.get("published", ""))
        if d and d >= cutoff.date():
            recent.append(a)
    if recent:
        log.info(f"過去24h の記事: {len(recent)} 件（全 {len(articles)} 件中）")
        return recent
    log.info("過去24h の記事なし → 最新記事を使用")
    return articles

# ── RSS パーサー ──────────────────────────────────────────────────
_ATOM_NS = "http://www.w3.org/2005/Atom"

def _extract_feed_title(content: str) -> str | None:
    """RSS/Atom フィードのタイトルを取得する"""
    if not content:
        return None
    try:
        root = ET.fromstring(content)
        # RSS 2.0
        el = root.find("channel/title")
        if el is not None and el.text:
            return el.text.strip()
        # Atom
        el = root.find(f"{{{_ATOM_NS}}}title")
        if el is not None and el.text:
            return el.text.strip()
    except Exception:
        pass
    return None

def _parse_rss(content: str, source: str, max_items: int = 5) -> list[dict]:
    if not content:
        return []
    try:
        root  = ET.fromstring(content)
        items = root.findall(".//item") or root.findall(f".//{{{_ATOM_NS}}}entry")
        results = []
        for item in items[:max_items]:
            def _t(tag: str) -> str:
                el = item.find(tag) or item.find(f"{{{_ATOM_NS}}}{tag}")
                if el is None:
                    return ""
                if tag == "link" and not (el.text or "").strip():
                    return el.get("href", "")
                return (el.text or "").strip()
            title = _t("title")
            url   = _t("link") or _t("id")
            pub   = _t("pubDate") or _t("updated") or _t("published")
            desc  = _t("description") or _t("summary") or _t("content")
            if len(desc) > 500:
                desc = desc[:500] + "..."
            if title and url:
                results.append({"title": title, "url": url, "published": pub,
                                 "description": desc, "source": source})
        return results
    except ET.ParseError as e:
        log.warning(f"RSS パースエラー ({source}): {e}")
        return []

# ── arXiv API ────────────────────────────────────────────────────
def _fetch_arxiv(max_results: int = 5) -> list[dict]:
    url = (
        "http://export.arxiv.org/api/query"
        f"?search_query=cat:cs.AI&start=0&max_results={max_results}"
        "&sortBy=submittedDate&sortOrder=descending"
    )
    content = _get(url)
    if not content:
        return []
    try:
        root = ET.fromstring(content)
        results = []
        for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
            def _t(tag: str) -> str:
                el = entry.find(f"{{{_ATOM_NS}}}{tag}")
                return (el.text or "").strip() if el is not None else ""
            title   = _t("title").replace("\n", " ")
            link    = _t("id")
            pub     = _t("published")
            summary = _t("summary")[:400]
            if title and link:
                results.append({"title": title, "url": link, "published": pub,
                                 "description": summary, "source": "arXiv cs.AI"})
        return results
    except Exception as e:
        log.warning(f"arXiv パースエラー: {e}")
        return []

# ── ニュース収集 ───────────────────────────────────────────────────
_RSS_SOURCES = [
    ("TechCrunch AI",  "https://techcrunch.com/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/feed/"),
    ("The Verge AI",   "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("Wired AI",       "https://www.wired.com/feed/rss"),
]

_GOOGLE_ALERTS = [
    "https://www.google.co.jp/alerts/feeds/07966966265337213514/5429403290748738893",
    "https://www.google.co.jp/alerts/feeds/07966966265337213514/5429403290748735905",
    "https://www.google.co.jp/alerts/feeds/07966966265337213514/4912133052765876976",
    "https://www.google.co.jp/alerts/feeds/07966966265337213514/4912133052765876727",
    "https://www.google.co.jp/alerts/feeds/07966966265337213514/12790639888723147772",
    "https://www.google.co.jp/alerts/feeds/07966966265337213514/18339811749606432890",
]

def collect_articles() -> list[dict]:
    all_articles = []

    for name, url in _RSS_SOURCES:
        log.info(f"  収集: {name}")
        articles = _parse_rss(_get(url), source=name)
        all_articles.extend(articles)
        log.info(f"    → {len(articles)} 件")

    for url in _GOOGLE_ALERTS:
        content  = _get(url)
        name     = _extract_feed_title(content) or url.split("/")[-1]
        articles = _parse_rss(content, source=name)
        all_articles.extend(articles)
        log.info(f"  収集: {name} → {len(articles)} 件")

    log.info("  収集: arXiv cs.AI")
    arxiv = _fetch_arxiv(5)
    all_articles.extend(arxiv)
    log.info(f"    → {len(arxiv)} 件")

    log.info(f"合計 {len(all_articles)} 件収集完了")
    return filter_24h(all_articles)

# ── AI 分析（GitHub Models） ──────────────────────────────────────
_ANALYSIS_PROMPT = """\
あなたはAI業界のアナリストです。以下の記事リストを日本語に翻訳・分析し、JSONで出力してください。

## 入力記事
{articles_json}

## 出力（JSONのみ。説明・コードブロック不要）
{{
  "daily_summary": "本日のAI業界の重要トピックを3〜5文で日本語まとめ。最後の1文では「どの業界・職種・企業規模の人が」「どのような具体的な変化（コスト構造・競争優位・業務プロセス・意思決定）を迫られるか」を断言する。「重要な影響を与える」「注目が高まる」「動向を注視すべき」のような抽象的・傍観者的な終わり方は不可。",
  "articles": [
    {{
      "title_ja": "日本語タイトル",
      "url": "記事URL（変更しない）",
      "source": "ソース名（変更しない）",
      "published": "公開日（変更しない）",
      "summary_ja": "日本語要約（3文以内）",
      "business_use": "この技術・動向が実際のビジネスでどう使えるかの具体的仮説。「何の業務・プロセスに」「どのように組み込むと」「どんな効果が期待できるか」を1〜2文で断言する。表面的な「活用できる」は不可。業界・職種・規模感を想定して具体的に書く（例: 大手製造業の品質管理部門がXXXに適用するとYYYが削減できる、など）。該当なければ空文字。",
      "category": "LLM・基盤モデル | 画像・マルチモーダル | エージェント・自動化 | 規制・政策・倫理 | 研究・論文 | ビジネス・投資",
      "impact": "High | Medium | Low",
      "impact_reason": "誰に（どの業界・職種・企業規模の人が）、何のインパクトを受けるかを具体的に1文で書く。「業界に影響」のような抽象表現は不可。例: 「LLM APIを使うSaaS企業のコスト構造が変わり、競合優位の源泉がモデル選定から応用設計にシフトする」"
    }}
  ],
  "trend_summary": {{
    "themes": ["主要テーマ1", "主要テーマ2", "主要テーマ3"],
    "business_insight": "今日のAIビジネス活用における示唆（1〜2文）"
  }}
}}

## 判定基準
- High: 業界全体の競争構造・コスト・規制に直接影響する重大ニュース
- Medium: 特定セクターや職種において戦略・業務の見直しが必要になる動向
- Low: 技術トレンドの把握・将来予測に有用な情報
"""

def analyze_with_ai(articles: list[dict]) -> dict | None:
    if not AI_API_TOKEN:
        log.warning("AI_API_TOKEN 未設定 → AI 分析をスキップ")
        return None
    prompt = _ANALYSIS_PROMPT.format(
        articles_json=json.dumps(articles, ensure_ascii=False, indent=2)
    )
    body = json.dumps({
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 8192,
    }).encode()
    req = Request(AI_ENDPOINT, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AI_API_TOKEN}",
    })
    for attempt in range(3):
        try:
            with urlopen(req, timeout=60) as r:
                resp = json.loads(r.read())
            text = resp["choices"][0]["message"]["content"].strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            result = json.loads(text)
            log.info(f"AI 分析完了: {len(result.get('articles', []))} 件")
            return result
        except Exception as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")  # type: ignore
            except Exception:
                pass
            if attempt < 2:
                wait = 15 * (attempt + 1)
                log.warning(f"AI API リトライ {attempt+1}/3 ({wait}秒待機): {e} | {err_body[:200]}")
                time.sleep(wait)
            else:
                log.error(f"AI API 失敗: {e} | {err_body[:400]}")
                return None

# ── Notion API ────────────────────────────────────────────────────
def _notion(method: str, endpoint: str, body: dict | None = None) -> dict:
    url  = f"https://api.notion.com/v1/{endpoint}"
    data = json.dumps(body).encode() if body else None
    req  = Request(url, data=data, method=method, headers={
        "Authorization":  f"Bearer {NOTION_API_KEY}",
        "Content-Type":   "application/json",
        "Notion-Version": NOTION_VERSION,
    })
    for attempt in range(3):
        try:
            with urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except URLError as e:
            if attempt < 2:
                log.warning(f"Notion API リトライ {attempt+1}/3: {e}")
                time.sleep(1)
            else:
                raise

def _create_page() -> str:
    today    = datetime.datetime.now(_JST).date()
    today_jp = today.strftime("%Y年%m月%d日")
    result   = _notion("POST", "pages", {
        "parent":     {"database_id": NOTION_DATABASE_ID},
        "icon":       {"type": "emoji", "emoji": "📰"},
        "properties": {
            "名前": {"title": [{"text": {"content": f"{today_jp} AI ニュース"}}]},
        },
    })
    page_id  = result["id"]
    log.info(f"Notion ページ作成: https://notion.so/{page_id.replace('-', '')}")
    return page_id

# ── Notion ブロックビルダー ────────────────────────────────────────
def _rich(text: str) -> list:
    return [{"type": "text", "text": {"content": text[:2000]}}]

def _rich_link(text: str, url: str) -> list:
    return [{"type": "text", "text": {"content": text[:2000], "link": {"url": url}}}]

def _h1(text: str) -> dict:
    return {"object": "block", "type": "heading_1",
            "heading_1": {"rich_text": _rich(text)}}

def _para(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rich(text)}}

def _para_link(text: str, url: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rich_link(text, url)}}

def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rich(text)}}

def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}

def _callout(text: str, emoji: str = "📋") -> dict:
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": _rich(text), "icon": {"type": "emoji", "emoji": emoji}}}

def _toggle(title: str, children: list[dict]) -> dict:
    return {"object": "block", "type": "toggle",
            "toggle": {"rich_text": _rich(title), "children": children}}

def _article_toggle(a: dict) -> dict:
    impact_icon = {"High": "🔴", "Medium": "🟡", "Low": "🔵"}.get(a.get("impact", ""), "⚪")
    title    = f"{impact_icon} {a.get('title_ja', a.get('title', ''))}"
    url      = a.get("url", "")
    children = [
        _para(f"📅 {a.get('published', '')}  |  📌 {a.get('source', '')}  |  🏷 {a.get('category', '')}"),
        _para(f"📝 {a.get('summary_ja', a.get('description', ''))}"),
    ]
    if a.get("business_use"):
        children.append(_para(f"💼 ビジネス活用: {a['business_use']}"))
    children.append(_para(f"📊 インパクト [{a.get('impact', '')}]: {a.get('impact_reason', '')}"))
    if url:
        children.append(_para_link("🔗 記事を読む", url))
    return _toggle(title, children)

def _send_blocks(page_id: str, blocks: list[dict]) -> None:
    for i in range(0, len(blocks), 50):
        chunk = blocks[i:i + 50]
        _notion("PATCH", f"blocks/{page_id}/children", {"children": chunk})
        log.info(f"ブロック追加: {i+1}〜{i+len(chunk)} / {len(blocks)}")

def add_blocks_analyzed(page_id: str, analysis: dict) -> None:
    articles = analysis.get("articles", [])
    high   = [a for a in articles if a.get("impact") == "High"]
    medium = [a for a in articles if a.get("impact") == "Medium"]
    low    = [a for a in articles if a.get("impact") == "Low"]

    blocks: list[dict] = []

    # 冒頭サマリー
    if analysis.get("daily_summary"):
        blocks.append(_callout(analysis["daily_summary"], "🗞️"))
        blocks.append(_divider())

    # トレンドサマリー
    trend = analysis.get("trend_summary", {})
    if trend.get("themes") or trend.get("business_insight"):
        blocks.append(_h1("📊 本日のトレンド"))
        for theme in trend.get("themes", []):
            blocks.append(_bullet(theme))
        if trend.get("business_insight"):
            blocks.append(_para(f"💼 ビジネス示唆: {trend['business_insight']}"))
        blocks.append(_divider())

    # 記事一覧
    blocks.append(_h1("🔴 Top Pick（High Impact）"))
    blocks += [_article_toggle(a) for a in high] or [_para("該当なし")]
    blocks.append(_h1("🟡 注目記事（Medium Impact）"))
    blocks += [_article_toggle(a) for a in medium] or [_para("該当なし")]
    blocks.append(_h1("🔵 参考情報（Low Impact）"))
    blocks += [_article_toggle(a) for a in low] or [_para("該当なし")]

    _send_blocks(page_id, blocks)

def add_blocks_raw(page_id: str, articles: list[dict]) -> None:
    """AI 分析なし: ソース別トグルで転記（フォールバック用）"""
    by_source: dict[str, list[dict]] = {}
    for a in articles:
        by_source.setdefault(a.get("source", "Other"), []).append(a)

    blocks: list[dict] = []
    for source, items in by_source.items():
        blocks.append(_h1(source))
        for a in items:
            url = a.get("url", "")
            children = [
                _para(f"📅 公開日: {a.get('published', '')}"),
                _para(a.get("description", "")),
            ]
            if url:
                children.append(_para_link("🔗 記事を読む", url))
            blocks.append(_toggle(a.get("title", ""), children))
        blocks.append(_divider())

    _send_blocks(page_id, blocks)

# ── ジョブ ────────────────────────────────────────────────────────
def _execute_job() -> None:
    log.info("━━━ Newspick ジョブ 開始 ━━━")
    try:
        log.info("Step 1: ニュース収集（本日分）")
        articles = collect_articles()
        if not articles:
            log.error("記事を1件も収集できませんでした → 中断")
            return

        log.info("Step 2: AI 分析（和訳・要約・分類）")
        analysis = analyze_with_ai(articles)

        log.info("Step 3: Notion ページ作成")
        page_id = _create_page()

        log.info("Step 4: Notion ブロック追加")
        if analysis:
            add_blocks_analyzed(page_id, analysis)
        else:
            log.warning("AI 分析失敗 → 英文のままトグル形式で転記")
            add_blocks_raw(page_id, articles)

    except Exception:
        log.exception("ジョブ実行中に例外が発生しました")
    log.info("━━━ Newspick ジョブ 完了 ━━━")

# ── スケジューラー / エントリポイント ──────────────────────────────
_running = True

def _on_stop(signum, _frame):
    global _running
    log.info(f"シグナル受信 ({signum}) → シャットダウン")
    _running = False

signal.signal(signal.SIGINT,  _on_stop)
signal.signal(signal.SIGTERM, _on_stop)

def main() -> None:
    if not _validate():
        sys.exit(1)

    if "--now" in sys.argv:
        log.info("=== Newspick 実行（--now） ===")
        _execute_job()
        log.info("=== 完了 ===")
        return

    log.info("=" * 55)
    log.info("  Newspick デーモン 起動")
    log.info(f"  実行スケジュール : 毎日 {SCHEDULE_TIME}")
    log.info(f"  チェック間隔     : {CHECK_INTERVAL_SEC} 秒")
    log.info(f"  PID              : {os.getpid()}  (→ {PID_FILE})")
    log.info("=" * 55)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    last_run_date: datetime.date | None = None
    try:
        while _running:
            now   = datetime.datetime.now(_JST)
            today = now.date()
            if now.strftime("%H:%M") == SCHEDULE_TIME and last_run_date != today:
                _execute_job()
                last_run_date = today
            for _ in range(CHECK_INTERVAL_SEC):
                if not _running:
                    break
                time.sleep(1)
    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()
        log.info("Newspick デーモン 停止")

if __name__ == "__main__":
    main()
