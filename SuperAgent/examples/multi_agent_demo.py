"""Quick-start example: multi-agent research group with full Phase 1-4 features.

Demonstrates:
  - Spawning agent containers (#1/#2)
  - Agent grouping (#72)
  - Priority task queue (#88)
  - Auto-scaling (#93)
  - Co-pilot control (#71)
  - RBAC login (#73)
  - Context sharing (#61)
  - Health checking (#87)
  - Alert monitoring (#84/#85/#86)
  - Cost tracking (#80)
  - Trajectory recording (#57)

Run:
    python examples/multi_agent_demo.py
"""

from __future__ import annotations

import asyncio
import logging

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    from infrastructure.container_manager import ContainerManager
    from infrastructure.task_db import TaskDatabase
    from pathlib import Path

    from superagent import (
        # Phase 1
        RelayServer,
        TrajectoryRecorder,
        # Phase 2
        RBACManager, Role,
        CredentialVault,
        CoPilotServer,
        # Phase 3
        PluginRegistry, ContextManager,
        AgentShell,
        # Phase 4
        AlertManager, HealthChecker, CostTracker,
        # Dashboard
        DashboardAPIServer, register_agent_desktop,
    )
    from hermes.orchestrator import HermesOrchestrator

    # ── 1. Start infrastructure ──────────────────────────────────────────────
    db = TaskDatabase(Path("./data/demo.db"))
    container_mgr = ContainerManager(max_agents=10)
    orchestrator = HermesOrchestrator(container_manager=container_mgr, task_db=db)

    # ── 2. RBAC — create admin & operator accounts ────────────────────────────
    rbac = RBACManager()
    try:
        admin = rbac.create_user("admin", "admin-secret", role=Role.ADMIN)
        rbac.create_user("operator", "op-secret", role=Role.OPERATOR)
        print(f"Created admin user: {admin.username}")
    except Exception:
        pass  # Users may already exist

    # ── 3. Start relay server (#89) ───────────────────────────────────────────
    relay = RelayServer()
    asyncio.create_task(relay.start())

    # ── 4. Start co-pilot server (#71) ────────────────────────────────────────
    copilot = CoPilotServer()
    asyncio.create_task(copilot.start())

    # ── 5. Dashboard API (#82, #69, #70, #73) ─────────────────────────────────
    dashboard = DashboardAPIServer(port=9200)
    asyncio.create_task(dashboard.start())

    # ── 6. Cost tracker (#80) ─────────────────────────────────────────────────
    cost_tracker = CostTracker(persist_path=".superagent/costs.json")
    cost_tracker.set_budget("agent-1", daily_usd=5.0, total_usd=50.0)

    # ── 7. Alert manager (#84/#85/#86) ────────────────────────────────────────
    alert_mgr = AlertManager(webhook_url=None)  # set webhook_url for real alerts
    alert_mgr.set_threshold("cpu_pct",    warn=70.0,  critical=90.0)
    alert_mgr.set_threshold("memory_pct", warn=80.0,  critical=95.0)
    alert_mgr.set_budget("token_cost_usd", daily_limit=10.0)
    asyncio.create_task(alert_mgr.start_monitoring(interval=30))

    # ── 8. Health checker (#87) ───────────────────────────────────────────────
    health_checker = HealthChecker(container_manager=container_mgr)
    # health_checker.register("agent-1", health_url="http://127.0.0.1:8001/health")

    # ── 9. Spawn 3 agent containers ───────────────────────────────────────────
    print("Spawning agents...")
    # await container_mgr.spawn_all(3)   # uncomment when Docker is available

    # ── 10. Create a research group (#72) ─────────────────────────────────────
    group = await orchestrator.create_group(
        "research-team",
        agent_ids=[1, 2, 3],
        shared_goal="Research quantum computing papers from 2024",
        shared_memory=True,
    )
    print(f"Created group: {group['group_id']}")

    # ── 11. Context sharing (#61) ─────────────────────────────────────────────
    ctx = ContextManager(agent_id="agent-1", model="gpt-4o")
    ctx.set_system_prompt(
        "You are agent {agent_id}, a research specialist. Goal: {goal}",
        goal="Find and summarize quantum computing papers",
    )
    ctx.inject_tool_list([{"name": "web_search", "description": "Search the web"}])
    ctx.publish_to_namespace("research-team")

    # ── 12. Trajectory recording (#57) ────────────────────────────────────────
    recorder = TrajectoryRecorder(agent_id="agent-1")

    # ── 13. Plugin registry (#64) ─────────────────────────────────────────────
    registry = PluginRegistry(agent_id="agent-1")
    registry.load_from_directory("plugins/")
    print(f"Loaded {len(registry.list_tools())} plugins: {[t['name'] for t in registry.list_tools()]}")

    # ── 14. Priority task submission (#88) ────────────────────────────────────
    result = await orchestrator.submit_priority(
        "Find the top 10 quantum computing papers from 2024",
        priority=10,  # URGENT
    )
    print(f"Submitted priority task: {result['task_id']}")

    # ── 15. Auto-scale check (#93) ────────────────────────────────────────────
    scale_result = await orchestrator.auto_scale(min_agents=1, max_agents=10)
    print(f"Auto-scale: {scale_result}")

    # ── 16. Credential vault (#77) ────────────────────────────────────────────
    vault = CredentialVault(agent_id="agent-1")
    vault.store("github.com", username="agent-bot", password="gh-token-xxx")
    sites = vault.list_sites()
    print(f"Vault sites: {sites}")

    # ── 17. Shell (#66) ───────────────────────────────────────────────────────
    shell = AgentShell(agent_id="agent-1")
    result = await shell.run("echo 'Agent shell ready'")
    print(f"Shell: {result.stdout.strip()}")

    print("\n✅ SuperAgent multi-agent stack is running.")
    print("   Dashboard:  http://127.0.0.1:9200")
    print("   Relay WS:   ws://127.0.0.1:9100/ws")
    print("   CoPilot WS: ws://127.0.0.1:9300/copilot/{agent_id}/view")

    # Keep running
    await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
