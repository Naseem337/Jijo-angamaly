import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "secret_check", Path(__file__).resolve().parents[2] / "scripts" / "secret_check.py")
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)


def test_tracked_files_have_no_secrets_or_data_files():
    assert sc.check(sc.collect(staged=False)) == []


def test_detects_keys_without_returning_values():
    fake = "x = '" + "rzp" + "_live_" + "A1b2C3d4E5f6G7" + "'"
    problems = sc.check({"app/x.py": fake})
    assert problems == ["app/x.py:1: looks like a Razorpay live key"]
    assert "A1b2C3d4" not in problems[0]
    assert sc.check({"a.py": "EA" + "A" + "B" * 30})
    assert sc.check({"a.py": "gs" + "k_" + "a" * 20})
    assert sc.check({"a.py": "sk" + "-ant-" + "api03-abcdefgh"})


def test_prefix_mentions_in_docs_are_fine():
    assert sc.check({"docs/x.md": "test keys start with " + "rzp" + "_test_…"}) == []


def test_blocks_env_and_database_files():
    for bad in (".env", "data/railway_resort.env", "x.sqlite", "data/resort.sqlite3", "a.db", ".env.local"):
        assert sc.blocked_file(bad), bad
    assert not sc.blocked_file(".env.example")
    assert not sc.blocked_file("app/environment.py")
