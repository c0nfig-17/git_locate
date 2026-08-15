"""End-to-end CLI orchestration tests with a stubbed enumeration phase."""
import json

import gitlocate.cli as cli
from gitlocate.models import Entity, Findings, KIND_ORG, KIND_REPO


class FakeEngine:
    def __init__(self, config):
        pass

    def run(self, companies, domains):
        f = Findings()
        o = Entity(KIND_ORG, "acme", "https://github.com/acme"); o.confidence = 0.9
        f.add(o)
        r = Entity(KIND_REPO, "acme/web", "https://github.com/acme/web"); r.confidence = 0.8
        r.extra["clone_url"] = "https://github.com/acme/web.git"
        f.add(r)
        return f


def _patch(monkeypatch, calls):
    class FakeChainer:
        def __init__(self, config, dry_run=False):
            calls["chain"] = True

        def run(self, findings):
            return {"repos": 1, "commands_run": 0, "failures": 0, "skipped": 0}

    monkeypatch.setattr(cli, "Engine", FakeEngine)
    monkeypatch.setattr(cli, "Chainer", FakeChainer)
    monkeypatch.setattr(cli.notify_mod, "send",
                        lambda cfg, txt: calls.__setitem__("notify", True) or True)


def test_default_run_does_all_phases(tmp_path, monkeypatch):
    calls = {}
    _patch(monkeypatch, calls)
    out = tmp_path / "out"
    rc = cli.main(["-c", "Acme", "-d", "acme.com", "-o", str(out), "--no-log-file"])
    assert rc == 0
    assert calls.get("chain") is True      # phase 2 ran by default
    assert calls.get("notify") is True     # notify fired by default
    doc = json.loads((out / "results.json").read_text())
    assert doc["summary"]["organizations"] == 1
    assert doc["summary"]["repositories"] == 1


def test_no_chain_no_notify_skips_both(tmp_path, monkeypatch):
    calls = {}
    _patch(monkeypatch, calls)
    out = tmp_path / "out"
    rc = cli.main(["-c", "Acme", "-d", "acme.com", "-o", str(out),
                   "--no-chain", "--no-notify", "--no-log-file"])
    assert rc == 0
    assert "chain" not in calls
    assert "notify" not in calls
    assert (out / "results.json").exists()


def test_missing_company_is_an_error(tmp_path, monkeypatch):
    _patch(monkeypatch, {})
    rc = cli.main(["-d", "acme.com", "-o", str(tmp_path / "o"), "--no-log-file"])
    assert rc == 2


def test_print_config_exits_zero(capsys):
    rc = cli.main(["--print-config"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"chaining"' in out
