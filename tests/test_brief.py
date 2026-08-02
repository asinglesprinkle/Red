"""The push half: prose in, a drafted project out, nothing created until told."""

from __future__ import annotations

import json

import pytest

from forman.review import Approval, Decision, Question

from red.brief import (
    BriefError,
    draft_project,
    parse_brief,
    parse_draft,
    redraft_project,
    render_draft,
    to_project,
)

from fakes import ScriptedConversation, brief_json


class Answering:
    """A Reviewer that answers questions from a script."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.questions: list[Question] = []

    def show(self, text: str) -> None:
        pass

    def answer(self, question: Question) -> str:
        self.questions.append(question)
        return self.answers.pop(0)

    def decide(self, approval: Approval) -> Decision:
        raise AssertionError("draft_project must not reach the gate")


# -- the conversation ---------------------------------------------------------


def test_prose_becomes_a_project_after_the_agent_stops_asking():
    reviewer = Answering(["python, and only the write endpoints"])
    conversation = ScriptedConversation(
        ["Which language, and which endpoints?", brief_json()]
    )

    project = draft_project(
        prose="rate limit the api", reviewer=reviewer, conversation=conversation
    )

    assert project.name == "Rate limiting"
    assert [item.title for item in project.scope] == [
        "Add a token bucket",
        "Wire it into the middleware",
    ]
    assert project.scope[1].depends_on == [1]
    assert [q.text for q in reviewer.questions] == ["Which language, and which endpoints?"]


def test_the_agent_is_cut_off_after_three_rounds_and_drafts_anyway():
    reviewer = Answering(["a", "b", "c"])
    conversation = ScriptedConversation(["q1", "q2", "q3", "q4", brief_json()])

    project = draft_project(
        prose="rate limit the api", reviewer=reviewer, conversation=conversation
    )

    assert project.name == "Rate limiting"
    assert len(reviewer.questions) == 3


def test_a_failed_session_is_an_error_and_not_an_empty_project():
    def failing(**_kwargs):
        from forman.spawn import AgentRun

        return AgentRun(text="", error="session died")

    with pytest.raises(BriefError, match="session died"):
        draft_project(prose="x", reviewer=Answering([]), conversation=failing)


def test_redrafting_carries_the_feedback_and_asks_nothing():
    conversation = ScriptedConversation([brief_json(name="Rate limiting v2")])
    original = to_project(json.loads(brief_json())["project"])

    revised = redraft_project(
        prose="rate limit the api",
        project=original,
        feedback="fold the two slices into one",
        conversation=conversation,
    )

    assert revised.name == "Rate limiting v2"
    assert "fold the two slices into one" in conversation.openings[0]
    assert "do not ask more questions" in conversation.openings[0]


# -- parsing ------------------------------------------------------------------


def test_output_without_a_project_object_is_refused():
    with pytest.raises(BriefError, match="no `project` object"):
        parse_brief('{"tickets": []}')


def test_a_project_without_scope_items_is_refused():
    with pytest.raises(BriefError, match="no scope items"):
        parse_brief(json.dumps({"project": {"name": "X", "scope_items": []}}))


def test_a_project_without_a_name_is_refused():
    with pytest.raises(BriefError, match="no name"):
        parse_brief(json.dumps({"project": {"name": "  ", "scope_items": [{}]}}))


def test_scope_items_are_reordered_so_nothing_precedes_its_blocker():
    payload = {
        "name": "P",
        "scope_items": [
            {"title": "Last", "depends_on": [2], "prose": "c"},
            {"title": "First", "depends_on": [], "prose": "a"},
        ],
    }
    project = to_project(payload)

    assert [item.title for item in project.scope] == ["First", "Last"]
    assert project.scope[0].depends_on == []
    assert project.scope[1].depends_on == [1]


def test_circular_scope_dependencies_are_refused():
    payload = {
        "name": "P",
        "scope_items": [
            {"title": "A", "depends_on": [2], "prose": "a"},
            {"title": "B", "depends_on": [1], "prose": "b"},
        ],
    }
    with pytest.raises(BriefError, match="circular"):
        to_project(payload)


def test_an_untitled_slice_still_gets_a_name_rather_than_being_dropped():
    project = to_project({"name": "P", "scope_items": [{"prose": "do a thing"}]})
    assert project.scope[0].title == "Slice 1"


# -- the draft round trip -----------------------------------------------------


def test_a_draft_survives_being_rewritten_by_hand():
    original = to_project(json.loads(brief_json())["project"])
    edited = render_draft(original).replace(
        "name: Rate limiting", "name: Rate limiting, properly"
    )

    parsed = parse_draft(edited)

    assert parsed.name == "Rate limiting, properly"
    assert parsed.scope == original.scope


def test_a_draft_with_the_name_deleted_is_refused_clearly():
    original = to_project(json.loads(brief_json())["project"])
    edited = render_draft(original).replace("name: Rate limiting", "name:")

    with pytest.raises(BriefError, match="no name"):
        parse_draft(edited)


def test_a_draft_with_every_slice_deleted_is_refused_clearly():
    original = to_project(json.loads(brief_json())["project"])
    head, _, _ = render_draft(original).partition("<!-- red:scope -->")

    with pytest.raises(BriefError, match="no scope items"):
        parse_draft(head)
