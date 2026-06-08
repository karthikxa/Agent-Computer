"""End-to-end smoke tests for SuperAgent."""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from superagent.actions import ActionParser, StopAction, TextAction
from superagent.agent import SuperAgent
from superagent.config import AgentConfig
from superagent.cost_tracker import CostTracker
from superagent.loop import AgentLoop
from superagent.memory import AgentMemory, MemoryRecord
from superagent.monitor import WatchdogManager
from superagent.pool import AgentPool
from superagent.providers import LocalProvider
from superagent.queue import PriorityTaskQueue
from superagent.scheduler import TaskScheduler
from superagent.session import SessionManager
from superagent.stream import StreamManager
from superagent.verification import HumanVerificationHandler


class FakeDesktop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def screenshot(self) -> bytes:
        self.calls.append(("screenshot", (), {}))
        return b"same-screenshot"

    async def click(self, *args, **kwargs):
        self.calls.append(("click", args, kwargs))

    async def drag(self, *args, **kwargs):
        self.calls.append(("drag", args, kwargs))

    async def press_keys(self, *args, **kwargs):
        self.calls.append(("press_keys", args, kwargs))

    async def scroll(self, *args, **kwargs):
        self.calls.append(("scroll", args, kwargs))

    async def type_text(self, *args, **kwargs):
        self.calls.append(("type_text", args, kwargs))

    async def run_command(self, *args, **kwargs):
        self.calls.append(("run_command", args, kwargs))
        return "ok"

    async def wait(self, *args, **kwargs):
        self.calls.append(("wait", args, kwargs))


class SuperAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_parser(self):
        action = ActionParser.parse('{"kind":"type","text":"hello","enter":true}')
        self.assertIsInstance(action, TextAction)
        self.assertEqual(action.text, "hello")

    async def test_cost_tracker(self):
        tracker = CostTracker()
        tracker.record("openai", "gpt-4o-mini", input_tokens=1000, output_tokens=500)
        self.assertGreater(tracker.total_cost(), 0)

    async def test_queue_priority(self):
        queue = PriorityTaskQueue()
        await queue.enqueue("low", {"value": 1}, priority=10)
        await queue.enqueue("high", {"value": 2}, priority=1)
        first = await queue.dequeue()
        self.assertEqual(first.task_id, "high")

    async def test_memory_store_and_recall(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = AgentMemory(Path(tmp) / "memory.sqlite3")
            await memory.store(MemoryRecord(memory_id="m1", text="hello world", tags=["greeting"]))
            results = await memory.recall("hello")
            self.assertTrue(results)
            self.assertEqual(results[0].memory_id, "m1")

    async def test_scheduler_interval(self):
        scheduler = TaskScheduler()
        job_id = scheduler.schedule_interval(60, lambda: None)
        self.assertTrue(job_id)
        scheduler.shutdown()

    async def test_verification_totp(self):
        handler = HumanVerificationHandler(totp_secrets=["JBSWY3DPEHPK3PXP"])
        token = handler.generate_totp("JBSWY3DPEHPK3PXP")
        self.assertTrue(handler.verify_totp(token))

    async def test_watchdog_restart(self):
        restarted: list[str] = []

        async def restart(agent_id: str) -> None:
            restarted.append(agent_id)

        watchdog = WatchdogManager(heartbeat_interval_seconds=0.01)
        watchdog.register("agent-a", restart_callback=restart)
        task = asyncio.create_task(watchdog.watch())
        await asyncio.sleep(0.05)
        await watchdog.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self.assertTrue(restarted)

    async def test_loop_stuck_detection(self):
        desktop = FakeDesktop()
        provider = LocalProvider("local")
        executor = __import__("superagent.actions", fromlist=["ActionExecutor"]).ActionExecutor(desktop)
        loop = AgentLoop(provider, executor, desktop, stuck_threshold=2, max_steps=3)
        actions = await loop.run("open terminal")
        self.assertTrue(actions)
        self.assertIsInstance(actions[-1], StopAction)

    async def test_stream_urls(self):
        stream = StreamManager()
        self.assertIn(":6901", stream.get_url())
        self.assertIn("index.m3u8", stream.get_hls_url())

    async def test_session_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SessionManager(tmp)
            session = await manager.create("s1", {"hello": "world"})
            loaded = await manager.load(session.session_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.state["hello"], "world")

    async def test_agent_pool(self):
        pool = AgentPool()
        config = AgentConfig(agent_id="agent-1")
        agent = pool.create_agent(config)

        async def fake_run(objective: str):
            return [objective]

        agent.run = fake_run  # type: ignore[assignment]
        result = await pool.assign_task("agent-1", "do thing")
        self.assertEqual(result, ["do thing"])
        status = pool.get_status("agent-1")
        self.assertIsNotNone(status)
        self.assertFalse(status.busy)

    async def test_superagent_constructs(self):
        agent = SuperAgent(AgentConfig())
        self.assertIsNotNone(agent.runtime)


if __name__ == "__main__":
    unittest.main()
