![Red](assets/red.jpg)

# Red

Talk it through with a product manager, get a Linear project you approved, then
let Forman turn it into tickets.

---

Red is a local, single-user tool that sits one level above
[Forman](https://github.com/cmichaelsd/Forman). Forman closes the loop between
a ticket and a pull request; Red closes the one between an idea and the tickets.
You describe what you want in plain language; it drafts a Linear project. You
review that project in Linear and move it on. Later Red walks it a slice at a
time and hands each slice to Forman to become tickets.

Red never answers on your behalf. When Forman's agent has a question, that
question reaches your terminal exactly as it was asked. Every run ends at a
human, twice over.

## How it works

**Push.** `red push` talks it through with you first. It checks your description
against the six things a project needs before it can be sliced, asks only for the
gaps, then shows you the draft. Nothing is filed until you say so. The project
lands in **Backlog**.

**Review.** You read it in Linear and edit it there. The scope section is the
part that matters: each slice becomes one handover to Forman. Move the project
to **Planned** when you want Red to start on it.

**Pull.** `red pull` reads the project back out of Linear, so whatever you edited
wins. It walks the slices blockers-first and runs Forman's push on each one.
Every question and every set of drafts comes to you. At the end it comments on
the project saying what it filed, and stops.

Linear only ever sees projects and the issues Forman creates. Red never moves a
project: Backlog to Planned is you saying you have read it, and anything after
that is yours too.

```
you <-> red push          -> Linear project (Backlog)
        [you review and edit it in Linear, move it to Planned]
you <-> red pull -> forman push -> Linear issues, in that project
        [then, unchanged: forman pull -> a pull request]
```

## Install

Needs Python 3.11+, git, and the [Claude Code CLI](https://claude.com/claude-code)
on your PATH. Forman is pulled in as a dependency; you do not need to install it
separately.

```sh
git clone https://github.com/cmichaelsd/Red && cd Red
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Red has no configuration of its own, on purpose. It reads Forman's, so if
`forman` already works here, so does `red`. If it does not:

```sh
forman init
```

That asks for a personal API key, checks it against the API before saving
anything, and writes `~/.config/forman/.env` with mode `0600`. One key serves
both tools, because the key belongs to *you* and not to a tool.

## Quick start

Run it from **inside the repo the project is about**. Red reads that repo to
ground what it drafts, the same way Forman does, and never writes to it beyond
`.red/`.

```sh
red push                          # talk through a project, then file it
red push "add rate limiting"      # or start from a line of prose
red push --dry-run "..."          # print the draft, create nothing

red pull                          # the one project in Planned
red pull "Rate limiting"          # or a specific one, by name or slug id
red pull --verbatim               # no proposed answers, just the pipe

red status                        # what has been pulled in this repo
```

Add `--linear stub` to any of them to run the whole thing offline against
`.red/linear-stub.json`, with no account and no network.

### Defining a project

`red push` is a short conversation, not a one-shot command:

```
$ red push
What do you want to achieve? (a paragraph is fine)
> the api falls over when one client hammers it

Two things I want to pin down:
- Should the limit be per API key, per IP, or both?
- Is the existing middleware in src/api the right place, or is this a
  new layer in front of it?

> per key, and yes, extend the existing middleware

---
name: Rate limiting
summary: Stop one client from taking the API down for everyone.
---

## Outcome
...

## Scope

<!-- red:scope -->
### 1. Add a token bucket
...

[c]reate the project, [e]dit, [q]uit, or type feedback:
```

The questions come from what the next stage needs in order to run: the outcome,
checkable success criteria, the independently shippable slices, what is out of
scope, the ordering between slices, and the constraints. The agent reads your
repo to answer what it can and asks only about the rest.

Each slice is handed to a fresh Forman session that has never seen this
conversation, and the tickets it writes are executed by agents that have never
seen it either. Nobody downstream can come back and ask. That is why this step is
a conversation.

`e` opens the draft in `$EDITOR`; anything else you type is feedback and
redrafts.

### Answering Forman

During a pull, every question Forman raises is printed exactly as it was asked.
Red proposes a reply drawn from the project brief, labelled as its own, and
waits:

```
=== slice 1 of 3: Add a token bucket (1/3) ===

-- forman asks (Rate limiting, slice 1 of 3) --

Which endpoints should the limit apply to, and what is the limit?

red is drafting a reply from the project brief...

red > Everything under src/api, 100 requests a minute, per API key.

[enter] to send that, or type your own:
```

Press enter and Red's sentence goes to Forman. Type anything and yours does
instead. `--verbatim` removes the proposal entirely and leaves a byte-for-byte
pipe.

At the gate, Red proposes nothing:

```
-- forman drafted 2 issue(s) for Rate limiting --

[c]reate 2 issue(s), [e]dit, [s]kip this slice, or type feedback to redraft:
```

`s` skips the slice and files nothing for it. Red then asks whether to carry on
with the rest.

## What a run actually does

1. Picks the project in **Planned**, or the one you named. More than one
   candidate and it lists them rather than guessing, because a project is a big
   thing to start by accident.
2. Re-reads the project's content from Linear, so an edit you made there wins
   over anything Red remembers.
3. Reconciles that against `.red/<slug>/state.json`, matching slices on their
   title rather than their position.
4. Orders the slices blockers-first and hands each pending one to Forman's push,
   with the project brief in front of it so a fresh session can stand on its own.
5. Records what was filed after **every** slice, so an interrupted run never
   loses one that really happened.
6. Records cross-slice ordering as blocking relations in Linear.
7. Comments on the project saying what it filed, and stops.

Run it again and it skips whatever already finished. Skipping a slice is a real
answer and is remembered as one; re-filing it means editing the project in Linear
or asking for it by hand.

## Configuration

None of its own. Red calls Forman's settings loader, so the same environment
variables, the same `.env` in the target repo, and the same
`~/.config/forman/.env` apply, with the same precedence. See
[Forman's configuration table](https://github.com/cmichaelsd/Forman#configuration).

Red keeps its bookkeeping in `.red/` inside the target repo and adds that to
`.git/info/exclude`, so it cannot be committed by accident. This is tooling's
business, not a change to your tracked files.

| what | where | why |
|---|---|---|
| the project | Linear, in its `content` field | so you can edit it, and your edit wins |
| what has been filed | `.red/<slug>/state.json` | so re-running never files a slice twice |
| a readable summary | `.red/<slug>/manifest.md` | rendered on every write, never parsed back |
| your API key | `~/.config/forman/.env` | it is yours, and it is Forman's already |

## Design decisions

- **Red is a pipe, not a proxy.** It could answer most of Forman's questions
  correctly most of the time. That is exactly the problem: you cannot correct a
  plausible answer you never saw. So every question surfaces, and the only thing
  Red adds is a first draft you can accept with one keystroke.
- **Red drafts answers, never approvals.** A tool that proposed "yes, create
  them" would be approving its own work, and the gate would be theatre.
- **The project lives in Linear.** Keeping it on disk would be faster and would
  quietly diverge from the one you edited. Linear is the source of truth for what
  the work is; `.red/` is the source of truth only for what has already been done.
- **Slices reconcile on title, not position.** Inserting a slice in Linear
  renumbers everything below it. Matching on position would file the wrong slice
  twice. A retitled slice therefore looks new, and Red says so rather than
  assuming.
- **Issues are attached to their project by id.** Forman attaches by name,
  taking the first case-insensitive match out of the first hundred projects. Red
  holds the real id, so it uses it.
- **Nothing here can move a project's status.** There is no mutation in the
  codebase that can, so there is no call to forget to leave out.
- **The pull loop performs no I/O.** Every side effect sits behind a port on a
  `Deps` dataclass, which is what lets the whole thing be tested without a
  network, a repo, a terminal, or a model.

## Status and limits

Alpha. The whole loop runs end to end offline; the parts that touch real Linear
have been exercised against a real workspace, but it has not been through much
mileage.

- Built for one person on one machine, one repository at a time. No locking, no
  concurrency story, by design.
- Considers at most **50 projects** when selecting or searching by name. More
  than that needs real pagination.
- Two projects with the same name are refused rather than resolved. Pass the slug
  id, which is the last part of the project URL.
- Every proposed answer costs one extra agent call. `--verbatim` is there for
  when you would rather just answer.
- It does not drive `forman pull`. Forman's execution phase has no human seam
  by design: blockers surface asynchronously in `state.json`, `manifest.md` and
  Linear comments, and `forman resume` is you saying you have dealt with one. Run
  `forman pull` yourself.
- Forman is pinned to a commit in `pyproject.toml`. It exports nothing from
  `__init__` and promises no API stability, so an unpinned dependency would be a
  dependency on whatever happened to be pushed. Bumping it is a deliberate act.

## Development

```sh
pip install -e ".[dev]"
pytest
```

The suite runs both halves against a stubbed Linear and a stubbed model,
asserting the round trip through a hand-edited project, the relay's promise that
your words are the ones sent, and that no run ever moves a project. No network,
no account, no model calls: an autouse fixture in `tests/conftest.py` makes any
attempt to start a real agent session fail loudly rather than hang. Fakes are
hand-written; there is no mocking library.

`docs/build-spec.md` is the original specification, kept for provenance.

## License

[MIT](LICENSE)
