"""Fixture lifecycle management — copy, start agent-core, cleanup."""

import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

AGENT_CORE_MIN_PORT = 18001
AGENT_CORE_MAX_PORT = 18100
AGENT_CORE_READY_TIMEOUT = 30


def _find_free_port() -> int:
    import socket

    for port in range(AGENT_CORE_MIN_PORT, AGENT_CORE_MAX_PORT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("No free port found in range")


def copy_fixture(fixture_dir: Path) -> Path:
    """Copy a fixture directory to a temp location and init as a git repo."""
    temp_dir = Path(tempfile.mkdtemp(prefix="beanbench-fixture-"))
    shutil.copytree(fixture_dir, temp_dir, dirs_exist_ok=True, symlinks=False)
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(temp_dir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=str(temp_dir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "beanbench fixture init"],
        cwd=str(temp_dir),
        check=True,
        capture_output=True,
    )
    return temp_dir


def _patch_env_local(agent_core_dir: Path, openai_base_url: str | None, langfuse_enabled: bool = False) -> str:
    """Rewrite agent-core/.env.local to set AGENT_MODE=local.

    Returns the original content for later restoration.
    """
    env_local = agent_core_dir / ".env.local"
    if env_local.exists():
        original = env_local.read_text(encoding="utf-8")
    else:
        original = ""

    lines = []
    for line in original.splitlines():
        if any(line.startswith(p) for p in ("AGENT_MODE=", "OPENAI_MODEL=", "OPENAI_BASE_URL=")):
            continue
        if not langfuse_enabled and line.startswith("LANGFUSE"):
            continue
        lines.append(line)
    lines.append("AGENT_MODE=local")
    if openai_base_url:
        lines.append(f"OPENAI_BASE_URL={openai_base_url}")
    if not langfuse_enabled:
        lines.append("LANGFUSE_ENABLED=false")
    env_local.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return original


def _restore_env_local(agent_core_dir: Path, original: str) -> None:
    """Restore the original .env.local content."""
    env_local = agent_core_dir / ".env.local"
    if original:
        env_local.write_text(original, encoding="utf-8")
    elif env_local.exists():
        env_local.unlink()


class _EnvLocalGuard:
    """Context manager that patches .env.local and restores on exit."""

    def __init__(self, agent_core_dir: Path, openai_base_url: str | None, langfuse_enabled: bool = False):
        self._agent_core_dir = agent_core_dir
        self._original = ""
        self._openai_base_url = openai_base_url
        self._langfuse_enabled = langfuse_enabled

    def __enter__(self):
        self._original = _patch_env_local(self._agent_core_dir, self._openai_base_url, self._langfuse_enabled)
        return self

    def __exit__(self, *args):
        _restore_env_local(self._agent_core_dir, self._original)


def start_agent_core(
    fixture_path: Path,
    port: int,
    env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Start agent-core in local mode pointing at the given fixture.

    Caller must hold an active _EnvLocalGuard to ensure AGENT_MODE=local
    is set in .env.local during startup.
    """
    agent_core_dir = Path(__file__).resolve().parent.parent.parent.parent
    process_env = os.environ.copy()
    process_env["LOCAL_REPO_URL"] = str(fixture_path)
    if env:
        process_env.update(env)

    proc = subprocess.Popen(
        [
            "python",
            "-m",
            "agent_core.main",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(agent_core_dir),
        env=process_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    return proc


def wait_ready(port: int, timeout: int = AGENT_CORE_READY_TIMEOUT) -> bool:
    """Poll /health until agent-core responds with 200."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def stop_agent_core(proc: subprocess.Popen) -> None:
    """Gracefully stop the agent-core subprocess."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=2)
    except Exception:
        pass


@contextlib.contextmanager
def fixture_context(
    fixture_dir: Path,
    env: dict[str, str] | None = None,
    langfuse_enabled: bool = False,
):
    """Context manager: copy fixture, patch .env.local, start agent-core, yield (port, temp_path), cleanup."""
    port = _find_free_port()
    temp_path = copy_fixture(fixture_dir)
    agent_core_dir = Path(__file__).resolve().parent.parent.parent.parent
    openai_base_url = (env or {}).get("OPENAI_BASE_URL")
    proc = None
    try:
        with _EnvLocalGuard(agent_core_dir, openai_base_url, langfuse_enabled):
            proc = start_agent_core(temp_path, port, env)
            if not wait_ready(port):
                stop_agent_core(proc)
                raise RuntimeError(
                    f"Agent-core failed to become ready on port {port} within {AGENT_CORE_READY_TIMEOUT}s"
                )
        yield (port, temp_path)
    finally:
        if proc is not None:
            stop_agent_core(proc)
        shutil.rmtree(temp_path, ignore_errors=True)
