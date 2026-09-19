"""
MCP server exposing Docker service installation/monitoring tools.

Run with:  ./venv/bin/python -m server.mcp_server
"""

import os
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer

from server.service_manager import (
    ServiceError,
    check_port_available,
    check_prerequisites,
    compose_exec,
    describe_services,
    find_available_port,
    get_service_dir,
    get_service_logs as _get_logs,
    install_service,
    list_deployed_services,
    read_healthcheck,
    read_published_ports,
    read_service_config,
    read_service_volumes,
    service_urls,
    stop_service,
    uninstall_service,
    verify_readiness,
    verify_service_status,
)

SERVICES_ROOT = os.environ.get(
    "SERVICE_INSTALLER_ROOT",
    "/home/jonathan/projects/ai/service-installer-mcp/services",
)

server = MCPServer(
    name="service-installer",
    instructions=(
        "Tools to install, monitor and manage Docker Compose services. "
        "Typical flow: optionally call find_available_port / check_prerequisites, "
        "then create_service with an image name; the tool returns a report including "
        "whether the service ended up running and the URLs where it is reachable. "
        "Use get_service_status and get_service_logs to troubleshoot, list_services "
        "to see everything currently installed, and exec_in_service to smoke-test "
        "a deployment (e.g. redis-cli ping). "
        "Always include the service URL(s) from tool reports in your answers. "
        "IMPORTANT: if create_service returns action='user_decision_required', an "
        "existing service collides with the request — ask the user whether to reuse "
        "the existing one or install another instance, then re-call create_service "
        "with on_existing set to their choice ('reuse' or 'create_new')."
    ),
)


@server.tool()
def check_prerequisites_tool() -> Dict[str, bool]:
    """Check whether docker and docker compose are installed on this machine."""
    return check_prerequisites()


@server.tool()
def port_available(port: int) -> bool:
    """Check if a specific TCP port is available on localhost."""
    return check_port_available(port)


@server.tool()
def find_free_port(start: int = 8000, end: int = 9000) -> int:
    """Find a free TCP port in the given range (defaults to 8000-9000)."""
    return find_available_port(start, end)


