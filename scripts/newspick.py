#!/usr/bin/env python3
"""
Newspick — Gemini API 版
毎日 AI 関連ニュースを RSS/arXiv から収集し、Gemini で要約・分類、Notion に転記する。
外部ライブラリ不要（Python 標準ライブラリのみ使用）。

起動（即時実行）: python scripts/newspick.py --now
起動（デーモン）: python scripts/newspick.py
停止: Ctrl+C
"""

import datetime
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
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "").strip()
NOTION_API_KEY     = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
NOTION_VERSION     = "2022-06-28"
SCHEDULE_TIME      = os.environ.get("SCHEDULE_TIME", "08:30")
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "30"))
GEMINI_MODEL       = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

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
        "GEMINI_API_KEY":     GEMINI_API_KEY,
        "NOTION_API_KEY":     NOTION_API_KEY,
        "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
    }.items() if not v]
    if missing:
        log.error(f"未設定の必須項目: {', '.join(missing)}")
        log.error(f"  {ENV_FILE} に設定してください（.env.example 参照）")
        return False
    return True

# ── HTTP ユーティリティ ────────────────────────────────────────────
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Newspick/2.0; +https://github.com/sitonoy/Newspicks)"
}

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

# ── RSS パーサー ──────────────────────────────────────────────────
_ATOM_NS = "http://www.w3.org/2005/Atom"

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
            if len(desc) > 600:
                desc = desc[:600] + "..."
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
        root    = ET.fromstring(content)
        results = []
        for entry in root.findall(f"{{{_ATOM_NS}}}entry"):
            def _t(tag: str) -> str:
                el = entry.find(f"{{{_ATOM_NS}}}{tag}")
                return (el.text or "").strip() if el is not None else ""
            title   = _t("title").replace("\n", " ")
            link    = _t("id")
            pub     = _t("published")
            summary = _t("summary")[:500]
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

def collect_articles() -> list[dict]:
    all_articles = []
    for name, url in _RSS_SOURCES:
        log.info(f"  収集: {name}")
        articles = _parse_rss(_get(url), source=name)
        all_articles.extend(articles)
        log.info(f"    → {len(articles)} 件")

    log.info("  収集: arXiv cs.AI")
    arxiv = _fetch_arxiv(5)
    all_articles.extend(arxiv)
    log.info(f"    → {len(arxiv)} 件")

    log.info(f"合計 {len(all_articles)} 件収集完了")
    return all_articles

# ── Gemini API ────────────────────────────────────────────────────
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

_ANALYSIS_PROMPT = """\
あなたはAI業界のアナリストです。以下の記事リストを分析し、指定のJSON形式で構造化してください。

## 入力記事
{articles_json}

## 出力（JSONのみ。前後に説明・コードブロック不要）
{{
  "articles": [
    {{
      "title_ja": "日本語タイトル",
      "title_en": "元の英語タイトル",
      "url": "記事URL",
      "source": "ソース名",
      "published": "公開日",
      "summary_ja": "日本語要約（3文以内）",
      "category": "LLM・基盤モデル | 画像・マルチモーダル | エージェント・自動化 | 規制・政策・倫理 | 研究・論文 | ビジネス・投資",
      "impact": "High | Medium | Low",
      "impact_reason": "インパクトの理由（1文）"
    }}
  ],
  "trend_summary": {{
    "themes": ["主要テーマ1", "主要テーマ2", "主要テーマ3"],
    "insight": "来週への示唆（1文）"
  }}
}}

## 判定基準
- High: 業界全体に影響する重大ニュース（主要モデルリリース、規制決定、巨額投資等）
- Medium: 特定領域の重要動向
- Low: トレンド把握用の参考情報
"""

