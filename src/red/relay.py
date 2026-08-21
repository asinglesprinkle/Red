"""The pipe.

Forman stops at a human twice in its push phase: once when its agent has a
question, once when the drafts are ready. Red is standing where that human
stands. It does not get to answer.

What Red does instead is spare you the lookup. It reads the project brief you
already approved and proposes a first answer, labelled as its own, and then
waits. Empty line accepts it. Anything else replaces it. Nothing reaches Forman
that you did not see on the way past.

The asymmetry is deliberate and load bearing:

  questions - Red proposes. The brief usually does contain the answer, and
              retyping it out of another window is not review, it is friction.
  approval  - Red proposes nothing. A tool that drafted "yes, create them" would
              be approving its own work, and the gate would be theatre.

`--verbatim` turns the proposing off entirely and leaves a byte-for-byte pipe.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

from forman.review import Approval, Decision, Question, parse_decision
from forman.spawn import (
    DEFAULT_MODEL,
    READ_ONLY_TOOLS,
    names_turn_limit,
    run_agent,
)

from .models import Project, ScopeItem

# Short on purpose. This is a lookup in a document you already read, not a piece
# of research, and every round of it sits between you and your next keystroke.
DRAFT_MAX_TURNS = 8

NOTHING_FOUND = "(nothing in the brief covers this)"

ANSWER_CONTRACT = """\
You are drafting a reply on behalf of a person who is being asked a question \
about a project they own. They will read your draft before it is sent, and they \
will send their own words instead if yours are wrong.

Answer ONLY from the project brief below and from the repository you are in. You \
may read files to ground the answer in real paths and names. Do not write or \
edit any code.

If the brief and the repository do not settle the question, say exactly this and \
nothing else:

{nothing_found}

Do not guess, do not hedge, and do not offer options. Guessing here is worse \
than silence: the person can answer a question they are told is open, and cannot \
correct a plausible answer they skim past. Reply with the answer itself, in one \
short paragraph, addressed to whoever asked. No preamble.\
"""


def _off_loop(runner: Callable[..., object], /, **kwargs: object) -> object:
    """Call a runner that wants an event loop of its own, from a thread without one.

    Forman's `run_agent` ends in `asyncio.run`. Red calls it from inside the
    `respond` callback of Forman's push conversation, which is itself being
    driven by `asyncio.run`, and nesting those raises rather than waiting. So
    when this thread already has a loop, the call goes to a thread that has
    none. Nothing is lost by blocking: the person is sitting at a prompt waiting
    for this one answer, and the conversation cannot proceed without it.

    The worker is a daemon and is joined rather than shut down through a pool,
    so a ctrl-c while drafting leaves immediately instead of waiting on a
    session nobody wants any more.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return runner(**kwargs)

    box: dict[str, object] = {}

    def _target() -> None:
        try:
            box["value"] = runner(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True, name="red-drafter")
    thread.start()
    thread.join()

    error = box.get("error")
    if error is not None:
        raise error  # type: ignore[misc]
    return box.get("value")


def _prompt(brief: str, item: ScopeItem, question: str) -> str:
    return (
        f"# Project brief\n\n{brief}\n\n"
        f"# The slice being turned into tickets\n\n"
        f"## {item.title}\n\n{item.prose}\n\n"
        f"# The question you are drafting a reply to\n\n{question}"
    )


def draft_answer(
    *,
    brief: str,
    item: ScopeItem,
    question: str,
    cwd: str = ".",
    runner: Callable[..., object] = run_agent,
    model: str | None = DEFAULT_MODEL,
) -> str:
    """One agent call, read-only, returning a proposed reply or NOTHING_FOUND.

    A failure here is not fatal. The proposal is a convenience; the person is
    still sitting there and can answer for themselves, so a broken session
    should cost them a keystroke and not the run.
    """
    try:
        run = _off_loop(
            runner,
            prompt=_prompt(brief, item, question),
            system_prompt=ANSWER_CONTRACT.format(nothing_found=NOTHING_FOUND),
            cwd=cwd,
            allowed_tools=READ_ONLY_TOOLS,
            max_turns=DRAFT_MAX_TURNS,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001 - any failure degrades to no proposal
        return f"{NOTHING_FOUND} (drafting failed: {exc})"

    failure = getattr(run, "error", None)
    if failure and names_turn_limit(failure):
        # The CLI reports a turn limit by exiting non-zero, and what reaches
        # here is the SDK exception that exit raised. A person waiting at a
        # prompt for one drafted sentence should be told the lookup was too
        # short, not handed the wording of a subprocess exit.
        failure = f"the draft ran out of its {DRAFT_MAX_TURNS} turns"
    if failure:
        return f"{NOTHING_FOUND} (drafting failed: {failure})"
    return (getattr(run, "text", "") or "").strip() or NOTHING_FOUND


class RelayReviewer:
    """Forman asks; you answer. Red only ever hands you a first draft."""

    def __init__(
        self,
        *,
        ask: Callable[[str], str],
        show: Callable[[str], None],
        project: Project,
        brief: str,
        item: ScopeItem,
        position: str = "",
        cwd: str = ".",
        verbatim: bool = False,
        drafter: Callable[..., str] = draft_answer,
        model: str | None = DEFAULT_MODEL,
    ) -> None:
        self._ask = ask
        self._show = show
        self.project = project
        self.brief = brief
        self.item = item
        self.position = position or f"scope {item.number}"
        self.cwd = cwd
        self.verbatim = verbatim
        self.drafter = drafter
        self.model = model
        # What actually went to Forman, in order. The CLI prints nothing from
        # this; it exists so a test can prove the human's word was the one sent.
        self.sent: list[str] = []
        self.proposed: list[str] = []

    # -- Reviewer ------------------------------------------------------------

    def show(self, text: str) -> None:
        self._show(text)

    def answer(self, question: Question) -> str:
        self._show("")
        self._show(f"-- forman asks ({self.project.name}, {self.position}) --")
        self._show("")
        self._show(question.text)  # verbatim, always

        if self.verbatim:
            reply = self._ask("\nyou > ").strip()
            self.proposed.append("")
            self.sent.append(reply)
            return reply

        self._show("")
        self._show("red is drafting a reply from the project brief...")
        proposal = self.drafter(
            brief=self.brief,
            item=self.item,
            question=question.text,
            cwd=self.cwd,
            model=self.model,
        )
        self.proposed.append(proposal)

        self._show("")
        for line in proposal.splitlines() or [""]:
            self._show(f"red > {line}")
        typed = self._ask("\n[enter] to send that, or type your own: ").strip()

        reply = typed or proposal
        self.sent.append(reply)
        return reply

    def decide(self, approval: Approval) -> Decision:
        """The gate. Red proposes nothing here, on purpose."""
        self._show("")
        # Naming the project here is not decoration. Forman renders its own
        # drafts with `project: null`, because it does not know yet that Red is
        # about to file them somewhere; without this line the gate would look
        # like it was about to create loose issues.
        self._show(
            f"-- forman drafted {len(approval.tickets)} issue(s) "
            f"for {self.project.name} --"
        )
        self._show("")
        self._show(approval.rendered)
        answer = self._ask(
            f"[c]reate {len(approval.tickets)} issue(s), [e]dit, [s]kip this slice, "
            "or type feedback to redraft: "
        ).strip()
        if answer.strip().lower() in ("s", "skip"):
            return Decision("quit")
        return parse_decision(answer)
