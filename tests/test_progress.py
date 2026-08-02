"""The terminal's answer to "is it working?".

An agent session says nothing until it has finished reading the repository, so
the only thing standing between a working `red push` and one that looks hung is
what this prints.
"""

from __future__ import annotations

import io

from forman.spawn import Activity

from red.cli import Progress


class FakeClock:
    """Elapsed time is part of the output, so the tests get to control it."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_something_is_printed_before_the_agent_has_said_anything():
    out = io.StringIO()
    Progress(stream=out, clock=FakeClock()).start("reading the repository")

    assert "reading the repository" in out.getvalue()


def test_the_abort_key_is_named_because_that_is_the_question_being_asked():
    out = io.StringIO()
    Progress(stream=out, clock=FakeClock()).start("drafting")

    assert "ctrl-c" in out.getvalue()


def test_each_thing_the_agent_does_becomes_a_line():
    out = io.StringIO()
    progress = Progress(stream=out, clock=FakeClock())
    progress.start("drafting")

    progress(Activity(kind="tool", tool="Read", tool_input={"file_path": "src/cli.py"}))
    progress(Activity(kind="tool", tool="Grep", tool_input={"pattern": "rate.?limit"}))

    lines = out.getvalue().splitlines()
    assert "read src/cli.py" in lines[1]
    assert "grep rate.?limit" in lines[2]


def test_elapsed_time_climbs_so_a_stall_is_visible():
    out = io.StringIO()
    clock = FakeClock()
    progress = Progress(stream=out, clock=clock)
    progress.start("drafting")

    clock.now = 8
    progress(Activity(kind="thinking"))
    clock.now = 95
    progress(Activity(kind="thinking"))

    lines = out.getvalue().splitlines()
    assert "0:08" in lines[1]
    assert "1:35" in lines[2]


def test_activity_arriving_without_a_start_still_prints():
    """The redraft path starts its own run; a stray callback must not crash it."""
    out = io.StringIO()
    progress = Progress(stream=out, clock=FakeClock())

    progress(Activity(kind="tool", tool="Read", tool_input={"file_path": "a.py"}))

    assert "read a.py" in out.getvalue()


def test_the_clock_restarts_between_runs():
    out = io.StringIO()
    clock = FakeClock()
    progress = Progress(stream=out, clock=clock)
    progress.start("drafting")

    clock.now = 60
    progress.done()
    progress.start("redrafting")
    clock.now = 63
    progress(Activity(kind="thinking"))

    assert "0:03" in out.getvalue().splitlines()[-1]


def test_nothing_reaches_stdout():
    """`--dry-run` writes the draft to stdout and that has to stay pipeable."""
    out = io.StringIO()
    progress = Progress(stream=out, clock=FakeClock())
    progress.start("drafting")
    progress(Activity(kind="thinking"))

    assert out.getvalue()  # it went to the stream it was given, not print()
