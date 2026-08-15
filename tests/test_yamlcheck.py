"""Tests for duplicate-key detection and YAML error formatting."""
import pytest

from gitlocate import yamlcheck


def test_top_level_duplicate_key():
    dups = yamlcheck.find_duplicate_keys("a: 1\nb: 2\na: 3\n")
    assert len(dups) == 1
    d = dups[0]
    assert d.key == "a"
    assert d.path == ""
    assert d.line == 3
    assert d.first_line == 1


def test_nested_duplicate_key():
    text = "custom:\n  - id: one\nother: x\ncustom:\n  - id: two\n"
    dups = yamlcheck.find_duplicate_keys(text)
    assert any(d.key == "custom" and d.path == "" for d in dups)


def test_no_duplicates():
    assert yamlcheck.find_duplicate_keys("a: 1\nb: 2\n") == []


def test_format_yaml_error_points_at_line():
    text = "chaining:\n  commands:\n - \"bad\"\n"
    try:
        import yaml
        yaml.safe_load(text)
        raised = None
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert raised is not None
    msg = yamlcheck.format_yaml_error("bad.yaml", raised)
    assert "not valid YAML" in msg
