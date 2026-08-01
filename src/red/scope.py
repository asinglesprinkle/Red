"""The scope block inside a project's Linear content, and how to read it back.

This is the seam between the two halves of Red. `red push` writes it, a human
edits it in Linear, and `red pull` reads whatever survived that edit.

The parser is deliberately forgiving, for the same reason Foreman's
`parse_ticket_markdown` is: someone rewriting their own project in Linear's
editor should not have to keep the numbering consecutive, the markers intact, or
the `depends_on` lines present for their work to survive. Anything it cannot
make sense of is dropped rather than raised, because a project that fails to
parse is a project a person cannot pull, and they have no way to see why.
"""

from __future__ import annotations

import re

from .models import ScopeItem

SCOPE_MARKER = "<!-- red:scope -->"
SCOPE_HEADING = "## Scope"

# "### 3. Wire up the relay" and also "### Wire up the relay".
_TITLE = re.compile(r"^#{1,6}\s*(?:(\d+)[.)]\s*)?(.+?)\s*$")
_DEPENDS = re.compile(r"^depends[_ ]on\s*:\s*(.*)$", re.IGNORECASE)
_REFS = re.compile(r"\d+")


def render_scope(items: list[ScopeItem]) -> str:
    """Render the scope block. Positions are renumbered from 1 as we go."""
    blocks = []
    for position, item in enumerate(items, start=1):
        depends = ", ".join(str(ref) for ref in item.depends_on)
        blocks.append(
            "\n".join(
                [
                    SCOPE_MARKER,
                    f"### {position}. {item.title}",
                    f"depends_on: [{depends}]",
                    "",
                    item.prose.strip() or "(not described)",
                    "",
                ]
            )
        )
    return "\n".join(blocks).rstrip() + "\n"


def split_scope(content: str) -> tuple[str, str]:
    """Separate the brief from the scope block.

    The brief is everything a relay needs as context; the scope block is the
    work. Splitting on the heading rather than the marker means a human who
    deletes every marker still gets a brief that stops in the right place.
    """
    for candidate in (SCOPE_MARKER, SCOPE_HEADING):
        index = content.find(candidate)
        if index != -1:
            head = content[:index].rstrip()
            if candidate == SCOPE_HEADING:
                _, _, tail = content[index:].partition("\n")
                return head, tail.strip()
            return head, content[index:].strip()
    return content.strip(), ""


def _chunks(block: str) -> list[str]:
    """Cut the scope block into one string per item.

    Markers are the intended boundary. If they are gone, headings are the
    fallback, because that is what is left after a round trip through an editor
    that strips HTML comments.
    """
    if SCOPE_MARKER in block:
        return [chunk for chunk in block.split(SCOPE_MARKER) if chunk.strip()]

    chunks: list[str] = []
    current: list[str] = []
    for line in block.splitlines():
        if line.lstrip().startswith("#"):
            if current:
                chunks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _parse_chunk(chunk: str) -> tuple[int | None, str, list[int], str] | None:
    """One chunk into (written number, title, written refs, prose)."""
    lines = chunk.strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None

    match = _TITLE.match(lines[0].strip())
    if not match or not match.group(2).strip():
        return None
    written = int(match.group(1)) if match.group(1) else None
    title = match.group(2).strip()

    rest = lines[1:]
    refs: list[int] = []
    for index, line in enumerate(rest):
        if not line.strip():
            continue
        found = _DEPENDS.match(line.strip())
        if found:
            refs = [int(ref) for ref in _REFS.findall(found.group(1))]
            rest = rest[index + 1 :]
        break

    return written, title, refs, "\n".join(rest).strip()


def parse_scope(block: str) -> list[ScopeItem]:
    """Read a scope block back into items, renumbered by position.

    References are resolved against the numbers as written, so a `depends_on:
    [2]` still means the slice labelled 2 even if a person inserted something
    above it and never renumbered. A reference that resolves to nothing, or to
    the item itself, is dropped: an unparseable ordering hint is worth less than
    the item it is attached to.
    """
    parsed = [entry for entry in (_parse_chunk(c) for c in _chunks(block)) if entry]

    position_of: dict[int, int] = {}
    for position, (written, _title, _refs, _prose) in enumerate(parsed, start=1):
        position_of.setdefault(written if written is not None else position, position)

    items: list[ScopeItem] = []
    for position, (_written, title, refs, prose) in enumerate(parsed, start=1):
        depends = sorted(
            {
                position_of[ref]
                for ref in refs
                if ref in position_of and position_of[ref] != position
            }
        )
        items.append(
            ScopeItem(number=position, title=title, prose=prose, depends_on=depends)
        )
    return items
