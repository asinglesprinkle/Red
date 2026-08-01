"""The Linear boundary for projects.

Foreman's Linear layer knows a project only as a name string hanging off an
issue: it reads `project { name }` back, and on create it takes the first
case-insensitive name match out of `projects(first: 100)`. That is enough for
Foreman, which never has to find a project or make one. It is not enough here.

So this module adds the project half, in Foreman's own idiom: raw GraphQL
through a `GraphQLLinearClient`, whose `.query()` already carries the auth
header, the error handling, and the injectable transport the tests run against.
Nothing is reimplemented that Foreman already does.

Three things live here:

  LinearProjects      - create, find, read and comment on projects.
  ProjectScopedLinear - a LinearClient that files everything Foreman creates
                        into one known project, by id rather than by name.
  StubProjectClient   - JSON-file backed, no token, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from foreman.linear_graphql import LinearApiError

from .models import Project
from .scope import split_scope

STUB_FILE = "linear-stub.json"

# The status a drafted project lands in. Red never moves a project out of it;
# that is the human's review, and it happens in Linear.
DRAFT_STATUS = "Backlog"

# The status Red will pull from. A human moving a project here is them saying
# they have read it and it is ready to become issues.
READY_STATUS = "Planned"

_PROJECT_FIELDS = """
  id
  name
  description
  content
  url
  slugId
  priority
  updatedAt
  status { id name type }
"""

_PROJECTS_QUERY = f"""
query RedProjects($first: Int!) {{
  projects(first: $first) {{ nodes {{ {_PROJECT_FIELDS} }} }}
}}
"""

_PROJECT_QUERY = f"""
query RedProject($id: String!) {{ project(id: $id) {{ {_PROJECT_FIELDS} }} }}
"""

_CREATE_MUTATION = f"""
mutation RedCreateProject($input: ProjectCreateInput!) {{
  projectCreate(input: $input) {{
    success
    project {{ {_PROJECT_FIELDS} }}
  }}
}}
"""

_STATUSES_QUERY = """
query RedProjectStatuses { projectStatuses { nodes { id name type } } }
"""

_TEAM_ID_QUERY = """
query RedTeam($key: String!) {
  teams(filter: { key: { eq: $key } }, first: 1) { nodes { id key } }
}
"""

_COMMENT_MUTATION = """
mutation RedProjectComment($projectId: String!, $body: String!) {
  commentCreate(input: { projectId: $projectId, body: $body }) { success }
}
"""

_ATTACH_MUTATION = """
mutation RedAttachIssue($issueId: String!, $projectId: String!) {
  issueUpdate(id: $issueId, input: { projectId: $projectId }) { success }
}
"""

# Linear scores every query for complexity. The project fragment is shallow
# next to Foreman's issue fragment, but there is no reason to walk a workspace
# either: a person picking a project out of more than this many is picking it by
# name, and --project takes a name.
PAGE_SIZE = 50


def project_from_node(node: dict[str, Any]) -> Project:
    """Build a Project from a Linear node, splitting content into brief and scope."""
    from .brief import parse_content

    content = node.get("content") or ""
    parsed = parse_content(content)
    status = (node.get("status") or {}).get("name") or ""
    return Project(
        name=node.get("name", ""),
        summary=node.get("description") or "",
        outcome=parsed["outcome"],
        success_criteria=parsed["success_criteria"],
        constraints=parsed["constraints"],
        out_of_scope=parsed["out_of_scope"],
        scope=parsed["scope"],
        id=node.get("id"),
        slug_id=node.get("slugId"),
        url=node.get("url"),
        status=status,
    )


def brief_of(project: Project) -> str:
    """Everything above the scope block, as the relay's context."""
    from .brief import render_content

    head, _ = split_scope(render_content(project))
    return head.strip()


