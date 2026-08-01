# Red

Talk it through with a product manager, get a Linear project you approved, then
let [Foreman](https://github.com/cmichaelsd/Foreman) turn it into tickets.

---

Foreman closes the loop between a ticket and a pull request. Red is the layer
above it. You describe what you want in plain language; it files the project.
Later it picks that project up, walks it a slice at a time, and hands each slice
to Foreman to become tickets.

Red never answers for you. When Foreman's agent has a question, that question
reaches your terminal exactly as it was asked. Red proposes a reply drawn from
the project brief, labelled as its own, and waits. Every run ends at a human,
twice over.

## How it works

**Push.** `red push` talks it through with you first. It checks your description
against the six things a project needs before it can be sliced, asks only for
the gaps, then shows you the draft. Nothing is filed until you say so. The
project lands in **Backlog**.

**Review.** You read it in Linear and edit it there. The scope section is the
part that matters: each slice becomes one handover to Foreman. Move the project
to **Planned** when you want Red to start on it.

**Pull.** `red pull` reads the project back out of Linear, so whatever you
edited wins. It walks the slices blockers-first and runs `foreman push` on each
one. Every question Foreman raises comes to you. Every set of drafts comes to
you. At the end it comments on the project saying what it filed, and stops.

Red never moves a project. Backlog to Planned is you saying you have read it;
anything after that is yours too.

```
you <-> red push          -> Linear project (Backlog)
        [you review and edit it in Linear, move it to Planned]
you <-> red pull -> foreman push -> Linear issues, in that project
        [then, unchanged: foreman pull -> a pull request]
```

## Install

Needs Python 3.11+, git, and the [Claude Code CLI](https://claude.com/claude-code)
on your PATH.

```sh
git clone https://github.com/cmichaelsd/Red && cd Red
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Red has no configuration of its own, on purpose. It reads Foreman's, so if
`foreman` already works here, so does `red`. If it does not:

```sh
foreman init
```

Your API key belongs to you, not to a tool, and one key saved in
`~/.config/foreman/.env` serves both.

## Use

```sh
red push                          # it will ask what you want
red push "add rate limiting"
red push --dry-run "..."          # print the draft, create nothing

red pull                          # the one project in Planned
red pull "Rate limiting"          # by name or slug id
red pull --verbatim               # no proposed answers, just the pipe

red status                        # what has been pulled in this repo
```

Add `--linear stub` to any of them to run the whole thing offline against
`.red/linear-stub.json`, with no token and no network.

## What you see during a pull

```
=== slice 1 of 2: Add a token bucket (1/2) ===

-- foreman asks (Rate limiting, slice 1 of 2) --

Which endpoints should the limit apply to, and what is the limit?

red is drafting a reply from the project brief...

red > Everything under src/api, 100 requests a minute.

[enter] to send that, or type your own:
```

Press enter and Red's sentence goes to Foreman. Type anything and yours does
instead. Red's proposal is never sent unseen, and `--verbatim` removes it
entirely.

At the gate, Red proposes nothing:

```
-- foreman drafted 2 issue(s) for Rate limiting --

[c]reate 2 issue(s), [e]dit, [s]kip this slice, or type feedback to redraft:
```

A tool that drafted "yes, create them" would be approving its own work.

## Where things live

| what | where | why |
|---|---|---|
| the project | Linear, in its `content` field | so you can edit it, and your edit wins |
| what has been filed | `.red/<slug>/state.json` | so re-running never files a slice twice |
| a readable summary | `.red/<slug>/manifest.md` | rendered on every write, never read back |
| your API key | `~/.config/foreman/.env` | it is yours, and it is Foreman's already |

`.red/` is added to `.git/info/exclude`, not to `.gitignore`: this is tooling's
business, not a change to your tracked files.

## Design decisions

**Red is a pipe, not a proxy.** It could answer most of Foreman's questions
correctly most of the time. That is exactly the problem: you cannot correct a
plausible answer you never saw. So every question surfaces, and the only thing
Red adds is a first draft you can accept with one keystroke.

**Slices are re-read from Linear every run.** The project on disk would be
faster and would quietly diverge from the one you edited. Linear is the source
of truth for what the work is; `.red/` is the source of truth only for what has
already been done.

**Scope items are matched on their title, not their position.** Inserting a
slice in Linear renumbers everything below it. Matching on position would file
the wrong slice twice; matching on title means a retitled slice looks new, and
Red says so rather than assuming.

**Issues are attached to the project by id.** Foreman attaches by name, taking
the first case-insensitive match out of the first hundred projects. Red is
holding the real id, so it uses it.

**Red never moves a project.** There is no mutation in this codebase that can,
so there is no call to forget to leave out.

## Status and limits

Single user, local, one repository at a time. It does the two halves above and
nothing else.

It does not drive `foreman pull`. Foreman's execution phase has no human seam by
design: blockers surface asynchronously in `state.json`, `manifest.md` and
Linear comments, and `foreman resume` is you saying you have dealt with one. Run
`foreman pull` yourself. When that changes, the seam is the `spawn` port on
Foreman's `Deps`, and Red can wrap it without Foreman changing again.

Foreman is pinned to a commit in `pyproject.toml`. It exports nothing from
`__init__` and promises no API stability, so an unpinned dependency would be a
dependency on whatever happened to be pushed.

## Development

```sh
pip install -e '.[dev]' && pytest
```

The suite runs with no network, no token, and no model: an autouse fixture in
`tests/conftest.py` makes any attempt to start a real agent session fail loudly
rather than hang. Fakes are hand-written; there is no mocking library.
