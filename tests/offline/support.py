"""No production imports here: guard must be installed first."""
import asyncio
import copy
import json
import selectors
import time
import unittest

GUARD = None
ROOT = None


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self.start_violations = len(GUARD.violations)
        self.addCleanup(self.check_isolation)

    def check_isolation(self):
        self.assertEqual(self.start_violations, len(GUARD.violations),
                         "Isolation violation was caught or swallowed by application code")

    def async_run(self, coroutine):
        return asyncio.run(asyncio.wait_for(coroutine, timeout=5), loop_factory=OfflineLoop)


class TimerSelector(selectors.SelectSelector):
    def select(self, timeout=None):
        if self.get_map():
            raise RuntimeError("File/socket IO is not supported in offline coroutine tests")
        if timeout:
            time.sleep(timeout)
        return []


class OfflineLoop(asyncio.SelectorEventLoop):
    """Single-threaded coroutine tests; no socketpair or thread wakeup support.

    Windows implements socketpair using a loopback listener. Our tests use only
    events, timers and in-memory transports, so no self-pipe is needed.
    """
    def __init__(self):
        super().__init__(selector=TimerSelector())

    def _make_self_pipe(self):
        pass

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        raise RuntimeError("Thread wakeups are not supported in offline tests")


class ScriptedAgent:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.inputs = []

    async def reply(self, message):
        from agentscope.message import Msg
        self.inputs.append(message.content)
        if not self.outputs:
            raise AssertionError("Unexpected extra model/agent call")
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        content = json.dumps(output, ensure_ascii=False) if isinstance(output, dict) else copy.deepcopy(output)
        return Msg(name="offline", content=content, role="assistant")
