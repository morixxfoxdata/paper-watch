from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

import feedparser
import requests
from dotenv import load_dotenv
from slack_sdk import WebClient

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = pathlib.Path(os.environ.get("PAPER_WATCH_CONFIG_PATH", ROOT / "config.toml"))
DEFAULT_PROMPT_PATH = pathlib.Path(os.environ.get("PAPER_WATCH_PROMPT_PATH", ROOT / "prompts" / "evaluate.md"))
DEFAULT_STATE_DIR = pathlib.Path("~/.local/state/paper-watch")
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
HEALTH_STATUS_ORDER = {"ok": 0, "warn": 1, "fail": 2}


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def expand_path(raw: str | pathlib.Path) -> pathlib.Path:
    return pathlib.Path(raw).expanduser()


def load_config(path: pathlib.Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def state_dir(cfg: dict[str, Any]) -> pathlib.Path:
    return expand_path(cfg.get("state", {}).get("dir", DEFAULT_STATE_DIR))


def processed_path(cfg: dict[str, Any]) -> pathlib.Path:
    return state_dir(cfg) / "processed.jsonl"


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    return re.sub(r"v\d+$", "", url)


def health_item(name: str, status: str, summary: str, **details: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "status": status, "summary": summary}
    if details:
        item["details"] = details
    return item


def worst_status(statuses: list[str]) -> str:
    if not statuses:
        return "ok"
    return max(statuses, key=lambda status: HEALTH_STATUS_ORDER.get(status, 2))


def load_processed(cfg: dict[str, Any]) -> set[str]:
    path = processed_path(cfg)
    if not path.exists():
        return set()
    urls: set[str] = set()
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = rec.get("url")
            if isinstance(url, str):
                urls.add(normalize_url(url))
    return urls


def append_processed(papers: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
    path = processed_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for paper in papers:
            rec = {
                "url": normalize_url(paper["url"]),
                "relevance": paper.get("relevance", 0),
                "ts": now_iso(),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def rotate_processed(cfg: dict[str, Any]) -> None:
    path = processed_path(cfg)
    if not path.exists():
        return
    max_age_days = int(cfg.get("state", {}).get("processed_max_age_days", 30))
    cutoff = dt.datetime.now().astimezone() - dt.timedelta(days=max_age_days)
    kept: list[str] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = dt.datetime.fromisoformat(rec["ts"])
            except (json.JSONDecodeError, KeyError, ValueError):
                kept.append(line)
                continue
            if ts >= cutoff:
                rec["url"] = normalize_url(str(rec.get("url", "")))
                kept.append(json.dumps(rec, ensure_ascii=False))
    path.write_text("\n".join(kept) + "\n" if kept else "")


def collect_arxiv(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    papers: list[dict[str, Any]] = []
    arxiv_cfg = cfg.get("arxiv", {})
    max_results = int(arxiv_cfg.get("max_results", 10))
    rate_limit_sec = float(arxiv_cfg.get("rate_limit_sec", 3))

    for category in arxiv_cfg.get("categories", []):
        url = (
            "https://export.arxiv.org/api/query"
            f"?search_query=cat:{urllib.parse.quote(category)}"
            "&sortBy=submittedDate&sortOrder=descending"
            f"&max_results={max_results}"
        )
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            papers.extend(parse_arxiv_xml(resp.text, source=f"arxiv:cat:{category}"))
        except Exception as exc:
            logging.warning("arXiv category %s failed: %s", category, exc)
        time.sleep(rate_limit_sec)

    for keyword in arxiv_cfg.get("keywords", []):
        encoded = urllib.parse.quote(f'"{keyword}"')
        url = (
            "https://export.arxiv.org/api/query"
            f"?search_query=all:{encoded}"
            "&sortBy=submittedDate&sortOrder=descending"
            f"&max_results={max_results}"
        )
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            papers.extend(parse_arxiv_xml(resp.text, source=f"arxiv:kw:{keyword}"))
        except Exception as exc:
            logging.warning("arXiv keyword %s failed: %s", keyword, exc)
        time.sleep(rate_limit_sec)

    logging.info("arXiv: collected %d papers", len(papers))
    return papers


def parse_arxiv_xml(xml_text: str, source: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    root = ET.fromstring(xml_text)
    for entry in root.findall("atom:entry", ARXIV_NS):
        title_el = entry.find("atom:title", ARXIV_NS)
        id_el = entry.find("atom:id", ARXIV_NS)
        if title_el is None or id_el is None or not id_el.text:
            continue

        summary_el = entry.find("atom:summary", ARXIV_NS)
        published_el = entry.find("atom:published", ARXIV_NS)
        category_el = entry.find("arxiv:primary_category", ARXIV_NS)
        title = " ".join((title_el.text or "").split())
        url = id_el.text.strip()
        summary = " ".join((summary_el.text or "").split())[:1000] if summary_el is not None else ""
        published = (published_el.text or "")[:10] if published_el is not None else ""
        category = category_el.get("term", "") if category_el is not None else ""
        authors_elems = entry.findall("atom:author", ARXIV_NS)
        authors = _format_arxiv_authors(authors_elems)
        arxiv_id = re.sub(r"https?://arxiv\.org/abs/", "", url)

        entries.append(
            {
                "title": title,
                "url": url,
                "authors": authors,
                "summary": summary,
                "published": published,
                "category": category,
                "arxiv_id": arxiv_id,
                "doi": "",
                "source": source,
                "journal": "arXiv",
            }
        )
    return entries


def _format_arxiv_authors(author_elems: list[ET.Element]) -> str:
    names: list[str] = []
    for author in author_elems[:3]:
        name_el = author.find("atom:name", ARXIV_NS)
        if name_el is not None and name_el.text:
            names.append(name_el.text)
    authors = ", ".join(names)
    if len(author_elems) > 3:
        authors += " et al."
    return authors


def collect_pubmed(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    papers: list[dict[str, Any]] = []
    pm_cfg = cfg.get("pubmed", {})
    if not pm_cfg.get("enabled", False):
        logging.info("PubMed: disabled")
        return papers

    for keyword in pm_cfg.get("keywords", []):
        try:
            search_url = (
                f"{PUBMED_BASE}/esearch.fcgi"
                f"?db=pubmed&term={urllib.parse.quote(keyword)}"
                f"&retmax={int(pm_cfg.get('retmax', 10))}"
                "&sort=date&retmode=json"
                f"&datetype=pdat&reldate={int(pm_cfg.get('reldate', 7))}"
                f"&email={urllib.parse.quote(pm_cfg.get('email', ''))}"
            )
            resp = requests.get(search_url, timeout=30)
            resp.raise_for_status()
            id_list = resp.json().get("esearchresult", {}).get("idlist", [])
            if not id_list:
                continue

            fetch_url = (
                f"{PUBMED_BASE}/efetch.fcgi"
                f"?db=pubmed&id={','.join(id_list)}"
                "&retmode=xml"
                f"&email={urllib.parse.quote(pm_cfg.get('email', ''))}"
            )
            resp = requests.get(fetch_url, timeout=30)
            resp.raise_for_status()
            papers.extend(parse_pubmed_xml(resp.text, source=f"pubmed:kw:{keyword}"))
            time.sleep(1)
        except Exception as exc:
            logging.warning("PubMed keyword %s failed: %s", keyword, exc)

    logging.info("PubMed: collected %d papers", len(papers))
    return papers


def parse_pubmed_xml(xml_text: str, source: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    root = ET.fromstring(xml_text)
    for article in root.findall(".//PubmedArticle"):
        medline = article.find(".//MedlineCitation")
        art = medline.find("Article") if medline is not None else None
        if medline is None or art is None:
            continue

        title_el = art.find("ArticleTitle")
        abstract_el = art.find(".//AbstractText")
        pmid_el = medline.find("PMID")
        journal_el = art.find(".//Journal/Title")
        title = "".join(title_el.itertext()).strip() if title_el is not None else ""
        abstract = " ".join("".join(abstract_el.itertext()).split())[:1000] if abstract_el is not None else ""
        pmid = pmid_el.text if pmid_el is not None else ""
        journal = journal_el.text if journal_el is not None and journal_el.text else "PubMed"

        doi = ""
        for article_id in article.findall(".//ArticleId"):
            if article_id.get("IdType") == "doi":
                doi = article_id.text or ""
                break

        entries.append(
            {
                "title": title,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "authors": _format_pubmed_authors(art.findall(".//Author")),
                "summary": abstract,
                "published": "",
                "category": "",
                "arxiv_id": "",
                "doi": doi,
                "source": source,
                "journal": journal,
            }
        )
    return entries


def _format_pubmed_authors(author_elems: list[ET.Element]) -> str:
    parts: list[str] = []
    for author in author_elems[:3]:
        last = author.find("LastName")
        fore = author.find("ForeName")
        if last is None or not last.text:
            continue
        name = last.text
        if fore is not None and fore.text:
            name = f"{fore.text} {last.text}"
        parts.append(name)
    authors = ", ".join(parts)
    if len(author_elems) > 3:
        authors += " et al."
    return authors


def collect_rss(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    papers: list[dict[str, Any]] = []
    for feed_cfg in cfg.get("rss", {}).get("feeds", []):
        try:
            feed = feedparser.parse(feed_cfg["url"])
            for entry in feed.entries[:20]:
                title = entry.get("title", "").strip()
                url = entry.get("link", "").strip()
                if not title or not url:
                    continue
                papers.append(
                    {
                        "title": title,
                        "url": url,
                        "authors": entry.get("author", ""),
                        "summary": entry.get("summary", "")[:1000],
                        "published": entry.get("published", "")[:10],
                        "category": "",
                        "arxiv_id": "",
                        "doi": "",
                        "source": f"rss:{feed_cfg['name']}",
                        "journal": feed_cfg["name"],
                    }
                )
        except Exception as exc:
            logging.warning("RSS feed %s failed: %s", feed_cfg.get("name", "unknown"), exc)
        time.sleep(1)

    logging.info("RSS: collected %d papers", len(papers))
    return papers


def deduplicate(papers: list[dict[str, Any]], processed: set[str]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for paper in papers:
        key = normalize_url(paper["url"])
        if key not in seen and key not in processed:
            seen[key] = paper
    deduped = list(seen.values())
    logging.info(
        "Deduplicated: %d -> %d papers (%d already processed)",
        len(papers),
        len(deduped),
        len(processed),
    )
    return deduped


def build_evaluation_prompt(papers: list[dict[str, Any]], cfg: dict[str, Any], prompt_path: pathlib.Path) -> str:
    prompt_text = prompt_path.read_text()
    profile = cfg.get("profile", {})
    payload = json.dumps(
        [
            {
                "title": paper["title"],
                "url": paper["url"],
                "authors": paper["authors"],
                "summary": paper["summary"],
                "journal": paper["journal"],
                "category": paper["category"],
                "published": paper.get("published", ""),
            }
            for paper in papers
        ],
        ensure_ascii=False,
    )
    profile_payload = json.dumps(profile, ensure_ascii=False, indent=2)
    return (
        f"{prompt_text}\n\n"
        "# Researcher profile\n"
        f"```json\n{profile_payload}\n```\n\n"
        "# Output language\n"
        f"{profile.get('output_language', 'Japanese')}\n\n"
        "# Input papers\n"
        f"```json\n{payload}\n```\n"
    )


def evaluate_papers(
    papers: list[dict[str, Any]],
    cfg: dict[str, Any],
    prompt_path: pathlib.Path = DEFAULT_PROMPT_PATH,
) -> list[dict[str, Any]]:
    if not papers:
        return []

    claude_cfg = cfg.get("claude", {})
    command = str(claude_cfg.get("command", "claude"))
    model = str(claude_cfg.get("model", "sonnet"))
    timeout = int(claude_cfg.get("timeout_sec", 120))
    results: list[dict[str, Any]] = []
    batch_size = int(claude_cfg.get("batch_size", 10))

    for batch_start in range(0, len(papers), batch_size):
        batch = papers[batch_start : batch_start + batch_size]
        full_prompt = build_evaluation_prompt(batch, cfg, prompt_path)
        cmd = [command, "--print", "--output-format", "json", "--model", model, full_prompt]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            logging.error("Claude CLI timed out for batch %d", batch_start // batch_size)
            continue
        except Exception as exc:
            logging.error("Claude CLI evaluation failed: %s", exc)
            continue

        if result.returncode != 0:
            logging.error("Claude CLI failed (rc=%d): %s", result.returncode, result.stderr[:500])
            continue
        try:
            results.extend(parse_claude_json(result.stdout))
        except ValueError as exc:
            logging.error("Claude output parse failed: %s", exc)

    result_by_url = {result["url"]: result for result in results if "url" in result}
    for paper in papers:
        result = result_by_url.get(paper["url"], {})
        paper["relevance"] = int(result.get("relevance", 1))
        paper["summary"] = str(result.get("summary") or result.get("summary_ja") or "")
        paper["relevance_reason"] = str(result.get("relevance_reason", ""))

    return papers


def parse_claude_json(stdout: str) -> list[dict[str, Any]]:
    try:
        outer = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stdout is not JSON: {stdout[:200]}") from exc

    if isinstance(outer, list):
        return outer
    if isinstance(outer, dict):
        inner = outer.get("result", outer.get("content", ""))
        if isinstance(inner, list):
            return inner
        if not isinstance(inner, str):
            raise ValueError("Claude JSON does not contain a text result")
        match = re.search(r"\[.*\]", inner, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    raise ValueError("No JSON array found in Claude output")


def append_log(papers: list[dict[str, Any]], cfg: dict[str, Any]) -> pathlib.Path:
    path = state_dir(cfg) / f"{dt.date.today().strftime('%Y%m%d')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for paper in papers:
            rec = {
                "url": paper["url"],
                "title": paper["title"],
                "journal": paper["journal"],
                "relevance": paper.get("relevance", 0),
                "summary": paper.get("summary", ""),
                "relevance_reason": paper.get("relevance_reason", ""),
                "action": planned_action(paper, cfg),
                "ts": now_iso(),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    logging.info("Log: %d papers written to %s", len(papers), path)
    return path


def planned_action(paper: dict[str, Any], cfg: dict[str, Any]) -> str:
    relevance = int(paper.get("relevance", 0))
    actions: list[str] = []
    if cfg.get("slack", {}).get("enabled", False) and relevance >= int(cfg["thresholds"]["slack_min"]):
        actions.append("slack")
    if (
        cfg.get("pdf", {}).get("enabled", True)
        and relevance >= int(cfg["thresholds"]["pdf_min"])
        and paper.get("arxiv_id")
    ):
        actions.append("pdf")
    return "+".join(actions) if actions else "log_only"


def post_to_slack(slack: WebClient, cfg: dict[str, Any], papers: list[dict[str, Any]]) -> None:
    slack_cfg = cfg["slack"]
    threshold = int(cfg["thresholds"]["slack_min"])
    papers_to_post = sorted(
        [paper for paper in papers if int(paper.get("relevance", 0)) >= threshold],
        key=lambda paper: int(paper.get("relevance", 0)),
        reverse=True,
    )
    if not papers_to_post:
        logging.info("No papers above Slack threshold (%d)", threshold)
        return

    today = dt.date.today().isoformat()
    header_text = f"Paper Watch ({today}) - {len(papers_to_post)} papers"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
        {"type": "divider"},
    ]

    for paper in papers_to_post:
        text = (
            f":page_facing_up: *New paper (relevance: {paper['relevance']}/5)*\n"
            f"*Title:* <{paper['url']}|{paper['title']}>\n"
            f"*Journal:* {paper['journal']} | *Authors:* {paper['authors']}\n"
            f"*Summary:* {paper.get('summary', '')}\n"
            f":arrow_right: *Why it matters:* {paper.get('relevance_reason', '')}"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append({"type": "divider"})

    for chunk_start in range(0, len(blocks), 49):
        slack.chat_postMessage(
            channel=slack_cfg["channel"],
            text=header_text,
            blocks=blocks[chunk_start : chunk_start + 49],
            username=slack_cfg.get("username", "Paper Watch"),
            icon_emoji=slack_cfg.get("icon_emoji", ":page_facing_up:"),
            unfurl_links=False,
            unfurl_media=False,
        )
    logging.info("Slack: posted %d papers to %s", len(papers_to_post), slack_cfg["channel"])


def download_pdfs(papers: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
    if not cfg.get("pdf", {}).get("enabled", True):
        logging.info("PDF download: disabled")
        return
    threshold = int(cfg["thresholds"]["pdf_min"])
    download_dir = expand_path(cfg["pdf"]["download_dir"])
    download_dir.mkdir(parents=True, exist_ok=True)

    for paper in papers:
        if int(paper.get("relevance", 0)) < threshold or not paper.get("arxiv_id"):
            continue

        arxiv_id = str(paper["arxiv_id"])
        dest = download_dir / f"arxiv-{arxiv_id.replace('/', '-')}.pdf"
        if dest.exists():
            logging.debug("PDF already exists: %s", dest)
            continue

        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        try:
            resp = requests.get(pdf_url, timeout=60, stream=True)
            resp.raise_for_status()
            with dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            logging.info("PDF downloaded: %s", dest)
        except Exception as exc:
            logging.error("PDF download failed for %s: %s", pdf_url, exc)


def collect_papers(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    papers: list[dict[str, Any]] = []
    papers.extend(collect_arxiv(cfg))
    papers.extend(collect_pubmed(cfg))
    papers.extend(collect_rss(cfg))
    return papers


def run(
    cfg: dict[str, Any],
    config_path: pathlib.Path,
    prompt_path: pathlib.Path,
    dry_run: bool = False,
    collect_only: bool = False,
) -> None:
    rotate_processed(cfg)
    processed = load_processed(cfg)
    logging.info("Processed cache: %d URLs", len(processed))

    papers = deduplicate(collect_papers(cfg), processed)
    if collect_only:
        for paper in papers:
            print(
                json.dumps(
                    {
                        "title": paper["title"],
                        "url": paper["url"],
                        "journal": paper["journal"],
                        "source": paper["source"],
                    },
                    ensure_ascii=False,
                )
            )
        logging.info("Collect-only mode: %d papers found", len(papers))
        return

    if not papers:
        logging.info("No new papers to evaluate")
        return

    papers = evaluate_papers(papers, cfg, prompt_path)
    append_log(papers, cfg)

    if dry_run:
        for paper in papers:
            print(
                json.dumps(
                    {
                        "title": paper["title"],
                        "url": paper["url"],
                        "relevance": paper.get("relevance", 0),
                        "summary": paper.get("summary", ""),
                        "relevance_reason": paper.get("relevance_reason", ""),
                    },
                    ensure_ascii=False,
                )
            )
        logging.info("Dry-run mode: %d papers evaluated", len(papers))
        return

    if cfg.get("slack", {}).get("enabled", False):
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            logging.error("SLACK_BOT_TOKEN is not set; skipping Slack notification")
        else:
            post_to_slack(WebClient(token=token), cfg, papers)

    download_pdfs(papers, cfg)
    append_processed(papers, cfg)
    relevant = sum(
        1 for paper in papers if int(paper.get("relevance", 0)) >= int(cfg["thresholds"]["slack_min"])
    )
    logging.info("Done: %d papers evaluated, %d relevant", len(papers), relevant)
    logging.debug("Config path: %s", config_path)


def run_health(config_path: pathlib.Path, prompt_path: pathlib.Path, cfg: dict[str, Any] | None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if config_path.exists():
        checks.append(health_item("config", "ok", f"config readable at {config_path}"))
    else:
        checks.append(health_item("config", "fail", f"config missing at {config_path}"))

    if prompt_path.exists():
        checks.append(health_item("prompt", "ok", f"prompt readable at {prompt_path}"))
    else:
        checks.append(health_item("prompt", "fail", f"prompt missing at {prompt_path}"))

    if cfg is None:
        checks.append(health_item("config_load", "fail", "config could not be loaded"))
    else:
        checks.extend(run_config_health(cfg))

    command = cfg.get("claude", {}).get("command", "claude") if cfg else "claude"
    claude_path = shutil.which(str(command))
    if claude_path:
        checks.append(health_item("claude_cli", "ok", f"{command} found at {claude_path}"))
    else:
        checks.append(health_item("claude_cli", "warn", f"{command} not found in PATH"))

    if cfg and cfg.get("slack", {}).get("enabled", False):
        checks.append(run_slack_health())

    status = worst_status([check["status"] for check in checks])
    ok_count = sum(1 for check in checks if check["status"] == "ok")
    return {
        "system": "paper-watch",
        "status": status,
        "summary": f"paper-watch: {ok_count}/{len(checks)} checks ok",
        "checks": checks,
        "checked_at": now_iso(),
    }


def run_config_health(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    arxiv_sources = len(cfg.get("arxiv", {}).get("categories", [])) + len(
        cfg.get("arxiv", {}).get("keywords", [])
    )
    pubmed_sources = len(cfg.get("pubmed", {}).get("keywords", [])) if cfg.get("pubmed", {}).get("enabled") else 0
    rss_sources = len(cfg.get("rss", {}).get("feeds", []))
    total_sources = arxiv_sources + pubmed_sources + rss_sources
    if total_sources:
        checks.append(health_item("sources", "ok", f"{total_sources} source queries configured"))
    else:
        checks.append(health_item("sources", "fail", "no arXiv, PubMed, or RSS sources configured"))

    try:
        target = state_dir(cfg)
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".healthcheck.tmp"
        probe.write_text(now_iso())
        probe.unlink()
        checks.append(health_item("state_dir", "ok", f"state dir writable at {target}"))
    except Exception as exc:
        checks.append(health_item("state_dir", "fail", f"state dir is not writable: {exc}"))

    return checks


def run_slack_health() -> dict[str, Any]:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return health_item("slack_auth", "fail", "SLACK_BOT_TOKEN is not set")
    try:
        resp = WebClient(token=token).auth_test()
        team = resp.get("team") or "unknown team"
        user = resp.get("user") or resp.get("bot_id") or "unknown bot"
        return health_item("slack_auth", "ok", f"Slack auth ok for {user} on {team}")
    except Exception as exc:
        return health_item("slack_auth", "fail", f"Slack auth.test failed: {exc}")


def print_health(result: dict[str, Any], json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, ensure_ascii=False))
        return
    print(f"{result['system']}: {result['status'].upper()} - {result['summary']}")
    for check in result["checks"]:
        print(f"- {check['status'].upper()} {check['name']}: {check['summary']}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="paper-watch")
    parser.add_argument("--config", type=pathlib.Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--prompt", type=pathlib.Path, default=DEFAULT_PROMPT_PATH)
    parser.add_argument("--dry-run", action="store_true", help="evaluate only; no Slack, no PDF download")
    parser.add_argument("--collect-only", action="store_true", help="collect papers only; no evaluation")
    parser.add_argument("--health", action="store_true", help="run health checks, then exit")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output for --health")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    load_dotenv(ROOT / ".env")
    load_dotenv()

    cfg: dict[str, Any] | None = None
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        if not args.health:
            raise SystemExit(f"Config file not found: {args.config}. Copy config.example.toml first.")
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"Config file is not valid TOML: {exc}") from exc

    if args.health:
        print_health(run_health(args.config, args.prompt, cfg), json_output=args.json)
        return

    if cfg is None:
        raise SystemExit(f"Config file not found: {args.config}. Copy config.example.toml first.")
    if not args.prompt.exists():
        raise SystemExit(f"Prompt file not found: {args.prompt}")

    run(cfg, args.config, args.prompt, dry_run=args.dry_run, collect_only=args.collect_only)


if __name__ == "__main__":
    main(sys.argv[1:])
