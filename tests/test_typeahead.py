"""Keystrokes pressed at a terminal that looks frozen must not count as answers.

An empty line at Red's gate means create. The terminal queues whatever is typed
while an agent works and hands it to the next read, so without this an impatient
enter during a silent minute would approve a draft nobody had seen. This runs
the real thing under a pty, because the behaviour being tested belongs to the
terminal driver and disappears under any fake.
"""

from __future__ import annotations

import os
import pty
import subprocess
import sys
import textwrap
import time

# The child imports Red's real `_ask`, waits the way an agent session would,
# and reports what the prompt actually received.
CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {src!r})
    from red.cli import _ask
    time.sleep(1.0)                       # the agent, working, saying nothing
    got = _ask("[c]reate, [e]dit, [q]uit, or feedback: ")
    sys.stderr.write("GOT[" + got + "]\\n")
    sys.stderr.flush()
    """
)

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _gate_receives(*, impatient: bytes, then: bytes) -> str:
    """Type `impatient` while the agent works, `then` once the prompt is up.

    Returns what the gate read. Both writes happen either way, so the test
    terminates whether or not the guard is doing its job: if type-ahead leaks
    through, the gate answers with it and `then` is simply left unread.
    """
    main, worker = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD.format(src=SRC)],
        stdin=worker,
        stdout=worker,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    os.close(worker)
    try:
        time.sleep(0.2)
        os.write(main, impatient)  # frozen-looking terminal, so: enter, enter
        time.sleep(1.4)            # the prompt appears at ~1.0s
        os.write(main, then)       # and now a deliberate answer
        err = proc.stderr.read().decode() if proc.stderr else ""
        proc.wait(timeout=15)
    finally:
        os.close(main)
    # The prompt is written to the same stream, so GOT[...] can share its line.
    marker = err.find("GOT[")
    if marker == -1:
        raise AssertionError(f"the gate never answered: {err!r}")
    return err[marker + len("GOT[") : err.index("]\n", marker)]


def test_enter_pressed_while_the_agent_works_does_not_reach_the_gate():
    got = _gate_receives(impatient=b"\r\r", then=b"q\r")

    # "" is the dangerous one: at Red's gate an empty line means create.
    assert got != "", "type-ahead was accepted as assent at the gate"
    assert got == "q", f"the gate should have read the deliberate answer, got {got!r}"


def test_an_answer_typed_at_the_prompt_is_still_kept():
    """The guard drops what came too early, not what the person meant."""
    got = _gate_receives(impatient=b"", then=b"looks good, ship it\r")

    assert got == "looks good, ship it"
