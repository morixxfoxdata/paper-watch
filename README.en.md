# Paper Watch

Paper Watch is a local automation tool for monitoring new research papers.
It collects candidates from arXiv, PubMed, and RSS feeds, asks Claude CLI to
score their relevance to your research interests, writes JSONL logs, and can
optionally notify Slack or download highly relevant arXiv PDFs.

The tool is designed to be adapted by each researcher: your fields, keywords,
RSS feeds, relevance rubric, thresholds, output language, and Slack destination
all live in configuration files.

## Features

- Collect papers from arXiv categories, arXiv keyword searches, PubMed keyword
  searches, and journal RSS feeds.
- Deduplicate paper URLs and keep a processed URL ledger so the same paper is
  not reviewed repeatedly.
- Evaluate papers with Claude CLI using a configurable researcher profile.
- Save every evaluated paper to dated JSONL logs.
- Post only relevant papers to Slack when enabled.
- Download highly relevant arXiv PDFs when enabled.
- Run manually, from cron, or from launchd on macOS.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) or another Python environment manager
- Claude CLI available on `PATH`
- Optional: Slack bot token when Slack notifications are enabled

## Quick Start

```bash
git clone https://github.com/morixxfoxdata/paper-watch.git
cd paper-watch
uv sync
cp config.example.toml config.toml
cp .env.example .env
```

Edit `config.toml` for your research field. At minimum, update:

- `[profile]`: your interests, background, and relevance rubric
- `[arxiv]`: categories and keywords
- `[pubmed]`: keywords and contact email if you use PubMed
- `[[rss.feeds]]`: journal or conference feeds
- `[slack]`: enable and set a channel only if you want Slack notifications

Then run:

```bash
# Check local configuration and optional integrations
uv run paper-watch --health

# Collect candidates only; no Claude, no Slack, no PDF download
uv run paper-watch --collect-only

# Evaluate with Claude, write logs, but do not notify Slack or download PDFs
uv run paper-watch --dry-run

# Full run
uv run paper-watch
```

## Configuration

The default search behavior is intentionally controlled by `config.toml`.
Do not edit the prompt or Python code just to change fields.

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

Set `slack.enabled = false` if you only want local logs.

Secrets are read from the environment. If you use `.env`, `run.sh` loads it
before starting Paper Watch.

```bash
SLACK_BOT_TOKEN=replace-with-your-slack-bot-token
```

## Output

Paper Watch stores state and logs under the configured state directory
(`~/.local/state/paper-watch` by default):

- `processed.jsonl`: URLs already processed
- `YYYYMMDD.jsonl`: evaluated papers for each run date
- `launchd.out.log` and `launchd.err.log`: useful if you schedule via launchd

Downloaded PDFs are saved under `pdf.download_dir`.

## Scheduling

For macOS launchd, copy the template and replace placeholders:

```bash
cp examples/com.example.paper-watch.plist ~/Library/LaunchAgents/com.example.paper-watch.plist
plutil -replace ProgramArguments.0 -string "$PWD/run.sh" ~/Library/LaunchAgents/com.example.paper-watch.plist
plutil -replace WorkingDirectory -string "$PWD" ~/Library/LaunchAgents/com.example.paper-watch.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.example.paper-watch.plist
```

The template runs weekly on Monday at 07:00. Edit `StartCalendarInterval` if you
want a different schedule.

For cron, use:

```cron
0 7 * * 1 cd /path/to/paper-watch && ./run.sh
```

## Safety Notes

- Keep `.env`, `config.toml`, logs, downloaded PDFs, and local state out of Git.
- Public repositories should commit `config.example.toml`, not personal
  `config.toml`.
- PubMed asks API users to provide an email address; set your own in
  `config.toml`.

## Development

```bash
uv sync --group dev
uv run pytest
uv run python -m compileall src tests
```

## License

MIT
