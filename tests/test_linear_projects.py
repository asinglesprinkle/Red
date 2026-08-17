"""The project half of the Linear boundary, and the attach that has to be exact."""

from __future__ import annotations

import json

import pytest
from fakes import FakeTransport, project
from forman.linear_graphql import GraphQLLinearClient, LinearApiError
from forman.models import Ticket

from red.brief import render_content
from red.linear_projects import (
    DRAFT_STATUS,
    READY_STATUS,
    LinearProjects,
    ProjectScopedLinear,
    StubProjectClient,
    brief_of,
    project_from_node,
)


def node(name="Rate limiting", status="Planned", **kwargs):
    fields = {
        "id": "project-uuid",
        "name": name,
        "description": "Stop the API falling over.",
        "content": render_content(project(name=name)),
        "url": "https://linear.app/eng/project/rate-limiting-abc",
        "slugId": "rate-limiting-abc",
        "priority": 2,
        "updatedAt": "2026-08-01T10:00:00Z",
        "status": {"id": "status-uuid", "name": status, "type": status.lower()},
    }
    fields.update(kwargs)
    return fields


def client(responses: dict) -> tuple[LinearProjects, FakeTransport]:
    transport = FakeTransport(responses)
    inner = GraphQLLinearClient(api_key="k", team_key="ENG", transport=transport)
    return LinearProjects(inner), transport


# -- reading ------------------------------------------------------------------


def test_a_project_node_becomes_a_project_with_its_scope_parsed():
    parsed = project_from_node(node())

    assert parsed.name == "Rate limiting"
    assert parsed.summary == "Stop the API falling over."
    assert parsed.slug_id == "rate-limiting-abc"
    assert parsed.status == "Planned"
    assert [item.title for item in parsed.scope] == ["Slice 1", "Slice 2"]
    assert parsed.scope[1].depends_on == [1]
    assert parsed.success_criteria == ["429 after 100 requests in a minute"]


def test_ready_only_returns_what_a_human_moved():
    projects, _t = client(
        {
            "RedProjects": {
                "projects": {
                    "nodes": [
                        node("Ready one", status=READY_STATUS),
                        node("Still drafting", status=DRAFT_STATUS),
                        node("Ready two", status=READY_STATUS),
                    ]
                }
            }
        }
    )
    assert [p.name for p in projects.ready()] == ["Ready one", "Ready two"]


def test_find_matches_a_name_case_insensitively_and_a_slug_exactly():
    projects, _t = client(
        {"RedProjects": {"projects": {"nodes": [node("Rate Limiting")]}}}
    )
    assert projects.find("rate limiting").name == "Rate Limiting"
    assert projects.find("rate-limiting-abc").name == "Rate Limiting"
    assert projects.find("nothing like it") is None


def page(nodes, cursor=None):
    """One page of RedProjects. A cursor means another page follows."""
    return {
        "projects": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
        }
    }


def test_find_walks_past_the_first_page():
    # The failure this prevents is the quiet one: a project that exists reading
    # back as None, and the caller filing the work somewhere else.
    projects, transport = client(
        {
            "RedProjects": [
                page([node("Something else", id="a", slugId="a1")], cursor="page2"),
                page([node("Rate limiting", id="b", slugId="b1")]),
            ]
        }
    )

    assert projects.find("rate limiting").id == "b"
    assert [v["after"] for v in transport.variables_for("RedProjects")] == [
        None,
        "page2",
    ]


def test_ready_sees_projects_on_later_pages():
    projects, _t = client(
        {
            "RedProjects": [
                page([node("Ready one", status=READY_STATUS)], cursor="page2"),
                page([node("Ready two", status=READY_STATUS)]),
            ]
        }
    )
    assert [p.name for p in projects.ready()] == ["Ready one", "Ready two"]


def test_a_duplicate_name_on_a_later_page_is_still_refused():
    # The ambiguity guard is only a guard if it can see both of them.
    projects, _t = client(
        {
            "RedProjects": [
                page([node("Rate limiting", id="a", slugId="a1")], cursor="page2"),
                page([node("Rate limiting", id="b", slugId="b1")]),
            ]
        }
    )
    with pytest.raises(LinearApiError, match="2 projects are called"):
        projects.find("Rate limiting")


