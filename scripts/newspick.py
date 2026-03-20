#!/usr/bin/env python3
"""
Newspick デーモン
───────────────────────────────────────────────────────────────
毎日 SCHEDULE_TIME（デフォルト 08:30）にAIニュースを収集し
Notion へ転記し続ける常駐プロセス。

起動: python scripts/newspick.py
停止: Ctrl+C  または  kill $(cat scripts/newspick.pid)
───────────────────────────────────────────────────────────────
"""

import datetime
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

# ─────────────────────────────────────────────
# パス定義
# ─────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ENV_FILE   = Path.home() / ".config" / "newspick" / ".env"  # プロジェクト外に配置
PID_FILE   = SCRIPT_DIR / "newspick.pid"
LOG_FILE   = SCRIPT_DIR / "newspick.log"

# ─────────────────────────────────────────────
# .env 読み込み（python-dotenv 不要）
# ─────────────────────────────────────────────
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

# ─────────────────────────────────────────────
# 設定（すべて .env または環境変数で上書き可）
# ─────────────────────────────────────────────
NOTION_API_KEY      = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID  = os.environ.get("NOTION_DATABASE_ID", "")
NOTION_VERSION      = "2022-06-28"
SCHEDULE_TIME       = os.environ.get("SCHEDULE_TIME", "08:30")   # HH:MM
CHECK_INTERVAL_SEC  = int(os.environ.get("CHECK_INTERVAL_SEC", "30"))
CLAUDE_CLI          = os.environ.get("CLAUDE_CLI_PATH", "claude")

# ─────────────────────────────────────────────
# ロギング
# ─────────────────────────────────────────────
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

# ─────────────────────────────────────────────
# シグナルハンドラ（Ctrl+C / kill 対応）
# ─────────────────────────────────────────────
_running = True

def _on_stop(signum, _frame):
    global _running
    log.info(f"シグナル受信 ({signum}) → シャットダウンします")
    _running = False

signal.signal(signal.SIGINT,  _on_stop)
signal.signal(signal.SIGTERM, _on_stop)

# ─────────────────────────────────────────────
# バリデーション
# ─────────────────────────────────────────────
def _validate() -> bool:
    missing = [k for k, v in {
        "NOTION_API_KEY":     NOTION_API_KEY,
        "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
    }.items() if not v]
    if missing:
        log.error(f"未設定の必須項目: {', '.join(missing)}")
        log.error(f"→ {ENV_FILE} に設定を記入してください（.env.example 参照）")
        return False
    return True

# ─────────────────────────────────────────────
# Notion API
# ─────────────────────────────────────────────
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
                log.warning(f"Notion API リトライ {attempt + 1}/3: {e}")
                time.sleep(1)
            else:
                raise


def _create_page() -> str:
    """本日付の Notion ページを作成し page_id を返す"""
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
    log.info(f"Notionページ作成完了: {page_url}")
    return page_id

# ─────────────────────────────────────────────
# Claude CLI 実行
# ─────────────────────────────────────────────
def _run_claude(page_id: str) -> bool:
    prompt = (
        f"本日のAI最新ニュースを各ソースから収集・構造化し、"
        f"Notion page_id={page_id} に転記してください。"
        f"/newspick スキルの手順に従い実行してください。"
    )
    env = {**os.environ,
           "NOTION_API_KEY":     NOTION_API_KEY,
           "NOTION_DATABASE_ID": NOTION_DATABASE_ID}
    env.pop("CLAUDECODE", None)  # ネストセッション制限を回避
    cmd = [CLAUDE_CLI, "-p", prompt,
           "--allowedTools", "WebSearch,WebFetch,Bash"]

    log.info(f"Claude CLI 起動: {CLAUDE_CLI}")
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        log.error(f"Claude CLI 失敗 (exit {result.returncode}):\n{stderr}")
        return False

    log.info("Claude CLI 正常終了")
    stdout = (result.stdout or "").strip()
    if stdout:
        log.debug(stdout)
    return True

# ─────────────────────────────────────────────
# 1回分のジョブ
# ─────────────────────────────────────────────
def _execute_job() -> None:
    log.info("━━━ Newspick ジョブ 開始 ━━━")
    try:
        page_id = _create_page()
        _run_claude(page_id)
    except Exception:
        log.exception("ジョブ実行中に例外が発生しました")
    log.info("━━━ Newspick ジョブ 完了 ━━━")

# ─────────────────────────────────────────────
# メインループ
# ─────────────────────────────────────────────
def main() -> None:
    # --now フラグ: 即時1回実行して終了（テスト用）
    run_now = "--now" in sys.argv

    if not _validate():
        sys.exit(1)

    if run_now:
        log.info("=== Newspick テスト実行（--now） ===")
        _execute_job()
        log.info("=== テスト実行 完了 ===")
        return

    log.info("=" * 55)
    log.info("  Newspick デーモン 起動")
    log.info(f"  実行スケジュール : 毎日 {SCHEDULE_TIME}")
    log.info(f"  チェック間隔     : {CHECK_INTERVAL_SEC} 秒")
    log.info(f"  PID              : {os.getpid()}  (→ {PID_FILE})")
    log.info(f"  停止方法         : Ctrl+C  または  kill {os.getpid()}")
    log.info("=" * 55)

    # PID ファイル書き込み
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    last_run_date: datetime.date | None = None

    try:
        while _running:
            now   = datetime.datetime.now()
            today = now.date()

            if now.strftime("%H:%M") == SCHEDULE_TIME and last_run_date != today:
                _execute_job()
                last_run_date = today

            # _running が False になるまで待機（30秒ごとに確認）
            for _ in range(CHECK_INTERVAL_SEC):
                if not _running:
                    break
                time.sleep(1)

    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()
        log.info("Newspick デーモン 停止完了")


if __name__ == "__main__":
    main()
