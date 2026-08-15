"""Tests for phase-3 per-leak parsing and notification."""
import json

from gitlocate.config import load_config
from gitlocate.output import leaks as leaks_mod
from gitlocate.output.leaks import (
    Leak,
    LeakNotifier,
    collect_leaks,
    parse_gitleaks_json,
    parse_trufflehog_jsonl,
)


def _write_trufflehog(path, *objs):
    with open(path, "w", encoding="utf-8") as fh:
        for obj in objs:
            fh.write(json.dumps(obj) + "\n")


def _th_finding(detector="AWS", verified=True, repo="acme/api", file="a.env",
                line=7, commit="abcdef1234567890", redacted="AKIA****"):
    return {
        "SourceMetadata": {"Data": {"Git": {
            "repository": f"https://github.com/{repo}.git",
            "file": file, "line": line, "commit": commit,
        }}},
        "DetectorName": detector,
        "Verified": verified,
        "Redacted": redacted,
    }


def test_trufflehog_verified_only_filters(tmp_path):
    p = tmp_path / "trufflehog.jsonl"
    _write_trufflehog(p, _th_finding(verified=True), _th_finding(verified=False))
    verified = parse_trufflehog_jsonl(str(p), verified_only=True)
    assert len(verified) == 1 and verified[0].verified
    both = parse_trufflehog_jsonl(str(p), verified_only=False)
    assert len(both) == 2


def test_trufflehog_extracts_location(tmp_path):
    p = tmp_path / "trufflehog.jsonl"
    _write_trufflehog(p, _th_finding(file="config/prod.env", line=12,
                                     repo="acme/api"))
    leak = parse_trufflehog_jsonl(str(p))[0]
    assert leak.repo == "acme/api"
    assert leak.file == "config/prod.env"
    assert leak.line == 12
    assert leak.commit == "abcdef123456"        # truncated to 12
    assert leak.tool == "trufflehog"


def test_trufflehog_skips_non_finding_lines(tmp_path):
    p = tmp_path / "trufflehog.jsonl"
    with open(p, "w", encoding="utf-8") as fh:
        fh.write('{"level":"info-0","msg":"running source"}\n')  # a log line
        fh.write("not json at all\n")
        fh.write(json.dumps(_th_finding()) + "\n")
    assert len(parse_trufflehog_jsonl(str(p))) == 1


def test_gitleaks_parsing_and_repo_hint(tmp_path):
    p = tmp_path / "acme__api.gitleaks.json"
    p.write_text(json.dumps([
        {"RuleID": "generic-api-key", "File": "src/x.py", "StartLine": 4,
         "Commit": "deadbeef", "Match": "key=..."},
    ]))
    leaks = parse_gitleaks_json(str(p), repo_hint="acme/api")
    assert len(leaks) == 1
    assert leaks[0].rule == "generic-api-key"
    assert leaks[0].repo == "acme/api"
    assert leaks[0].line == 4
    assert leaks[0].verified is False


def test_collect_leaks_reads_both_tools(tmp_path):
    _write_trufflehog(tmp_path / "trufflehog.jsonl", _th_finding())
    (tmp_path / "acme__api.gitleaks.json").write_text(
        json.dumps([{"RuleID": "aws", "File": "a", "StartLine": 1}]))
    leaks = collect_leaks(str(tmp_path), verified_only=True)
    tools = sorted(l.tool for l in leaks)
    assert tools == ["gitleaks", "trufflehog"]


def test_fingerprint_is_stable_and_message_has_location():
    leak = Leak(tool="trufflehog", rule="AWS", repo="acme/api",
                file="a.env", line=7, commit="abc123", verified=True)
    assert leak.fingerprint() == Leak(
        tool="trufflehog", rule="AWS", repo="acme/api", file="a.env",
        line=7, commit="abc123", verified=True).fingerprint()
    msg = leak.format_message()
    assert "acme/api" in msg and "a.env:7" in msg and "VERIFIED" in msg


def test_notifier_dedupes_across_sweeps(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(leaks_mod.notify_mod, "send",
                        lambda cfg, payload, quiet=False: sent.append(payload) or True)
    _write_trufflehog(tmp_path / "trufflehog.jsonl", _th_finding(file="a.env"))
    cfg = load_config(None)
    notifier = LeakNotifier(cfg, str(tmp_path))

    assert notifier.sweep() == 1          # first time: one leak sent
    assert notifier.sweep() == 0          # nothing new
    assert len(sent) == 1

    # a fresh notifier reloads the persisted seen-set -> still no re-send
    assert LeakNotifier(cfg, str(tmp_path)).sweep() == 0
    assert len(sent) == 1


def test_notifier_respects_max_messages(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(leaks_mod.notify_mod, "send",
                        lambda cfg, payload, quiet=False: sent.append(payload) or True)
    _write_trufflehog(tmp_path / "trufflehog.jsonl",
                      _th_finding(file="a"), _th_finding(file="b"),
                      _th_finding(file="c"))
    cfg = load_config(None)
    cfg.data["notify"]["per_leak_max_messages"] = 2
    notifier = LeakNotifier(cfg, str(tmp_path))
    notifier.sweep()
    assert len(sent) == 2                  # capped


def test_dry_run_does_not_persist_state(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(leaks_mod.notify_mod, "send",
                        lambda cfg, payload, quiet=False: sent.append(payload) or True)
    _write_trufflehog(tmp_path / "trufflehog.jsonl", _th_finding(file="a.env"))
    cfg = load_config(None)

    # a preview marks nothing as sent on disk...
    assert LeakNotifier(cfg, str(tmp_path), dry_run=True).sweep() == 1
    assert not (tmp_path / leaks_mod.STATE_FILE).exists()
    # ...so a subsequent real run still sends the leak.
    assert LeakNotifier(cfg, str(tmp_path)).sweep() == 1
    assert (tmp_path / leaks_mod.STATE_FILE).exists()


def test_enabled_flag():
    cfg = load_config(None)
    cfg.data["notify"]["per_leak"] = True
    assert leaks_mod.enabled(cfg) is True
    cfg.data["notify"]["per_leak"] = False
    assert leaks_mod.enabled(cfg) is False
