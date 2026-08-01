# Red: the product manager in front of Foreman

Build spec. Written before any code existed, kept for provenance. Read the
README for how the shipped tool actually behaves; where the two disagree, the
README is right. Foreman keeps its own original spec here for the same reason.

Built since, and not in this document: `red status`, a `ProjectReviewer` for the
push gate (Foreman's counts tickets, and there are none yet at that point), a
`conftest.py` that makes the suite fail rather than start a real agent session,
and a title-based reconciliation so editing a project in Linear between runs
does not misfile a slice.

## Context

Foreman (`/home/cole/Projects/Foreman`, `github.com/cmichaelsd/Foreman`) closes
the loop from a Linear **issue** to a pull request. It stops at a human twice:
once before tickets are filed (`push_interactive`), once at PR-open
(`run_once`). Its whole design is that nothing crosses a boundary unseen.

What is missing is the layer above. Today a human has to arrive already knowing
what the issues are. Red is that layer: you talk to it the way you would talk to
a product manager, it drafts a Linear **Project**, you review the Project in
Linear, and later Red walks that Project's scope and drives Foreman's push once
per slice to produce the issues.

The point is not automation, it is **transparency**. Red is a pipe. When
Foreman's push agent asks a question, that question reaches you. Red may propose
an answer drawn from the Project brief, but nothing is sent on your behalf
without you seeing it and pressing a key. This is deliberate: real tickets are
never self-explanatory, and a Red that silently answered would be the one
component in the chain with no gate, which is exactly the asymmetry
`push_interactive` was written to close (`push.py:353-357`).

Result: two projects, one philosophy. Red bubbles Foreman's human reviews up to
you instead of absorbing them.

```
you <-> red push          -> Linear Project (Backlog)
        [you review + edit the Project in Linear, move it to Planned]
you <-> red pull -> foreman.push_interactive -> Linear Issues (in the Project)
        [later, unchanged: foreman pull -> PR]
```

Decisions taken up front, so they are not re-litigated later:

- Red is a terminal CLI, mirroring Foreman's shape.
- Red relays every Foreman question to the human, with a drafted answer the
  human must accept or override. Never an unattended answer.
- Red imports Foreman as a library at a pinned SHA, and Foreman gains a small
  additive typed review port so the relay is not built on prompt-string matching.
- v1 covers both halves: chat to Project, and Project to Issues.

---

## Part 1: a small additive change to Foreman

Red cannot use the `foreman` CLI, which binds `input`/`print` at
`cli.py:226-233`. It must import `push_interactive`. That already accepts
injected `ask`/`show` callables, but the two things a human is asked are
distinguished only by their prompt literals: `"> "` (`push.py:373`) versus
`"[c]reate N ticket(s), [e]dit, [q]uit, or type feedback to redraft: "`
(`push.py:392`). Red would have to string-match those. Since both repos have the
same owner, make the boundary honest instead.

### New file: `Foreman/src/foreman/review.py`

```python
"""The human gate in the push phase, as a port.

push_interactive has always stopped at a human. Until this existed, the shape of
that stop was two callables and two prompt strings, which meant an embedder could
not tell "the agent is asking you something" apart from "these drafts are ready"
without matching literals. Both are now typed.
"""

@dataclass
class Question:
    """The push agent wants something from the human before it can draft."""
    text: str
    round: int          # 1-based, capped by MAX_QUESTION_ROUNDS

@dataclass
class Approval:
    """Drafts are ready. Nothing has been created."""
    tickets: list[Ticket]
    rendered: str       # render_drafts(tickets), so an embedder need not re-render

@dataclass
class Decision:
    action: str         # "create" | "edit" | "quit" | "feedback"
    feedback: str = ""  # only meaningful when action == "feedback"

@runtime_checkable
class Reviewer(Protocol):
    def show(self, text: str) -> None: ...
    def answer(self, question: Question) -> str: ...
    def decide(self, approval: Approval) -> Decision: ...

class TerminalReviewer:
    """What the CLI has always done, now behind the port. The free-text parsing
    of the approval answer lives here because that is where free text is."""
    def __init__(self, ask: Callable[[str], str] = input,
                 show: Callable[[str], None] = print) -> None: ...
```

### Change: `Foreman/src/foreman/push.py`

- `push_interactive` gains `reviewer: Reviewer | None = None`.
- When `reviewer is None`, build `TerminalReviewer(ask=ask, show=show)` from the
  existing parameters. **Every current caller and every existing test keeps
  working untouched.**
- `respond` calls `reviewer.answer(Question(text=agent_text, round=rounds))`
  instead of `show(...)` then `ask("> ")`.
- The approval loop (`push.py:389-429`) calls
  `reviewer.decide(Approval(tickets, render_drafts(tickets)))` and branches on
  `Decision.action` - a closed set - instead of lowercasing free text inline.
  The `[c]/[e]/[q]/feedback` parsing moves verbatim into
  `TerminalReviewer.decide`, so terminal behaviour is byte-identical.

### Change: `Foreman/src/foreman/cli.py`

`cmd_push` passes `reviewer=TerminalReviewer(ask=input, show=print)`. Keep
`edit=_edit_in_editor` as a separate parameter; it is already `str -> str` and
needs no port.

### Pinning test: `Foreman/tests/test_review_port.py`

- legacy `ask`/`show` path produces the same transcript as before (back-compat),
- a `Reviewer` that returns `Decision("quit")` raises `Aborted` and creates
  nothing,
- `Decision("feedback", "...")` redrafts without creating.

Nothing else in Foreman changes. `orchestrator.py`, `spawn.py`, `decompose.py`
and their LOCKED docstrings are untouched.

---

## Part 2: Red

### Shape

Python 3.11+, src-layout, hatchling, `red = red.cli:main`. Matches Foreman's
conventions exactly, because half of Red is Foreman's own code:
`from __future__ import annotations` everywhere, PEP-604 unions, `@dataclass`
for data and `Protocol` for every boundary, keyword-only args on anything that
might grow, injected-dependency-with-default as the universal seam, prose
docstrings that explain *why*, **no em dashes in any generated file or output**
(`Foreman/docs/build-spec.md:82-83`).

```toml
[project]
name = "red"
requires-python = ">=3.11"
dependencies = [
  "claude-agent-sdk>=0.1.0",
  "foreman @ git+https://github.com/cmichaelsd/Foreman@<pin-a-sha>",
]
```

Pin a SHA. Foreman exports nothing from `__init__.py` and makes no API
stability promise.

**Red has no configuration of its own, on purpose.** It calls
`foreman.config.load_settings()` and `Settings.require_api_key()`. `foreman init`
and `~/.config/foreman/.env` serve both tools. Credentials belong to you, not to
a tool.

### Modules

| module | job |
|---|---|
| `red/models.py` | `Project`, `ScopeItem`, `ProjectState` dataclasses |
| `red/brief.py` | conversation contract, project JSON shape, render/parse project markdown |
| `red/scope.py` | render and parse the scope section of a Project's `content` |
| `red/linear_projects.py` | Project GraphQL, `ProjectScopedLinear`, `StubProjectClient` |
| `red/relay.py` | `RelayReviewer` - the pipe |
| `red/state.py` | `.red/<slugId>/state.json` |
| `red/pipeline.py` | the pull loop, I/O-free, ports on a `Deps` dataclass |
| `red/cli.py` | argparse, all terminal I/O, `$EDITOR`, exit codes |

Reuse rather than rewrite:
`foreman.spawn.run_agent` / `run_conversation` / `extract_last_json`,
`foreman.topo.topo_sort`, `foreman.config.load_settings`,
`foreman.git_ops.repo_root` / `ensure_ignored`, `foreman.models.Ticket`,
`foreman.linear_graphql.GraphQLLinearClient` (its `.query()` is the transport),
and `foreman.cli._edit_in_editor`'s shape for `$EDITOR` round-trips.

### `red push [prose]`

Mirrors `foreman push` turn for turn.

1. Prose from argv, else stdin, else an `input("> ")` prompt
   (`Foreman/src/foreman/cli.py:214-218`).
2. `run_conversation(system_prompt=RED_CONVERSATION_CONTRACT, ...)` with a
   `respond` callback shaped exactly like `push.py:364-373`: prose means the
   agent still has questions, a final JSON object means it is ready.
   `MAX_QUESTION_ROUNDS = 3`. Tools `["Read", "Grep", "Glob"]` - Red never
   writes code.
3. The contract asks for the six things a Project needs before it can be sliced:
   the outcome and who it is for; project-level success criteria; the
   independently shippable slices; explicit non-goals; ordering between slices;
   constraints (repo, stack, dates). Same house rule as Foreman: prefer reading
   the repository over asking, ask only for real gaps, ask all at once.
4. Final JSON `{"project": {name, summary, outcome, success_criteria[],
   constraints, out_of_scope, scope_items: [{title, depends_on[], prose}]}}`.
5. Approval loop identical in shape to `push.py:389-429`: show the rendered
   markdown, then `[c]reate, [e]dit, [q]uit, or feedback`. `[e]dit` opens
   `$EDITOR` and round-trips through the same render/parse pair.
6. `projectCreate` with `name`, `description` (the one-line summary),
   `content` (the markdown below), `teamIds: [ENG]`, `leadId: viewer`,
   `statusId: Backlog`.
7. Print the URL and say plainly: review it in Linear, edit it there, move it to
   **Planned** when you want `red pull` to see it.

### The Project `content` format

This is the whole durable artifact. It lives in Linear, not on disk, because the
review gate happens *in Linear*: you edit it there and `red pull` re-reads it
fresh, so your edits always win.

```markdown
## Outcome
...

## Success criteria
- [ ] observable, checkable

## Constraints
...

## Out of scope
...

## Scope

<!-- red:scope -->
### 1. Title of the first slice
depends_on: []

Prose handed to Foreman verbatim.

<!-- red:scope -->
### 2. Title of the second slice
depends_on: [1]

...
```

`red/scope.py` splits on `<!-- red:scope -->`, takes `### N. title`, an optional
`depends_on:` line, and the rest as prose. Everything above the first marker is
the **brief**, which becomes the relay's context. The parser is deliberately
forgiving in the same way `parse_ticket_markdown` is (`push.py:229-266`):
someone editing their own project in Linear should not have to get the numbering
right for their work to survive.

### `red pull [PROJECT]`

1. Select: `--project <name|slugId>`, else the Planned projects sorted by
   `(priority, updatedAt)`, taking the first. Mirrors
   `orchestrator.select_ticket`'s posture - an explicit argument bypasses all
   readiness checks.
2. `ensure_ignored(repo)` for `.red/`, reusing `foreman.git_ops.ensure_ignored`
   (it writes `.git/info/exclude`, not `.gitignore`).
3. Load or init `.red/<slugId>/state.json`: the project id and name, and one
   record per scope item (`pending | created | skipped`, plus the issue
   identifiers it produced). Re-running is resumable and never re-files an item
   already marked `created`.
4. `topo_sort` the items on `depends_on` (`foreman.topo`).
5. For each pending item, in order:
   - prose = the brief + this item's prose. One `push_interactive` call.
   - `linear=ProjectScopedLinear(...)`, `reviewer=RelayReviewer(...)`,
     `edit=_edit_in_editor`, `cwd=repo`.
   - `Aborted` means you said no: mark the item `skipped`, ask whether to
     continue with the remaining items or stop. Nothing is retried behind your
     back.
   - record identifiers, save state after every item.
6. Wire cross-item ordering with `GraphQLLinearClient.relate_blocks(blocker,
   blocked)` (`linear_graphql.py`, already public). Foreman's own `blocked_by`
   uses 1-based indices **within a single push call**, so it cannot express
   ordering across slices. Red owns that and does it after each item completes.
7. Post one summary comment on the project via `commentCreate(projectId=...)`
   listing what was created, then **stop**.

**LOCKED: Red never changes a project's status.** Backlog to Planned is your
review; Planned onward is your call. Red comments and stops. This is the same
rule as `orchestrator.py:3-5` - nothing is ever advanced past a gate by code.

### `red/relay.py` - the pipe

The heart of the thing.

```python
class RelayReviewer:
    """Foreman asks; you answer. Red only ever hands you a first draft.

    Every question Foreman raises reaches the terminal verbatim. Red proposes an
    answer from the project brief because you should not have to re-read the
    brief in another window, but the proposal is labelled, and nothing is sent
    until you accept it. Red drafts answers to questions. Red never drafts the
    approval: proposing "yes, create them" would be Red approving its own work.
    """
```

`answer(question)`:
1. `show` a header naming where you are: `-- foreman asks (project "X", scope 3/7) --`
2. `show(question.text)` **verbatim, unedited**
3. one `run_agent` call (`allowed_tools=["Read","Grep","Glob"]`, low
   `max_turns`) with a contract that says: answer only from this brief and this
   repository; if the brief does not cover it, say so rather than inventing
4. `show` the proposal prefixed `red > `, or `red > (nothing in the brief
   covers this)`
5. `ask("[enter] to send, or type your own: ")` - empty accepts, anything else
   replaces

`decide(approval)`: `show(approval.rendered)` verbatim, then the same
`[c]reate/[e]dit/[q]uit/feedback` prompt Foreman's own terminal uses, mapped to
a `Decision`. No proposal.

`--verbatim` on `red pull` skips step 3 entirely and degrades to a byte-for-byte
pipe. Each drafted answer is an extra agent call; the flag is there for when you
would rather just answer.

### `red/linear_projects.py`

Verified against the live workspace on 2026-08-01: team `ENG` is the only team,
project statuses are `Backlog / Planned / In Progress / Completed / Canceled`,
`ProjectCreateInput` accepts `name`, `description`, `content`, `teamIds`,
`leadId`, `statusId`, and `commentCreate` accepts `projectId`.

Follow `linear_graphql.py`'s house style: named `_RED_*` query constants, raw
GraphQL through the wrapped client's `.query()`, one module-local
`RuntimeError` subclass.

```python
class ProjectScopedLinear:
    """A LinearClient that files everything Foreman creates into one project."""
    def create(self, ticket: Ticket) -> Ticket:
        ticket.project = self.project.name          # so drafts render it
        made = self.inner.create(ticket)
        self._attach(made)                          # issueUpdate(id, {projectId})
        return made
```

The explicit `issueUpdate` matters. Foreman attaches by **name**, taking the
first case-insensitive match out of `projects(first: 100)`
(`linear_graphql.py:485-489`). Two similarly named projects and issues land in
the wrong one silently. Red holds the real project id, so it uses it.

`StubProjectClient`, JSON-backed at `.red/linear-stub.json`, mirrors
`StubLinearClient` (`linear_client.py:54`) so the whole loop runs offline under
`--linear stub`.

### Out of scope for v1

Red does not drive `foreman.orchestrator.run_once`. Foreman's pull phase has no
human seam by design (`orchestrator.py:3-5`); its blockers surface
asynchronously through `state.json`, `manifest.md` and Linear comments, and
`foreman resume` is the human answering. You run `foreman pull` yourself. When
that changes, the seam is the `spawn` port on `Deps` (`orchestrator.py:87-97`),
which is a plain dataclass with no defaults - Red can wrap it without Foreman
changing again.

---

## Files

**Foreman** - `src/foreman/review.py` (new), `src/foreman/push.py` (edit),
`src/foreman/cli.py` (edit), `tests/test_review_port.py` (new).

**Red** - `pyproject.toml`, `README.md`, `.gitignore`, `src/red/{__init__,
models,brief,scope,linear_projects,relay,state,pipeline,cli}.py`, `tests/`.

## Tests

Mirror Foreman exactly: pytest, `pythonpath = ["src"]`, no `conftest.py`, no
`unittest.mock`, hand-written fakes only, frozen clock.

- `FakeTransport` replaying canned project GraphQL, copied in shape from
  `Foreman/tests/test_linear_graphql.py:24`.
- `ScriptedConversation` for the Red brief conversation, from
  `Foreman/tests/test_push_interactive.py:44`.
- Round-trip: `render_project(p)` then `parse_project(text)` is `p`, including a
  hand-mangled edit with wrong scope numbering.
- **The relay's pinning test**: a `RelayReviewer` wired to a scripted human that
  always types its own answer must never send the drafted one. Empty input sends
  the draft. `--verbatim` never invokes the drafter at all.
- `ProjectScopedLinear.create` always issues the `issueUpdate` attach, even when
  the name already matched.
- Pinning: nothing in Red ever calls `projectUpdate` with a `statusId`.
- Resumability: a state file with item 2 `created` re-runs and calls
  `push_interactive` only for the others.

## Verification

1. `cd Foreman && .venv/bin/pytest` - green before and after the review port.
   That suite is the back-compat proof.
2. `cd Red && pytest`.
3. Offline end to end: `red push --linear stub "..."` then `red pull --linear
   stub`, and read `.red/linear-stub.json` to see exactly what would have been
   created.
4. Real, against `ENG`: `red push` on a small real want. Confirm the Project
   lands in **Backlog** with readable content, edit the scope in Linear, move it
   to **Planned**.
5. `red pull` in a real repo. Confirm as it runs: every Foreman question appears
   verbatim, each proposal is labelled `red > `, typing over one sends yours,
   and `[q]uit` at an approval creates nothing.
6. In Linear: issues exist, all attached to the Project, cross-slice blocking
   relations present, one summary comment on the Project, project status
   **still Planned**.
7. `foreman pull` on one of those issues, unchanged, all the way to a PR.
