# service-installer

An AI-driven Docker service installer. The end goal is an **agent** that can take a
high-level request like *"deploy nginx on an available port"* and autonomously install,
run, and verify the service by calling **tools exposed through an MCP (Model Context
Protocol) server**.

```
User request ──► Agent (LLM) ──► MCP server ──► tools ──► Docker Compose
```

## Project goal

1. Wrap the core deployment logic as MCP tools (`create_service`, `get_service_status`,
   `get_service_logs`, ...).
2. Provide an agent that connects to the MCP server and uses those tools automatically
   to install and monitor Docker services with minimal human input.

## What is done so far

### `generate_compose.py` — CLI prototype (step 1)

A plain Python script that proves out the full deployment workflow:

- `check_prerequisites()` — verifies that `docker` and the `docker compose` plugin exist.
- `check_port_available()` / `find_available_port()` — finds a free TCP port in a range
  (default 8000–9000).
- `check_inputs()` — validates user input and generates defaults:
  - image name/URL (**required**),
  - service name derived from the image name if missing,
  - free port picked automatically if missing,
  - service folder under the current directory if no location given.
- `create_service_folder()` — creates `<location>/<service_name>/`.
- `generate_compose_file()` — writes a `docker-compose.yaml` (image, port mapping,
  env vars, volumes, optional curl-based healthcheck, `restart: always`).
- `run_docker_compose()` — runs `docker compose up -d` in the service folder.
- `verify_service_status()` — checks container status via `docker compose ps` and pulls
  recent logs to detect problems.

Usage:

```bash
python generate_compose.py '<json-input>'
# e.g.
python generate_compose.py '{"image": "nginx:alpine", "env_vars": {"DEBUG": "true"}}'
```

With no arguments it runs a demo deploy of `nginx:alpine`.

### `nginx/` — example output

Generated compose file from a test run of the prototype.

### `setup.md` — original design notes

The initial plan/checklist the prototype was built against.

## Roadmap (what is going to be done)

### Step 2 — MCP server (`server/`) ✅ done

Core logic refactored into `server/service_manager.py` and exposed as MCP tools in
`server/mcp_server.py` using the official Python SDK (`mcp`, `MCPServer` high-level
API), served over stdio:

| Tool | Purpose |
|------|---------|
| `check_prerequisites_tool()` | Report whether docker/docker compose are installed |
| `port_available(port)` | Check if a specific TCP port is free |
| `find_free_port(start?, end?)` | Find a free TCP port in a range |
| `create_service(...)` | Full install flow: validate input → create folder → generate compose file → `docker compose up -d` → verify status + readiness → return report + logs + service URL(s). Detects existing/similar services first and, unless `on_existing` says otherwise, returns `action="user_decision_required"` so the agent can ask the user. Slow deploys are bounded by `deploy_timeout_seconds` and rolled back cleanly (`action="deploy_timeout"`, layers stay cached for a fast retry) |
| `list_services()` | Inventory of everything installed: name, image, ports, URLs, running state |
| `get_service_status(service_name)` | Check if a deployed service's containers are running and, when a healthcheck was configured, whether it is ready; includes its image, port mappings and URL(s) |
| `exec_in_service(service_name, command)` | Run a command inside the service's own container (`compose exec`) and get stdout/stderr/exit_code — e.g. `redis-cli ping` smoke tests |
| `get_service_logs(service_name)` | Return recent container logs |
| `stop(service_name)` | Stop a deployed service (`docker compose stop`) |
| `remove(service_name, remove_volumes=True)` | `docker compose down` + delete the service folder; volumes (incl. image-declared data volumes) are removed by default, opt out with `remove_volumes=false`. Missing/already-removed services return `action="already_removed"` instead of an error; root-owned files that cannot be deleted yield `action="removed_with_leftovers"` listing paths + reasons |

Services are installed under `services/<name>/` (configurable via the
`SERVICE_INSTALLER_ROOT` env var).

Design notes:

- Duplicate protection: before deploying, `create_service` scans `services/` for
  same-name or same-base-image services (nginx vs nginx:alpine counts as similar).
  Default behavior is ask-first: the report comes back as
  `action="user_decision_required"` with nothing deployed, listing each existing
  service with the details needed to decide — image, host/container port mappings,
  URLs, running state and container status. The agent must present these and ask
  the user whether to reuse the existing service or create another instance
  (`on_existing="create_new"` auto-suffixes the name, e.g. `nginx-2`).
- Tool reports include `urls` (e.g. `http://localhost:8001`), and both the server
  instructions and the agent prompt tell the model to put those URLs in its final
  answer, so the deployed service is directly reachable from the AI response.
