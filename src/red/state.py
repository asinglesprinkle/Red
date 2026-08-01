"""On-disk state for a pull, and the manifest a human reads.

Layout, inside whichever repo Red was pointed at:

    .red/<slug>/state.json    the source of truth
                manifest.md   rendered on every write, never parsed back

Same split as Foreman's `.foreman/<TICKET>/`, and for the same reason: one file
a program owns and one file a person reads, so neither has to compromise for the
other.

What is deliberately NOT here is the project itself. That lives in Linear, where
a human can edit it. This directory only records what Red has already done, so
re-running never files the same slice twice.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import ItemState, ItemStatus, Project, ProjectState, iso_now

RED_DIR = ".red"
STATE_FILE = "state.json"
MANIFEST_FILE = "manifest.md"


class StateStore:
    """Everything Red writes to disk. Nothing else touches the filesystem."""

    def __init__(self, repo_root: str | Path) -> None:
        self.root = Path(repo_root)

    # -- paths ---------------------------------------------------------------

    def dir_for(self, slug: str) -> Path:
        return self.root / RED_DIR / slug

    def state_path(self, slug: str) -> Path:
        return self.dir_for(slug) / STATE_FILE

    def manifest_path(self, slug: str) -> Path:
        return self.dir_for(slug) / MANIFEST_FILE

    def exists(self, slug: str) -> bool:
        return self.state_path(slug).is_file()

    def projects(self) -> list[str]:
        base = self.root / RED_DIR
        if not base.is_dir():
            return []
        return sorted(d.name for d in base.iterdir() if (d / STATE_FILE).is_file())

    # -- read and write ------------------------------------------------------

    def load(self, slug: str) -> ProjectState:
        raw = json.loads(self.state_path(slug).read_text(encoding="utf-8"))
        return state_from_dict(raw)

    def save(self, state: ProjectState) -> None:
        directory = self.dir_for(state.slug_id)
        directory.mkdir(parents=True, exist_ok=True)
        self.state_path(state.slug_id).write_text(
            json.dumps(state_to_dict(state), indent=2) + "\n", encoding="utf-8"
        )
        self.manifest_path(state.slug_id).write_text(
            render_manifest(state), encoding="utf-8"
        )

    def init(self, project: Project) -> ProjectState:
        state = ProjectState(
            project_id=project.id or "",
            slug_id=project.slug_id or "",
            name=project.name,
            url=project.url,
            pulled_at=iso_now(),
        )
        reconcile(state, project)
        return state


# -- serialisation ------------------------------------------------------------


def state_to_dict(state: ProjectState) -> dict:
    return asdict(state)


def state_from_dict(raw: dict) -> ProjectState:
    items = [ItemState(**entry) for entry in raw.get("items", [])]
    return ProjectState(
        project_id=raw.get("project_id", ""),
        slug_id=raw.get("slug_id", ""),
        name=raw.get("name", ""),
        url=raw.get("url"),
        pulled_at=raw.get("pulled_at", ""),
        items=items,
    )


# -- reconciliation -----------------------------------------------------------


def reconcile(state: ProjectState, project: Project) -> list[str]:
    """Line the state up with the project as it reads in Linear right now.

    A human is expected to edit the project between runs; that is the whole
    point of it living in Linear. So the project is authoritative about what the
    work is, and the state is authoritative only about what has already been
    done.

    Items are matched on their title, not their position, because inserting a
    slice renumbers everything below it. A retitled slice therefore looks new,
    and Red says so rather than quietly assuming. Records whose item has
    disappeared are kept: they name issues that exist in Linear, and forgetting
    them would not un-file anything.
    """
    notes: list[str] = []
    existing = {entry.key: entry for entry in state.items}
    seen: set[str] = set()

    items: list[ItemState] = []
    for item in project.scope:
        key = item.key()
        seen.add(key)
        found = existing.get(key)
        if found is None:
            items.append(ItemState(number=item.number, title=item.title, key=key))
        else:
            found.number = item.number
            found.title = item.title
            items.append(found)

    for key, entry in existing.items():
        if key not in seen and entry.issues:
            notes.append(
                f"{entry.title!r} is no longer in the project, but filed "
                f"{', '.join(entry.issues)}"
            )
            items.append(entry)

    state.items = items
    return notes


# -- manifest -----------------------------------------------------------------

_MARK = {
    ItemStatus.PENDING.value: "[ ]",
    ItemStatus.CREATED.value: "[x]",
    ItemStatus.SKIPPED.value: "[-]",
}


def render_manifest(state: ProjectState) -> str:
    """A human-readable snapshot. Written on every save, never read back."""
    lines = [
        f"# {state.name}",
        "",
        f"- project: {state.project_id}",
        f"- slug: {state.slug_id}",
    ]
    if state.url:
        lines.append(f"- url: {state.url}")
    lines += [f"- pulled: {state.pulled_at}", "", "## Scope", ""]

    for entry in state.items:
        mark = _MARK.get(entry.status, "[?]")
        issues = f" -> {', '.join(entry.issues)}" if entry.issues else ""
        lines.append(f"{mark} {entry.number}. {entry.title}{issues}")

    filed = state.created_issues()
    lines += ["", f"{len(filed)} issue(s) filed."]
    if not state.all_settled():
        pending = sum(1 for e in state.items if not e.is_settled())
        lines.append(f"{pending} scope item(s) still to go. Run `red pull` again.")
    return "\n".join(lines) + "\n"
