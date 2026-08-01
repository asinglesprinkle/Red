"""The pipe, and the promise it makes.

Red proposes. You decide. Nothing reaches Foreman that you did not see on the
way past, and nothing you typed is ever second-guessed. Every test here exists
to keep that true.
"""

from __future__ import annotations

from foreman.review import Approval, Decision, Question
from foreman.models import Ticket

from red.relay import NOTHING_FOUND, RelayReviewer, draft_answer
from red.models import ScopeItem

from fakes import ScriptedHuman, agent_run, project

ITEM = ScopeItem(2, "Wire it in", "Call the bucket from the middleware.", [1])


def relay(human: ScriptedHuman, *, proposal: str = "Yes, all endpoints.", **kwargs):
    calls: list[dict] = []

    def drafter(**passed) -> str:
        calls.append(passed)
        return proposal

    reviewer = RelayReviewer(
        ask=human.ask,
        show=human.show,
        project=project(),
        brief="## Outcome\n\nThe API survives a burst.",
        item=ITEM,
        position="slice 2 of 2",
        drafter=drafter,
        **kwargs,
    )
    return reviewer, calls


# -- the promise --------------------------------------------------------------


def test_what_you_type_is_what_is_sent():
    human = ScriptedHuman(["no, only the write endpoints"])
    reviewer, _calls = relay(human, proposal="Yes, all endpoints.")

    sent = reviewer.answer(Question(text="Which endpoints?"))

    assert sent == "no, only the write endpoints"
    assert reviewer.sent == ["no, only the write endpoints"]
    # The proposal was offered and then dropped on the floor, which is the point.
    assert reviewer.proposed == ["Yes, all endpoints."]


def test_an_empty_line_accepts_the_proposal():
    human = ScriptedHuman([""])
    reviewer, _calls = relay(human, proposal="Yes, all endpoints.")

    assert reviewer.answer(Question(text="Which endpoints?")) == "Yes, all endpoints."
    assert reviewer.sent == ["Yes, all endpoints."]


def test_the_question_is_shown_verbatim_and_the_proposal_is_labelled():
    human = ScriptedHuman([""])
    reviewer, _calls = relay(human, proposal="Yes, all endpoints.")

    reviewer.answer(Question(text="Which endpoints, and what limit?"))

    assert "Which endpoints, and what limit?" in human.shown
    assert "red > Yes, all endpoints." in human.shown
    # Whose words are whose is never ambiguous: the agent's question is bare,
    # the proposal is prefixed.
    assert "red > Which endpoints, and what limit?" not in human.shown


def test_a_multi_line_proposal_is_labelled_on_every_line():
    human = ScriptedHuman([""])
    reviewer, _calls = relay(human, proposal="First line.\nSecond line.")

    reviewer.answer(Question(text="Why?"))

    assert "red > First line." in human.shown
    assert "red > Second line." in human.shown


def test_verbatim_never_calls_the_drafter_at_all():
    human = ScriptedHuman(["all of them"])
    reviewer, calls = relay(human, verbatim=True)

    assert reviewer.answer(Question(text="Which endpoints?")) == "all of them"
    assert calls == []
    assert reviewer.proposed == [""]
    assert not any(line.startswith("red > ") for line in human.shown)


def test_the_drafter_is_given_the_brief_the_slice_and_the_question():
    human = ScriptedHuman([""])
    reviewer, calls = relay(human)

    reviewer.answer(Question(text="Which endpoints?"))

    assert calls[0]["brief"] == "## Outcome\n\nThe API survives a burst."
    assert calls[0]["item"] is ITEM
    assert calls[0]["question"] == "Which endpoints?"


def test_the_header_says_which_project_and_slice_you_are_in():
    human = ScriptedHuman([""])
    reviewer, _calls = relay(human)

    reviewer.answer(Question(text="Which endpoints?"))

    assert "-- foreman asks (Rate limiting, slice 2 of 2) --" in human.shown


