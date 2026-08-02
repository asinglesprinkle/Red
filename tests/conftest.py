"""One guard, and it earned its place.

Every call site in Red takes its model runner as a parameter with a live default
(`runner=run_agent`, `conversation=run_conversation`), which is the seam that
makes all of this testable. The cost of that convenience is that forgetting to
pass the parameter does not fail: it quietly starts a real agent session, bills
a real account, and hangs the suite until something times out. That happened
once while this suite was being written.

So the suite refuses to reach a model at all. A test that wants an agent passes
a fake; a test that forgot says so immediately instead of hanging.

The block is on the SDK import rather than on `run_agent` and `run_conversation`
themselves, because those two are captured as default argument values when their
callers are defined. Rebinding the module attribute afterwards would not change
what any of those defaults point at, and the guard would quietly do nothing.
Forman imports the SDK lazily inside both functions, so this is the one place
every path really does pass through.
"""

from __future__ import annotations

import sys
import types

import pytest

MESSAGE = (
    "a test tried to start a real agent session. Pass runner= or conversation= "
    "a fake instead."
)


class _Refusing(types.ModuleType):
    def __getattr__(self, name: str):
        raise AssertionError(f"{MESSAGE} (reached claude_agent_sdk.{name})")


@pytest.fixture(autouse=True)
def no_live_agents(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _Refusing("claude_agent_sdk"))
