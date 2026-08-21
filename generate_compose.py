"""
Script to generate and run docker-compose services based on user input.
"""

import shutil
import os
import socket
import yaml
import subprocess
import time
from typing import Any, Dict, Optional, List, Tuple


def check_prerequisites() -> bool:
    """Check if docker and docker compose are installed."""
    if not shutil.which('docker'):
        return False
    try:
        # Check if 'docker compose' (plugin) is available
        subprocess.run(['docker', 'compose', 'version'], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def check_port_available(port: int) -> bool:
    """Check if a port is available on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) != 0

def find_available_port(start: int = 8000, end: int = 9000) -> int:
    """Find an available port in the specified range."""
    for port in range(start, end + 1):
        if check_port_available(port):
            return port
    raise OSError(f"No available ports in range {start}-{end}")

def check_inputs(
    image: str,
    service_name: Optional[str] = None,
    port: Optional[int] = None,
    env_vars: Optional[Dict[str, str]] = None,
    location: Optional[str] = None,
    volumes: Optional[List[str]] = None,
    healthcheck_path: Optional[str] = None
) -> Dict[str, Any]:
    """Validate and prepare inputs for service generation."""
    if not image:
        raise ValueError("Image name or URL is required")
    
    if not service_name:
        # Generate default name based on image name
        base_name = image.split('/')[-1].split(':')[0]
        service_name = base_name.replace('.', '-').replace('_', '-')
    
    if port is None:
        port = find_available_port()
    
    if not location:
        location = os.path.join(os.getcwd(), service_name)
    
    return {
        "image": image,
        "service_name": service_name,
        "port": port,
        "env_vars": env_vars or {},
        "location": location,
        "volumes": volumes or [],
        "healthcheck_path": healthcheck_path
    }

def create_service_folder(location: str):
    """Create the directory for the service."""
    os.makedirs(location, exist_ok=True)
    print(f"Created/verified directory: {location}")

def generate_compose_file(config: Dict[str, Any]) -> str:
    """Generate the docker-compose.yaml file."""
    service_config: Dict[str, Any] = {
        'image': config['image'],
        'ports': [f"{config['port']}:{config['port']}"],
        'environment': [f"{k}={v}" for k, v in config['env_vars'].items()],
        'restart': 'always'
    }
    
    if config['volumes']:
        service_config['volumes'] = config['volumes']
        
    if config.get('healthcheck_path'):
        service_config['healthcheck'] = {
            'test': ["CMD", "curl", "-f", f"http://localhost:{config['port']}{config['healthcheck_path']}"],
            'interval': '30s',
            'timeout': '10s',
            'retries': 3,
            'start_period': '10s'
        }
    
    compose_content: Dict[str, Any] = {
        'version': '3.8',
        'services': {
            config['service_name']: service_config
        }
    }
    
    filepath = os.path.join(config['location'], 'docker-compose.yaml')
    with open(filepath, 'w') as f:
        yaml.dump(compose_content, f, default_flow_style=False, sort_keys=False)
    
    print(f"Generated docker-compose.yaml at {filepath}")
    return filepath

def run_docker_compose(location: str) -> Tuple[bool, str]:
    """Run docker-compose up -d in the service directory."""
    try:
        result = subprocess.run(
            ['docker', 'compose', 'up', '-d'],
            cwd=location,
            check=True,
            capture_output=True,
            text=True
        )
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr

def verify_service_status(location: str, service_name: str) -> Tuple[bool, str]:
    """Verify if the service is running and get logs."""
    try:
        ps_result = subprocess.run(
            ['docker', 'compose', 'ps', '--format', 'json'],
            cwd=location,
            check=True,
            capture_output=True,
            text=True
        )
        
        is_running = "running" in ps_result.stdout.lower() or "up" in ps_result.stdout.lower()
        
        logs_result = subprocess.run(
            ['docker', 'compose', 'logs', '--tail', '50'],
            cwd=location,
            check=True,
            capture_output=True,
            text=True
        )
        
        return is_running, logs_result.stdout
    except subprocess.CalledProcessError as e:
        return False, f"Error verifying service: {e.stderr}"

def main():
    import sys
    import json

    if len(sys.argv) > 1:
        try:
            user_input = json.loads(sys.argv[1])
        except json.JSONDecodeError:
            print("Invalid JSON input")
            sys.exit(1)
    else:
        user_input: Dict[str, Any] = {
            "image": "nginx:alpine",
            "env_vars": {"DEBUG": "true"}
        }

    if not check_prerequisites():
        print("Error: Docker or Docker Compose not found.")
        sys.exit(1)

    try:
        config = check_inputs(**user_input)
        create_service_folder(config['location'])
        generate_compose_file(config)
        
        print(f"Starting service {config['service_name']}...")
        success, output = run_docker_compose(config['location'])
        
        if success:
            print("Service started successfully. Waiting for initialization...")
            time.sleep(5)
            running, logs = verify_service_status(config['location'], config['service_name'])
            if running:
                print(f"Service is RUNNING on port {config['port']}")
            else:
                print("Service is NOT running as expected.")
                print("Logs:")
                print(logs)
        else:
            print(f"Failed to start service: {output}")

    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
