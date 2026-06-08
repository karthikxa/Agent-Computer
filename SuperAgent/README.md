# SuperAgent

## Quick start
1. `git clone <repo-url>`
2. `docker compose up --build`
3. `python -c "from superagent import SuperAgent, AgentConfig; import asyncio; asyncio.run(SuperAgent(AgentConfig()).start())"`

## API keys
Set keys in `.env`:
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`

## Desktop stream
Open `http://localhost:6901` in your browser for KasmVNC, or `http://localhost:7080/index.m3u8` for HLS.

## Smoke test
Run:
`python -m pytest tests/test_live.py -v`
