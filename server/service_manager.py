"""
Core logic for installing, monitoring and managing Docker Compose services.

Refactored from generate_compose.py so it can be reused by both the CLI
prototype and the MCP server tools. All functions are framework-agnostic.
"""

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

DEFAULT_PORT_RANGE_START = 8000
DEFAULT_PORT_RANGE_END = 9000
HEALTHCHECK_FILE = "healthcheck.json"


class ServiceError(Exception):
    """Raised when a service operation fails."""


class DeployTimeoutError(ServiceError):
    """Raised when `docker compose up` exceeds its allotted time."""


def _run_cmd(
    args: List[str],
    cwd: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
) -> str:
    """Run a command, returning stdout. Raises ServiceError on failure."""
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return result.stdout
    except subprocess.TimeoutExpired as e:
        raise DeployTimeoutError(
            f"Command '{' '.join(args)}' timed out after {timeout_seconds}s"
        ) from e
    except subprocess.CalledProcessError as e:
        raise ServiceError(
            f"Command '{' '.join(args)}' failed "
            f"(exit {e.returncode}): {e.stderr.strip() or e.stdout.strip()}"
        ) from e


def check_prerequisites() -> Dict[str, bool]:
    """Check if docker and docker compose are installed."""
    docker_found = shutil.which("docker") is not None
    compose_ok = False
    if docker_found:
        try:
            subprocess.run(
                ["docker", "compose", "version"],
                check=True,
                capture_output=True,
            )
            compose_ok = True
        except (subprocess.CalledProcessError, FileNotFoundError):
            compose_ok = False
    return {"docker": docker_found, "docker_compose": compose_ok}


def check_port_available(port: int) -> bool:
    """Check if a port is available on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) != 0


def find_available_port(start: int = DEFAULT_PORT_RANGE_START, end: int = DEFAULT_PORT_RANGE_END) -> int:
    """Find an available port in the specified range."""
    for port in range(start, end + 1):
        if check_port_available(port):
            return port
    raise ServiceError(f"No available ports in range {start}-{end}")


def derive_service_name(image: str, service_name: Optional[str] = None) -> str:
    """Normalize the user-supplied name, or derive one from the image."""
    if not service_name:
        base_name = image.split("/")[-1].split(":")[0]
        service_name = base_name.replace(".", "-").replace("_", "-")
    return service_name.lower().strip()


def validate_inputs(
    image: str,
    service_name: Optional[str] = None,
    port: Optional[int] = None,
    container_port: Optional[int] = None,
    env_vars: Optional[Dict[str, str]] = None,
    volumes: Optional[List[str]] = None,
    healthcheck_path: Optional[str] = None,
    healthcheck_tcp_port: Optional[int] = None,
    healthcheck_command: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate inputs and fill in defaults. Does not touch the filesystem."""
    if not image or not image.strip():
        raise ServiceError("Image name or URL is required")

    specified = [
        kind
        for kind in (
            healthcheck_path is not None,
            healthcheck_tcp_port is not None,
            healthcheck_command is not None,
        )
        if kind
    ]
    if len(specified) > 1:
        raise ServiceError(
            "Pass only one of healthcheck_path, healthcheck_tcp_port "
            "or healthcheck_command"
        )

    service_name = derive_service_name(image, service_name)

    if port is None:
        port = find_available_port()
    elif not check_port_available(port):
        raise ServiceError(f"Port {port} is already in use")

    # The port the application listens on inside the container. Defaults to
    # the host port, which suits images configured at deploy time (env vars);
    # fixed-port images like nginx need this set explicitly (e.g. 80).
    if container_port is None:
        container_port = port

    return {
        "image": image,
        "service_name": service_name,
        "port": port,
        "container_port": container_port,
        "env_vars": env_vars or {},
        "volumes": volumes or [],
        "healthcheck_path": healthcheck_path,
        "healthcheck_tcp_port": healthcheck_tcp_port,
        "healthcheck_command": healthcheck_command,
    }


