"""The pull loop: a reviewed project becomes Linear issues, one slice at a time.

LOCKED: Red never changes a project's status. Moving a project from Backlog to
Planned is a human saying they have read it. Moving it on from there is a human
saying the work is happening. Neither is something code gets to assert. Red
files issues, comments what it filed, and stops. Same rule as Forman's
orchestrator, one level up.

This module performs no I/O of its own. Every external effect goes through a
port on `Deps`, which is what lets the whole loop be tested without Linear, a
model, a repo, or a terminal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from forman.topo import CycleError, topo_sort

from .models import ItemState, ItemStatus, Project, ProjectState, ScopeItem, iso_now
from .state import reconcile

# What a human sees at the top of each slice, and what the ticket agent is told
# before the slice itself. The brief is repeated per slice because each
# `forman push` is a fresh session that has never seen this project.
_OPENING = """\
This is one slice of a larger project. Here is the project it belongs to, so you \
can ground the ticket in it. File tickets ONLY for the slice, not for the rest \
of the project.

{brief}

# The slice to file tickets for: {title}

{prose}\
"""


class PushPort(Protocol):
    """Forman's push_interactive, or something shaped like it."""

    def __call__(
        self,
        *,
        prose: str,
        linear: Any,
        reviewer: Any,
        edit: Callable[[str], str] | None,
        cwd: str,
        on_activity: Callable[[Any], None] | None = ...,
        warn: Callable[[str], None] | None = ...,
    ) -> list[Any]: ...


class ReviewerFactory(Protocol):
    """Builds the Reviewer that stands between Forman and the human."""

    def __call__(self, *, item: ScopeItem, position: str) -> Any: ...


@dataclass
class Deps:
    """Every external effect the loop performs. Nothing else is allowed."""

    projects: Any  # LinearProjects or StubProjectClient
    linear: Any  # a LinearClient, already scoped to the project
    store: Any  # StateStore
    push: PushPort
    reviewer_for: ReviewerFactory
    confirm: Callable[[str], bool]  # "carry on after a skip?"
    note: Callable[[str], None]  # progress the human reads as it happens
    edit: Callable[[str], str] | None = None
    cwd: str = "."
    now: Callable[[], str] = iso_now
    # Optional, and injected like everything else here. A slice heading with a
    # silent terminal under it is the shape this loop had before: `started` says
    # the agent is running, `finished` takes the live line back down.
    started: Callable[[str], None] | None = None
    finished: Callable[[], None] | None = None
    on_activity: Callable[[Any], None] | None = None


@dataclass
class PullReport:
    """What one `red pull` produced. Returned so the CLI can print it without
    the loop doing any I/O of its own."""

    project: str = ""
    url: str | None = None
    outcome: str = "no_work"  # no_work | filed | stopped | nothing_created
    issues: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    remaining: int = 0
    notes: list[str] = field(default_factory=list)


def select_project(projects: list[Project]) -> Project | None:
    """Pick which ready project to work.

    Deliberately dumber than Forman's ticket selection. A project is a big
    thing to start by accident, so when there is more than one candidate the
    caller is expected to ask rather than guess. This only settles the
    unambiguous case.
    """
    if len(projects) == 1:
        return projects[0]
    return None


def order_scope(project: Project) -> list[ScopeItem]:
    """Blockers first, so a slice is never filed before what it depends on.

    A cycle is not fatal here the way it is in a decomposition. The ordering is
    a hint about which slice to hand over first, and a person watching each
    handover is a better safeguard than a refusal to start.
    """
    by_number = {item.number: item for item in project.scope}
    graph = {
        str(item.number): sorted(
            {str(ref) for ref in item.depends_on if ref in by_number},
            key=int,
        )
        for item in project.scope
    }
    try:
        return [by_number[int(node)] for node in topo_sort(graph)]
    except CycleError:
        return list(project.scope)


def opening_for(brief: str, item: ScopeItem) -> str:
    return _OPENING.format(
        brief=brief.strip(), title=item.title, prose=item.prose.strip()
    )


