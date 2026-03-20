# Newspick — AI ニュース収集・Notion転記スキル

## 概要

以下の手順でAI関連の最新記事を収集し、本日付のNotionページに構造的に転記する。

---

## 実行手順

### Step 1: ニュース収集

以下のソースから**本日のAI関連記事**を各サイト最大5件ずつ収集する。
WebSearch または WebFetch を使用すること。

**収集対象サイト:**
- https://techcrunch.com/category/artificial-intelligence/
- https://venturebeat.com/ai/
- https://www.theverge.com/ai-artificial-intelligence
- https://www.wired.com/tag/artificial-intelligence/
- https://arxiv.org/list/cs.AI/recent （論文トレンド用）

**収集する情報（各記事）:**
```
- タイトル（日本語訳付き）
- URL
- 公開日時
- 要約（3文以内）
- カテゴリタグ（LLM / 画像生成 / ロボティクス / 規制・政策 / 研究 / ビジネス / その他）
- インパクト評価（High / Medium / Low + 理由1文）
```

### Step 2: 内容の構造化

収集した記事を以下の軸で分類・整理する:

**優先度フィルタリング:**
1. **Top Pick** (High Impact) — 業界全体に影響する重大ニュース
2. **注目記事** (Medium Impact) — 特定領域の重要動向
3. **参考情報** (Low Impact) — トレンド把握用

**カテゴリ別グルーピング:**
- LLM・基盤モデル
- 画像・マルチモーダル
- エージェント・自動化
- 規制・政策・倫理
- 研究・論文
- ビジネス・投資

### Step 3: Notion ページ作成

以下のBashコマンドで本日付のNotionページを作成し、構造化コンテンツを転記する。

**必要環境変数:**
- `NOTION_API_KEY` — Notion Integration Token
- `NOTION_DATABASE_ID` — 転記先データベースID

**Notion APIを使ってページ作成:**

```bash
TODAY=$(date +"%Y-%m-%d")
DAY_JP=$(date +"%Y年%m月%d日")

# ページ作成リクエスト
curl -s -X POST "https://api.notion.com/v1/pages" \
  -H "Authorization: Bearer ${NOTION_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "Notion-Version: 2022-06-28" \
  -d "{
    \"parent\": { \"database_id\": \"${NOTION_DATABASE_ID}\" },
    \"properties\": {
      \"Name\": {
        \"title\": [{
          \"text\": { \"content\": \"${DAY_JP} AI ニュース\" }
        }]
      },
      \"Date\": {
        \"date\": { \"start\": \"${TODAY}\" }
      }
    }
  }"
```

**Notionページのブロック構成:**

作成したページIDを取得後、以下の構造でブロックを追加する:

```
📰 [日付] AI ニュース
├── 🔴 Top Pick（High Impact）
│   ├── [記事タイトル（日本語）]
│   │   ├── 原文タイトル・URL
│   │   ├── 要約
│   │   └── インパクト: [理由]
│   └── ...
├── 🟡 注目記事（Medium Impact）
│   └── ...
├── 🔵 参考情報（Low Impact）
│   └── ...
└── 📊 本日のトレンドサマリー
    ├── 主要テーマ（3点）
    └── 来週への示唆（1点）
```

### Step 4: ブロック追加

ページID取得後、以下のAPIでブロックを追加する:

```bash
PAGE_ID="[Step3で取得したpage_id]"

curl -s -X PATCH "https://api.notion.com/v1/blocks/${PAGE_ID}/children" \
  -H "Authorization: Bearer ${NOTION_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "Notion-Version: 2022-06-28" \
  -d '{
    "children": [
      {
        "object": "block",
        "type": "heading_1",
        "heading_1": {
          "rich_text": [{"type": "text", "text": {"content": "🔴 Top Pick"}}]
        }
      }
      // 記事ブロックを続けて追加
    ]
  }'
```

---

## 実行後の確認事項

- [ ] 全収集サイトからの記事取得成功
- [ ] Notionページ作成成功（ページURLを出力）
- [ ] 全記事のインパクト評価が付与されている
- [ ] トレンドサマリーが3点で記述されている

---

## エラーハンドリング

| エラー | 対処 |
|--------|------|
| サイトアクセス不可 | スキップして他サイトで補完 |
| NOTION_API_KEY未設定 | `echo "環境変数 NOTION_API_KEY を設定してください"` で停止 |
| API rate limit | 1秒待機後リトライ（最大3回） |

---

## 使用方法

```bash
# 手動実行
/newspick

# 環境変数を指定して実行
NOTION_API_KEY=secret_xxx NOTION_DATABASE_ID=xxx /newspick
```

スケジュール自動実行は `scripts/newspick.py` を参照。