class LinearProjects:
    """The project operations Red performs. Nothing else is allowed."""

    def __init__(self, client: Any) -> None:
        # A GraphQLLinearClient. Only its .query() is used, so a test can pass
        # anything with that method and never touch a network.
        self.client = client
        self._statuses: list[dict] | None = None

    # -- reads ---------------------------------------------------------------

    def list_projects(self, first: int = PAGE_SIZE) -> list[Project]:
        data = self.client.query(_PROJECTS_QUERY, first=first)
        return [project_from_node(n) for n in data["projects"]["nodes"]]

    def get(self, project_id: str) -> Project:
        node = self.client.query(_PROJECT_QUERY, id=project_id)["project"]
        if not node:
            raise LinearApiError(f"no such project: {project_id}")
        return project_from_node(node)

    def find(self, needle: str) -> Project | None:
        """By name, slug id or id. Name match is case-insensitive.

        Ambiguity is refused rather than resolved. Picking the first of two
        projects called the same thing is how issues end up in the wrong one,
        silently, which is exactly what this module exists to prevent.
        """
        wanted = needle.strip().lower()
        projects = self.list_projects()
        exact = [p for p in projects if (p.id or "").lower() == wanted]
        exact += [p for p in projects if (p.slug_id or "").lower() == wanted]
        if exact:
            return exact[0]

        by_name = [p for p in projects if p.name.strip().lower() == wanted]
        if len(by_name) > 1:
            raise LinearApiError(
                f"{len(by_name)} projects are called {needle!r}. "
                "Pass the slug id instead; it is the last part of the project URL."
            )
        return by_name[0] if by_name else None

    def ready(self, status: str = READY_STATUS) -> list[Project]:
        """Projects a human has marked ready, most recently touched first."""
        wanted = status.strip().lower()
        found = [p for p in self.list_projects() if p.status.strip().lower() == wanted]
        return sorted(found, key=lambda p: p.name.lower())

    def status_id(self, name: str) -> str | None:
        if self._statuses is None:
            self._statuses = self.client.query(_STATUSES_QUERY)["projectStatuses"]["nodes"]
        for status in self._statuses:
            if status["name"].strip().lower() == name.strip().lower():
                return status["id"]
        return None

    def team_id(self, team_key: str) -> str:
        nodes = self.client.query(_TEAM_ID_QUERY, key=team_key)["teams"]["nodes"]
        if not nodes:
            raise LinearApiError(f"no Linear team with key {team_key!r}")
        return nodes[0]["id"]

    # -- writes --------------------------------------------------------------

    def create(
        self,
        project: Project,
        *,
        team_key: str,
        status: str = DRAFT_STATUS,
        lead_id: str | None = None,
    ) -> Project:
        """File a drafted project. It lands in the draft status and stays there."""
        from .brief import render_content

        payload: dict[str, Any] = {
            "name": project.name,
            "description": project.summary,
            "content": render_content(project),
            "teamIds": [self.team_id(team_key)],
        }
        status_id = self.status_id(status)
        if status_id:
            payload["statusId"] = status_id
        if lead_id:
            payload["leadId"] = lead_id

        result = self.client.query(_CREATE_MUTATION, input=payload)["projectCreate"]
        if not result["success"]:
            raise LinearApiError(f"Linear refused to create the project {project.name!r}")

        made = project_from_node(result["project"])
        # Keep the scope we drafted rather than the round-tripped copy: the
        # caller is about to walk it, and a parse is not a promise.
        made.scope = project.scope
        return made

    def comment(self, project_id: str, body: str) -> None:
        result = self.client.query(_COMMENT_MUTATION, projectId=project_id, body=body)
        if not result["commentCreate"]["success"]:
            raise LinearApiError("Linear refused the comment on the project")

    def attach(self, issue_id: str, project_id: str) -> None:
        result = self.client.query(
            _ATTACH_MUTATION, issueId=issue_id, projectId=project_id
        )
        if not result["issueUpdate"]["success"]:
            raise LinearApiError(f"Linear refused to attach {issue_id} to the project")


class ProjectScopedLinear:
    """A LinearClient that files everything Foreman creates into one project.

    Foreman attaches by name, taking the first case-insensitive match out of the
    first hundred projects. Red is holding the real id, so it uses it. The name
    is still set, because that is what renders in the drafts a human approves.
    """

    def __init__(self, inner: Any, projects: LinearProjects, project: Project) -> None:
        self.inner = inner
        self.projects = projects
        self.project = project
        self.created: list[str] = []

    # -- delegated -----------------------------------------------------------

    def list_assigned(self):
        return self.inner.list_assigned()

    def get(self, identifier: str):
        return self.inner.get(identifier)

    def comment(self, identifier: str, body: str) -> None:
        self.inner.comment(identifier, body)

    def set_status(self, identifier: str, status: str) -> None:
        self.inner.set_status(identifier, status)

    # -- the one that differs ------------------------------------------------

    def create(self, ticket):
        ticket.project = self.project.name
        made = self.inner.create(ticket)
        if self.project.id:
            self.projects.attach(self._uuid_of(made.identifier), self.project.id)
        self.created.append(made.identifier)
        return made

    def _uuid_of(self, identifier: str) -> str:
        """The uuid Linear's mutations need, when the client can resolve one.

        `issue_id` is on the real GraphQL client, not on the LinearClient
        protocol every backend satisfies. The stub has no uuids to resolve, so
        it gets the identifier and records that instead.
        """
        resolve = getattr(self.inner, "issue_id", None)
        return resolve(identifier) if resolve else identifier


