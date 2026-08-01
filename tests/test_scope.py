"""The scope block, and what it survives.

A human edits this in Linear between the two halves of Red. Every test here is
about that edit not costing them their work.
"""

from __future__ import annotations

from red.brief import parse_content, parse_draft, render_content, render_draft
from red.models import Project, ScopeItem
from red.scope import SCOPE_MARKER, parse_scope, render_scope, split_scope

from fakes import project


def test_scope_round_trips():
    items = [
        ScopeItem(1, "Add a token bucket", "Implement it.", []),
        ScopeItem(2, "Wire it in", "Call it from the middleware.", [1]),
    ]
    assert parse_scope(render_scope(items)) == items


def test_a_whole_project_round_trips_through_the_draft_format():
    original = project(slices=3)
    parsed = parse_draft(render_draft(original))

    assert parsed.name == original.name
    assert parsed.summary == original.summary
    assert parsed.outcome == original.outcome
    assert parsed.success_criteria == original.success_criteria
    assert parsed.constraints == original.constraints
    assert parsed.out_of_scope == original.out_of_scope
    assert parsed.scope == original.scope


def test_an_inserted_slice_renumbers_and_keeps_its_ordering():
    """Someone adds a slice at the top in Linear and does not renumber."""
    block = "\n".join(
        [
            SCOPE_MARKER,
            "### 3. Do the new thing first",
            "depends_on: []",
            "",
            "New prose.",
            "",
            SCOPE_MARKER,
            "### 1. Add a token bucket",
            "depends_on: []",
            "",
            "Implement it.",
            "",
            SCOPE_MARKER,
            "### 2. Wire it in",
            "depends_on: [1]",
            "",
            "Call it.",
        ]
    )
    items = parse_scope(block)

    assert [i.number for i in items] == [1, 2, 3]
    assert [i.title for i in items] == [
        "Do the new thing first",
        "Add a token bucket",
        "Wire it in",
    ]
    # "depends_on: [1]" meant the slice labelled 1, which is now at position 2.
    assert items[2].depends_on == [2]


def test_scope_survives_losing_every_marker():
    """Linear's editor, or a person, strips the HTML comments."""
    block = "\n".join(
        [
            "### 1. Add a token bucket",
            "depends_on: []",
            "",
            "Implement it.",
            "",
            "### 2. Wire it in",
            "depends_on: [1]",
            "",
            "Call it.",
        ]
    )
    items = parse_scope(block)
    assert [i.title for i in items] == ["Add a token bucket", "Wire it in"]
    assert items[1].depends_on == [1]


def test_scope_survives_losing_the_numbering_and_the_depends_lines():
    block = "\n".join(
        [
            "### Add a token bucket",
            "",
            "Implement it.",
            "",
            "### Wire it in",
            "",
            "Call it.",
        ]
    )
    items = parse_scope(block)
    assert [(i.number, i.title, i.depends_on) for i in items] == [
        (1, "Add a token bucket", []),
        (2, "Wire it in", []),
    ]
    assert items[0].prose == "Implement it."


def test_a_dangling_dependency_is_dropped_not_raised():
    block = f"{SCOPE_MARKER}\n### 1. Only slice\ndepends_on: [7]\n\nProse."
    assert parse_scope(block)[0].depends_on == []


def test_a_slice_cannot_depend_on_itself():
    block = f"{SCOPE_MARKER}\n### 1. Only slice\ndepends_on: [1]\n\nProse."
    assert parse_scope(block)[0].depends_on == []


def test_split_scope_separates_the_brief_from_the_work():
    content = render_content(project())
    brief, block = split_scope(content)

    assert "## Outcome" in brief
    assert "## Out of scope" in brief
    assert "Slice 1" not in brief
    assert "Slice 1" in block


def test_split_scope_stops_at_the_heading_when_the_markers_are_gone():
    content = "## Outcome\n\nA thing.\n\n## Scope\n\n### 1. Only slice\n\nProse."
    brief, block = split_scope(content)
    assert brief.strip() == "## Outcome\n\nA thing."
    assert "Only slice" in block


def test_an_empty_scope_is_empty_not_an_error():
    assert parse_scope("") == []
    assert parse_content("## Outcome\n\nA thing.")["scope"] == []


def test_parse_content_drops_the_placeholders_it_wrote():
    empty = Project(name="Bare")
    parsed = parse_content(render_content(empty))
    assert parsed["outcome"] == ""
    assert parsed["constraints"] == ""
    assert parsed["out_of_scope"] == ""
    assert parsed["success_criteria"] == []


def test_criteria_survive_being_ticked_off_in_linear():
    content = "## Success criteria\n- [x] done one\n- [ ] not done\n\n## Scope\n"
    assert parse_content(content)["success_criteria"] == ["done one", "not done"]