def create_service_folder(location: str) -> str:
    os.makedirs(location, exist_ok=True)
    return location


def generate_compose_file(config: Dict[str, Any], location: str) -> str:
    """Write docker-compose.yaml into location. Returns the file path."""
    service_config: Dict[str, Any] = {
        "image": config["image"],
        "ports": [f"{config['port']}:{config['container_port']}"],
        "environment": [f"{k}={v}" for k, v in config["env_vars"].items()],
        "restart": "always",
    }

    if config["volumes"]:
        service_config["volumes"] = config["volumes"]

    if config.get("healthcheck_path"):
        service_config["healthcheck"] = {
            "test": [
                "CMD",
                "curl",
                "-f",
                f"http://localhost:{config['container_port']}{config['healthcheck_path']}",
            ],
            "interval": "30s",
            "timeout": "10s",
            "retries": 3,
            "start_period": "10s",
        }

    compose_content = {
        "services": {config["service_name"]: service_config}
    }

    filepath = os.path.join(location, "docker-compose.yaml")
    with open(filepath, "w") as f:
        yaml.dump(compose_content, f, default_flow_style=False, sort_keys=False)
    return filepath


def deploy_service(location: str, timeout_seconds: Optional[int] = None) -> str:
    """Run docker compose up -d in the service directory."""
    return _run_cmd(
        ["docker", "compose", "up", "-d"],
        cwd=location,
        timeout_seconds=timeout_seconds,
    )


def get_service_dir(services_root: str, service_name: str) -> str:
    service_dir = os.path.join(services_root, service_name)
    compose_file = os.path.join(service_dir, "docker-compose.yaml")
    if not os.path.isfile(compose_file):
        raise ServiceError(
            f"Service '{service_name}' not found (missing {compose_file})"
        )
    return service_dir


def read_service_config(service_dir: str) -> Dict[str, Any]:
    """Read the generated docker-compose.yaml back into a dict."""
    with open(os.path.join(service_dir, "docker-compose.yaml")) as f:
        data = yaml.safe_load(f) or {}
    services = data.get("services") or {}
    if not services:
        raise ServiceError(f"No services defined in {service_dir}/docker-compose.yaml")
    name, config = next(iter(services.items()))
    return {"name": name, **(config or {})}


def read_published_ports(service_dir: str) -> List[Dict[str, int]]:
    """Extract host->container port mappings from the service's compose file."""
    config = read_service_config(service_dir)
    ports: List[Dict[str, int]] = []
    for entry in config.get("ports") or []:
        parts = str(entry).split(":")
        if len(parts) >= 2:
            try:
                ports.append(
                    {
                        "host_port": int(parts[-2]),
                        "container_port": int(parts[-1].split("/")[0]),
                    }
                )
            except ValueError:
                continue
    return ports


def service_urls(service_dir: str) -> List[str]:
    """HTTP URLs for the published ports of a deployed service."""
    return [
        f"http://localhost:{mapping['host_port']}"
        for mapping in read_published_ports(service_dir)
    ]


