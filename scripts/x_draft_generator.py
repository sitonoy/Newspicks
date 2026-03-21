#!/usr/bin/env python3
"""
X 投稿下書き生成スクリプト
────────────────────────────────────
本日の Notion AI ニュースページを取得し、
GitHub Models API でX投稿文を生成して
Notion ページ末尾に下書きとして追記する。

起動: python scripts/x_draft_generator.py
────────────────────────────────────
"""

import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

# ── パス ──────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ENV_FILE   = Path.home() / ".config" / "newspick" / ".env"
LOG_FILE   = SCRIPT_DIR / "x_draft.log"

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
AI_MODEL           = os.environ.get("AI_MODEL", "gpt-4o-mini")
AI_ENDPOINT        = os.environ.get("AI_ENDPOINT",
                                    "https://models.inference.ai.azure.com/chat/completions")

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
log = logging.getLogger("x_draft")

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

def _find_today_page() -> str | None:
    """本日の AI ニュースページIDを返す"""
    today    = datetime.datetime.now(_JST).date()
    today_jp = today.strftime("%Y年%m月%d日")
    title    = f"{today_jp} AI ニュース"

    result = _notion("POST", f"databases/{NOTION_DATABASE_ID}/query", {
        "filter": {
            "property": "名前",
            "title": {"equals": title}
        },
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        "page_size": 1,
    })
    pages = result.get("results", [])
    if not pages:
        log.error(f"本日のページが見つかりません: {title}")
        return None
    page_id = pages[0]["id"]
    log.info(f"Notion ページ発見: {page_id}")
    return page_id

def _get_page_content(page_id: str) -> tuple[str, list[dict]]:
    """ページのテキストと記事URLリスト（title, url）を返す"""
    blocks = _notion("GET", f"blocks/{page_id}/children?page_size=100")

    parts = []
    urls: list[dict] = []

    for block in blocks.get("results", []):
        btype = block.get("type", "")
        if btype in ("paragraph", "heading_1", "heading_2", "heading_3",
                     "bulleted_list_item", "numbered_list_item", "callout"):
            rich_text = block.get(btype, {}).get("rich_text", [])
            text = "".join(t["plain_text"] for t in rich_text)
            if text.strip():
                parts.append(text.strip())
        elif btype == "toggle":
            rich_text = block.get("toggle", {}).get("rich_text", [])
            title = "".join(t["plain_text"] for t in rich_text).strip()
            if title:
                parts.append(title)
            # トグルの子ブロックからURLを抽出
            block_id = block["id"]
            time.sleep(0.35)  # Notion rate limit (3 req/s)
            children = _notion("GET", f"blocks/{block_id}/children?page_size=20")
            for child in children.get("results", []):
                if child.get("type") != "paragraph":
                    continue
                for rt in child.get("paragraph", {}).get("rich_text", []):
                    link = rt.get("text", {}).get("link") or {}
                    url = link.get("url", "")
                    if url and url.startswith("http"):
                        label = rt.get("text", {}).get("content", url)
                        if not any(u["url"] == url for u in urls):
                            urls.append({"title": title, "url": url, "label": label})

    log.info(f"テキスト: {len(parts)} ブロック / URL: {len(urls)} 件")
    return "\n".join(parts), urls

# ── GitHub Models API ──────────────────────────────────────────────
_DRAFT_PROMPT = """\
あなたは製造業・AIスタートアップ・戦略コンサルティングのキャリアを経た、
AI活用の実務家として個人アカウントで発信しています。

以下の本日のAI最新ニュースを読み、Xへの投稿文を1つ作成してください。

【記事の選び方】
- 収集した記事の中から「昨日までに発表されていない新しい情報」かつ「ビジネスインパクトが最も大きい」と判断できる記事を1つ選ぶ
- 選んだ記事と関連する別のニュースがあれば、それを補足として自然に絡める

【投稿の構成（口語でつなぐ）】
1. 事象: 何が起きたか（選んだ記事の核心を短く）
2. 自分ごと化: この事象を自分や自社に置き換えたとき——
   - なぜ: なぜこれが自分・自社にとって問題/機会になるのか
   - なにが: 具体的に何が変わる/求められるのか
   - 必要になるのか: だから何をしなければならないのか

【文体のイメージ】
「○○が△△を出したけど、これって✕✕な企業にとっては□□の前提が崩れる。だから今のうちに◇◇を見直す必要があるんじゃないかな」
「○○ってつまり△△ということで、自社でいうと✕✕が直撃する。なぜかというと□□だから。今やっておくべきは◇◇だと思う」

【制約】
- 全体で140文字以内
- 絵文字なし
- 断言・宣言調は避け、「〜だな」「〜けど」「〜じゃないか」「〜が必要になりそう」のような口語で締める
- 投稿文のみ出力（前置き・説明・JSON不要）
- 末尾にハッシュタグ2〜3個（#AI #AIX #生成AI #DX #グローバル 等から文脈に合うものを選ぶ）

【ニュース内容】
{content}
"""

def generate_x_draft(content: str) -> str:
    prompt = _DRAFT_PROMPT.replace("{content}", content[:3000])
    body = json.dumps({
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 512,
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
            log.info(f"生成完了 — {len(text)} 文字")
            return text
        except Exception as e:
            if attempt < 2:
                log.warning(f"AI API リトライ {attempt+1}/3: {e}")
                time.sleep(15)
            else:
                log.error(f"AI API 失敗: {e}")
                raise

# ── Notion への書き戻し ───────────────────────────────────────────
def _h1(text: str) -> dict:
    return {"object": "block", "type": "heading_1",
            "heading_1": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

def _h3(text: str) -> dict:
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

def _para(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

def _para_link(label: str, url: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": label, "link": {"url": url}}}]}}

def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}

def _append_draft_to_page(page_id: str, post: str, urls: list[dict]) -> None:
    blocks: list[dict] = [
        _divider(),
        _h1("📝 X投稿下書き"),
        _para(post),
    ]

    if urls:
        blocks.append(_divider())
        blocks.append(_h3("参考資料"))
        for u in urls[:10]:
            blocks.append(_para_link(u.get("label", u.get("title", u["url"])), u["url"]))

    _notion("PATCH", f"blocks/{page_id}/children", {"children": blocks})
    log.info("Notion ページに X下書きセクションを追記しました")

# ── メイン ────────────────────────────────────────────────────────
def main() -> None:
    missing = [k for k, v in {
        "AI_API_TOKEN":       AI_API_TOKEN,
        "NOTION_API_KEY":     NOTION_API_KEY,
        "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
    }.items() if not v]
    if missing:
        log.error(f"未設定の必須項目: {', '.join(missing)}")
        sys.exit(1)

    log.info("━━━ X 下書き生成 開始 ━━━")

    page_id = _find_today_page()
    if not page_id:
        sys.exit(1)

    content, urls = _get_page_content(page_id)
    if not content:
        log.error("Notion コンテンツが空です")
        sys.exit(1)
    log.info(f"Notion コンテンツ取得: {len(content)} 文字 / URL: {len(urls)} 件")

    post = generate_x_draft(content)
    log.info(f"投稿文:\n{post}")

    _append_draft_to_page(page_id, post, urls)

    log.info("━━━ X 下書き生成 完了 ━━━")


if __name__ == "__main__":
    main()
