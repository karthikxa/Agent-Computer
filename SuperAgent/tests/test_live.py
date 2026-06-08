import asyncio

from superagent.desktop_api import DesktopAPI
from superagent.stream import StreamManager


async def test_live_desktop():
    api = DesktopAPI(host="localhost", port=8000)
    png = await api.screenshot()
    assert len(png) > 1000
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    size = await api.get_screen_size()
    await api.click(size["width"] // 2, size["height"] // 2)
    await api.type_text("hello world")
    result = await api.run_command("echo hello")
    assert result["stdout"].strip() == "hello"

    stream = StreamManager(host="localhost") if False else StreamManager()
    url = await stream.auto_detect()
    assert url is not None

    print("ALL LIVE TESTS PASSED")
