# AGENTS.md

AI-driven Docker service installer: an MCP server exposing tools that deploy/monitor
Docker Compose services, plus an LLM agent that drives those tools from natural
language. See README.md for full context.

## Commands

Always use the venv interpreter (`mcp`, `pyyaml`, `openai` live only there; system
python3 will fail to import them):

```bash
./venv/bin/python -m server.mcp_server          # start MCP server (stdio)
./venv/bin/python -m agent.agent --list-tools   # smoke test MCP connection, no API key needed
GEMINI_API_KEY=... ./venv/bin/python -m agent.agent "install nginx on a free port"
```

- Module-style invocation (`-m`) requires cwd = repo root; running files directly
  breaks imports.
- No test suite / lint / typecheck config exists. Verify changes end-to-end:
  connect a stdio MCP client, call `create_service`, curl the returned URL, then call
  `remove`. `agent/mcp_client.py` shows the working client incantation.

## Gotchas

- Installed SDK is `mcp==2.0.0`: high-level class is `MCPServer`
  (`from mcp.server.mcpserver import MCPServer`); there is no `mcp.server.fastmcp`.
  Client-side tool schemas use attribute `input_schema`, not `inputSchema`.
- Duplicate protection: by default `create_service` refuses to silently double-install.
  Same-name or same-base-image collisions return `action="user_decision_required"`
  with nothing deployed; the agent must ask the user, then re-call with
  `on_existing='reuse' | 'create_new'`. Keep this ask-first contract intact when
  editing reports/prompts.

## Git Rules

- Never run `git commit` unless the user explicitly asks for a commit in the current conversation.
- Never run `git push` unless the user explicitly asks for it.
- Never amend, rebase, reset, clean, or force-push without explicit confirmation.
- Never discard uncommitted changes that were not created by you.
- Before modifying files, inspect `git status`.
- Do not modify unrelated files.
- Keep changes focused and minimal.
- After implementation, stop and let the user review the diff.

## Review Checkpoint

When the requested implementation is complete:

1. Stop making changes.
2. Run tests and static checks.
3. Show the final diff summary.
4. State that the changes are ready for review.
5. Wait for the user to approve any further changes or a commit.

Do not interpret “finish this task” or “make it production-ready” as permission to commit.