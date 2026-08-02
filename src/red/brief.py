"""Push phase: prose in, a Linear project out.

I describe what I want; an agent asks about the gaps and drafts a project. This
is the only phase that writes a project to Linear on purpose.

The questions are the point. A project drafted here is sliced later by a
different agent, and each slice is handed to Forman, which hands it to another
agent again. Nobody downstream can ask the original question. An ambiguity left
here does not produce a vague project, it produces a confident wrong one three
sessions later.

Splitting rule: a scope item is a slice that could ship on its own. That is the
same rule Forman uses for tickets, one level up.
"""

from __future__ import annotations

from typing import Callable

from forman.spawn import (
    DEFAULT_MODEL,
    Activity,
    AgentRun,
    extract_last_json,
    run_conversation,
)
from forman.topo import CycleError, topo_sort

from .models import Project, ScopeItem
from .scope import SCOPE_HEADING, parse_scope, render_scope, split_scope

BRIEF_MAX_TURNS = 24

MAX_QUESTION_ROUNDS = 3

READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]

_PROJECT_JSON_SHAPE = """\
Emit as your final message a single JSON object and nothing else:
{"project": {
  "name": "short, memorable, under 60 chars",
  "summary": "one sentence someone could read in a project list",
  "outcome": "what is different once this is done, and for whom. 2-4 sentences.",
  "success_criteria": ["observable outcome for the project as a whole"],
  "constraints": "repos, stack, deadlines, anything that limits the approach",
  "out_of_scope": "explicit non-goals for the whole project",
  "scope_items": [{
    "title": "imperative, under 80 chars",
    "depends_on": [],
    "prose": "a paragraph or two describing this slice as you would to an \
engineer who has not been in this conversation"
  }]
}}
Every field is required. `depends_on` holds the 1-based positions of other \
items in your own scope_items list. `prose` is handed to a ticket-writing agent \
verbatim and is the only thing it will see besides the brief, so write it to \
stand on its own.\
"""

_SPLIT_RULE = """\
Splitting rule: a scope item is a slice that could ship on its own and be worth \
having. Do not split by layer or by file. Do not invent work the person did not \
describe. Most projects are two to six items; fewer is usually better. Whenever \
ordering matters, populate depends_on.\
"""

BRIEF_CONVERSATION_CONTRACT = f"""\
You are helping someone define a Linear project. Later, each slice of it will be \
handed on its own to an agent that files tickets, and those tickets are executed \
by agents with no supervision. Nobody downstream can come back and ask you \
anything. Your job is to end up with a project that can survive that.

Before drafting, check whether you have these six things. They are what the \
pipeline needs in order to work, and nothing outside this list justifies a \
question:

1. The outcome. What is different once this is done, and for whom.
2. Success criteria for the project as a whole. Observable things someone could \
check without reading the author's mind.
3. The slices. What are the independently shippable pieces.
4. Out of scope. What must NOT be touched, for the whole project.
5. Ordering. Which slices have to happen before which.
6. Constraints. Repos, stack, deadlines, anything limiting the approach.

Answer as many of these as you can yourself by reading the repository you are \
in. Always prefer looking over asking: a question you could have settled by \
opening a file is a question you should not ask. Do not write or edit any code.

Then ask ONLY for the gaps that remain, all in one message rather than one \
question at a time. Keep it short and concrete. If the person's description \
already covers everything, ask nothing at all and draft immediately. You have at \
most {MAX_QUESTION_ROUNDS} rounds of questions; after that, draft with what you \
have and record the remaining uncertainty under out_of_scope or in the \
constraints.

{_SPLIT_RULE}

While you still have questions, write them as plain prose and nothing else. \
Emit no JSON until you are ready to draft, because the JSON is the signal that \
you are done asking.

{_PROJECT_JSON_SHAPE}"""


class BriefError(RuntimeError):
    """The brief agent produced something unusable."""


class Aborted(RuntimeError):
    """The human walked away. Nothing was created."""


# -- json in ------------------------------------------------------------------


def parse_brief(text: str) -> dict:
    payload = extract_last_json(text)
    if not isinstance(payload, dict) or "project" not in payload:
        raise BriefError("brief agent emitted no `project` object")
    fields = payload.get("project")
    if not isinstance(fields, dict) or not str(fields.get("name", "")).strip():
        raise BriefError("the drafted project has no name")
    items = fields.get("scope_items")
    if not isinstance(items, list) or not items:
        raise BriefError("the drafted project has no scope items")
    return fields


