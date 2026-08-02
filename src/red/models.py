"""Data model for the project layer.

Same rule as Forman's models.py: everything crossing a layer boundary is one of
these dataclasses, so a fake only has to return one of them rather than
impersonate Linear or an SDK.

Two units exist here that Forman does not have. A `Project` is what a human
reviews and edits in Linear. A `ScopeItem` is one independently shippable slice
of it, and is the prose a single `forman push` gets handed. Forman's Ticket
stays Forman's; Red never redefines it.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum


def iso_now() -> str:
    """UTC timestamp in ISO 8601, matching Forman's."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


class ItemStatus(str, Enum):
    """Scope item lifecycle.

    `skipped` is a human saying no at the gate, which is a real answer and not
    an error. It is never retried on its own; re-filing it means editing the
    project or asking for it by hand.
    """

    PENDING = "pending"
    CREATED = "created"
    SKIPPED = "skipped"


@dataclass
class ScopeItem:
    """One independently shippable slice of a project.

    `prose` is handed to `forman push` verbatim. `depends_on` holds the
    positions of other items in the same project, 1-based, in the order they
    appear after parsing.
    """

    number: int
    title: str
    prose: str = ""
    depends_on: list[int] = field(default_factory=list)

    def key(self) -> str:
        """What state records match on.

        Position is not stable: a human who inserts a slice in Linear shifts
        every number after it. The title is what a person actually thinks of the
        slice as, so that is what identity is built on.
        """
        return " ".join(self.title.lower().split())


@dataclass
class Project:
    """A Linear project. This is the only unit Red creates on purpose."""

    name: str
    summary: str = ""  # Linear's `description`, one line, shown in lists
    outcome: str = ""
    success_criteria: list[str] = field(default_factory=list)
    constraints: str = ""
    out_of_scope: str = ""
    scope: list[ScopeItem] = field(default_factory=list)
    # Populated once it exists in Linear. Absent on a draft.
    id: str | None = None
    slug_id: str | None = None
    url: str | None = None
    status: str = ""

    def item(self, number: int) -> ScopeItem:
        for entry in self.scope:
            if entry.number == number:
                return entry
        raise KeyError(f"no scope item {number} in {self.name!r}")


@dataclass
class ItemState:
    """What one scope item has produced so far."""

    number: int
    title: str
    key: str = ""
    status: str = ItemStatus.PENDING.value
    issues: list[str] = field(default_factory=list)
    finished_at: str | None = None

    def is_settled(self) -> bool:
        """Settled means Red will not offer to file it again on a re-run."""
        return self.status in (ItemStatus.CREATED.value, ItemStatus.SKIPPED.value)


@dataclass
class ProjectState:
    """Contents of `.red/<slug>/state.json`. The source of truth for a pull.

    `manifest.md` is rendered from this on every write and never parsed back,
    the same way Forman treats its own manifest.
    """

    project_id: str
    slug_id: str
    name: str
    url: str | None = None
    pulled_at: str = ""
    items: list[ItemState] = field(default_factory=list)

    def find(self, key: str) -> ItemState | None:
        for entry in self.items:
            if entry.key == key:
                return entry
        return None

    def created_issues(self) -> list[str]:
        return [issue for entry in self.items for issue in entry.issues]

    def all_settled(self) -> bool:
        return bool(self.items) and all(entry.is_settled() for entry in self.items)
