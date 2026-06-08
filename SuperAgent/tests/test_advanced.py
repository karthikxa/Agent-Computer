import unittest
import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

from superagent import (
    MCPServer,
    MCPTool,
    LLMKernel,
    KernelRequest,
    SemanticFileSystem,
    SFSFile,
    HITLServer,
    VirtualInputDriver,
    StreamManager,
    StreamConfig,
    AgentMemory,
    MemoryRecord,
    DashboardAPIServer,
)
from infrastructure.microvm import SandboxManager, SandboxSpec, SandboxBackend, SandboxHandle


class TestMCPFeatures(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_server_initialize_and_list(self):
        server = MCPServer(agent=None)
        
        # Test initialize
        init_res = server._handle_initialize({})
        self.assertEqual(init_res["protocolVersion"], MCPServer.PROTOCOL_VERSION)
        
        # Test tools list
        tools_res = server._handle_tools_list()
        self.assertIn("tools", tools_res)
        tool_names = [t["name"] for t in tools_res["tools"]]
        self.assertIn("screenshot", tool_names)
        self.assertIn("click", tool_names)
        self.assertIn("type_text", tool_names)
        self.assertIn("run_command", tool_names)

    async def test_mcp_server_tool_calls(self):
        mock_agent = MagicMock()
        mock_agent.desktop_api = AsyncMock()
        mock_agent.desktop_api.screenshot.return_value = b"fake-png-bytes"
        mock_agent.desktop_api.click.return_value = None
        mock_agent.desktop_api.type_text.return_value = None
        mock_agent.desktop_api.run_command.return_value = "command-output"
        
        server = MCPServer(agent=mock_agent)
        
        # Test screenshot tool
        res = await server._handle_tool_call({"name": "screenshot"})
        self.assertFalse(res["isError"])
        self.assertIn("screenshot_b64", res["content"][0]["text"])

        # Test click tool
        res = await server._handle_tool_call({"name": "click", "arguments": {"x": 100, "y": 200, "button": "left"}})
        self.assertFalse(res["isError"])
        mock_agent.desktop_api.click.assert_called_with(100, 200, button="left")

        # Test type_text tool
        res = await server._handle_tool_call({"name": "type_text", "arguments": {"text": "hello", "enter": True}})
        self.assertFalse(res["isError"])
        mock_agent.desktop_api.type_text.assert_called_with("hello")
        mock_agent.desktop_api.press_keys.assert_called_with(["Return"])

        # Test run_command tool
        res = await server._handle_tool_call({"name": "run_command", "arguments": {"command": "ls"}})
        self.assertFalse(res["isError"])
        self.assertIn("command-output", res["content"][0]["text"])


class TestLLMKernelScheduler(unittest.IsolatedAsyncioTestCase):
    async def test_kernel_submission_and_priority(self):
        # Create kernel with high TPM/RPM so tests aren't delayed
        kernel = LLMKernel(rpm=1000, tpm=1000000, poll_interval=0.01)
        
        # Mock callback
        mock_callback = AsyncMock(return_value="llm-response")
        
        # Start kernel in background
        kernel_task = asyncio.create_task(kernel.run())
        
        try:
            res = await kernel.submit(
                agent_id="agent-1",
                messages=[{"role": "user", "content": "hi"}],
                callback=mock_callback,
                priority=3
            )
            self.assertEqual(res, "llm-response")
            mock_callback.assert_called_once()
            
            stats = kernel.get_stats()
            self.assertEqual(stats["dispatched"]["agent-1"], 1)
        finally:
            await kernel.stop()
            kernel_task.cancel()
            try:
                await kernel_task
            except asyncio.CancelledError:
                pass


class TestSemanticFileSystem(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_sfs.db"
        self.sfs = SemanticFileSystem(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_sfs_lifecycle_and_search(self):
        # Write files
        self.sfs.write("doc1.txt", "The brown fox jumps over the lazy dog.", tags=["animal", "jump"])
        self.sfs.write("doc2.txt", "Quantum computing is a type of computation.", tags=["physics", "quantum"])
        
        # Read file
        f1 = self.sfs.read("doc1.txt")
        self.assertIsNotNone(f1)
        self.assertEqual(f1.name, "doc1.txt")
        self.assertIn("fox", f1.content)
        
        # Search
        results = self.sfs.search("quantum computer")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "doc2.txt")
        
        # Delete
        self.assertTrue(self.sfs.delete("doc1.txt"))
        self.assertIsNone(self.sfs.read("doc1.txt"))


class TestHITLServer(unittest.IsolatedAsyncioTestCase):
    async def test_hitl_server_endpoints(self):
        mock_agent = MagicMock()
        mock_loop = MagicMock()
        mock_loop.state.paused = False
        mock_loop.state.done = False
        mock_loop.state.step_count = 5
        mock_loop.state.objective = "test task"
        mock_loop.state.instructions = []
        mock_agent.loop = mock_loop
        
        mock_desktop = AsyncMock()
        mock_desktop.screenshot.return_value = b"png-bytes"
        mock_agent.desktop_api = mock_desktop
        
        server = HITLServer(agent=mock_agent, port=9999)
        
        # Test handlers directly to avoid opening actual sockets during unit test
        mock_request = MagicMock()
        
        # Status handler
        res = await server._handle_status(mock_request)
        self.assertEqual(res.status, 200)
        self.assertEqual(res.content_type, "application/json")
        body = res.text
        self.assertIn('"paused": false', body)
        self.assertIn('"step_count": 5', body)

        # Pause handler
        res = await server._handle_pause(mock_request)
        self.assertEqual(res.status, 200)
        mock_loop.pause.assert_called_once()

        # Resume handler
        res = await server._handle_resume(mock_request)
        self.assertEqual(res.status, 200)
        mock_loop.resume.assert_called_once()


class TestVirtualInputDriver(unittest.IsolatedAsyncioTestCase):
    @patch("superagent.virtual_input._SYSTEM", "Linux")
    @patch("superagent.virtual_input.shutil.which", return_value="/usr/bin/xdotool")
    async def test_linux_xdotool_click(self, mock_which):
        driver = VirtualInputDriver(display=":5", fallback_to_pyautogui=False)
        self.assertTrue(driver._xdotool_available)
        
        with patch.object(driver, "_run_cmd", new_callable=AsyncMock) as mock_run:
            await driver.click(150, 250, button="left")
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            cmd = args[0]
            self.assertIn("xdotool", cmd)
            self.assertIn("mousemove", cmd)
            self.assertIn("150", cmd)
            self.assertIn("250", cmd)
            self.assertIn("click", cmd)


class TestMicroVMManager(unittest.IsolatedAsyncioTestCase):
    async def test_process_backend_lifecycle(self):
        manager = SandboxManager(default_backend=SandboxBackend.PROCESS)
        spec = SandboxSpec(agent_id="test-process-agent", backend=SandboxBackend.PROCESS)
        
        handle = await manager.launch(spec)
        self.assertEqual(handle.backend, SandboxBackend.PROCESS)
        self.assertEqual(handle.agent_id, "test-process-agent")
        
        # Exec dummy command
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="hello-from-sandbox\n", returncode=0)
            output = await manager.exec(handle, "echo hello")
            self.assertIn("hello-from-sandbox", output)
            
        await manager.stop(handle)
        self.assertEqual(len(manager.list_handles()), 0)


class TestWebPStream(unittest.TestCase):
    def test_encode_webp(self):
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc````\x00\x00\x00\x05\x00\x01\xa5\xf6E\xdd\x00\x00\x00\x00IEND\xaeB`\x82"
        webp_bytes = StreamManager.encode_webp(png_bytes)
        if webp_bytes.startswith(b"RIFF"):
            self.assertNotEqual(png_bytes, webp_bytes)
        else:
            self.assertEqual(png_bytes, webp_bytes)

    def test_encode_qoi(self):
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc````\x00\x00\x00\x05\x00\x01\xa5\xf6E\xdd\x00\x00\x00\x00IEND\xaeB`\x82"
        qoi_bytes = StreamManager.encode_qoi(png_bytes)
        if qoi_bytes.startswith(b"qoif"):
            self.assertEqual(qoi_bytes[:4], b"qoif")
        else:
            self.assertEqual(png_bytes, qoi_bytes)


class TestVirtualInputDriverAdvanced(unittest.IsolatedAsyncioTestCase):
    async def test_clipboard_lifecycle(self):
        driver = VirtualInputDriver(fallback_to_pyautogui=False)
        # Verify it runs safely and returns a string (empty or value)
        val = await driver.get_clipboard()
        self.assertIsInstance(val, str)
        await driver.set_clipboard("hello-test")

    async def test_drag_and_drop_file(self):
        driver = VirtualInputDriver(fallback_to_pyautogui=False)
        with patch.object(driver, "click", new_callable=AsyncMock) as mock_click, \
             patch.object(driver, "press_keys", new_callable=AsyncMock) as mock_press:
            await driver.drag_and_drop_file("test.txt", 100, 200)
            mock_click.assert_called_with(100, 200)
            mock_press.assert_called_with(["ctrl", "v"])

    async def test_handle_touch(self):
        driver = VirtualInputDriver(fallback_to_pyautogui=False)
        with patch.object(driver, "click", new_callable=AsyncMock) as mock_click:
            await driver.handle_touch("tap", 0.5, 0.5, screen_width=1000, screen_height=1000)
            mock_click.assert_called_with(500, 500)


class TestSecurityDLP(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.audit_log = Path(self.temp_dir) / "audit.log"
        from superagent.security import SecurityManager, SecurityConfig, PermissionProfile
        self.config = SecurityConfig(
            max_clipboard_size=50,
            min_clipboard_interval=0.5,
            keyboard_rate_limit=0.01,
            enable_watermark=True
        )
        self.manager = SecurityManager(config=self.config, audit_log_path=self.audit_log)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_keystroke_logging(self):
        self.assertTrue(self.manager.validate_key_input(["ctrl", "c"]))
        with open(self.audit_log, "r", encoding="utf-8") as f:
            logs = f.read()
        self.assertIn("KEYSTROKE", logs)
        self.assertIn("ctrl,c", logs)

    def test_clipboard_dlp(self):
        # Normal copy
        self.assertTrue(self.manager.validate_clipboard_set("short content"))
        # Exceeds max size limit
        self.assertFalse(self.manager.validate_clipboard_set("a" * 100))
        
        # Test time spacing rate limiting
        self.assertFalse(self.manager.validate_clipboard_set("too fast"))

    def test_permission_profile(self):
        from superagent.security import PermissionProfile
        profile = PermissionProfile(allow_write=False)
        driver = VirtualInputDriver(security_manager=self.manager, fallback_to_pyautogui=False)
        self.manager.permissions = profile
        
        # Click should be blocked
        self.assertFalse(driver._check_permission("write"))
        self.assertTrue(driver._check_permission("read"))

    def test_watermark_overlay(self):
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc````\x00\x00\x00\x05\x00\x01\xa5\xf6E\xdd\x00\x00\x00\x00IEND\xaeB`\x82"
        watermarked = self.manager.apply_watermark(png_bytes)
        # Verify that it processed without exception
        self.assertIsInstance(watermarked, bytes)


class TestHITLServerSecurity(unittest.IsolatedAsyncioTestCase):
    async def test_permissions_and_copilot_endpoints(self):
        mock_agent = MagicMock()
        mock_loop = MagicMock()
        mock_agent.loop = mock_loop
        
        from superagent.security import SecurityManager
        sec_mgr = SecurityManager()
        mock_agent.security_manager = sec_mgr
        
        server = HITLServer(agent=mock_agent, port=9999)
        
        # Test permissions endpoint mock call
        mock_request = AsyncMock()
        mock_request.json.return_value = {"allow_write": False, "allow_execute": True}
        
        res = await server._handle_permissions(mock_request)
        self.assertEqual(res.status, 200)
        self.assertFalse(sec_mgr.permissions.allow_write)
        self.assertTrue(sec_mgr.permissions.allow_execute)

        # Test copilot takeover
        mock_request.json.return_value = {"action": "takeover"}
        res = await server._handle_copilot(mock_request)
        self.assertEqual(res.status, 200)
        mock_loop.pause.assert_called_once()


class TestSOMTagging(unittest.TestCase):
    def test_som_overlays(self):
        from superagent.som import SOMVisualTagger, InteractiveElement
        tagger = SOMVisualTagger()
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc````\x00\x00\x00\x05\x00\x01\xa5\xf6E\xdd\x00\x00\x00\x00IEND\xaeB`\x82"
        elements = [
            InteractiveElement(element_id="1", x1=10, y1=20, x2=30, y2=40, label="button"),
            InteractiveElement(element_id="2", x1=50, y1=60, x2=70, y2=80, label="input"),
        ]
        tagged_bytes, coord_map = tagger.tag_screenshot(png_bytes, elements)
        self.assertIsInstance(tagged_bytes, bytes)
        self.assertEqual(coord_map["1"], (20, 30))
        self.assertEqual(coord_map["2"], (60, 70))


class TestBenchmarkRunner(unittest.IsolatedAsyncioTestCase):
    async def test_benchmark_executes(self):
        from superagent.benchmark import BenchmarkRunner, BenchmarkTask
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock()
        mock_agent.loop.state.step_count = 3
        mock_agent.runtime.cost_tracker.get_total_cost.return_value = 0.05
        
        runner = BenchmarkRunner(agent=mock_agent)
        task = BenchmarkTask(
            task_id="t1",
            objective="open browser",
            validator=lambda agent: True
        )
        res = await runner.run_task(task)
        self.assertEqual(res.task_id, "t1")
        self.assertTrue(res.success)
        self.assertEqual(res.steps, 3)
        self.assertAlmostEqual(res.total_cost, 0.05)


class TestBrowserAuthUpgrades(unittest.IsolatedAsyncioTestCase):
    async def test_credential_vault(self):
        from worker.auth import AuthWorker
        mock_browser = MagicMock()
        auth = AuthWorker(browser=mock_browser)
        
        await auth.store_credential("github.com", "user1", "pass123")
        creds = await auth.get_credential("github.com")
        self.assertIsNotNone(creds)
        self.assertEqual(creds[0], "user1")
        self.assertEqual(creds[1], "pass123")

    async def test_browser_tabs_and_upload(self):
        from worker.browser import BrowserWorker
        worker = BrowserWorker()
        
        # Test tab stubs/handlers
        mock_context = MagicMock()
        mock_page = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.pages = [mock_page]
        worker._context = mock_context
        worker._page = mock_page
        
        idx = await worker.new_tab("http://example.com")
        self.assertEqual(idx, 0)
        
        await worker.switch_tab(0)
        await worker.close_tab(0)


class TestMemoryQuotaEviction(unittest.IsolatedAsyncioTestCase):
    async def test_quota_eviction(self):
        temp_dir = tempfile.mkdtemp()
        db_path = Path(temp_dir) / "quota_test.db"
        try:
            mem = AgentMemory(db_path, max_records=2)
            # Store 3 memories
            await mem.store(MemoryRecord("m1", "first content"))
            await mem.store(MemoryRecord("m2", "second content"))
            await mem.store(MemoryRecord("m3", "third content"))
            
            # Recall should find m3 and m2, but m1 should be evicted
            m1 = await mem.recall("first")
            self.assertEqual(len(m1), 0)
            
            m3 = await mem.recall("third")
            self.assertEqual(len(m3), 1)
        finally:
            shutil.rmtree(temp_dir)


class TestDashboardAPIServer(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_endpoints(self):
        mock_agent = MagicMock()
        mock_loop = MagicMock()
        mock_loop.state.paused = False
        mock_loop.state.done = False
        mock_loop.state.step_count = 10
        mock_loop.state.objective = "run benchmark"
        mock_agent.loop = mock_loop
        
        server = DashboardAPIServer(agent=mock_agent)
        mock_request = MagicMock()
        
        # Test metrics
        res = await server._handle_metrics(mock_request)
        self.assertEqual(res.status, 200)
        self.assertIn("cpu_percent", res.text)
        
        # Test agents
        res = await server._handle_agents(mock_request)
        self.assertEqual(res.status, 200)
        self.assertIn("run benchmark", res.text)
        
        # Test alerts
        res = await server._handle_alerts(mock_request)
        self.assertEqual(res.status, 200)
        self.assertIn("alerts", res.text)


if __name__ == "__main__":
    unittest.main()