def to_project(fields: dict) -> Project:
    """Build a Project with its scope items ordered so nothing precedes its
    blocker, the way Forman orders sub-tasks before numbering them."""
    raw = [entry for entry in fields["scope_items"] if isinstance(entry, dict)]
    ordered = _dependency_order(raw)

    position_of = {original: position for position, original in enumerate(ordered, 1)}
    scope = []
    for position, original in enumerate(ordered, start=1):
        entry = raw[original]
        depends = sorted(
            {
                position_of[int(ref) - 1]
                for ref in entry.get("depends_on") or []
                if str(ref).isdigit() and (int(ref) - 1) in position_of
                and position_of[int(ref) - 1] != position
            }
        )
        scope.append(
            ScopeItem(
                number=position,
                title=str(entry.get("title") or "").strip() or f"Slice {position}",
                prose=str(entry.get("prose") or "").strip(),
                depends_on=depends,
            )
        )

    return Project(
        name=str(fields["name"]).strip(),
        summary=str(fields.get("summary") or "").strip(),
        outcome=str(fields.get("outcome") or "").strip(),
        success_criteria=[str(c) for c in fields.get("success_criteria") or []],
        constraints=str(fields.get("constraints") or "").strip(),
        out_of_scope=str(fields.get("out_of_scope") or "").strip(),
        scope=scope,
    )


def _dependency_order(raw: list[dict]) -> list[int]:
    count = len(raw)
    graph: dict[str, list[str]] = {}
    for index, entry in enumerate(raw):
        deps = set()
        for ref in entry.get("depends_on") or []:
            ref = str(ref).strip()
            if ref.isdigit() and 0 <= int(ref) - 1 < count and int(ref) - 1 != index:
                deps.add(str(int(ref) - 1))
        graph[str(index)] = sorted(deps, key=int)
    try:
        return [int(node) for node in topo_sort(graph)]
    except CycleError as exc:
        raise BriefError(
            f"brief agent produced circular scope dependencies: {exc.remaining}"
        ) from exc


# -- markdown out and back ----------------------------------------------------

SEPARATOR = "---"


def render_content(project: Project) -> str:
    """The markdown that lives in Linear's `content` field.

    This is the durable artifact. It is what a human edits in Linear between the
    two halves of Red, and `red pull` re-reads it fresh every time, so an edit
    made there always wins over anything Red remembers.
    """
    criteria = [f"- [ ] {c}" for c in project.success_criteria] or ["- [ ] (none given)"]
    return "\n".join(
        [
            "## Outcome",
            project.outcome.strip() or "(not described)",
            "",
            "## Success criteria",
            *criteria,
            "",
            "## Constraints",
            project.constraints.strip() or "(none)",
            "",
            "## Out of scope",
            project.out_of_scope.strip() or "(none stated)",
            "",
            SCOPE_HEADING,
            "",
            render_scope(project.scope),
        ]
    )


def render_draft(project: Project) -> str:
    """What the human reviews before anything is created, and what $EDITOR opens.

    Same shape as Forman's ticket drafts: frontmatter for the fields Linear
    stores beside the body, then the body itself. Round-trips through
    parse_draft.
    """
    return "\n".join(
        [
            SEPARATOR,
            f"name: {project.name}",
            f"summary: {project.summary}",
            SEPARATOR,
            "",
            render_content(project).strip(),
            "",
        ]
    )


def _sections(body: str) -> dict[str, str]:
    """Split a body on `## ` headings, lowercased, whitespace-normalised."""
    found: dict[str, str] = {}
    heading = None
    lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if heading:
                found[heading] = "\n".join(lines).strip()
            heading = " ".join(line[3:].lower().split())
            lines = []
        elif heading:
            lines.append(line)
    if heading:
        found[heading] = "\n".join(lines).strip()
    return found


def _criteria(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        for prefix in ("- [ ] ", "- [x] ", "- [X] ", "- ", "* "):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix) :].strip()
                break
        else:
            continue
        if stripped and stripped != "(none given)":
            out.append(stripped)
    return out


