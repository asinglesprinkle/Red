"""The pipe.

Foreman stops at a human twice in its push phase: once when its agent has a
question, once when the drafts are ready. Red is standing where that human
stands. It does not get to answer.

What Red does instead is spare you the lookup. It reads the project brief you
already approved and proposes a first answer, labelled as its own, and then
waits. Empty line accepts it. Anything else replaces it. Nothing reaches Foreman
that you did not see on the way past.

The asymmetry is deliberate and load bearing:

  questions - Red proposes. The brief usually does contain the answer, and
              retyping it out of another window is not review, it is friction.
  approval  - Red proposes nothing. A tool that drafted "yes, create them" would
              be approving its own work, and the gate would be theatre.

`--verbatim` turns the proposing off entirely and leaves a byte-for-byte pipe.
"""

from __future__ import annotations

from typing import Callable

from foreman.review import Approval, Decision, Question, parse_decision
from foreman.spawn import DEFAULT_MODEL, run_agent

from .models import ScopeItem, Project

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
        run = runner(
            prompt=_prompt(brief, item, question),
            system_prompt=ANSWER_CONTRACT.format(nothing_found=NOTHING_FOUND),
            cwd=cwd,
            allowed_tools=["Read", "Grep", "Glob"],
            max_turns=DRAFT_MAX_TURNS,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001 - any failure degrades to no proposal
        return f"{NOTHING_FOUND} (drafting failed: {exc})"

    if getattr(run, "error", None):
        return f"{NOTHING_FOUND} (drafting failed: {run.error})"
    return (getattr(run, "text", "") or "").strip() or NOTHING_FOUND


class RelayReviewer:
    """Foreman asks; you answer. Red only ever hands you a first draft."""

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
        # What actually went to Foreman, in order. The CLI prints nothing from
        # this; it exists so a test can prove the human's word was the one sent.
        self.sent: list[str] = []
        self.proposed: list[str] = []

    # -- Reviewer ------------------------------------------------------------

    def show(self, text: str) -> None:
        self._show(text)

    def answer(self, question: Question) -> str:
        self._show("")
        self._show(f"-- foreman asks ({self.project.name}, {self.position}) --")
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
        # Naming the project here is not decoration. Foreman renders its own
        # drafts with `project: null`, because it does not know yet that Red is
        # about to file them somewhere; without this line the gate would look
        # like it was about to create loose issues.
        self._show(
            f"-- foreman drafted {len(approval.tickets)} issue(s) "
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
