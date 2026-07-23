"""Diff parsing: line numbering must stay exact, since inline comments depend on it."""
from review_bot.diff_parser import OMISSION_PREFIX, addable_lines, added_line_numbers, parse_diff


def test_tracks_new_file_line_numbers():
    diff = "@@ -1,3 +1,4 @@\n def foo():\n-    return 1\n+    return 2\n+    # added\n def bar():\n"
    assert added_line_numbers(diff) == [2, 3]
    lines = addable_lines(diff)
    assert lines[1] == "def foo():"
    assert lines[2] == "    return 2"
    assert lines[4] == "def bar():"


def test_deleted_lines_are_not_commentable():
    diff = "@@ -1,2 +1,1 @@\n keep\n-gone\n"
    assert 2 not in addable_lines(diff)


def test_multiple_hunks_resume_at_declared_offsets():
    diff = "@@ -1,1 +1,2 @@\n a\n+b\n@@ -50,1 +60,2 @@\n c\n+d\n"
    assert added_line_numbers(diff) == [2, 61]


def test_omission_markers_do_not_shift_numbering():
    """A condensed diff carries annotations; counting them would misplace comments."""
    plain = "@@ -1,1 +1,2 @@\n a\n+b\n@@ -50,1 +60,2 @@\n c\n+d\n"
    annotated = (
        "@@ -1,1 +1,2 @@\n a\n+b\n"
        f"{OMISSION_PREFIX} 7 hunks omitted\n"
        "@@ -50,1 +60,2 @@\n c\n+d\n"
    )
    assert added_line_numbers(annotated) == added_line_numbers(plain)
    assert not any(v.startswith(OMISSION_PREFIX) for v in addable_lines(annotated).values())


def test_headers_are_ignored():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    assert added_line_numbers(diff) == [1]
    assert all(k.kind != "context" or "+++" not in k.content for k in parse_diff(diff))
