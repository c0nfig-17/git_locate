"""Tests for the phase-2 chaining runner (the part that failed 'silently')."""
from gitlocate.config import load_config
from gitlocate.chaining import Chainer, _leading_executable
from gitlocate.models import Entity, Findings, KIND_REPO

MISSING = "gitlocate_nonexistent_tool_xyz"


def _one_repo():
    f = Findings()
    e = Entity(KIND_REPO, "acme/api", "https://github.com/acme/api")
    e.extra["owner_login"] = "acme"
    e.extra["clone_url"] = "https://github.com/acme/api.git"
    f.add(e)
    return f


def test_leading_executable():
    assert _leading_executable("trufflehog git URL --no-update >> o.jsonl") == "trufflehog"
    assert _leading_executable("GITHUB_TOKEN=abc github-subdomains -d x") == "github-subdomains"
    assert _leading_executable("FOO=1 BAR=2 tool -a") == "tool"
    assert _leading_executable("/usr/local/bin/gitleaks detect") == "/usr/local/bin/gitleaks"
    # unbalanced quotes -> undeterminable (let the shell try)
    assert _leading_executable('tool "unterminated') is None


def test_missing_tool_is_skipped_not_failed():
    cfg = load_config(None)
    cfg.data["chaining"]["clone"] = False
    cfg.data["chaining"]["commands"] = [f"{MISSING} scan {{repo_url}}"]
    stats = Chainer(cfg).run(_one_repo())
    assert stats["skipped"] == 1
    assert stats["failures"] == 0
    assert stats["commands_run"] == 0


def test_nonzero_exit_is_not_a_failure():
    cfg = load_config(None)
    cfg.data["chaining"]["clone"] = False
    cfg.data["chaining"]["commands"] = ["sh -c 'exit 1'"]  # findings-style non-zero
    stats = Chainer(cfg).run(_one_repo())
    assert stats["commands_run"] == 1
    assert stats["failures"] == 0


def test_clone_dir_command_skipped_when_clone_off():
    cfg = load_config(None)
    cfg.data["chaining"]["clone"] = False
    cfg.data["chaining"]["commands"] = ["gitleaks detect --source {clone_dir}"]
    stats = Chainer(cfg).run(_one_repo())
    assert stats["skipped"] == 1


def test_placeholders_expand(tmp_path):
    cfg = load_config(None)
    cfg.data["chaining"]["clone"] = False
    cfg.data["chaining"]["workdir"] = str(tmp_path)
    marker = tmp_path / "out.txt"
    cfg.data["chaining"]["commands"] = [
        "sh -c 'echo {repo} {owner} {name} > " + str(marker) + "'"
    ]
    stats = Chainer(cfg).run(_one_repo())
    assert stats["commands_run"] == 1
    assert marker.read_text().strip() == "acme/api acme api"


def test_dry_run_previews_even_missing_tools():
    cfg = load_config(None)
    cfg.data["chaining"]["clone"] = False
    cfg.data["chaining"]["commands"] = [f"{MISSING} scan {{repo_url}}"]
    stats = Chainer(cfg, dry_run=True).run(_one_repo())
    # dry-run does not skip on missing tools; it prints the command
    assert stats["commands_run"] == 1
    assert stats["skipped"] == 0


def test_no_commands_configured_is_not_an_error():
    cfg = load_config(None)
    cfg.data["chaining"]["commands"] = []
    stats = Chainer(cfg).run(_one_repo())
    assert stats == {"repos": 0, "commands_run": 0, "failures": 0,
                     "skipped": 0, "leaks_notified": 0}


def test_min_confidence_filters_repos():
    cfg = load_config(None)
    cfg.data["chaining"]["clone"] = False
    cfg.data["chaining"]["min_confidence"] = 0.5
    cfg.data["chaining"]["commands"] = ["sh -c 'true'"]
    f = _one_repo()
    # the single repo has confidence 0.0 -> filtered out
    stats = Chainer(cfg).run(f)
    assert stats["repos"] == 0
