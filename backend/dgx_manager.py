"""
DGX Spark service management.

Status checks use lightweight HTTP health endpoints (no SSH needed).
Start/stop still require SSH access to the DGX host.
"""

import asyncio
import json
import os

import httpx

DGX_HOST = os.environ.get("DGX_HOST", "192.168.0.200")

SERVICES: dict[str, dict] = {
    "flux":     {"path": "~/Projects/flux-render",     "port": 8001},
    "chord":    {"path": "~/Projects/chord-material",  "port": 8002},
    "trellis":  {"path": "~/Projects/trellis",         "port": 8003},
    "splatter": {"path": "~/Projects/dn-splatter",     "port": 8004},
    "blender":  {"path": "~/Projects/blender-render",  "port": 8005},
    "depth":    {"path": "~/Projects/depth-estimation","port": 8006},
}


# ── Health check via HTTP (lightweight, no SSH) ─────────────────────────────

async def get_service_status(name: str) -> dict:
    """Check service health via HTTP /health endpoint."""
    svc = SERVICES.get(name)
    if not svc:
        return {"name": name, "status": "unknown", "port": None}

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"http://{DGX_HOST}:{svc['port']}/health")
            if resp.status_code == 200:
                return {"name": name, "status": "running", "port": svc["port"]}
    except Exception:
        pass
    return {"name": name, "status": "stopped", "port": svc["port"]}


async def get_all_statuses() -> list[dict]:
    """Check status of all known services in parallel via HTTP."""
    tasks = [get_service_status(name) for name in SERVICES]
    return list(await asyncio.gather(*tasks))


# ── Start/stop via SSH (requires SSH key access) ────────────────────────────

async def _run_ssh(cmd: str, timeout: float = 30) -> tuple[int, str, str]:
    """Run a command on the DGX host via SSH and return (returncode, stdout, stderr)."""
    full = (
        f'ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no '
        f'-o BatchMode=yes luok@{DGX_HOST} "{cmd}"'
    )
    proc = await asyncio.create_subprocess_shell(
        full,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", "timeout"
    return proc.returncode, stdout.decode(), stderr.decode()


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
