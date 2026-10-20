import pytest

from context_hub import frontmatter
from context_hub.frontmatter import FrontmatterError

TRICKY = [
    "plain",
    "한글 제목",
    "colon: inside",
    "ends with colon:",
    "# looks like comment",
    "- looks like list",
    "[bracket] start",
    "has, comma",
    'has "quotes"',
    "it's fine",
    "true",
    "null",
    "123",
    "2026-10-20",
    "  leading space",
    "trailing space ",
    "",
    "multi\nline",
    "emoji ✅ and \\ backslash",
]


@pytest.mark.parametrize("value", TRICKY)
def test_scalar_roundtrip(value):
    meta = {"title": value, "tags": [value, "x"], "links": {"about": [value]}}
    parsed, body = frontmatter.parse(frontmatter.dump(meta, "본문"))
    assert parsed["title"] == value
    assert parsed["tags"] == [value, "x"]
    assert parsed["links"] == {"about": [value]}
    assert body == "본문"


def test_types_roundtrip():
    meta = {"n": 3, "flag": True, "off": False, "none": None, "empty": [], "map": {}, "date": "2026-10-20"}
    parsed, _ = frontmatter.parse(frontmatter.dump(meta, ""))
    assert parsed == meta


def test_hand_edited_block_lists_and_comments():
    text = """---
id: dec-1
# a comment line
sources:
  - inbox/a.jsonl#L1
  - 'single quoted'
links:
  supersedes: [dec-0]
  about: []
---

Body with [[concept-x|link]].
"""
    meta, body = frontmatter.parse(text)
    assert meta["sources"] == ["inbox/a.jsonl#L1", "single quoted"]
    assert meta["links"] == {"supersedes": ["dec-0"], "about": []}
    assert body.startswith("Body with")


def test_no_frontmatter_returns_text():
    assert frontmatter.parse("just text") == ({}, "just text")


@pytest.mark.parametrize("bad", [
    "---\nid: x\n",  # not closed
    "---\n  indented: first\n---\n",
    "---\nlist: [a, b\n---\n",
    "---\nno colon here\n---\n",
])
def test_errors_are_reported(bad):
    with pytest.raises(FrontmatterError):
        frontmatter.parse(bad)
