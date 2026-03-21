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

以下の記事リストと本文を読み、Xへの投稿文を1つ作成してください。

【記事リスト（番号・タイトル・URL）】
{articles_list}

【記事の選び方（重要）】
- 上記リストから「昨日までに発表されていない新しい情報」かつ「ビジネスインパクトが最も大きい」記事を1つ選ぶ
- 以下は選ばない: 規制・政策の詳細がまだ確定していない記事 / 「〜が検討中」「〜の可能性」など推測段階の記事 / 日本のビジネス現場への影響経路を論理的に説明できない記事
- 選んだ記事と因果関係が明確な関連記事があれば補足として絡める。複数使う場合は「○○が〜したことで」「また、△△では〜」のように出典を文中で切り分けること

【投稿の論理構造（4ステップを口語でつなぐ・飛躍禁止）】
① 事象: 何がどうなったか（記事の事実のみ）
② 領域: どの業界・機能・ビジネスモデルが影響を受けるか（「テクノロジー企業」のような広い括りは不可）
③ なぜ: ②の領域でなぜ前提や競争構造が変わるのか（「〜だから」と因果を明示する。「可能性がある」で終わるのは不可）
④ 企業が検討すべきこと: ③から論理的に導かれる具体的なアクション（「AIを活用すべき」「ビジネスモデルを再構築」のような抽象表現は不可。何の・どこを・どう検討するかが分かるレベル）

【文体（最重要）】
個人SNSで呟く感覚の口語体。です・ます調は絶対に使わない。

NG例（機械的・丁寧すぎる）:
「〜変わります。なぜなら〜からです。〜すべきでしょう。」

OK例（口語・思考の流れが見える）:
「GoogleがAIで見出し生成を始めたけど、これって記事のクリックが減るメディアには直撃する。なぜかというとユーザーが本文を読まなくなるから。広告依存モデルの単価設計を今のうちに見直しておく必要があるんじゃないかな」

文末は「〜だな」「〜じゃないかな」「〜必要があるんじゃないかな」「〜気になる」のどれかで締める。

【自己レビュー（出力前に全項目確認・1つでも×なら書き直し）】
□ 記事選定ルールの除外条件に引っかかる記事を使っていないか
□ 複数記事使用時: 各情報の出典が文中で分かるか
□ 文体: 「〜ます」「〜です」「〜でしょう」「〜べきです」が一切含まれていないか
□ ③なぜ: 「なぜかというと〜だから」という因果が文中に存在するか
□ ④how: 「何の・どこを・どう検討するか」が読んで分かるか（抽象アクションになっていないか）
□ ①→②→③→④の順で論理が繋がっているか

【出力形式（JSONのみ・前置き不要）】
{{
  "draft": "投稿文（140文字以内、末尾にハッシュタグ2〜3個）",
  "referenced_ids": [実際に言及した記事の番号をリストで。例: [1, 3]]
}}

【記事本文】
{content}
"""

def generate_x_draft(content: str, urls: list[dict]) -> tuple[str, list[dict]]:
    """投稿文と実際に参照した記事リストを返す"""
    # 番号付き記事リストを生成
    articles_list = "\n".join(
        f"[{i+1}] {u.get('label') or u.get('title', '')} {u['url']}"
        for i, u in enumerate(urls)
    )
    prompt = (_DRAFT_PROMPT
              .replace("{articles_list}", articles_list or "（記事リストなし）")
              .replace("{content}", content[:2500]))
    body = json.dumps({
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 600,
    }).encode()
    req = Request(AI_ENDPOINT, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AI_API_TOKEN}",
    })
    for attempt in range(3):
        try:
            with urlopen(req, timeout=60) as r:
                resp = json.loads(r.read())
            raw = resp["choices"][0]["message"]["content"].strip()
            if "```" in raw:
                raw = raw.split("```")[1].strip()
                if raw.startswith("json"):
                    raw = raw[4:].strip()
            parsed = json.loads(raw)
            draft = parsed.get("draft", "").strip()
            referenced_ids = [int(i) - 1 for i in parsed.get("referenced_ids", [])
                              if str(i).isdigit() and 0 < int(i) <= len(urls)]
            used_urls = [urls[i] for i in referenced_ids]
            log.info(f"生成完了 — {len(draft)} 文字 / 参照記事: {len(used_urls)} 件")
            return draft, used_urls
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

    post, used_urls = generate_x_draft(content, urls)
    log.info(f"投稿文:\n{post}")
    log.info(f"参考資料: {len(used_urls)} 件（全{len(urls)}件中）")

    _append_draft_to_page(page_id, post, used_urls)

    log.info("━━━ X 下書き生成 完了 ━━━")


if __name__ == "__main__":
    main()
