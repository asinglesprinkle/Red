"""The pull loop: a reviewed project becomes issues, and nothing else moves."""

from __future__ import annotations

from forman.models import Ticket

from red.models import ItemStatus, ScopeItem
from red.pipeline import Deps, opening_for, order_scope, run_pull, summary_comment
from red.state import StateStore, reconcile

from fakes import FROZEN, AutoApprove, FakeStore, ScriptedPush, project


class FakeLinear:
    """A LinearClient that hands back identifiers and records relations."""

    def __init__(self, prefix: str = "ENG") -> None:
        self.prefix = prefix
        self.created: list[Ticket] = []
        self.relations: list[tuple[str, str]] = []
        self._next = 1

    def create(self, ticket: Ticket) -> Ticket:
        ticket.identifier = f"{self.prefix}-{self._next}"
        self._next += 1
        self.created.append(ticket)
        return ticket

    def relate_blocks(self, blocker: str, blocked: str) -> None:
        self.relations.append((blocker, blocked))


class FakeProjects:
    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []
        self.status_changes: list[tuple[str, str]] = []

    def comment(self, project_id: str, body: str) -> None:
        self.comments.append((project_id, body))


def make_deps(
    push: ScriptedPush | None = None,
    *,
    store=None,
    linear=None,
    projects=None,
    confirm=lambda _q: True,
    reviewer=None,
):
    return Deps(
        projects=projects or FakeProjects(),
        linear=linear or FakeLinear(),
        store=store or FakeStore(),
        push=push or ScriptedPush(),
        reviewer_for=lambda *, item, position: reviewer or AutoApprove(),
        confirm=confirm,
        note=lambda _text: None,
        now=lambda: FROZEN,
    )


# -- the happy path -----------------------------------------------------------


def test_every_slice_is_handed_over_once_and_the_issues_come_back():
    push = ScriptedPush(
        per_slice=[
            {"tickets": ["Add a token bucket"]},
            {"tickets": ["Wire it in", "Add a metric"]},
        ]
    )
    deps = make_deps(push)

    report = run_pull(deps, project(slices=2))

    assert report.outcome == "filed"
    assert report.issues == ["ENG-1", "ENG-2", "ENG-3"]
    assert len(push.calls) == 2
    assert report.remaining == 0


def test_the_brief_and_the_slice_both_reach_forman():
    push = ScriptedPush()
    run_pull(make_deps(push), project(slices=1))

    prose = push.calls[0]["prose"]
    assert "The API survives a burst." in prose  # the brief
    assert "Do the 1 thing." in prose  # the slice
    assert "File tickets ONLY for the slice" in prose


def test_the_project_status_is_never_touched():
    projects = FakeProjects()
    deps = make_deps(projects=projects)

    run_pull(deps, project(slices=2))

    # Nothing on the port even offers to move it, which is the point: there is
    # no call to forget to leave out.
    assert not hasattr(projects, "set_status")
    assert projects.status_changes == []


def test_one_comment_names_what_was_filed():
    projects = FakeProjects()
    report = run_pull(make_deps(projects=projects), project(slices=1))

    assert len(projects.comments) == 1
    project_id, body = projects.comments[0]
    assert project_id == "project-uuid"
    assert "ENG-1" in body
    assert "The project status is unchanged." in body
    assert report.notes == []


def test_a_failed_comment_does_not_undo_real_work():
    class Broken(FakeProjects):
        def comment(self, project_id, body):
            raise RuntimeError("linear is down")

    report = run_pull(make_deps(projects=Broken()), project(slices=1))

    assert report.outcome == "filed"
    assert report.issues == ["ENG-1"]
    assert "could not comment" in report.notes[0]


# -- ordering -----------------------------------------------------------------


def test_slices_are_handed_over_blockers_first():
    scope = [
        ScopeItem(1, "Last", "c", depends_on=[2]),
        ScopeItem(2, "Middle", "b", depends_on=[3]),
        ScopeItem(3, "First", "a", depends_on=[]),
    ]
    assert [i.title for i in order_scope(project(scope=scope))] == [
        "First",
        "Middle",
        "Last",
    ]


def test_a_dependency_cycle_starts_anyway_rather_than_refusing():
    scope = [
        ScopeItem(1, "A", "a", depends_on=[2]),
        ScopeItem(2, "B", "b", depends_on=[1]),
    ]
    assert [i.title for i in order_scope(project(scope=scope))] == ["A", "B"]


def test_cross_slice_ordering_is_recorded_in_linear():
    linear = FakeLinear()
    push = ScriptedPush(
        per_slice=[{"tickets": ["one"]}, {"tickets": ["two", "three"]}]
    )
    run_pull(make_deps(push, linear=linear), project(slices=2))

    # Slice 2 depends on slice 1, so slice 1's issue blocks both of slice 2's.
    assert linear.relations == [("ENG-1", "ENG-2"), ("ENG-1", "ENG-3")]


def test_a_refused_relation_is_a_note_and_not_a_failure():
    class Refusing(FakeLinear):
        def relate_blocks(self, blocker, blocked):
            raise RuntimeError("nope")

    report = run_pull(make_deps(linear=Refusing()), project(slices=2))

    assert report.outcome == "filed"
    assert any("could not record" in note for note in report.notes)


# -- saying no ----------------------------------------------------------------


def test_quitting_a_slice_skips_it_and_creates_nothing_for_it():
    linear = FakeLinear()

    class Quitting(ScriptedPush):
        """Says no to the first slice, yes to the second."""

        def __call__(self, *, prose, linear, reviewer, edit=None, cwd=".", **_kw):
            from forman.push import Aborted

            self.calls.append({"prose": prose, "cwd": cwd, "script": {}})
            if len(self.calls) == 1:
                raise Aborted("nothing created")
            return [linear.create(Ticket("", "two"))]

    report = run_pull(make_deps(Quitting(), linear=linear), project(slices=2))

    assert report.skipped == ["Slice 1"]
    assert report.issues == ["ENG-1"]
    assert [t.title for t in linear.created] == ["two"]


