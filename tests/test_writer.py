"""Tests for output document + notify payload building."""
from gitlocate.models import Entity, Findings, KIND_ORG, KIND_REPO, KIND_USER
from gitlocate.output import writer


def _findings():
    f = Findings()
    o = Entity(KIND_ORG, "acme", "https://github.com/acme"); o.confidence = 0.9
    f.add(o)
    r = Entity(KIND_REPO, "acme/site", "https://github.com/acme/site"); r.confidence = 0.8
    f.add(r)
    u = Entity(KIND_USER, "dev", "https://github.com/dev"); u.confidence = 0.4
    f.add(u)
    return f


def test_build_document_summary_and_urls():
    doc = writer.build_document(_findings(), ["Acme"], ["acme.com"])
    assert doc["summary"] == {"organizations": 1, "repositories": 1, "users": 1}
    assert doc["repo_urls"] == ["https://github.com/acme/site"]
    assert doc["targets"] == {"companies": ["Acme"], "domains": ["acme.com"]}


def test_phase_transition_payload_mentions_phase_and_repos():
    doc = writer.build_document(_findings(), ["Acme"], ["acme.com"])
    text = writer.build_notify_text(doc, payload="phase_transition")
    assert "Phase 1" in text
    assert "1 repositories queued for scanning." in text


def test_repos_payload():
    doc = writer.build_document(_findings(), ["Acme"], ["acme.com"])
    assert writer.build_notify_text(doc, payload="repos") == "https://github.com/acme/site"


def test_write_artifacts(tmp_path):
    f = _findings()
    doc = writer.build_document(f, ["Acme"], ["acme.com"])
    jp = tmp_path / "results.json"
    rp = tmp_path / "repos.txt"
    writer.write_json(doc, str(jp))
    writer.write_repos_flat(f, str(rp))
    assert jp.exists() and rp.read_text().strip() == "https://github.com/acme/site"