@server.tool()
def create_service(
    image: str,
    service_name: Optional[str] = None,
    port: Optional[int] = None,
    container_port: Optional[int] = None,
    env_vars: Optional[Dict[str, str]] = None,
    volumes: Optional[List[str]] = None,
    healthcheck_path: Optional[str] = None,
    healthcheck_tcp_port: Optional[int] = None,
    healthcheck_command: Optional[str] = None,
    deploy_timeout_seconds: int = 300,
    readiness_timeout_seconds: int = 30,
    on_existing: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Install and start a Docker Compose service.

    Generates a docker-compose.yaml for the given image, runs `docker compose up -d`,
    then verifies the container is actually running AND ready (if a healthcheck
    was requested). The report includes running, ready, urls and the containers.

    If an existing service has the same name or uses a similar base image
    (e.g. nginx:alpine vs nginx), the report has action="user_decision_required":
    ASK THE USER whether to reuse the existing service or create another one,
    then call again with on_existing set to their choice. Never deploy over an
    existing service without the user's decision.

    If the deploy exceeds deploy_timeout_seconds (usually a slow image pull),
    the report has action="deploy_timeout": nothing was installed and it is
    safe to retry with the same arguments (docker caches downloaded layers).

    Args:
        image: Docker image name or URL (required), e.g. "nginx:alpine".
        service_name: Name for the service; derived from the image if omitted.
        port: Host port to publish; a free port in 8000-9000 is picked if omitted.
        container_port: Port the app listens on INSIDE the container (e.g. 80 for
            nginx). Defaults to `port`; set it when the image uses a fixed internal
            port.
        env_vars: Environment variables for the container.
        volumes: Volume mounts, long or short syntax. Short syntax strings like
            "./data:/var/lib/data" or "mydata:/var/lib/data", and dicts (long
            syntax) like {"type": "bind", "source": "./data", "target":
            "/var/lib/data"}. Relative bind-mount host paths resolve against the
            service's own folder; host directories are created at install time.
            Named volumes are declared in the compose file and removed with
            remove_volumes on remove.
        healthcheck_path: HTTP path polled until it answers (HTTP web apps).
        healthcheck_tcp_port: TCP port polled until connectable; probed on the
            published host port. Use for non-HTTP services like redis/postgres.
        healthcheck_command: Shell command executed inside the container via
            `compose exec` until it exits 0 (e.g. "redis-cli ping"). Exactly one
            of the three healthcheck options may be given.
        deploy_timeout_seconds: Max seconds for `docker compose up` before the
            install is rolled back (default 300).
        readiness_timeout_seconds: Max seconds to wait for the healthcheck to
            pass after the container is up (default 30).
        on_existing: What to do when a similar service already exists:
            "ask" (default) returns action="user_decision_required" without
            deploying; "reuse" returns the existing service's report and URL;
            "create_new" installs another instance under an auto-suffixed name.
    """
    return install_service(
        image=image,
        services_root=SERVICES_ROOT,
        service_name=service_name,
        port=port,
        container_port=container_port,
        env_vars=env_vars,
        volumes=volumes,
        healthcheck_path=healthcheck_path,
        healthcheck_tcp_port=healthcheck_tcp_port,
        healthcheck_command=healthcheck_command,
        deploy_timeout_seconds=deploy_timeout_seconds,
        readiness_timeout_seconds=readiness_timeout_seconds,
        on_existing=on_existing or "ask",
    )


@server.tool()
def list_services() -> Dict[str, Any]:
    """List every installed service with its image, ports, URLs and whether its
    containers are currently running. Use this to answer 'what is deployed?'
    or to inspect state after a failed/partial operation."""
    services = describe_services(list_deployed_services(SERVICES_ROOT))
    return {"count": len(services), "services": services}


@server.tool()
def exec_in_service(
    service_name: str,
    command: List[str],
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """
    Run a command inside a deployed service's own container and return
    stdout/stderr/exit_code — ideal for smoke tests after installing.

    Examples:
      - ["redis-cli", "ping"]
      - ["pg_isready", "-U", "postgres"]
      - ["curl", "-sf", "http://localhost:80/health"]

    The command runs only inside the named service's containers
    (`docker compose exec`). A non-zero exit code is reported, not raised.
    """
    service_dir = get_service_dir(SERVICES_ROOT, service_name)
    if not command:
        raise ServiceError("command must be a non-empty list")
    result = compose_exec(service_dir, command, timeout_seconds=timeout_seconds)
    return {
        "service_name": service_name,
        **result,
    }


@server.tool()
def get_service_status(service_name: str) -> Dict[str, Any]:
    """Check whether a deployed service's containers are running and, when a
    healthcheck was configured at install time, whether the service is ready."""
    service_dir = get_service_dir(SERVICES_ROOT, service_name)
    is_running, containers, _logs = verify_service_status(service_dir)
    report: Dict[str, Any] = {
        "service_name": service_name,
        "running": is_running,
        "urls": service_urls(service_dir),
        "ports": read_published_ports(service_dir),
        "volumes": read_service_volumes(service_dir),
        "containers": containers,
    }
    try:
        report["image"] = str(read_service_config(service_dir).get("image", ""))
    except (ServiceError, OSError, ValueError):
        report["image"] = None

    check = read_healthcheck(service_dir)
    if check is None:
        report.update(
            {
                "ready": None,
                "ready_via": "container_state",
                "readiness_detail": (
                    "No healthcheck configured; 'running' reflects container "
                    "state only."
                ),
            }
        )
    else:
        report.update(verify_readiness(service_dir, check, timeout_seconds=10))
    return report


@server.tool()
def get_service_logs(service_name: str, tail: int = 50) -> str:
    """Return recent logs from a deployed service's containers."""
    service_dir = get_service_dir(SERVICES_ROOT, service_name)
    return _get_logs(service_dir, tail=tail)


@server.tool()
def stop(service_name: str) -> str:
    """Stop a deployed service (containers are kept, not removed)."""
    service_dir = get_service_dir(SERVICES_ROOT, service_name)
    stop_service(service_dir)
    return f"Service '{service_name}' stopped."


@server.tool()
def remove(service_name: str, remove_volumes: bool = True) -> Dict[str, Any]:
    """
    Remove a deployed service (docker compose down) and delete its folder.

    By default also removes the service's volumes (-v), including named or
    anonymous data volumes created by the image. Set remove_volumes=false to
    keep volume data on disk.

    If the service is not installed (or already removed), the report has
    action="already_removed" — this is not an error. A folder without a
    docker-compose.yaml yields action="not_a_service" and is never deleted.
    """
    return uninstall_service(SERVICES_ROOT, service_name, remove_volumes=remove_volumes)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
