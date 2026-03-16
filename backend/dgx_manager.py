"""
DGX Spark container management — start/stop Docker Compose services over SSH.
"""

import asyncio
import json
import os

DGX_HOST = os.environ.get("DGX_HOST", "192.168.0.200")

SERVICES: dict[str, dict] = {
    "flux":     {"path": "~/Projects/flux-render",     "port": 8001},
    "chord":    {"path": "~/Projects/chord-material",  "port": 8002},
    "trellis":  {"path": "~/Projects/trellis",         "port": 8003},
    "splatter": {"path": "~/Projects/dn-splatter",     "port": 8004},
    "blender":  {"path": "~/Projects/blender-render",  "port": 8005},
}


async def _run_ssh(cmd: str, timeout: float = 30) -> tuple[int, str, str]:
    """Run a command on the DGX host via SSH and return (returncode, stdout, stderr)."""
    full = f'ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no luok@{DGX_HOST} "{cmd}"'
    proc = await asyncio.create_subprocess_shell(
        full,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "timeout"
    return proc.returncode, stdout.decode(), stderr.decode()


async def get_service_status(name: str) -> dict:
    """Check whether a single service's containers are running."""
    svc = SERVICES.get(name)
    if not svc:
        return {"name": name, "status": "unknown", "port": None}

    cmd = f"cd {svc['path']} && docker compose ps --format json"
    rc, stdout, stderr = await _run_ssh(cmd)

    if rc != 0:
        return {"name": name, "status": "stopped", "port": svc["port"]}

    # docker compose ps --format json outputs one JSON object per line
    status = "stopped"
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            state = obj.get("State", "").lower()
            if state == "running":
                status = "running"
                break
        except json.JSONDecodeError:
            continue

    return {"name": name, "status": status, "port": svc["port"]}


async def get_all_statuses() -> list[dict]:
    """Check status of all known services in parallel."""
    tasks = [get_service_status(name) for name in SERVICES]
    return list(await asyncio.gather(*tasks))


async def start_service(name: str) -> dict:
    """Start a service via docker compose up -d."""
    svc = SERVICES[name]
    cmd = f"cd {svc['path']} && docker compose up -d"
    rc, stdout, stderr = await _run_ssh(cmd, timeout=120)
    return {
        "name": name,
        "ok": rc == 0,
        "detail": stdout.strip() or stderr.strip(),
    }


async def stop_service(name: str) -> dict:
    """Stop a service via docker compose down."""
    svc = SERVICES[name]
    cmd = f"cd {svc['path']} && docker compose down"
    rc, stdout, stderr = await _run_ssh(cmd, timeout=60)
    return {
        "name": name,
        "ok": rc == 0,
        "detail": stdout.strip() or stderr.strip(),
    }
