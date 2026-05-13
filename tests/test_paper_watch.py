import json

from paper_watch.cli import (
    deduplicate,
    normalize_url,
    parse_arxiv_xml,
    parse_claude_json,
    planned_action,
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
