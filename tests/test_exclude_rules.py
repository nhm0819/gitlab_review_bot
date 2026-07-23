"""Per-project exclude rules loaded from .gitlab/review-bot.yml."""
from pathlib import Path

from review_bot.exclude_rules import ReviewRules

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "review-bot.example.yml"


def test_missing_file_yields_empty_rules(tmp_path):
    rules = ReviewRules.load(tmp_path / "absent.yml")
    assert not rules.skip_mr("main", "feature", "alice")
    assert not rules.skip_path("anything.py")


def test_example_file_parses():
    rules = ReviewRules.load(EXAMPLE)
    assert rules.custom_instructions


def test_branch_and_author_patterns():
    rules = ReviewRules.load(EXAMPLE)
    assert rules.skip_mr("release/1.0", "feature/x", "alice")
    assert rules.skip_mr("main", "dependabot/npm/x", "alice")
    assert rules.skip_mr("main", "feature/x", "ci-bot")
    assert not rules.skip_mr("main", "feature/x", "alice")


def test_path_patterns():
    rules = ReviewRules.load(EXAMPLE)
    assert rules.skip_path("yarn.lock")
    assert rules.skip_path("app/vendor/lib.js")
    assert not rules.skip_path("src/main.py")