class StubProjectClient:
    """A working project stand-in so the loop runs end to end with no token.

    Mirrors Foreman's StubLinearClient, including writing to a JSON file so a
    run is inspectable afterwards.
    """

    def __init__(
        self,
        projects: list[Project] | None = None,
        path: str | Path | None = None,
        next_number: int = 1,
    ) -> None:
        self.path = Path(path) if path else None
        self.projects: list[Project] = list(projects or [])
        self.comments: list[tuple[str, str]] = []
        self.attachments: list[tuple[str, str]] = []
        self._next_number = next_number
        if self.path and self.path.is_file() and not projects:
            self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        self.projects = [_project_from_dict(p) for p in raw.get("projects", [])]
        self.comments = [tuple(c) for c in raw.get("comments", [])]  # type: ignore[misc]
        self.attachments = [tuple(a) for a in raw.get("attachments", [])]  # type: ignore[misc]
        self._next_number = raw.get("next_number", self._next_number)

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "projects": [_project_to_dict(p) for p in self.projects],
                    "comments": [list(c) for c in self.comments],
                    "attachments": [list(a) for a in self.attachments],
                    "next_number": self._next_number,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    # -- the LinearProjects surface ------------------------------------------

    def list_projects(self, first: int = PAGE_SIZE) -> list[Project]:
        return list(self.projects)[:first]

    def get(self, project_id: str) -> Project:
        for project in self.projects:
            if project.id == project_id:
                return project
        raise LinearApiError(f"no such project: {project_id}")

    def find(self, needle: str) -> Project | None:
        wanted = needle.strip().lower()
        for project in self.projects:
            if wanted in ((project.id or "").lower(), (project.slug_id or "").lower()):
                return project
        matches = [p for p in self.projects if p.name.strip().lower() == wanted]
        if len(matches) > 1:
            raise LinearApiError(f"{len(matches)} projects are called {needle!r}.")
        return matches[0] if matches else None

    def ready(self, status: str = READY_STATUS) -> list[Project]:
        wanted = status.strip().lower()
        return [p for p in self.projects if p.status.strip().lower() == wanted]

    def create(
        self,
        project: Project,
        *,
        team_key: str = "TEAM",
        status: str = DRAFT_STATUS,
        lead_id: str | None = None,
    ) -> Project:
        project.id = f"stub-project-{self._next_number}"
        project.slug_id = f"stub-{self._next_number}"
        project.url = f"https://linear.app/stub/project/{project.slug_id}"
        project.status = status
        self._next_number += 1
        self.projects.append(project)
        self._save()
        return project

    def comment(self, project_id: str, body: str) -> None:
        self.comments.append((project_id, body))
        self._save()

    def attach(self, issue_id: str, project_id: str) -> None:
        self.attachments.append((issue_id, project_id))
        self._save()


def _project_to_dict(project: Project) -> dict:
    from .brief import render_content

    return {
        "id": project.id,
        "slug_id": project.slug_id,
        "url": project.url,
        "name": project.name,
        "description": project.summary,
        "status": project.status,
        "content": render_content(project),
    }


def _project_from_dict(raw: dict) -> Project:
    node = {
        "id": raw.get("id"),
        "slugId": raw.get("slug_id"),
        "url": raw.get("url"),
        "name": raw.get("name", ""),
        "description": raw.get("description", ""),
        "content": raw.get("content", ""),
        "status": {"name": raw.get("status", "")},
    }
    return project_from_node(node)


def stub_path(repo_root: str | Path) -> Path:
    from .state import RED_DIR

    return Path(repo_root) / RED_DIR / STUB_FILE
