"""The whole loop, offline: prose to a project to issues, with a human in it.

Everything here runs against Forman's real `push_interactive` and Red's real
`RelayReviewer`. The only fakes are the model and Linear. That is deliberate:
the relay's whole reason to exist is the seam between those two real things, and
a test that fakes the seam proves nothing about it.
"""

from __future__ import annotations

import json

from fakes import ScriptedConversation, ScriptedHuman, brief_json
from forman.linear_client import StubLinearClient
from forman.push import push_interactive

from red.brief import draft_project, render_draft
from red.linear_projects import ProjectScopedLinear, StubProjectClient, brief_of
from red.pipeline import Deps, run_pull
from red.relay import RelayReviewer
from red.state import StateStore

TICKETS = json.dumps(
    {
        "tickets": [
            {
                "title": "Add a token bucket",
                "priority": "high",
                "labels": [],
                "project": None,
                "estimate": "m",
                "blocked_by": [],
                "blocks": [],
                "problem": "No rate limiting.",
                "acceptance_criteria": ["429 after 100 requests"],
                "context": "src/api/limits.py",
                "out_of_scope": "Per-tenant quotas.",
            }
        ]
    }
)


class Reviewing:
    """The push-half gate, scripted."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)

    def show(self, text):
        pass

    def answer(self, question):
        return self.answers.pop(0)

    def decide(self, approval):
        from forman.review import parse_decision

        return parse_decision(self.answers.pop(0))


def _project(tmp_path, stub):
    """Run the push half and file the project into the stub."""
    reviewer = Reviewing(["python", "c"])
    project = draft_project(
        prose="rate limit the api",
        reviewer=reviewer,
        conversation=ScriptedConversation(["Which language?", brief_json()]),
    )
    return stub.create(project, team_key="TEAM")


def test_prose_becomes_a_project_and_then_issues(tmp_path):
    projects = StubProjectClient(path=tmp_path / "projects.json")
    linear = StubLinearClient(path=tmp_path / "issues.json")

    project = _project(tmp_path, projects)
    assert project.status == "Backlog"
    assert len(project.scope) == 2

    # A human reviews it in Linear and moves it on. Red never does this.
    project.status = "Planned"

    human = ScriptedHuman(
        [
            "",  # accept the drafted answer to forman's question
            "c",  # create the drafted issues
            "no, only the write endpoints",  # override on the second slice
            "c",
        ]
    )
    scoped = ProjectScopedLinear(linear, projects, project)
    status_before = projects.find(project.name).status

    def reviewer_for(*, item, position):
        return RelayReviewer(
            ask=human.ask,
            show=human.show,
            project=project,
            brief=brief_of(project),
            item=item,
            position=position,
            drafter=lambda **_kw: "Everything in src/api.",
        )

    report = run_pull(
        Deps(
            projects=projects,
            linear=scoped,
            store=StateStore(tmp_path),
            push=_pushing_once(),
            reviewer_for=reviewer_for,
            confirm=lambda _q: True,
            note=lambda _t: None,
        ),
        project,
    )

    assert report.outcome == "filed"
    assert report.issues == ["TEAM-100", "TEAM-101"]

    # Both issues really are in the project.
    assert projects.attachments == [
        ("TEAM-100", project.id),
        ("TEAM-101", project.id),
    ]
    assert all(t.project == project.name for t in linear.tickets.values())

    # One summary comment, and the run left the status exactly where the human
    # put it.
    assert len(projects.comments) == 1
    assert projects.find(project.name).status == status_before == "Planned"


def test_the_words_the_human_typed_are_the_words_forman_received(tmp_path):
    """The one promise the whole design rests on."""
    projects = StubProjectClient(path=tmp_path / "projects.json")
    linear = StubLinearClient(path=tmp_path / "issues.json")
    project = _project(tmp_path, projects)
    project.scope = project.scope[:1]

    human = ScriptedHuman(["no, only the write endpoints", "c"])
    conversation = ScriptedConversation(["Which endpoints?", TICKETS])
    relay = RelayReviewer(
        ask=human.ask,
        show=human.show,
        project=project,
        brief=brief_of(project),
        item=project.scope[0],
        drafter=lambda **_kw: "Everything in src/api.",
    )

    push_interactive(
        prose="add a token bucket",
        linear=ProjectScopedLinear(linear, projects, project),
        reviewer=relay,
        conversation=conversation,
    )

    # Forman's second turn is the reply it was handed. It is the human's
    # sentence, not Red's proposal, and Red's proposal appears nowhere in it.
    reply = conversation.openings[0] if len(conversation.openings) > 1 else None
    assert relay.sent == ["no, only the write endpoints"]
    assert relay.proposed == ["Everything in src/api."]
    assert reply is None or "Everything in src/api." not in reply

    # And the human saw the question exactly as Forman asked it.
    assert "Which endpoints?" in human.shown


def test_what_red_files_is_what_forman_will_pull(tmp_path):
    """The far end of the handoff.

    Forman only selects tickets carrying its own label, so an unstamped ticket
    is one Forman will never work. That failure is silent and arrives days
    later, at `forman pull`, with nothing on the board to explain it. The
    project scoping sits between Red and Forman's create, so this checks the
    mark survives that wrapper rather than trusting that it delegates.
    """
    from forman.config import DEFAULT_LABEL
    from forman.orchestrator import select_ticket

    projects = StubProjectClient(path=tmp_path / "projects.json")
    linear = StubLinearClient(path=tmp_path / "issues.json", label=DEFAULT_LABEL)
    project = _project(tmp_path, projects)
    project.status = "Planned"

    human = ScriptedHuman(["", "c", "", "c"])

    def reviewer_for(*, item, position):
        return RelayReviewer(
            ask=human.ask,
            show=human.show,
            project=project,
            brief=brief_of(project),
            item=item,
            position=position,
            drafter=lambda **_kw: "Everything in src/api.",
        )

    report = run_pull(
        Deps(
            projects=projects,
            linear=scoped_for(linear, projects, project),
            store=StateStore(tmp_path),
            push=_pushing_once(),
            reviewer_for=reviewer_for,
            confirm=lambda _q: True,
            note=lambda _t: None,
        ),
        project,
    )

    assert report.outcome == "filed"
    assert all(t.has_label(DEFAULT_LABEL) for t in linear.tickets.values())
    # Not just labelled: actually selectable by the phase that reads it back.
    picked = select_ticket(linear.list_assigned(), label=DEFAULT_LABEL)
    assert picked is not None
    assert picked.identifier in report.issues


def test_quitting_at_the_gate_files_nothing_anywhere(tmp_path):
    projects = StubProjectClient(path=tmp_path / "projects.json")
    linear = StubLinearClient(path=tmp_path / "issues.json")
    project = _project(tmp_path, projects)
    project.status = "Planned"

    human = ScriptedHuman(["", "s", "", "s"])  # skip every slice

    def reviewer_for(*, item, position):
        return RelayReviewer(
            ask=human.ask,
            show=human.show,
            project=project,
            brief=brief_of(project),
            item=item,
            position=position,
            drafter=lambda **_kw: "A proposal.",
        )

    report = run_pull(
        Deps(
            projects=projects,
            linear=scoped_for(linear, projects, project),
            store=StateStore(tmp_path),
            push=_pushing_once(),
            reviewer_for=reviewer_for,
            confirm=lambda _q: True,
            note=lambda _t: None,
        ),
        project,
    )

    assert report.outcome == "nothing_created"
    assert report.issues == []
    assert len(report.skipped) == 2
    assert linear.tickets == {}
    assert projects.attachments == []
    assert projects.comments == []


def scoped_for(linear, projects, project):
    return ProjectScopedLinear(linear, projects, project)


def _pushing_once():
    """forman.push_interactive with a fresh scripted conversation per slice."""

    def push(
        *, prose, linear, reviewer, edit=None, cwd=".", on_activity=None, warn=None
    ):
        return push_interactive(
            prose=prose,
            linear=linear,
            reviewer=reviewer,
            edit=edit,
            cwd=cwd,
            on_activity=on_activity,
            warn=warn,
            conversation=ScriptedConversation(["Which endpoints?", TICKETS]),
        )

    return push


def test_a_resumed_run_re_reads_the_project_from_linear(tmp_path):
    """A human edits the project between runs, and the edit wins."""
    projects = StubProjectClient(path=tmp_path / "projects.json")
    linear = StubLinearClient(path=tmp_path / "issues.json")
    project = _project(tmp_path, projects)
    project.status = "Planned"

    human = ScriptedHuman(["", "c", "", "s"])

    def reviewer_for(*, item, position):
        return RelayReviewer(
            ask=human.ask,
            show=human.show,
            project=project,
            brief=brief_of(project),
            item=item,
            position=position,
            drafter=lambda **_kw: "A proposal.",
        )

    def deps():
        return Deps(
            projects=projects,
            linear=scoped_for(linear, projects, project),
            store=StateStore(tmp_path),
            push=_pushing_once(),
            reviewer_for=reviewer_for,
            confirm=lambda _q: True,
            note=lambda _t: None,
        )

    first = run_pull(deps(), project)
    assert first.issues == ["TEAM-100"]
    assert first.skipped == ["Wire it into the middleware"]

    # Nothing is pending any more, so a second run offers nothing at all.
    second = run_pull(deps(), project)
    assert second.outcome == "no_work"
    assert second.issues == ["TEAM-100"]


def test_the_draft_a_human_sees_is_the_content_that_reaches_linear(tmp_path):
    projects = StubProjectClient(path=tmp_path / "projects.json")
    project = _project(tmp_path, projects)

    drafted = render_draft(project)
    stored = json.loads((tmp_path / "projects.json").read_text())["projects"][0]

    for item in project.scope:
        assert item.title in drafted
        assert item.title in stored["content"]
    assert project.outcome in drafted
    assert project.outcome in stored["content"]