def analyze_with_gemini(articles: list[dict]) -> dict | None:
    prompt = _ANALYSIS_PROMPT.format(
        articles_json=json.dumps(articles, ensure_ascii=False, indent=2)
    )
    url  = f"{_GEMINI_BASE}/{GEMINI_MODEL}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
    }).encode()
    req = Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    })

    for attempt in range(3):
        try:
            with urlopen(req, timeout=60) as r:
                resp = json.loads(r.read())
            text = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            result = json.loads(text)
            log.info(f"Gemini 分析完了: {len(result.get('articles', []))} 件")
            return result
        except Exception as e:
            # HTTPError の場合はレスポンスボディも出力する
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")  # type: ignore
            except Exception:
                pass
            if attempt < 2:
                wait = 30 * (attempt + 1)
                log.warning(f"Gemini API リトライ {attempt+1}/3 ({wait}秒待機): {e} | body={body[:300]}")
                time.sleep(wait)
            else:
                log.error(f"Gemini API 失敗: {e} | body={body[:500]}")
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
    today    = datetime.date.today()
    today_jp = today.strftime("%Y年%m月%d日")
    result   = _notion("POST", "pages", {
        "parent":     {"database_id": NOTION_DATABASE_ID},
        "icon":       {"type": "emoji", "emoji": "📰"},
        "properties": {
            "名前": {"title": [{"text": {"content": f"{today_jp} AI ニュース"}}]},
        },
    })
    page_id  = result["id"]
    page_url = f"https://notion.so/{page_id.replace('-', '')}"
    log.info(f"Notion ページ作成: {page_url}")
    return page_id

# ── Notion ブロックビルダー ────────────────────────────────────────
def _rich(text: str) -> list:
    return [{"type": "text", "text": {"content": text[:2000]}}]

def _h1(text: str) -> dict:
    return {"object": "block", "type": "heading_1",
            "heading_1": {"rich_text": _rich(text)}}

def _h2(text: str) -> dict:
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rich(text)}}

def _para(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rich(text)}}

def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rich(text)}}

def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}

def _article_blocks(a: dict) -> list[dict]:
    return [
        _h2(a.get("title_ja", "")),
        _para(f"原文: {a.get('title_en', '')}"),
        _para(f"URL: {a.get('url', '')}"),
        _para(f"ソース: {a.get('source', '')}  |  {a.get('published', '')}"),
        _para(f"カテゴリ: {a.get('category', '')}"),
        _para(a.get("summary_ja", "")),
        _para(f"インパクト [{a.get('impact', '')}]: {a.get('impact_reason', '')}"),
        _divider(),
    ]

def add_blocks(page_id: str, analysis: dict) -> None:
    articles = analysis.get("articles", [])
    high   = [a for a in articles if a.get("impact") == "High"]
    medium = [a for a in articles if a.get("impact") == "Medium"]
    low    = [a for a in articles if a.get("impact") == "Low"]

    blocks: list[dict] = []

    blocks.append(_h1("🔴 Top Pick（High Impact）"))
    if high:
        for a in high:
            blocks.extend(_article_blocks(a))
    else:
        blocks.append(_para("該当なし"))

    blocks.append(_h1("🟡 注目記事（Medium Impact）"))
    if medium:
        for a in medium:
            blocks.extend(_article_blocks(a))
    else:
        blocks.append(_para("該当なし"))

    blocks.append(_h1("🔵 参考情報（Low Impact）"))
    if low:
        for a in low:
            blocks.extend(_article_blocks(a))
    else:
        blocks.append(_para("該当なし"))

    trend = analysis.get("trend_summary", {})
    blocks.append(_h1("📊 本日のトレンドサマリー"))
    for theme in trend.get("themes", []):
        blocks.append(_bullet(theme))
    if trend.get("insight"):
        blocks.append(_para(f"来週への示唆: {trend['insight']}"))

    # Notion は1リクエスト100ブロックまで → 50件ずつ分割送信
    for i in range(0, len(blocks), 50):
        chunk = blocks[i:i + 50]
        _notion("PATCH", f"blocks/{page_id}/children", {"children": chunk})
        log.info(f"ブロック追加: {i+1}〜{i+len(chunk)} / {len(blocks)}")

# ── ジョブ ────────────────────────────────────────────────────────
def _execute_job() -> None:
    log.info("━━━ Newspick ジョブ 開始 ━━━")
    try:
        log.info("Step 1: ニュース収集")
        articles = collect_articles()
        if not articles:
            log.error("記事を1件も収集できませんでした → 中断")
            return

        log.info("Step 2: Gemini で分析・構造化")
        analysis = analyze_with_gemini(articles)
        if not analysis:
            log.error("Gemini 分析失敗 → 中断")
            return

        log.info("Step 3: Notion ページ作成")
        page_id = _create_page()

        log.info("Step 4: Notion ブロック追加")
        add_blocks(page_id, analysis)

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
            now   = datetime.datetime.now()
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
