# Paper Watch

[![CI](https://github.com/morixxfoxdata/paper-watch/actions/workflows/ci.yml/badge.svg)](https://github.com/morixxfoxdata/paper-watch/actions/workflows/ci.yml)

[English README](README.en.md)

Paper Watch は、新着論文をローカルで定期監視するための自動化ツールです。
arXiv、PubMed、RSS フィードから候補論文を集め、Claude CLI / Codex CLI / Gemini CLI のいずれかで自分の研究関心との関連度を評価し、JSONL ログに保存します。必要に応じて Slack 通知や、高関連度の arXiv PDF ダウンロードも行えます。

研究分野、検索キーワード、RSS フィード、関連度の評価基準、通知閾値、出力言語、Slack の投稿先は設定ファイルで差し替えられます。各研究者が自分の分野向けに調整して使うことを想定しています。

## 機能

- arXiv カテゴリ検索、arXiv キーワード検索、PubMed キーワード検索、ジャーナル RSS から論文候補を収集
- URL の重複排除と処理済み台帳による再評価の抑制
- 設定可能な研究者プロフィールを使った Claude CLI / Codex CLI / Gemini CLI による関連度評価
- 評価済み論文を日付別 JSONL ログに保存
- Slack 有効時のみ、関連度の高い論文を通知
- PDF ダウンロード有効時のみ、高関連度の arXiv PDF を保存
- 手動実行、cron、macOS launchd に対応

## 必要なもの

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) または任意の Python 環境管理ツール
- `PATH` 上で実行できる Claude CLI、Codex CLI、Gemini CLI のいずれか
- 任意: Slack 通知を使う場合は Slack bot token

## クイックスタート

```bash
git clone https://github.com/morixxfoxdata/paper-watch.git
cd paper-watch
uv sync
cp config.example.toml config.toml
cp .env.example .env
```

`config.toml` を自分の研究分野に合わせて編集します。最低限、次を確認してください。

- `[profile]`: 関心分野、背景知識、関連度評価基準
- `[arxiv]`: arXiv のカテゴリとキーワード
- `[pubmed]`: PubMed を使う場合のキーワードと連絡先メール
- `[[rss.feeds]]`: 監視したいジャーナルやカンファレンスの RSS
- `[slack]`: Slack 通知を使う場合のみ有効化し、投稿先チャンネルを設定

実行例:

```bash
# 設定と任意連携の確認
uv run paper-watch --health

# 候補収集のみ。LLM 評価、Slack 通知、PDF 保存は行わない
uv run paper-watch --collect-only

# LLM 評価とログ保存まで実行。Slack 通知と PDF 保存は行わない
uv run paper-watch --dry-run

# 通常実行
uv run paper-watch
```

## 設定

検索対象や評価方針は `config.toml` で管理します。分野を変えるだけなら、プロンプトや Python コードを直接編集する必要はありません。

```toml
[profile]
name = "Your research profile"
output_language = "Japanese"
interests = [
  "Ghost Imaging",
  "Compressive Sensing",
]
background = [
  "Computer Vision",
  "Deep Learning",
]

[thresholds]
slack_min = 4
pdf_min = 5
```

評価に使う CLI は `[llm]` で切り替えます。

```toml
[llm]
provider = "claude" # claude, codex, or gemini
timeout_sec = 120
batch_size = 10

[llm.providers.claude]
command = "claude"
model = "sonnet"

[llm.providers.codex]
command = "codex"
model = "" # 空なら Codex CLI の既定モデルを使う

[llm.providers.gemini]
command = "gemini"
model = "auto"
```

各 provider の `args` を設定すると、既定の起動引数を上書きできます。`{prompt}` と `{model}` は実行時に置換されます。

ローカルログだけでよい場合は、`slack.enabled = false` にしてください。

秘密情報は環境変数から読み込みます。`.env` を使う場合、`run.sh` が起動時に読み込みます。

```bash
SLACK_BOT_TOKEN=replace-with-your-slack-bot-token
```

## 出力

Paper Watch は、設定された state directory に状態とログを保存します。デフォルトは `~/.local/state/paper-watch` です。

- `processed.jsonl`: 処理済み URL の台帳
- `YYYYMMDD.jsonl`: 実行日ごとの評価済み論文ログ
- `launchd.out.log` / `launchd.err.log`: launchd で定期実行する場合のログ

ダウンロードした PDF は `pdf.download_dir` に保存されます。

## 定期実行

macOS launchd を使う場合は、テンプレートをコピーしてプレースホルダを置き換えます。

```bash
cp examples/com.example.paper-watch.plist ~/Library/LaunchAgents/com.example.paper-watch.plist
plutil -replace ProgramArguments.0 -string "$PWD/run.sh" ~/Library/LaunchAgents/com.example.paper-watch.plist
plutil -replace WorkingDirectory -string "$PWD" ~/Library/LaunchAgents/com.example.paper-watch.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.example.paper-watch.plist
```

テンプレートは毎週月曜 07:00 に実行する設定です。別の時刻にしたい場合は `StartCalendarInterval` を編集してください。

cron を使う場合:

```cron
0 7 * * 1 cd /path/to/paper-watch && ./run.sh
```

## 公開・運用時の注意

- `.env`、`config.toml`、ログ、ダウンロード済み PDF、ローカル state は Git に含めないでください。
- 公開リポジトリには個人用の `config.toml` ではなく、`config.example.toml` を置いてください。
- PubMed API を使う場合は、自分のメールアドレスを `config.toml` に設定してください。

## 開発

```bash
uv sync --group dev
uv run pytest
uv run python -m compileall src tests
```

## Issue / Pull Request

バグ報告、改善提案、ドキュメント修正、対応データソースの追加などは Issue または Pull Request で歓迎します。自分の研究分野向けに使っていて困った点や、汎用化できそうな設定例があれば気軽に共有してください。

Pull Request を送る場合は、可能な範囲で `uv run pytest` を実行し、個人用の `config.toml`、`.env`、ログ、ダウンロード済み PDF が含まれていないことを確認してください。

## ライセンス

MIT