def parse_content(content: str) -> dict:
    """Read a project's Linear content back into fields and scope items.

    Used both after a human edits a draft in $EDITOR and after they edit the
    real project in Linear. Missing sections are empty rather than fatal.
    """
    brief, block = split_scope(content)
    found = _sections(brief)

    def section(name: str) -> str:
        value = found.get(name, "").strip()
        return "" if value in ("(none)", "(none stated)", "(not described)") else value

    return {
        "outcome": section("outcome"),
        "success_criteria": _criteria(found.get("success criteria", "")),
        "constraints": section("constraints"),
        "out_of_scope": section("out of scope"),
        "brief": brief.strip(),
        "scope": parse_scope(block),
    }


def parse_draft(text: str) -> Project:
    """Read a draft back after a human has edited it.

    Deliberately forgiving in the same way Forman's is: someone editing their
    own project in vim should not have to get YAML exactly right for their work
    to survive.
    """
    body = text.strip()
    fields: dict[str, str] = {}

    if body.startswith(SEPARATOR):
        _, _, rest = body.partition(SEPARATOR)
        front, sep, remainder = rest.partition("\n" + SEPARATOR)
        if sep:
            body = remainder.strip()
            for line in front.splitlines():
                key, colon, value = line.partition(":")
                if colon:
                    fields[key.strip().lower()] = value.strip()

    name = fields.get("name", "").strip()
    if not name:
        raise BriefError("the edited project has no name")

    parsed = parse_content(body)
    if not parsed["scope"]:
        raise BriefError("the edited project has no scope items")

    return Project(
        name=name,
        summary=fields.get("summary", "").strip(),
        outcome=parsed["outcome"],
        success_criteria=parsed["success_criteria"],
        constraints=parsed["constraints"],
        out_of_scope=parsed["out_of_scope"],
        scope=parsed["scope"],
    )


# -- the conversation ---------------------------------------------------------


def draft_project(
    *,
    prose: str,
    reviewer,
    cwd: str = ".",
    conversation: Callable[..., AgentRun] = run_conversation,
    model: str | None = DEFAULT_MODEL,
    on_activity: Callable[[Activity], None] | None = None,
) -> Project:
    """Talk it through until the agent stops asking, then return the draft.

    Nothing here touches Linear. Creating is the caller's job, after a human has
    said so.

    The agent reads the repository before it says anything, which can take
    minutes. `on_activity` is how the terminal shows that is happening; see
    `cli.Progress`.
    """
    from forman.review import Question

    rounds = 0

    def respond(agent_text: str) -> str | None:
        nonlocal rounds
        payload = extract_last_json(agent_text)
        if isinstance(payload, dict) and "project" in payload:
            return None  # the JSON is the signal that it is done asking
        rounds += 1
        if rounds > MAX_QUESTION_ROUNDS:
            return "That is enough questions. Draft the project with what you have."
        return reviewer.answer(Question(text=agent_text, round=rounds))

    run = conversation(
        system_prompt=BRIEF_CONVERSATION_CONTRACT,
        opening=f"Here is what I want to achieve:\n\n{prose.strip()}",
        respond=respond,
        cwd=cwd,
        allowed_tools=READ_ONLY_TOOLS,
        max_rounds=MAX_QUESTION_ROUNDS + 2,
        model=model,
        on_activity=on_activity,
    )
    if run.error:
        raise BriefError(f"brief session failed: {run.error}")

    return to_project(parse_brief(run.text))


def redraft_project(
    *,
    prose: str,
    project: Project,
    feedback: str,
    cwd: str = ".",
    conversation: Callable[..., AgentRun] = run_conversation,
    model: str | None = DEFAULT_MODEL,
    on_activity: Callable[[Activity], None] | None = None,
) -> Project:
    """Revise a draft with feedback, without asking anything further."""
    run = conversation(
        system_prompt=BRIEF_CONVERSATION_CONTRACT,
        opening=(
            f"Here is what I want to achieve:\n\n{prose.strip()}\n\n"
            f"You drafted:\n\n{render_draft(project)}\n\n"
            f"Revise it: {feedback}\n\nRedraft now; do not ask more questions."
        ),
        respond=lambda _text: None,
        cwd=cwd,
        allowed_tools=READ_ONLY_TOOLS,
        max_rounds=1,
        model=model,
        on_activity=on_activity,
    )
    if run.error:
        raise BriefError(f"redraft failed: {run.error}")
    return to_project(parse_brief(run.text))