# -- the gate -----------------------------------------------------------------


def _approval(count: int = 2) -> Approval:
    tickets = [Ticket("", f"Ticket {n}") for n in range(count)]
    return Approval(tickets=tickets, rendered="title: Ticket 0\ntitle: Ticket 1")


def test_red_proposes_nothing_at_the_approval_gate():
    human = ScriptedHuman(["c"])
    reviewer, calls = relay(human)

    assert reviewer.decide(_approval()) == Decision("create")
    # No drafting, and nothing prefixed as Red's own opinion of the drafts.
    assert calls == []
    assert not any(line.startswith("red > ") for line in human.shown)


def test_the_drafts_are_shown_verbatim_at_the_gate():
    human = ScriptedHuman(["c"])
    reviewer, _calls = relay(human)

    reviewer.decide(_approval())

    assert "title: Ticket 0\ntitle: Ticket 1" in human.shown
    # Foreman renders its drafts with `project: null`, so the gate says where
    # they are actually going.
    assert "-- foreman drafted 2 issue(s) for Rate limiting --" in human.shown


def test_skip_is_a_quit_so_this_slice_creates_nothing():
    for typed in ("s", "skip", "SKIP"):
        human = ScriptedHuman([typed])
        reviewer, _calls = relay(human)
        assert reviewer.decide(_approval()) == Decision("quit")


def test_the_gate_keeps_foremans_own_vocabulary():
    for typed, expected in [
        ("", "create"),
        ("c", "create"),
        ("q", "quit"),
        ("e", "edit"),
        ("make it one ticket", "feedback"),
    ]:
        human = ScriptedHuman([typed])
        reviewer, _calls = relay(human)
        assert reviewer.decide(_approval()).action == expected


def test_feedback_is_carried_through_verbatim():
    human = ScriptedHuman(["  split the auth part out  "])
    reviewer, _calls = relay(human)

    assert reviewer.decide(_approval()) == Decision("feedback", "split the auth part out")


# -- the drafter --------------------------------------------------------------


def test_a_drafter_failure_costs_a_keystroke_and_not_the_run():
    def explode(**_kwargs):
        raise RuntimeError("no model today")

    answer = draft_answer(
        brief="b", item=ITEM, question="q", runner=lambda **_k: explode()
    )

    assert answer.startswith(NOTHING_FOUND)
    assert "no model today" in answer


def test_an_agent_error_becomes_a_non_answer_rather_than_a_guess():
    answer = draft_answer(
        brief="b",
        item=ITEM,
        question="q",
        runner=lambda **_k: agent_run("", error="session died"),
    )
    assert answer.startswith(NOTHING_FOUND)
    assert "session died" in answer


def test_an_empty_reply_is_a_non_answer():
    answer = draft_answer(
        brief="b", item=ITEM, question="q", runner=lambda **_k: agent_run("   ")
    )
    assert answer == NOTHING_FOUND


def test_the_drafter_is_read_only():
    seen: dict = {}

    def runner(**kwargs):
        seen.update(kwargs)
        return agent_run("answer")

    draft_answer(brief="b", item=ITEM, question="q", runner=runner)

    assert seen["allowed_tools"] == ["Read", "Grep", "Glob"]
    assert "Write" not in seen["allowed_tools"]
    assert "Edit" not in seen["allowed_tools"]
    assert "Bash" not in seen["allowed_tools"]


def test_the_drafter_is_told_to_say_so_rather_than_guess():
    seen: dict = {}

    def runner(**kwargs):
        seen.update(kwargs)
        return agent_run("answer")

    draft_answer(brief="the brief", item=ITEM, question="the question", runner=runner)

    assert NOTHING_FOUND in seen["system_prompt"]
    assert "Do not guess" in seen["system_prompt"]
    assert "the brief" in seen["prompt"]
    assert "the question" in seen["prompt"]
    assert ITEM.prose in seen["prompt"]
