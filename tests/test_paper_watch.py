import json
import subprocess

from paper_watch.cli import (
    build_llm_command,
    deduplicate,
    evaluate_papers,
    normalize_url,
    parse_arxiv_xml,
    parse_claude_json,
    parse_llm_json,
    planned_action,
    selected_llm_provider,
)


def test_normalize_url_removes_arxiv_version():
    assert normalize_url("https://arxiv.org/abs/2601.12345v2/") == "https://arxiv.org/abs/2601.12345"


def test_deduplicate_uses_processed_normalized_urls():
    papers = [
        {"url": "https://arxiv.org/abs/2601.12345v2", "title": "old"},
        {"url": "https://arxiv.org/abs/2601.99999v1", "title": "new"},
        {"url": "https://arxiv.org/abs/2601.99999v2", "title": "duplicate"},
    ]
    processed = {"https://arxiv.org/abs/2601.12345"}

    assert deduplicate(papers, processed) == [{"url": "https://arxiv.org/abs/2601.99999v1", "title": "new"}]


def test_parse_arxiv_xml_extracts_core_fields():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry>
        <id>http://arxiv.org/abs/2601.12345v1</id>
        <title> A sample paper </title>
        <summary> Abstract text. </summary>
        <published>2026-01-01T00:00:00Z</published>
        <arxiv:primary_category term="cs.CV"/>
        <author><name>Ada Lovelace</name></author>
        <author><name>Alan Turing</name></author>
      </entry>
    </feed>
    """

    papers = parse_arxiv_xml(xml, "arxiv:kw:test")

    assert papers[0]["title"] == "A sample paper"
    assert papers[0]["category"] == "cs.CV"
    assert papers[0]["authors"] == "Ada Lovelace, Alan Turing"
    assert papers[0]["arxiv_id"] == "2601.12345v1"


def test_parse_claude_json_supports_outer_result_shape():
    stdout = json.dumps(
        {
            "result": 'Here is the result: [{"url":"https://example.com","relevance":4,"summary":"x","relevance_reason":"y"}]'
        }
    )

    parsed = parse_claude_json(stdout)

    assert parsed[0]["url"] == "https://example.com"
    assert parsed[0]["relevance"] == 4


def test_parse_llm_json_supports_raw_and_gemini_shapes():
    raw = 'Final answer:\n[{"url":"https://example.com/raw","relevance":3}]'
    gemini = json.dumps({"response": '[{"url":"https://example.com/gemini","relevance":5}]'})
    content_blocks = json.dumps(
        {"content": [{"type": "text", "text": '[{"url":"https://example.com/content","relevance":2}]'}]}
    )

    assert parse_llm_json(raw)[0]["url"] == "https://example.com/raw"
    assert parse_llm_json(gemini)[0]["relevance"] == 5
    assert parse_llm_json(content_blocks)[0]["url"] == "https://example.com/content"


def test_build_llm_command_defaults_for_supported_providers():
    prompt = "return json"

    assert build_llm_command("claude", {"command": "claude", "model": "sonnet"}, prompt) == [
        "claude",
        "--print",
        "--output-format",
        "json",
        "--model",
        "sonnet",
        prompt,
    ]
    assert build_llm_command("codex", {"command": "codex", "model": ""}, prompt) == [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        prompt,
    ]
    assert build_llm_command("gemini", {"command": "gemini", "model": "auto"}, prompt) == [
        "gemini",
        "--prompt",
        prompt,
        "--output-format",
        "json",
        "--model",
        "auto",
    ]


def test_selected_llm_provider_keeps_legacy_claude_config_working():
    provider, llm_cfg, provider_cfg = selected_llm_provider(
        {"claude": {"command": "custom-claude", "model": "sonnet", "timeout_sec": 60}}
    )

    assert provider == "claude"
    assert llm_cfg["timeout_sec"] == 60
    assert provider_cfg["command"] == "custom-claude"


def test_evaluate_papers_uses_configured_provider_command(monkeypatch, tmp_path):
    prompt_path = tmp_path / "evaluate.md"
    prompt_path.write_text("Return JSON only.")
    papers = [
        {
            "title": "Sample",
            "url": "https://example.com/paper",
            "authors": "Ada",
            "summary": "Abstract",
            "journal": "arXiv",
            "category": "cs.CV",
        }
    ]
    cfg = {
        "profile": {"output_language": "Japanese"},
        "llm": {
            "provider": "gemini",
            "timeout_sec": 10,
            "batch_size": 10,
            "providers": {"gemini": {"command": "fake-gemini", "model": "auto"}},
        },
    }
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, check):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {
                    "response": json.dumps(
                        [
                            {
                                "url": "https://example.com/paper",
                                "relevance": 4,
                                "summary": "要約",
                                "relevance_reason": "理由",
                            }
                        ],
                        ensure_ascii=False,
                    )
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    evaluated = evaluate_papers(papers, cfg, prompt_path)

    assert captured["cmd"][:2] == ["fake-gemini", "--prompt"]
    assert "--output-format" in captured["cmd"]
    assert evaluated[0]["relevance"] == 4
    assert evaluated[0]["summary"] == "要約"


def test_planned_action_respects_enabled_integrations():
    cfg = {
        "slack": {"enabled": True},
        "pdf": {"enabled": True},
        "thresholds": {"slack_min": 4, "pdf_min": 5},
    }
    paper = {"relevance": 5, "arxiv_id": "2601.12345v1"}

    assert planned_action(paper, cfg) == "slack+pdf"

    cfg["slack"]["enabled"] = False
    assert planned_action(paper, cfg) == "pdf"
