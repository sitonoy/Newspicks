# Newspicks

毎日 08:30 JST に AI 関連ニュースを自動収集し、Notion に転記するツール。
GitHub Actions で動作するため、PC の電源状態に依存しません。

## セットアップ

### 1. GitHub Secrets の設定

リポジトリの Settings > Secrets and variables > Actions に以下を追加:

| Secret 名 | 内容 |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API キー |
| `NOTION_API_KEY` | Notion Integration Token |
| `NOTION_DATABASE_ID` | 転記先 Notion データベース ID |

### 2. 動作確認

Actions タブ > "Newspick Daily" > "Run workflow" で手動実行できます。

## ローカル実行（テスト用）

```bash
# .env を設定してから実行
cp .env.example .env
# .env に各キーを記入

python scripts/newspick.py --now
```
