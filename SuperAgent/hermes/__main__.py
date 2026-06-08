"""Hermes HTTP service entrypoint."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from aiohttp import web

from .orchestrator import HermesOrchestrator


async def create_app() -> web.Application:
    """Create the Hermes web application."""

    app = web.Application()
    orchestrator = HermesOrchestrator(
        model=os.getenv("HERMES_MODEL"),
        api_key=os.getenv("TOGETHER_API_KEY"),
        base_url=os.getenv("HERMES_BASE_URL"),
        max_agents=int(os.getenv("MAX_AGENTS", "250")),
        log_dir=os.getenv("LOG_PATH", "./logs"),
    )

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def run(request: web.Request) -> web.Response:
        payload = await request.json()
        command = payload["command"]
        result = await orchestrator.run(command)
        return web.json_response({"result": result})

    async def decompose(request: web.Request) -> web.Response:
        payload = await request.json()
        subtasks = await orchestrator.decompose(payload["command"], int(payload.get("n_agents", 1)))
        return web.json_response({"subtasks": subtasks})

    async def aggregate(request: web.Request) -> web.Response:
        payload = await request.json()
        result = await orchestrator.aggregate(payload["command"], payload.get("results", []))
        return web.json_response({"result": result})

    app.router.add_get("/health", health)
    app.router.add_post("/run", run)
    app.router.add_post("/decompose", decompose)
    app.router.add_post("/aggregate", aggregate)
    return app


def main() -> None:
    """Run the Hermes HTTP service."""

    app = asyncio.run(create_app())
    web.run_app(app, host="0.0.0.0", port=9000)


if __name__ == "__main__":
    main()