def run_pull(deps: Deps, project: Project) -> PullReport:
    """Walk a project's scope, handing each pending slice to Forman."""
    from forman.push import Aborted

    from .linear_projects import brief_of

    report = PullReport(project=project.name, url=project.url)

    slug = project.slug_id or project.id or project.name
    if deps.store.exists(slug):
        state: ProjectState = deps.store.load(slug)
        state.name = project.name
        state.url = project.url
        report.notes += reconcile(state, project)
    else:
        state = deps.store.init(project)
    deps.store.save(state)

    ordered = order_scope(project)
    pending = [
        item
        for item in ordered
        if _record(state, item).status == ItemStatus.PENDING.value
    ]
    if not pending:
        report.outcome = "no_work"
        report.issues = state.created_issues()
        report.notes.append("every scope item is already settled.")
        return report

    brief = brief_of(project)
    stopped = False

    for index, item in enumerate(pending, start=1):
        record = _record(state, item)
        position = f"slice {item.number} of {len(project.scope)}"
        deps.note(f"\n=== {position}: {item.title} ({index}/{len(pending)}) ===")

        reviewer = deps.reviewer_for(item=item, position=position)
        try:
            if deps.started:
                deps.started(f"drafting tickets for {item.title!r}")
            tickets = deps.push(
                prose=opening_for(brief, item),
                linear=deps.linear,
                reviewer=reviewer,
                edit=deps.edit,
                cwd=deps.cwd,
                on_activity=deps.on_activity,
                # A slice that could not record what it depends on is worth
                # reading about while the run is still in front of you: the
                # tickets look right, and only `forman pull` finds out later
                # that it does not know what order to work them in.
                warn=lambda message: deps.note(f"warning: {message}"),
            )
        except Aborted:
            record.status = ItemStatus.SKIPPED.value
            record.finished_at = deps.now()
            deps.store.save(state)
            report.skipped.append(item.title)
            deps.note(f"skipped: {item.title}. nothing was created for it.")
            if index < len(pending) and not deps.confirm(
                "carry on with the remaining slices?"
            ):
                stopped = True
                break
            continue
        finally:
            # Whatever happened, the live line comes down before anything else
            # is printed. `continue` and `break` run this too.
            if deps.finished:
                deps.finished()

        record.issues = [t.identifier for t in tickets]
        record.status = ItemStatus.CREATED.value
        record.finished_at = deps.now()
        deps.store.save(state)
        report.issues += record.issues
        deps.note(f"filed: {', '.join(record.issues) or '(nothing)'}")

        _link(deps, state, project, item, record, report)

    report.remaining = sum(1 for entry in state.items if not entry.is_settled())
    if stopped:
        report.outcome = "stopped"
    elif report.issues:
        report.outcome = "filed"
    else:
        report.outcome = "nothing_created"

    if report.issues and project.id:
        try:
            deps.projects.comment(project.id, summary_comment(report))
        except Exception as exc:  # noqa: BLE001 - a failed note must not undo real work
            report.notes.append(f"could not comment on the project: {exc}")

    return report


def _record(state: ProjectState, item: ScopeItem) -> ItemState:
    found = state.find(item.key())
    if found is None:  # reconcile always makes one, so this is a programming error
        raise KeyError(f"no state record for scope item {item.title!r}")
    return found


def _link(
    deps: Deps,
    state: ProjectState,
    project: Project,
    item: ScopeItem,
    record: ItemState,
    report: PullReport,
) -> None:
    """Record cross-slice ordering in Linear.

    Forman's own `blocked_by` uses 1-based indices within a single push call,
    so it cannot say anything about a slice filed in a different call. Red owns
    that half. Every issue from a blocking slice blocks every issue from this
    one, which is coarse but true: the whole slice had to happen first.
    """
    relate = getattr(deps.linear, "relate_blocks", None)
    if relate is None or not record.issues:
        return

    by_number = {entry.number: entry for entry in project.scope}
    for ref in item.depends_on:
        blocker_item = by_number.get(ref)
        if blocker_item is None:
            continue
        blocker = state.find(blocker_item.key())
        if blocker is None:
            continue
        for upstream in blocker.issues:
            for downstream in record.issues:
                try:
                    relate(upstream, downstream)
                except Exception as exc:  # noqa: BLE001 - ordering is a hint, not the work
                    report.notes.append(
                        f"could not record {upstream} blocks {downstream}: {exc}"
                    )


def summary_comment(report: PullReport) -> str:
    lines = ["Red filed issues for this project:", ""]
    lines += [f"- {issue}" for issue in report.issues]
    if report.skipped:
        lines += ["", "Skipped at review:"]
        lines += [f"- {title}" for title in report.skipped]
    if report.remaining:
        lines += ["", f"{report.remaining} scope item(s) still to go."]
    lines += ["", "Nothing was moved. The project status is unchanged."]
    return "\n".join(lines)
