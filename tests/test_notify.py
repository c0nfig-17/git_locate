"""Tests for Notify provider-config validation and command building."""
from gitlocate.config import load_config
from gitlocate.output import notify as notify_mod


def test_valid_provider_config_has_no_problems(tmp_path):
    p = tmp_path / "provider.yaml"
    p.write_text(
        "custom:\n"
        "  - id: one\n"
        "    custom_webhook_url: https://example.invalid/one\n"
        "  - id: two\n"
        "    custom_webhook_url: https://example.invalid/two\n"
    )
    assert notify_mod.check_provider_config(str(p)) == []


def test_duplicate_provider_key_is_reported(tmp_path):
    p = tmp_path / "provider.yaml"
    p.write_text(
        "custom:\n"
        "  - id: one\n"
        "    custom_webhook_url: https://example.invalid/one\n"
        "custom:\n"
        "  - id: two\n"
        "    custom_webhook_url: https://example.invalid/two\n"
    )
    problems = notify_mod.check_provider_config(str(p))
    assert problems
    joined = "\n".join(problems)
    assert "duplicate" in joined.lower()
    assert "custom" in joined


def test_missing_file_required(tmp_path):
    p = tmp_path / "does-not-exist.yaml"
    problems = notify_mod.check_provider_config(str(p), require_exists=True)
    assert problems and "not found" in problems[0].lower()


def test_missing_file_optional_is_ok(tmp_path):
    p = tmp_path / "does-not-exist.yaml"
    assert notify_mod.check_provider_config(str(p), require_exists=False) == []


def test_build_command_none_when_binary_absent():
    cfg = load_config(None)
    cfg.data["notify"]["binary"] = "notify_definitely_missing_binary_xyz"
    assert notify_mod.build_command(cfg) is None


def test_resolve_provider_config_prefers_env(monkeypatch, tmp_path):
    p = tmp_path / "prov.yaml"
    p.write_text("custom: []\n")
    monkeypatch.setenv("NOTIFY_PROVIDER_CONFIG", str(p))
    cfg = load_config(None)
    path, explicit = notify_mod.resolve_provider_config(cfg)
    assert path == str(p)
    assert explicit is True
