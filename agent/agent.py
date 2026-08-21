"""
LLM agent that fulfils natural-language service requests using the
service-installer MCP server tools.

Works with any OpenAI-compatible chat completions endpoint:

- Google Gemini:   export GEMINI_API_KEY=...            (auto-detected)
- OpenAI:          export OPENAI_API_KEY=...
- Ollama/local:    export OPENAI_BASE_URL=http://localhost:11434/v1
                   export OPENAI_API_KEY=dummy

Optional: AGENT_MODEL (defaults depend on the detected provider).

Usage:
    ./venv/bin/python -m agent.agent --list-tools        # smoke test, no LLM needed
    ./venv/bin/python -m agent.agent "install nginx on a free port"
"""

import asyncio
import json
import os
import sys

from openai import AsyncOpenAI

from agent.mcp_client import MCPConnection

MAX_STEPS = 10

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
}

SYSTEM_PROMPT = """You are a service installation agent. You install and monitor \
Docker services by calling the provided tools.

Guidelines:
- To install a service you only strictly need the image name; missing details are \
auto-filled (free port, service name).
- For images with a well-known fixed internal port (e.g. nginx listens on 80), set \
container_port explicitly.
- If create_service returns action="user_decision_required", a similar service \
already exists. STOP and ask the user whether they want to reuse the existing \
service or create another instance. Include the relevant details from \
existing_services in your question (name, image, port mappings, URLs, running \
state) so the user can pick the right one. Do not deploy anything until they \
answer; then re-call create_service with on_existing="reuse" or \
on_existing="create_new" and report the outcome.
- After create_service, check the "running" and "ready" fields of the report. \
If running is false, use get_service_logs to investigate. If ready is false, \
the container is up but not answering yet — check get_service_status or logs \
and tell the user what you observed.
- For non-HTTP services (redis, postgres, ...), pass a readiness probe to \
create_service: healthcheck_command like "redis-cli ping", or \
healthcheck_tcp_port for a plain TCP port. Smoke-test deployments with \
exec_in_service when useful.
- If create_service returns action="deploy_timeout", nothing was installed; \
tell the user the image pull was slow and retry with the same arguments.
- Use list_services whenever the user asks what is deployed or after any \
failed/partial operation — never guess service names.
- Never guess ports that are already in use; rely on find_free_port or let \
create_service pick one.
- Always include the service URL(s) (the "urls" field, e.g. http://localhost:8001) \
prominently in your final answer so the user can open the service right away.
- Summarize clearly at the end: what was installed, its state, and the URL(s).
"""


def build_llm() -> tuple[AsyncOpenAI, str]:
    """Build the LLM client from environment variables."""
    model_override = os.environ.get("AGENT_MODEL")

    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if gemini_key:
        client = AsyncOpenAI(api_key=gemini_key, base_url=GEMINI_BASE_URL)
        return client, model_override or DEFAULT_MODELS["gemini"]

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit(
            "No API key found. Set GEMINI_API_KEY (Gemini) or OPENAI_API_KEY "
            "(OpenAI / any OpenAI-compatible endpoint via OPENAI_BASE_URL)."
        )

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=os.environ.get("OPENAI_BASE_URL"),  # None -> official OpenAI
    )
    return client, model_override or DEFAULT_MODELS["openai"]


class ServiceAgent:
    def __init__(self) -> None:
        self.llm, self.model = build_llm()

    async def run(self, request: str) -> str:
        async with MCPConnection() as mcp:
            tools = await mcp.list_tools()
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": request},
            ]

            for _step in range(MAX_STEPS):
                response = await self.llm.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                )
                choice = response.choices[0]
                messages.append(choice.message.model_dump(exclude_none=True))

                if not choice.message.tool_calls:
                    return choice.message.content or "(empty response)"

                for call in choice.message.tool_calls:
                    args = json.loads(call.function.arguments or "{}")
                    print(f"[agent] calling {call.function.name}({args})")
                    output = await mcp.call_tool(call.function.name, args)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": output[:8000],
                        }
                    )

            return "Stopped after max steps without a final answer."


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--list-tools":
        async def _list() -> list[str]:
            async with MCPConnection() as mcp:
                tools = await mcp.list_tools()
                return [t["function"]["name"] for t in tools]

        names = asyncio.run(_list())
        print(f"{len(names)} tools:", ", ".join(names))
        return

    request = " ".join(sys.argv[1:])
    if not request:
        print('Usage: python -m agent.agent "<request>"  (or --list-tools)')
        sys.exit(1)

    agent = ServiceAgent()
    print(f"[agent] provider model: {agent.model}")
    result = asyncio.run(agent.run(request))
    print("\n=== Agent answer ===")
    print(result)


if __name__ == "__main__":
    main()