def test_a_response_without_pageinfo_stops_after_one_page():
    # Truncating is survivable; walking forever against Linear is not.
    projects, transport = client(
        {"RedProjects": {"projects": {"nodes": [node("Rate limiting")]}}}
    )

    assert len(projects.list_projects()) == 1
    assert len(transport.variables_for("RedProjects")) == 1


def test_two_projects_with_the_same_name_are_refused_not_guessed():
    projects, _t = client(
        {
            "RedProjects": {
                "projects": {
                    "nodes": [
                        node("Rate limiting", id="a", slugId="a1"),
                        node("Rate limiting", id="b", slugId="b1"),
                    ]
                }
            }
        }
    )
    with pytest.raises(LinearApiError, match="2 projects are called"):
        projects.find("Rate limiting")


# -- creating -----------------------------------------------------------------


def _create_responses():
    return {
        "RedTeam": {"teams": {"nodes": [{"id": "team-uuid", "key": "ENG"}]}},
        "RedProjectStatuses": {
            "projectStatuses": {
                "nodes": [
                    {"id": "backlog-uuid", "name": "Backlog", "type": "backlog"},
                    {"id": "planned-uuid", "name": "Planned", "type": "planned"},
                ]
            }
        },
        "RedCreateProject": {
            "projectCreate": {"success": True, "project": node(status="Backlog")}
        },
    }


def test_a_created_project_lands_in_the_draft_status_with_its_content():
    projects, transport = client(_create_responses())

    made = projects.create(project(id=None, slug_id=None), team_key="ENG")

    payload = transport.variables_for("RedCreateProject")[0]["input"]
    assert payload["name"] == "Rate limiting"
    assert payload["description"] == "Stop the API falling over."
    assert payload["teamIds"] == ["team-uuid"]
    assert payload["statusId"] == "backlog-uuid"
    assert "## Outcome" in payload["content"]
    assert "<!-- red:scope -->" in payload["content"]
    assert made.id == "project-uuid"
    assert made.url


def test_the_scope_we_drafted_survives_the_round_trip():
    projects, _t = client(_create_responses())
    drafted = project(slices=3, id=None)

    made = projects.create(drafted, team_key="ENG")

    assert made.scope is drafted.scope
    assert len(made.scope) == 3


def test_a_refused_creation_raises_rather_than_returning_a_ghost():
    responses = _create_responses()
    responses["RedCreateProject"] = {
        "projectCreate": {"success": False, "project": None}
    }
    projects, _t = client(responses)

    with pytest.raises(LinearApiError, match="refused to create"):
        projects.create(project(id=None), team_key="ENG")


def test_red_has_no_way_to_move_a_project_at_all():
    """The gate is a human moving the project in Linear.

    This is pinned structurally rather than behaviourally on purpose: the
    guarantee is not that Red currently declines to move a project, it is that
    Red never learned how. There is no call to forget to leave out.
    """
    import red.linear_projects as module

    mutations = {
        name: value
        for name, value in vars(module).items()
        if name.endswith("_MUTATION") and isinstance(value, str)
    }
    assert mutations
    assert not any("projectUpdate" in body for body in mutations.values())

    assert not hasattr(LinearProjects, "set_status")
    assert not hasattr(StubProjectClient, "set_status")


def test_the_only_issue_update_red_sends_is_the_attach():
    import red.linear_projects as module

    updates = [
        body
        for name, body in vars(module).items()
        if name.endswith("_MUTATION")
        and isinstance(body, str)
        and "issueUpdate" in body
    ]
    assert updates == [module._ATTACH_MUTATION]
    assert "projectId" in module._ATTACH_MUTATION
    assert "stateId" not in module._ATTACH_MUTATION


# -- attaching ----------------------------------------------------------------


class RecordingInner:
    """A LinearClient that hands back an identifier and a known uuid."""

    def __init__(self) -> None:
        self.created: list[Ticket] = []
        self.calls: list[str] = []

    def create(self, ticket: Ticket) -> Ticket:
        ticket.identifier = "ENG-1"
        self.created.append(ticket)
        return ticket

    def issue_id(self, identifier: str) -> str:
        self.calls.append(identifier)
        return "issue-uuid"

    def list_assigned(self):
        return []

    def get(self, identifier):
        return Ticket(identifier, "t")

    def comment(self, identifier, body):
        self.calls.append(f"comment:{identifier}")

    def set_status(self, identifier, status):
        self.calls.append(f"status:{identifier}")

    def relate_blocks(self, blocker, blocked):
        self.calls.append(f"blocks:{blocker}>{blocked}")


