"""Production-oriented tests for SuperAgent orchestration."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from hermes.orchestrator import HermesOrchestrator
from infrastructure.container_manager import ContainerManager
from infrastructure.shared_storage import SharedStorage
from infrastructure.task_db import TaskDatabase
from superagent.config import AgentConfig
from superagent.desktop_api import DesktopAPI
from superagent.memory import SQLiteMemory
from superagent.queue import PriorityTaskQueue
from superagent.scheduler import TaskScheduler
from superagent.stream import StreamManager
from superagent.verification import HumanVerificationHandler
from worker.auth import AuthWorker
from worker.browser import BrowserWorker


class ProductionTests(unittest.IsolatedAsyncioTestCase):
    """Production-oriented smoke tests."""

    async def test_task_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = TaskDatabase(Path(tmp) / "superagent.db")
            task_id = await db.create_task("echo hello", "do one thing", 1)
            await db.assign_task(task_id, "1")
            await db.complete_task(task_id, "done")
            self.assertTrue((await db.get_workforce_status())["tasks"]["completed"] >= 1)

    async def test_shared_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shared = SharedStorage(Path(tmp))
            shared.write_result("1", "task-1", {"output": "ok"})
            self.assertEqual(shared.read_result("task-1")["output"], "ok")

    async def test_priority_queue(self) -> None:
        q = PriorityTaskQueue()
        await q.enqueue("a", {"x": 1}, priority=10)
        await q.enqueue("b", {"x": 1}, priority=1)
        self.assertEqual((await q.dequeue()).task_id, "b")

    async def test_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = SQLiteMemory(Path(tmp) / "memory.db")
            memory.store("agent1", "login", "github credentials saved", "login task")
            self.assertTrue(memory.recall("agent1", "github"))

    async def test_scheduler(self) -> None:
        scheduler = TaskScheduler()
        ran = asyncio.Event()

        async def job() -> None:
            ran.set()

        scheduler.schedule_interval(0.01, job)
        await asyncio.wait_for(ran.wait(), timeout=2)
        scheduler.shutdown()

    async def test_hermes_decompose_aggregate(self) -> None:
        hermes = HermesOrchestrator(model="local", api_key="", base_url="http://localhost:11434/v1", max_agents=3)
        subtasks = await hermes.decompose("build a report", 3)
        self.assertEqual(len(subtasks), 3)
        summary = await hermes.aggregate("build a report", ["a", "b", "c"])
        self.assertTrue(summary)

    async def test_container_manager_interface(self) -> None:
        manager = ContainerManager(max_agents=2)
        self.assertEqual(manager.get_ports(1)["desktop"], "http://127.0.0.1:8001")

    async def test_desktop_api_model(self) -> None:
        api = DesktopAPI(host="localhost", port=8000)
        self.assertEqual(api.base_url, "http://localhost:8000")

    async def test_verification_totp(self) -> None:
        handler = HumanVerificationHandler(totp_secrets=["JBSWY3DPEHPK3PXP"])
        code = handler.handle_totp("test", {"test": "JBSWY3DPEHPK3PXP"})
        self.assertEqual(len(code), 6)

    async def test_browser_and_auth_classes(self) -> None:
        browser = BrowserWorker()
        auth = AuthWorker(browser=browser)
        self.assertIsNotNone(auth)

    @unittest.skipUnless(os.getenv("RUN_LIVE_TESTS") == "1", "Live container not requested")
    async def test_live_desktop(self) -> None:
        api = DesktopAPI(host="localhost", port=8000)
        png = await api.screenshot()
        self.assertGreater(len(png), 1000)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        size = await api.get_screen_size()
        await api.click(size["width"] // 2, size["height"] // 2)
        await api.type_text("hello world")
        result = await api.run_command("echo hello")
        self.assertEqual(result["stdout"].strip(), "hello")
        stream = StreamManager()
        url = await stream.auto_detect()
        self.assertIsNotNone(url)


if __name__ == "__main__":
    unittest.main()
