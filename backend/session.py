import asyncio
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_DIR = Path("sessions")

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Per-session locks to serialise concurrent state mutations
_session_locks: dict[str, asyncio.Lock] = {}


def _get_lock(session_id: str) -> asyncio.Lock:
    """Return (or create) the asyncio.Lock for *session_id*."""
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


def _atomic_write(path: Path, data: dict) -> None:
    """Write *data* as JSON to *path* atomically (write-tmp-then-rename)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _validate_session_id(session_id: str) -> None:
    """Raise ValueError if session_id contains path-traversal or invalid characters."""
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"Invalid session id: {session_id!r}")


def create_session() -> str:
    session_id = uuid.uuid4().hex[:12]
    session_dir = SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "status": "created",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "iterations": 0,
    }
    _atomic_write(session_dir / "state.json", state)
    return session_id


def get_session_dir(session_id: str) -> Path:
    _validate_session_id(session_id)
    return SESSIONS_DIR / session_id


def get_state(session_id: str) -> dict:
    _validate_session_id(session_id)
    path = get_session_dir(session_id) / "state.json"
    if not path.exists():
        raise FileNotFoundError(f"Session {session_id} not found")
    return json.loads(path.read_text())


async def update_state(session_id: str, updates: dict) -> None:
    _validate_session_id(session_id)
    async with _get_lock(session_id):
        state = get_state(session_id)
        state.update(updates)
        path = get_session_dir(session_id) / "state.json"
        _atomic_write(path, state)


def get_room_data(session_id: str) -> dict:
    _validate_session_id(session_id)
    path = get_session_dir(session_id) / "room_data.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


async def save_room_data(session_id: str, room_data: dict) -> None:
    _validate_session_id(session_id)
    async with _get_lock(session_id):
        path = get_session_dir(session_id) / "room_data.json"
        _atomic_write(path, room_data)


async def modify_room_data(session_id: str, modifier, *args) -> dict:
    """Atomically read room_data, apply modifier(room_data, *args), save, and return result.

    This avoids the race condition where two concurrent requests both read stale data.
    The modifier function should return the new room_data dict.
    """
    _validate_session_id(session_id)
    async with _get_lock(session_id):
        room_data = get_room_data(session_id)
        if not room_data:
            raise ValueError("No scene data yet — upload a photo first")
        new_room_data = modifier(room_data, *args)
        path = get_session_dir(session_id) / "room_data.json"
        _atomic_write(path, new_room_data)
        return new_room_data


def get_render_path(session_id: str) -> Path:
    _validate_session_id(session_id)
    return get_session_dir(session_id) / "render_current.png"


def get_depth_path(session_id: str) -> Path:
    _validate_session_id(session_id)
    return get_session_dir(session_id) / "render_depth.png"


def get_preview_path(session_id: str) -> Path:
    return get_session_dir(session_id) / "preview_current.png"


def archive_render(session_id: str) -> None:
    state = get_state(session_id)
    iteration = state.get("iterations", 0)
    current = get_render_path(session_id)
    if current.exists():
        archive = get_session_dir(session_id) / f"render_{iteration:03d}.png"
        current.rename(archive)