def test_saying_no_to_carrying_on_stops_the_run():
    class AlwaysQuits(ScriptedPush):
        def __call__(self, **_kwargs):
            from forman.push import Aborted

            self.calls.append({})
            raise Aborted("nothing created")

    push = AlwaysQuits()
    report = run_pull(
        make_deps(push, confirm=lambda _q: False), project(slices=3)
    )

    assert report.outcome == "stopped"
    assert len(push.calls) == 1
    assert report.remaining == 2


def test_the_last_slice_never_asks_whether_to_carry_on():
    asked: list[str] = []

    class AlwaysQuits(ScriptedPush):
        def __call__(self, **_kwargs):
            from forman.push import Aborted

            raise Aborted("nothing created")

    run_pull(
        make_deps(AlwaysQuits(), confirm=lambda q: asked.append(q) or True),
        project(slices=1),
    )
    assert asked == []


# -- resuming -----------------------------------------------------------------


def test_a_second_run_only_offers_what_is_still_pending(tmp_path):
    store = StateStore(tmp_path)
    subject = project(slices=3)
    first = ScriptedPush()

    run_pull(make_deps(first, store=store), subject)
    assert len(first.calls) == 3

    second = ScriptedPush()
    report = run_pull(make_deps(second, store=store), subject)

    assert second.calls == []
    assert report.outcome == "no_work"
    assert "already settled" in report.notes[-1]


def test_a_stopped_run_picks_up_where_it_left_off(tmp_path):
    store = StateStore(tmp_path)
    subject = project(slices=3)

    class QuitsOnce(ScriptedPush):
        def __call__(self, *, prose, linear, reviewer, edit=None, cwd=".", **kw):
            from forman.push import Aborted

            self.calls.append({"prose": prose})
            raise Aborted("nothing created")

    run_pull(make_deps(QuitsOnce(), store=store, confirm=lambda _q: False), subject)

    resumed = ScriptedPush()
    report = run_pull(make_deps(resumed, store=store), subject)

    # The first slice was skipped, so it is settled; the other two are offered.
    assert len(resumed.calls) == 2
    assert report.issues == ["ENG-1", "ENG-2"]


def test_state_is_saved_after_every_slice_not_just_at_the_end(tmp_path):
    store = FakeStore()
    run_pull(make_deps(ScriptedPush(), store=store), project(slices=3))

    # One save on load, then one per slice. A crash halfway never loses a slice
    # that was really filed.
    filed_counts = [len(s.created_issues()) for s in store.saved]
    assert filed_counts == [0, 1, 2, 3]


# -- reconciliation -----------------------------------------------------------


def test_an_edited_project_is_matched_on_titles_not_positions():
    subject = project(slices=2)
    state = StateStore(".").init(subject)
    state.items[1].status = ItemStatus.CREATED.value
    state.items[1].issues = ["ENG-9"]

    # A human inserts a slice at the top in Linear.
    subject.scope.insert(0, ScopeItem(1, "New first thing", "prose", []))
    for position, item in enumerate(subject.scope, start=1):
        item.number = position

    notes = reconcile(state, subject)

    assert [e.title for e in state.items] == ["New first thing", "Slice 1", "Slice 2"]
    assert state.items[2].issues == ["ENG-9"]  # still attached to its own slice
    assert state.items[0].status == ItemStatus.PENDING.value
    assert notes == []


def test_a_slice_deleted_after_filing_is_remembered_and_reported():
    subject = project(slices=2)
    state = StateStore(".").init(subject)
    state.items[0].status = ItemStatus.CREATED.value
    state.items[0].issues = ["ENG-5"]

    subject.scope = subject.scope[1:]
    notes = reconcile(state, subject)

    assert "ENG-5" in notes[0]
    assert any(e.issues == ["ENG-5"] for e in state.items)


def test_a_retitled_slice_looks_new_rather_than_being_assumed():
    subject = project(slices=1)
    state = StateStore(".").init(subject)
    state.items[0].status = ItemStatus.CREATED.value
    state.items[0].issues = ["ENG-5"]

    subject.scope[0].title = "Slice one, renamed"
    reconcile(state, subject)

    fresh = [e for e in state.items if e.title == "Slice one, renamed"]
    assert fresh and fresh[0].status == ItemStatus.PENDING.value


def test_titles_match_regardless_of_case_and_spacing():
    subject = project(slices=1)
    state = StateStore(".").init(subject)
    state.items[0].status = ItemStatus.CREATED.value

    subject.scope[0].title = "  SLICE   1  "
    reconcile(state, subject)

    assert len(state.items) == 1
    assert state.items[0].status == ItemStatus.CREATED.value


# -- odds and ends ------------------------------------------------------------


def test_a_project_with_no_scope_is_no_work():
    report = run_pull(make_deps(), project(scope=[]))
    assert report.outcome == "no_work"


def test_the_opening_stands_on_its_own():
    item = ScopeItem(1, "Wire it in", "Call the bucket.", [])
    opening = opening_for("## Outcome\n\nSurvive a burst.", item)

    assert "Survive a burst." in opening
    assert "Wire it in" in opening
    assert "Call the bucket." in opening


def test_the_summary_says_what_was_skipped_and_what_is_left():
    from red.pipeline import PullReport

    body = summary_comment(
        PullReport(issues=["ENG-1"], skipped=["Slice 2"], remaining=1)
    )
    assert "ENG-1" in body
    assert "Slice 2" in body
    assert "1 scope item(s) still to go" in body
