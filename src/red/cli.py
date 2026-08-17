"""The command line. All of Red's terminal I/O lives here and nowhere else.

Every other module takes its input and output as injected callables, which is
what lets the whole thing be tested without a terminal, a model, or an account.
That is Forman's rule, kept.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from forman.config import MissingApiKey, load_settings
from forman.git_ops import GitError, ensure_ignored, repo_root
from forman.linear_client import StubLinearClient
from forman.linear_client import stub_path as forman_stub_path
from forman.linear_graphql import GraphQLLinearClient, LinearApiError

# The display and the input guard live in Forman because both CLIs need exactly
# the same ones, for the same reason. `Progress` is re-exported here rather than
# imported where used, so `red.cli` stays the single name for Red's terminal.
from forman.progress import (
    Progress,
    ask_after_agent,
    drop_typeahead,
    for_terminal,
    quiet_for_prompt,
)
from forman.push import Aborted as PushAborted
from forman.push import PushError, push_interactive
from forman.review import CREATE, EDIT, FEEDBACK, QUIT, Approval

from .brief import (
    Aborted,
    BriefError,
    draft_project,
    parse_draft,
    redraft_project,
    render_draft,
)
from .linear_projects import (
    DRAFT_STATUS,
    READY_STATUS,
    LinearProjects,
    ProjectScopedLinear,
    StubProjectClient,
    brief_of,
    stub_path,
)
from .models import Project
from .pipeline import Deps, run_pull, select_project
from .relay import RelayReviewer
from .state import RED_DIR, StateStore

# Imported for its import side effect, not for anything it exports: this is what
# makes `input()` use GNU readline instead of the terminal driver's canonical
# mode. Without it the kernel handles backspace, and it cannot move the cursor
# back up a row, so editing a line that has wrapped stops dead at the start of
# the current visual row. Red asks for a paragraph at that prompt, so its input
# wraps as a matter of course. Readline also brings arrow keys, ^A/^E/^W/^U, and
# recall of what you typed at an earlier prompt in the same run.
#
# Do not "clean up" this import. Not available on every build, hence the guard.
try:  # pragma: no cover - presence is a property of the interpreter build
    import readline  # noqa: F401
except ImportError:  # pragma: no cover
    pass


# -- shared plumbing ----------------------------------------------------------


def resolve_repo(given: str | None) -> str:
    """Red reads the repository to ground what it drafts, the same way Forman
    does. It never writes to it beyond `.red/`."""
    start = Path(given or ".").resolve()
    try:
        return str(repo_root(start))
    except GitError:
        return str(start)


def _edit_in_editor(text: str, suffix: str = ".md") -> str:
    """Hand text to $EDITOR and read back whatever comes out."""
    import subprocess
    import tempfile

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=suffix, delete=False) as handle:
        handle.write(text)
        path = handle.name
    try:
        subprocess.run([*editor.split(), path], check=True)
        return Path(path).read_text(encoding="utf-8")
    finally:
        Path(path).unlink(missing_ok=True)


def _progress() -> Progress | None:
    """Only narrate to a human watching a terminal. Piped or redirected, stay
    quiet: nothing downstream asked for a running commentary."""
    return for_terminal()


def _ask(prompt: str) -> str:
    return ask_after_agent(prompt)


def _confirm(question: str) -> bool:
    quiet_for_prompt()
    drop_typeahead()
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def build_clients(repo: str, backend: str):
    """Return (projects, linear, team_key). One place decides real or stub.

    The label is not optional here. Forman only pulls tickets carrying its
    provenance mark, so a Red project filed without one is a project Forman
    will never work: the whole point of Red is the handoff, and an unstamped
    ticket breaks it silently at the far end, days later.
    """
    settings = load_settings(repo)
    if backend == "stub":
        return (
            StubProjectClient(path=stub_path(repo)),
            StubLinearClient(path=forman_stub_path(repo), label=settings.label),
            "TEAM",
        )
    settings.require_api_key()
    linear = GraphQLLinearClient(
        api_key=settings.api_key,
        team_key=settings.team_key,
        review_state=settings.review_state,
        progress_state=settings.progress_state,
        user=settings.user,
        label=settings.label,
    )
    team_key = settings.team_key or linear.default_team_key()
    return LinearProjects(linear), linear, team_key


# -- red push -----------------------------------------------------------------


def cmd_push(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)

    prose = args.prose
    if not prose and not sys.stdin.isatty():
        prose = sys.stdin.read()
    if not prose and sys.stdin.isatty():
        print("What do you want to achieve? (a paragraph is fine)")
        prose = input("> ").strip()
    if not prose or not prose.strip():
        print(
            "nothing to push: give prose as an argument or on stdin.", file=sys.stderr
        )
        return 2

    reviewer = ProjectReviewer(ask=_ask, show=print)
    progress = _progress()

    try:
        projects, _linear, team_key = build_clients(repo, args.linear)
        if progress:
            progress.start("reading the repository and drafting")
        project = draft_project(
            prose=prose, reviewer=reviewer, cwd=repo, on_activity=progress
        )
        if progress:
            progress.done()
        project = _approve(
            project, prose=prose, reviewer=reviewer, repo=repo, progress=progress
        )
        if args.dry_run:
            print(render_draft(project))
            print("dry run: nothing was created.")
            return 0
        # The gate is the last thing the human touches; everything after it is
        # network. Say so, or approving looks like the moment it froze.
        if progress:
            progress.start(f"creating {project.name!r} in Linear")
        made = projects.create(project, team_key=team_key, status=DRAFT_STATUS)
        if progress:
            progress.done()
    except Aborted:
        print("nothing created.")
        return 0
    except MissingApiKey as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (LinearApiError, BriefError) as exc:
        print(f"push failed: {exc}", file=sys.stderr)
        return 2

    print()
    print(f"created: {made.name}")
    if made.url:
        print(f"         {made.url}")
    print(f"         {len(made.scope)} scope item(s), status {DRAFT_STATUS}")
    print()
    print(f"Read it in Linear and edit it there. Move it to {READY_STATUS!r} when you")
    print("want `red pull` to start turning it into issues.")
    return 0


def _approve(
    project: Project, *, prose: str, reviewer, repo: str, progress=None
) -> Project:
    """Show the draft and do only what you are told to do with it."""
    while True:
        decision = reviewer.decide(Approval(tickets=[], rendered=render_draft(project)))

        if decision.action == CREATE:
            return project
        if decision.action == QUIT:
            raise Aborted("nothing created")
        if decision.action == EDIT:
            try:
                project = parse_draft(_edit_in_editor(render_draft(project)))
            except BriefError as exc:
                reviewer.show(f"Could not read that back: {exc}. Nothing changed.")
            continue
        if decision.action != FEEDBACK:
            raise BriefError(f"unknown action: {decision.action!r}")

        if progress:
            progress.start("redrafting")
        project = redraft_project(
            prose=prose,
            project=project,
            feedback=decision.feedback,
            cwd=repo,
            on_activity=progress,
        )
        if progress:
            progress.done()


class ProjectReviewer:
    """The terminal gate for `red push`.

    Forman's TerminalReviewer would do, except that its approval prompt counts
    tickets and there are none here yet. Everything else about it is kept, down
    to treating an empty line as assent.
    """

    def __init__(self, ask=_ask, show=print) -> None:
        self._ask = ask
        self._show = show

    def show(self, text: str) -> None:
        self._show(text)

    def answer(self, question) -> str:
        self._show("")
        self._show(question.text)
        return self._ask("\n> ")

    def decide(self, approval):
        from forman.review import parse_decision

        self._show("")
        self._show(approval.rendered)
        return parse_decision(
            self._ask("[c]reate the project, [e]dit, [q]uit, or type feedback: ")
        )


# -- red pull -----------------------------------------------------------------


def cmd_pull(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    progress = _progress()

    try:
        projects, linear, _team_key = build_clients(repo, args.linear)
        project = _choose(projects, args.project)
        if project is None:
            return 1

        ensure_ignored(repo, f"{RED_DIR}/")
        # The offline backend writes issues to Forman's own stub file, so that
        # `forman pull --linear stub` can see what Red filed. That means Red
        # can be the first thing to create `.forman/` in a repo, and it should
        # not leave it lying untracked for someone else to notice.
        ensure_ignored(repo, ".forman/")
        if not project.scope:
            print(f"{project.name} has no scope items to pull.", file=sys.stderr)
            print("Add some in Linear under the Scope heading, then try again.")
            return 1

        scoped = ProjectScopedLinear(linear, projects, project)
        brief = brief_of(project)

        def reviewer_for(*, item, position):
            return RelayReviewer(
                ask=_ask,
                show=print,
                project=project,
                brief=brief,
                item=item,
                position=position,
                cwd=repo,
                verbatim=args.verbatim,
            )

        deps = Deps(
            projects=projects,
            linear=scoped,
            store=StateStore(repo),
            push=push_interactive,
            reviewer_for=reviewer_for,
            confirm=_confirm,
            note=print,
            edit=_edit_in_editor,
            cwd=repo,
            started=progress.start if progress else None,
            finished=progress.done if progress else None,
            on_activity=progress,
        )
        report = run_pull(deps, project)
    except MissingApiKey as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except PushAborted:
        print("nothing created.")
        return 0
    except (LinearApiError, PushError, BriefError) as exc:
        print(f"pull failed: {exc}", file=sys.stderr)
        return 2

    return _report(report, project)


def _choose(projects, wanted: str | None):
    if wanted:
        found = projects.find(wanted)
        if found is None:
            print(f"no project matching {wanted!r}.", file=sys.stderr)
            return None
        return found

    ready = projects.ready(READY_STATUS)
    chosen = select_project(ready)
    if chosen:
        return chosen

    if not ready:
        print(f"no projects in {READY_STATUS!r}.", file=sys.stderr)
        print("Move one there in Linear when it has been reviewed.")
        return None

    print(f"{len(ready)} projects are in {READY_STATUS!r}. Name one with --project:")
    for project in ready:
        print(f"  {project.name}")
    return None


def _report(report, project: Project) -> int:
    print()
    for note in report.notes:
        print(f"note: {note}")

    if report.outcome == "no_work":
        print(f"{project.name}: nothing left to file.")
        return 0

    print(f"{project.name}: filed {len(report.issues)} issue(s).")
    for issue in report.issues:
        print(f"  {issue}")
    for title in report.skipped:
        print(f"  skipped: {title}")
    if report.remaining:
        print(f"\n{report.remaining} scope item(s) still to go. Run `red pull` again.")
    if report.url:
        print(f"\n{report.url}")
    print("\nThe project status is unchanged. Forman can pull these issues now.")
    return 0


# -- red status ---------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    store = StateStore(repo)
    slugs = store.projects()
    if not slugs:
        print(f"nothing pulled yet in {repo}.")
        return 0
    for slug in slugs:
        print(store.manifest_path(slug).read_text(encoding="utf-8"))
    return 0


# -- entry point --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="red",
        description=(
            "Talk it through, file a Linear project, then turn a reviewed "
            "project into issues with Forman."
        ),
    )
    parser.add_argument("--repo", help="repository to work in (default: cwd)")
    parser.add_argument(
        "--linear",
        choices=("api", "stub"),
        default="api",
        help="stub runs the whole loop offline against a local JSON file",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    push = sub.add_parser("push", help="talk it through and file a Linear project")
    push.add_argument(
        "prose", nargs="?", help="what you want; read from stdin if absent"
    )
    push.add_argument(
        "--dry-run", action="store_true", help="print the draft and create nothing"
    )
    push.set_defaults(func=cmd_push)

    pull = sub.add_parser("pull", help="turn a reviewed project into Linear issues")
    pull.add_argument("project", nargs="?", help="project name or slug id")
    pull.add_argument(
        "--verbatim",
        action="store_true",
        help="do not draft answers; relay every question byte for byte",
    )
    pull.set_defaults(func=cmd_pull)

    status = sub.add_parser("status", help="what has been pulled in this repo")
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        sys.stdout.flush()
        print("\ninterrupted. nothing further was created.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
