"""Large-diff handling: ranking, condensing, and batching must never lose the important parts."""
import pytest

from review_bot.batching import (
    FileChange, batch, condense_diff, hunk_signature, is_low_value, prepare, priority,
    score_hunk, split_hunks,
)
from review_bot.diff_parser import addable_lines, added_line_numbers


def hunk(start, lines):
    return f"@@ -{start},3 +{start},{len(lines)} @@\n" + "".join(lines)


SECURITY = hunk(10, ["-    if user.role == 'admin':\n", "+    if user.token or True:\n"])
LOGIC = hunk(20, ["-    return total / count\n", "+    return total / max(count, 1)\n"])
IMPORTS = hunk(1, ["-import os\n", "+import os, sys\n"])
COMMENTS = hunk(30, ["-# old\n", "+# new\n"])
WHITESPACE = hunk(40, ["-\n", "+   \n"])


@pytest.mark.parametrize("path,expected", [
    ("yarn.lock", True), ("package-lock.json", True), ("src/app.min.js", True),
    ("api/v1_pb2.py", True), ("frontend/dist/bundle.js", True),
    ("src/main.py", False), ("internal/handler.go", False),
])
def test_low_value_detection(path, expected):
    assert is_low_value(path) is expected


def test_hunk_scores_are_ordered_by_review_value():
    assert (score_hunk(SECURITY) > score_hunk(LOGIC) > score_hunk(IMPORTS)
            > score_hunk(COMMENTS) > score_hunk(WHITESPACE))


def test_source_file_outranks_a_much_larger_lockfile():
    source = FileChange("src/main.py", "x" * 1000, added_lines=list(range(50)))
    lock = FileChange("yarn.lock", "x" * 90000, added_lines=list(range(3000)))
    assert priority(source) > priority(lock)


def test_signature_normalizes_literals_so_repeated_edits_collide():
    a = hunk(10, ["-  retries = 3\n", "+  retries = 5\n"])
    b = hunk(90, ["-  retries = 7\n", "+  retries = 9\n"])
    assert hunk_signature(a) == hunk_signature(b)


def _boilerplate_plus_critical():
    boiler = "".join(
        hunk(100 + i * 10, [f"-  log.debug('step {i}')\n", f"+  log.info('step {i}')\n"])
        for i in range(60)
    )
    critical = hunk(9000, [
        "-    if not verify_signature(payload, secret):\n",
        "-        raise AuthError('bad signature')\n",
        "+    # signature check disabled\n",
        "+    pass\n",
    ])
    return boiler + critical


def test_condensing_keeps_a_critical_hunk_at_the_end_of_a_large_file():
    big = _boilerplate_plus_critical()
    out, condensed, omitted = condense_diff(big, 1200)
    assert condensed and omitted > 0
    assert len(out) <= 1200
    assert "verify_signature" in out, "the security change must survive condensing"


def test_prefix_truncation_would_have_lost_it():
    """Guards the regression this feature exists to fix."""
    assert "verify_signature" not in _boilerplate_plus_critical()[:1200]


def test_condensed_diff_keeps_line_numbers_traceable():
    out, _, _ = condense_diff(_boilerplate_plus_critical(), 1200)
    shown = {line[1:] for line in out.splitlines() if line[:1] in ("+", " ")}
    for number, content in addable_lines(out).items():
        assert content in shown, f"line {number} was never shown to the model"
    assert 9000 in added_line_numbers(out)


def test_single_oversized_hunk_still_produces_valid_output():
    huge = hunk(1, [f"+    v{i} = compute({i})\n" for i in range(500)])
    out, condensed, _ = condense_diff(huge, 400)
    assert condensed and out.startswith("@@") and len(out) <= 500


def test_small_diff_is_untouched():
    small = hunk(1, ["-a\n", "+b\n"])
    assert condense_diff(small, 10000) == (small, False, 0)


def test_split_hunks_roundtrips():
    diff = SECURITY + LOGIC
    preamble, hunks = split_hunks(diff)
    assert preamble == "" and len(hunks) == 2
    assert "".join(hunks) == diff


def test_batching_splits_instead_of_dropping():
    files = [FileChange(f"src/f{i}.py", "d" * 3000, added_lines=list(range(20))) for i in range(10)]
    batches, dropped = batch(prepare(files, 40000), batch_chars=10000, max_batches=8)
    assert not dropped
    assert sum(len(b) for b in batches) == 10


def test_every_file_is_either_batched_or_reported():
    files = [FileChange(f"src/g{i}.py", "d" * 9000, added_lines=list(range(20))) for i in range(30)]
    batches, dropped = batch(prepare(files, 40000), batch_chars=10000, max_batches=3)
    assert len(batches) == 3
    assert sum(len(b) for b in batches) + len(dropped) == 30, "a file must never vanish silently"


def test_low_value_files_are_dropped_before_important_ones():
    mix = [FileChange("src/important.py", "d" * 9000, added_lines=list(range(100)))]
    mix += [FileChange(f"v{i}/vendor/x.js", "d" * 9000, added_lines=list(range(100))) for i in range(5)]
    batches, dropped = batch(prepare(mix, 40000), batch_chars=10000, max_batches=1)
    kept = [c.path for b in batches for c in b]
    assert "src/important.py" in kept
    assert all("vendor" in c.path for c in dropped)
