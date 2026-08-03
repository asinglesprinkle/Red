"""Hand-written stand-ins. No mocking library, the way Forman's suite does it.

A fake only has to return one of Red's dataclasses. None of them impersonates
Linear, git, or an SDK.
"""

from __future__ import annotations

import json
from typing import Any

from forman.models import Ticket
from forman.review import Approval, Decision, Question
from forman.spawn import AgentRun

from red.models import Project, ScopeItem

FROZEN = "2026-08-01T10:00:00+00:00"


def project(name: str = "Rate limiting", slices: int = 2, **kwargs) -> Project:
    scope = [
        ScopeItem(
            number=n,
            title=f"Slice {n}",
            prose=f"Do the {n} thing.",
            depends_on=[n - 1] if n > 1 else [],
        )
        for n in range(1, slices + 1)
    ]
    fields = {
        "name": name,
        "summary": "Stop the API falling over.",
        "outcome": "The API survives a burst.",
        "success_criteria": ["429 after 100 requests in a minute"],
        "constraints": "Python, no new dependencies.",
        "out_of_scope": "Per-tenant quotas.",
        "scope": scope,
        "id": "project-uuid",
        "slug_id": "rate-limiting-abc",
        "url": "https://linear.app/eng/project/rate-limiting-abc",
        "status": "Planned",
    }
    fields.update(kwargs)
    return Project(**fields)


class FakeTransport:
    """Replays canned GraphQL responses and records what was asked for.

    Keyed on a substring of the query, so a test names the operation it means
    rather than repeating the whole document.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, query: str, variables: dict) -> dict:
        self.calls.append((query, variables))
        for marker, payload in self.responses.items():
            if marker in query:
                return {"data": payload} if "data" not in payload else payload
        raise AssertionError(f"no canned response for query:\n{query}")

    def operations(self) -> list[str]:
        """The operation names in call order, e.g. ['RedProjects', 'RedCreateProject']."""
        names = []
        for query, _variables in self.calls:
            for token in query.split():
                if token.startswith(("Red", "Forman")):
                    names.append(token.split("(")[0].split("{")[0])
                    break
        return names

    def variables_for(self, marker: str) -> list[dict]:
        return [v for q, v in self.calls if marker in q]


class ScriptedConversation:
    """Replays agent turns and records the openings it was handed."""

    def __init__(self, turns: list[str]) -> None:
        self.turns = list(turns)
        self.openings: list[str] = []

    def __call__(self, *, respond, opening, **_kwargs) -> AgentRun:
        self.openings.append(opening)
        text = self.turns.pop(0)
        while True:
            reply = respond(text)
            if reply is None or not self.turns:
                return AgentRun(text=text)
            text = self.turns.pop(0)


class ScriptedHuman:
    """A terminal that answers from a list and remembers everything it saw."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.shown: list[str] = []
        self.prompts: list[str] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"the human ran out of answers at: {prompt!r}")
        return self.answers.pop(0)

    def show(self, text: str) -> None:
        self.shown.append(text)

    def transcript(self) -> str:
        return "\n".join(self.shown)


class ScriptedPush:
    """Stands in for forman.push_interactive.

    Drives the reviewer exactly the way the real one does: it asks whatever
    questions it was given, then presents drafts at the gate, then honours the
    decision. That is what makes a relay test meaningful rather than decorative.
    """

    def __init__(
        self,
        per_slice: list[dict] | None = None,
        default: dict | None = None,
    ) -> None:
        self.per_slice = list(per_slice or [])
        self.default = default or {"questions": [], "tickets": ["ENG-1"]}
        self.calls: list[dict] = []
        self.answers: list[str] = []
        self._next = 100

    def __call__(self, *, prose, linear, reviewer, edit=None, cwd=".", **_kwargs):
        from forman.push import Aborted

        script = self.per_slice.pop(0) if self.per_slice else dict(self.default)
        self.calls.append({"prose": prose, "cwd": cwd, "script": script})

        for index, question in enumerate(script.get("questions") or [], start=1):
            self.answers.append(reviewer.answer(Question(text=question, round=index)))

        titles = script.get("tickets") or []
        tickets = [Ticket(identifier="", title=title) for title in titles]
        decision = reviewer.decide(
            Approval(tickets=tickets, rendered=_render(tickets))
        )
        if decision.action == "quit":
            raise Aborted("nothing created")

        made = []
        for ticket in tickets:
            made.append(linear.create(ticket))
        return made


def _render(tickets: list[Ticket]) -> str:
    return "\n".join(f"title: {t.title}" for t in tickets)


class AutoApprove:
    """A Reviewer that says yes to everything. For tests about the loop, not
    about the gate; the gate has its own tests in test_relay.py."""

    def __init__(self, answer: str = "sure") -> None:
        self.answer_with = answer
        self.questions: list[str] = []
        self.approvals: list[Approval] = []

    def show(self, text: str) -> None:
        pass

    def answer(self, question: Question) -> str:
        self.questions.append(question.text)
        return self.answer_with

    def decide(self, approval: Approval) -> Decision:
        self.approvals.append(approval)
        return Decision("create")


class FakeStore:
    """An in-memory StateStore. Same surface, no filesystem."""

    def __init__(self) -> None:
        self.saved: list[Any] = []
        self.states: dict[str, Any] = {}

    def exists(self, slug: str) -> bool:
        return slug in self.states

    def load(self, slug: str):
        import copy

        return copy.deepcopy(self.states[slug])

    def save(self, state) -> None:
        import copy

        self.states[state.slug_id] = copy.deepcopy(state)
        self.saved.append(copy.deepcopy(state))

    def init(self, project):
        from red.state import StateStore

        return StateStore(".").init(project)


def agent_run(text: str, error: str | None = None) -> AgentRun:
    return AgentRun(text=text, error=error)


def brief_json(**overrides) -> str:
    payload = {
        "name": "Rate limiting",
        "summary": "Stop the API falling over.",
        "outcome": "The API survives a burst.",
        "success_criteria": ["429 after 100 requests in a minute"],
        "constraints": "Python, no new dependencies.",
        "out_of_scope": "Per-tenant quotas.",
        "scope_items": [
            {
                "title": "Add a token bucket",
                "depends_on": [],
                "prose": "Implement the bucket in src/api/limits.py.",
            },
            {
                "title": "Wire it into the middleware",
                "depends_on": [1],
                "prose": "Call the bucket from src/api/middleware.py.",
            },
        ],
    }
    payload.update(overrides)
    return json.dumps({"project": payload})


__all__ = [
    "FROZEN",
    "AutoApprove",
    "Decision",
    "FakeStore",
    "FakeTransport",
    "ScriptedConversation",
    "ScriptedHuman",
    "ScriptedPush",
    "agent_run",
    "brief_json",
    "project",
]
