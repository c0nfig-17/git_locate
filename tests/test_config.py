"""Tests for config loading, defaults, env overrides and error reporting."""
import pytest

from gitlocate.config import load_config


def test_defaults_enable_phase2_and_notify():
    cfg = load_config(None)
    assert cfg.get("chaining.enabled") is True
    assert cfg.get("chaining.clone") is True
    assert cfg.get("notify.on_phase_transition") is True
    assert cfg.get("github.include_forks") is False
    # ships working default scanners
    cmds = cfg.get("chaining.commands")
    assert any(c.startswith("trufflehog") for c in cmds)
    assert any(c.startswith("gitleaks") for c in cmds)


def test_dotted_get_default():
    cfg = load_config(None)
    assert cfg.get("nope.nothing.here", "fallback") == "fallback"


def test_env_override_disables_chaining(monkeypatch):
    monkeypatch.setenv("GITLOCATE_CHAINING", "false")
    cfg = load_config(None)
    assert cfg.get("chaining.enabled") is False


def test_env_override_output_dir(monkeypatch):
    monkeypatch.setenv("GITLOCATE_OUTPUT_DIR", "/tmp/xyz-out")
    cfg = load_config(None)
    assert cfg.get("output.dir") == "/tmp/xyz-out"


def test_secret_resolution(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    cfg = load_config(None)
    assert cfg.github_token == "ghp_secret"


def test_file_overrides_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("output:\n  dir: ./custom-out\nchaining:\n  enabled: false\n")
    cfg = load_config(str(p))
    assert cfg.get("output.dir") == "./custom-out"
    assert cfg.get("chaining.enabled") is False
    # untouched keys keep their defaults
    assert cfg.get("chaining.clone") is True


def test_malformed_yaml_raises_valueerror(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("chaining:\n  commands:\n - \"bad indent\"\n")
    with pytest.raises(ValueError):
        load_config(str(p))


def test_non_mapping_config_raises(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError):
        load_config(str(p))