class RecordingProjects:
    def __init__(self) -> None:
        self.attached: list[tuple[str, str]] = []

    def attach(self, issue_id: str, project_id: str) -> None:
        self.attached.append((issue_id, project_id))


def test_every_created_issue_is_attached_by_id_not_by_name():
    inner, projects = RecordingInner(), RecordingProjects()
    scoped = ProjectScopedLinear(inner, projects, project())

    made = scoped.create(Ticket("", "Add a token bucket"))

    # The name is set so the drafts a human approved still render it...
    assert made.project == "Rate limiting"
    # ...but the attach uses the real id, because a name match picks the first
    # of two projects called the same thing, silently.
    assert projects.attached == [("issue-uuid", "project-uuid")]
    assert scoped.created == ["ENG-1"]


def test_attaching_is_skipped_only_when_there_is_no_project_id():
    inner, projects = RecordingInner(), RecordingProjects()
    scoped = ProjectScopedLinear(inner, projects, project(id=None))

    scoped.create(Ticket("", "Add a token bucket"))

    assert projects.attached == []


def test_everything_else_is_passed_straight_through():
    inner, projects = RecordingInner(), RecordingProjects()
    scoped = ProjectScopedLinear(inner, projects, project())

    scoped.comment("ENG-1", "hello")
    scoped.set_status("ENG-1", "in_review")
    scoped.get("ENG-1")
    scoped.list_assigned()

    assert inner.calls == ["comment:ENG-1", "status:ENG-1"]


def test_ordering_between_slices_reaches_the_backend():
    """Red files one slice per push, so every edge between slices crosses this
    decorator. Not delegating it puts a whole project on the board unordered."""
    inner, projects = RecordingInner(), RecordingProjects()
    scoped = ProjectScopedLinear(inner, projects, project())

    scoped.relate_blocks("ENG-1", "ENG-2")

    assert inner.calls == ["blocks:ENG-1>ENG-2"]


def test_the_attach_mutation_is_an_issue_update_with_a_project_id():
    projects, transport = client({"RedAttachIssue": {"issueUpdate": {"success": True}}})
    projects.attach("issue-uuid", "project-uuid")

    variables = transport.variables_for("RedAttachIssue")[0]
    assert variables == {"issueId": "issue-uuid", "projectId": "project-uuid"}


# -- the stub -----------------------------------------------------------------


def test_the_stub_round_trips_a_project_through_its_json_file(tmp_path):
    path = tmp_path / "linear-stub.json"
    stub = StubProjectClient(path=path)

    made = stub.create(project(id=None, slug_id=None, scope=project(slices=3).scope))
    stub.comment(made.id, "filed things")
    stub.attach("ENG-1", made.id)

    reloaded = StubProjectClient(path=path)
    found = reloaded.find(made.name)

    assert found is not None
    assert found.id == made.id
    assert [item.title for item in found.scope] == ["Slice 1", "Slice 2", "Slice 3"]
    assert found.status == DRAFT_STATUS
    saved = json.loads(path.read_text())
    assert saved["comments"] == [[made.id, "filed things"]]
    assert saved["attachments"] == [["ENG-1", made.id]]


def test_the_stub_only_offers_ready_projects_to_a_pull(tmp_path):
    stub = StubProjectClient(path=tmp_path / "s.json")
    stub.create(project(id=None), status=DRAFT_STATUS)
    stub.create(project(name="Other", id=None), status=READY_STATUS)

    assert [p.name for p in stub.ready()] == ["Other"]


# -- the brief ----------------------------------------------------------------


def test_the_brief_is_everything_above_the_scope():
    brief = brief_of(project(slices=2))

    assert "## Outcome" in brief
    assert "The API survives a burst." in brief
    assert "Per-tenant quotas." in brief
    assert "Slice 1" not in brief
    assert "<!-- red:scope -->" not in brief