- Readiness vs running: container state alone doesn't prove a service answers
  (redis/postgres speak TCP, not HTTP). `create_service` therefore accepts one of
  three healthcheck kinds — `healthcheck_path` (HTTP GET), `healthcheck_tcp_port`
  (host-side TCP connect) or `healthcheck_command` (e.g. `pg_isready`, run via
  `compose exec`) — polls it after deploy and reports `ready` in install/status
  reports. The chosen check is persisted per service (`healthcheck.json`) so
  `get_service_status` can re-verify later.
- Slow pulls: `docker compose up` is bounded by `deploy_timeout_seconds` (default
  300). On expiry the half-install is rolled back and the report says so
  (`action="deploy_timeout"`); docker keeps downloaded layers, so retrying is fast.
- `container_port` parameter: the prototype mapped `host:host`, which breaks images
  with a fixed internal port (e.g. nginx listens on 80). `create_service` now accepts
  `container_port` separately (`host_port:container_port` mapping) and defaults it to
  `port`.
- Status verification parses `docker compose ps --format json` per-container states,
  with a substring fallback for older compose versions.

Run the server:

```bash
./venv/bin/python -m server.mcp_server
```

### Step 3 — Agent client (`agent/`) ✅ scaffolded

- `agent/mcp_client.py` — `MCPConnection`: opens a stdio session to the MCP server,
  discovers tools (converted to OpenAI tool-schema format), and invokes them.
- `agent/agent.py` — LLM agent loop (OpenAI-compatible chat completions API):
  feeds discovered MCP tools to the model, executes requested tool calls against the
  server, and loops until the service request is fulfilled.

Works with any OpenAI-compatible endpoint:

```bash
export OPENAI_API_KEY=...            # dummy value is fine for local endpoints
export OPENAI_BASE_URL=http://localhost:11434/v1   # optional (Ollama example)
export AGENT_MODEL=gpt-4o-mini       # optional

./venv/bin/python -m agent.agent "install nginx on a free port"
```

## Testing the MCP server

Two ways to exercise the tools as an agent would:

### Option A — LLM agent (`agent/agent.py`)

The agent auto-detects the provider from environment variables:

```bash
# Gemini (uses Gemini's OpenAI-compatible endpoint)
export GEMINI_API_KEY=...
./venv/bin/python -m agent.agent "install nginx on a free port"

# OpenAI or anything else speaking the OpenAI protocol
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=http://localhost:11434/v1   # e.g. Ollama; omit for OpenAI
export AGENT_MODEL=llama3.1                        # optional override

# Smoke test without any key: lists tools discovered through MCP
./venv/bin/python -m agent.agent --list-tools
```

### Option B — OpenCode as the installing agent

Register the MCP server once in your **global** OpenCode config
(`~/.config/opencode/opencode.jsonc`) and the `service-installer` tools become
available in every project — run `opencode` anywhere:

```jsonc
{
  "mcp": {
    "service-installer": {
      "type": "local",
      "command": ["/absolute/path/to/this/repo/venv/bin/python", "-m", "server.mcp_server"],
      "cwd": "/absolute/path/to/this/repo",
      "enabled": true,
      "timeout": 10000
    }
  }
}
```

then prompt, e.g.:

```
Use the service-installer tools to install redis on a free port and confirm it is running.
```

Notes:

- The global config is loaded for every workspace, so `command`/`cwd` must be
  absolute; adjust them if you move the repo.
- Installed services land in `<repo>/services/` no matter where you invoke
  opencode from, so every project shares one service registry.
- To temporarily disable the server set `"enabled": false`.
- Prefer project-only availability? A repo-level `opencode.json` with relative
  paths (`"cwd": "."`, `"command": ["venv/bin/python", ...]`) also works.

### Later ideas

- End-to-end agent test against a real/local LLM endpoint.
- Support multiple services per request / dependency ordering.
- Reverse-proxy integration (the `nginx/` example hints at exposing services behind one).
- Template library for common images (sane default env vars, healthchecks, volumes,
  known container ports).
- Tests (pytest) for port scanning, compose generation, and config validation.

## Development setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirement.txt
```

## Repository layout

```
service-installer/
├── README.md              # this file
├── setup.md               # original design notes
├── requirement.txt        # python dependencies
├── generate_compose.py    # step 1: CLI prototype
├── nginx/                 # example generated service folder
├── server/                # step 2: MCP server (service_manager.py + mcp_server.py)
├── agent/                 # step 3: agent client (mcp_client.py + agent.py)
└── services/              # runtime output: one folder per installed service (not tracked)
```