def healthcheck_from_config(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Reduce an install config to the active healthcheck descriptor, if any."""
    if config.get("healthcheck_path"):
        return {
            "type": "http",
            "path": config["healthcheck_path"],
            "host_port": config["port"],
            "container_port": config["container_port"],
        }
    if config.get("healthcheck_tcp_port") is not None:
        return {
            "type": "tcp",
            "port": config["healthcheck_tcp_port"],
            "host_port": config["port"],
        }
    if config.get("healthcheck_command"):
        return {"type": "command", "command": config["healthcheck_command"]}
    return None


def save_healthcheck(location: str, check: Optional[Dict[str, Any]]) -> None:
    """Persist the service's healthcheck next to its compose file."""
    path = os.path.join(location, HEALTHCHECK_FILE)
    if check is None:
        if os.path.exists(path):
            os.remove(path)
        return
    with open(path, "w") as f:
        json.dump(check, f, indent=2)


def read_healthcheck(service_dir: str) -> Optional[Dict[str, Any]]:
    """Load the persisted healthcheck descriptor, or None."""
    path = os.path.join(service_dir, HEALTHCHECK_FILE)
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("type") else None
    except (OSError, ValueError):
        return None


def _probe_http(url: str) -> Tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            ok = resp.status < 400
            return ok, f"GET {url} -> HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"GET {url} -> HTTP {e.code}"
    except Exception as e:
        return False, f"GET {url} failed: {e}"


def _probe_tcp(host_port: int) -> Tuple[bool, str]:
    try:
        with socket.create_connection(("localhost", host_port), timeout=3):
            return True, f"tcp connect localhost:{host_port} ok"
    except OSError as e:
        return False, f"tcp connect localhost:{host_port} failed: {e}"


def compose_exec(
    service_dir: str,
    command: List[str],
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """Run a command inside the service's own container via compose exec."""
    svc_name = read_service_config(service_dir)["name"]
    args = ["docker", "compose", "exec", "-T", svc_name, *command]
    try:
        result = subprocess.run(
            args,
            cwd=service_dir,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        err = e.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        return {
            "exit_code": None,
            "stdout": out,
            "stderr": err,
            "timed_out": True,
        }


def _probe_command(service_dir: str, command: str) -> Tuple[bool, str]:
    result = compose_exec(service_dir, ["sh", "-c", command])
    detail = (result["stdout"].strip() or result["stderr"].strip())[:200]
    if result["timed_out"]:
        return False, f"exec '{command}' timed out"
    ok = result["exit_code"] == 0
    return ok, f"exec '{command}' exit={result['exit_code']} {detail}".strip()


def probe_readiness_once(
    service_dir: str, check: Dict[str, Any]
) -> Tuple[bool, str]:
    """Run one iteration of the configured healthcheck."""
    kind = check.get("type")
    if kind == "http":
        url = f"http://localhost:{check['host_port']}{check['path']}"
        return _probe_http(url)
    if kind == "tcp":
        return _probe_tcp(int(check["host_port"]))
    if kind == "command":
        return _probe_command(service_dir, str(check["command"]))
    raise ServiceError(f"Unknown healthcheck type '{kind}'")


def verify_readiness(
    service_dir: str,
    check: Dict[str, Any],
    timeout_seconds: int = 30,
    interval_seconds: int = 2,
) -> Dict[str, Any]:
    """
    Poll the configured healthcheck until it passes or the deadline hits.

    Returns {"ready": bool, "ready_via": <type>, "readiness_detail": str}.
    """
    deadline = time.monotonic() + max(timeout_seconds, interval_seconds)
    last_detail = ""
    while True:
        try:
            ok, last_detail = probe_readiness_once(service_dir, check)
        except ServiceError as exc:
            ok, last_detail = False, str(exc)
        if ok or time.monotonic() >= deadline:
            break
        time.sleep(interval_seconds)
    return {
        "ready": ok,
        "ready_via": check.get("type"),
        "readiness_detail": last_detail,
    }


def list_deployed_services(services_root: str) -> List[Dict[str, Any]]:
    """List services tracked under services_root (one folder per service)."""
    services = []
    if not os.path.isdir(services_root):
        return services
    for entry in sorted(os.listdir(services_root)):
        service_dir = os.path.join(services_root, entry)
        compose_file = os.path.join(service_dir, "docker-compose.yaml")
        if not os.path.isfile(compose_file):
            continue
        try:
            config = read_service_config(service_dir)
        except (ServiceError, OSError, yaml.YAMLError):
            continue
        services.append(
            {
                "name": entry,
                "image": str(config.get("image", "")),
                "urls": service_urls(service_dir),
                "location": service_dir,
            }
        )
    return services


def _base_image(image: str) -> str:
    """Reduce 'repo/name:tag' to 'name' for loose similarity matching."""
    return image.split("/")[-1].split(":")[0].strip().lower()


def find_existing_services(
    services_root: str, service_name: str, image: str
) -> List[Dict[str, Any]]:
    """
    Find already-deployed services that collide with a requested install.

    A collision is an exact name match, an exact image match, or the same base
    image (e.g. 'nginx:alpine' vs 'nginx').
    """
    return [
        svc
        for svc in list_deployed_services(services_root)
        if svc["name"] == service_name
        or _base_image(svc["image"]) == _base_image(image)
    ]


def describe_services(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Enrich collision matches with the details a user needs to decide:
    port mappings, URLs, whether the service is currently up, and container
    states. Status is best-effort; failures don't block the report.
    """
    described = []
    for match in matches:
        info = dict(match)
        info["ports"] = read_published_ports(match["location"])
        try:
            is_running, containers, _logs = verify_service_status(match["location"])
            info["running"] = is_running
            info["containers"] = [
                {k: v for k, v in c.items() if k not in ("Labels", "Networks")}
                for c in containers
            ]
        except ServiceError as exc:
            info["running"] = None
            info["status_error"] = str(exc)
        described.append(info)
    return described


def unique_service_name(services_root: str, name: str) -> str:
    """Return name, or name-2, name-3, ... until the folder is unused."""
    candidate = name
    counter = 2
    while os.path.isdir(os.path.join(services_root, candidate)):
        candidate = f"{name}-{counter}"
        counter += 1
    return candidate


def _parse_compose_ps(ps_output: str) -> List[Dict[str, Any]]:
    """Parse 'docker compose ps --format json' output (one JSON object per line)."""
    containers = []
    for line in ps_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            containers.append(_json_loads(line))
        except ValueError:
            continue
    return containers


def _json_loads(text: str) -> Dict[str, Any]:
    import json

    return json.loads(text)


def verify_service_status(service_dir: str, tail: int = 50) -> Tuple[bool, List[Dict[str, Any]], str]:
    """
    Check whether the service's containers are running.

    Returns (is_running, container_states, logs).
    """
    ps_output = _run_cmd(["docker", "compose", "ps", "--format", "json"], cwd=service_dir)
    containers = _parse_compose_ps(ps_output)

    is_running = False
    if containers:
        states = [str(c.get("State") or c.get("state") or "").lower() for c in containers]
        is_running = bool(states) and all("running" in s or s == "up" for s in states)
    else:
        # Fallback for older docker compose versions without json format
        plain = _run_cmd(["docker", "compose", "ps"], cwd=service_dir)
        lowered = plain.lower()
        is_running = "running" in lowered or "up" in lowered

    logs = _run_cmd(["docker", "compose", "logs", "--tail", str(tail)], cwd=service_dir)
    return is_running, containers, logs


def get_service_logs(service_dir: str, tail: int = 50) -> str:
    return _run_cmd(["docker", "compose", "logs", "--tail", str(tail)], cwd=service_dir)


def stop_service(service_dir: str) -> str:
    return _run_cmd(["docker", "compose", "stop"], cwd=service_dir)


def remove_service(service_dir: str, remove_volumes: bool = True) -> str:
    args = ["docker", "compose", "down"]
    if remove_volumes:
        args.append("-v")
    return _run_cmd(args, cwd=service_dir)


def uninstall_service(
    services_root: str,
    service_name: str,
    remove_volumes: bool = True,
) -> Dict[str, Any]:
    """
    Remove a deployed service: docker compose down (-v) + delete its folder.

    Volumes are removed by default so named/anonymous data volumes (declared
    by images like postgres or redis) do not linger on disk. Returns a report
    dict; a missing service yields action="already_removed" and a folder that
    is not a tracked service yields action="not_a_service" (nothing deleted).

    Files the container wrote as root (e.g. bind-mounted data dirs) can be
    undeletable by the host user; in that case the report has
    action="removed_with_leftovers" and lists them under "leftovers".
    """
    service_dir = os.path.join(services_root, service_name)
    compose_file = os.path.join(service_dir, "docker-compose.yaml")

    if not os.path.isdir(service_dir):
        return {
            "action": "already_removed",
            "service_name": service_name,
            "message": f"No service named '{service_name}' is installed.",
        }

    if not os.path.isfile(compose_file):
        return {
            "action": "not_a_service",
            "service_name": service_name,
            "location": service_dir,
            "message": (
                f"'{service_dir}' exists but contains no docker-compose.yaml; "
                "nothing was deleted. Remove it manually if that is intended."
            ),
        }

    output = remove_service(service_dir, remove_volumes=remove_volumes)

    leftovers: List[Dict[str, str]] = []

    def _collect(_fn: Any, path: str, exc_info: Any) -> None:
        reason = (
            "permission_denied"
            if isinstance(exc_info[1], PermissionError)
            else str(exc_info[1])
        )
        leftovers.append({"path": path, "reason": reason})

    try:
        shutil.rmtree(service_dir, onexc=_collect)
    except TypeError:
        # Python < 3.12 fallback
        leftovers.clear()
        shutil.rmtree(service_dir, onerror=lambda fn, p, e: _collect(fn, p, e))

    # Keep root causes only: directories reported as "not empty" merely
    # failed because something inside them survived.
    leftovers = [
        item
        for item in leftovers
        if not any(
            other["path"] != item["path"] and other["path"].startswith(item["path"] + os.sep)
            for other in leftovers
        )
    ]

    if os.path.isdir(service_dir):
        return {
            "action": "removed_with_leftovers",
            "service_name": service_name,
            "volumes_removed": remove_volumes,
            "location": service_dir,
            "leftovers": leftovers,
            "output": output.strip(),
            "message": (
                "Containers were stopped and removed, but some files could not "
                "be deleted (usually root-owned data written by the container). "
                "Remove them manually, e.g. with sudo, then delete the folder."
            ),
        }

    return {
        "action": "removed",
        "service_name": service_name,
        "volumes_removed": remove_volumes,
        "location": service_dir,
        "leftovers": [],
        "output": output.strip(),
    }


def install_service(
    image: str,
    services_root: str,
    service_name: Optional[str] = None,
    port: Optional[int] = None,
    container_port: Optional[int] = None,
    env_vars: Optional[Dict[str, str]] = None,
    volumes: Optional[List[str]] = None,
    healthcheck_path: Optional[str] = None,
    healthcheck_tcp_port: Optional[int] = None,
    healthcheck_command: Optional[str] = None,
    startup_wait_seconds: int = 5,
    readiness_timeout_seconds: int = 30,
    deploy_timeout_seconds: int = 300,
    on_existing: str = "ask",
) -> Dict[str, Any]:
    """
    Full install flow: validate -> create folder -> generate compose file ->
    deploy -> verify -> readiness check. Returns a report dictionary.

    If an existing service collides (same name or same base image), the
    behavior depends on `on_existing`:
      - "ask": deploy nothing; return an action="user_decision_required"
        report so the caller can ask the user what to do.
      - "reuse": deploy nothing; return the existing service's report.
      - "create_new": deploy anyway, under an auto-suffixed name if needed
        (e.g. nginx -> nginx-2).

    If `docker compose up` exceeds deploy_timeout_seconds (typically a slow
    image pull), everything is rolled back and the report has
    action="deploy_timeout": nothing is installed, already-downloaded layers
    stay cached, so a retry with the same arguments is fast.
    """
    if on_existing not in ("ask", "reuse", "create_new"):
        raise ServiceError(
            f"Invalid on_existing '{on_existing}'; "
            "expected 'ask', 'reuse' or 'create_new'"
        )

    prereqs = check_prerequisites()
    if not prereqs["docker"] or not prereqs["docker_compose"]:
        raise ServiceError(f"Docker/Docker Compose not available: {prereqs}")

    requested_name = derive_service_name(image, service_name)
    matches = find_existing_services(services_root, requested_name, image)

    if matches:
        if on_existing == "ask":
            described = describe_services(matches)
            return {
                "action": "user_decision_required",
                "requested": {"image": image, "service_name": requested_name},
                "existing_services": described,
                "message": (
                    f"A service named '{requested_name}' or using a similar image "
                    "already exists. Present the details under existing_services to "
                    "the user (name, image, ports, URLs, running state) and ask "
                    "whether to reuse it or create another one BEFORE deploying. "
                    "Then call again with on_existing='reuse' or "
                    "on_existing='create_new'."
                ),
                "options": {
                    "reuse": "Return the existing service's URL; nothing is installed.",
                    "create_new": (
                        "Install an additional instance under an auto-suffixed name."
                    ),
                },
            }

        if on_existing == "reuse":
            # Prefer the exact-name match when several services collide.
            target = next(
                (m for m in matches if m["name"] == requested_name), matches[0]
            )
            (target,) = describe_services([target])
            return {
                "action": "reused_existing",
                "service_name": target["name"],
                "image": target["image"],
                "ports": target["ports"],
                "urls": target["urls"],
                "location": target["location"],
                "running": target["running"],
                "containers": target.get("containers", []),
                "logs_tail": "",
            }

        # on_existing == "create_new"
        requested_name = unique_service_name(services_root, requested_name)

    config = validate_inputs(
        image=image,
        service_name=requested_name,
        port=port,
        container_port=container_port,
        env_vars=env_vars,
        volumes=volumes,
        healthcheck_path=healthcheck_path,
        healthcheck_tcp_port=healthcheck_tcp_port,
        healthcheck_command=healthcheck_command,
    )

    location = create_service_folder(os.path.join(services_root, config["service_name"]))
    compose_file = generate_compose_file(config, location)

    try:
        deploy_service(location, timeout_seconds=deploy_timeout_seconds)
    except DeployTimeoutError:
        # Roll back so state stays clean for a retry; docker keeps the
        # already-downloaded image layers cached, so the retry pulls much less.
        try:
            remove_service(location, remove_volumes=True)
        except ServiceError:
            pass
        shutil.rmtree(location, ignore_errors=True)
        return {
            "action": "deploy_timeout",
            "service_name": config["service_name"],
            "image": config["image"],
            "deploy_timeout_seconds": deploy_timeout_seconds,
            "message": (
                f"'docker compose up' did not finish within {deploy_timeout_seconds}s "
                "(usually a slow image pull). The install was rolled back — nothing "
                "is deployed. Downloaded layers are cached by docker, so calling "
                "create_service again with the same arguments resumes the pull and "
                "should be faster."
            ),
        }

    time.sleep(startup_wait_seconds)
    is_running, containers, logs = verify_service_status(location)

    check = healthcheck_from_config(config)
    save_healthcheck(location, check)
    if check is None:
        readiness = {
            "ready": None,
            "ready_via": "container_state",
            "readiness_detail": (
                "No healthcheck configured; 'running' reflects container state only."
            ),
        }
    else:
        readiness = verify_readiness(
            location, check, timeout_seconds=readiness_timeout_seconds
        )

    return {
        "action": "installed",
        "service_name": config["service_name"],
        "image": config["image"],
        "host_port": config["port"],
        "container_port": config["container_port"],
        "urls": [f"http://localhost:{config['port']}"],
        "location": location,
        "compose_file": compose_file,
        "running": is_running,
        **readiness,
        "containers": containers,
        "logs_tail": logs,
    }
