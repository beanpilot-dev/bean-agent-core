"""Beancount CLI utilities: binary resolution, validation, and ad-hoc BQL execution."""

import csv
import io
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)


def _bean_bin(workspace: str, name: str) -> str:
    """Resolve the path to a beancount CLI binary (bean-check, bean-query, etc.).

    Search order:
    1. Workspace-local venv (legacy: personal-accounting had its own .venv)
    2. Same venv as the running Python (beancount installed via requirements.txt)
    3. PATH fallback
    """
    candidate = os.path.join(workspace, ".venv", "bin", name)
    if os.path.exists(candidate):
        return candidate
    candidate = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.exists(candidate):
        return candidate
    return name


def bean_format(workspace: str, file_path: str) -> None:
    """Run bean-format on a file and write the result back in-place.

    Silently ignores failures — formatting is best-effort; bean-check is the hard gate.
    """
    result = subprocess.run(
        [_bean_bin(workspace, "bean-format"), file_path],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout:
        with open(file_path, "w") as f:
            f.write(result.stdout)
    elif result.returncode != 0:
        logger.warning("bean-format failed on %s: %s", file_path, result.stderr.strip())


def bean_check(workspace: str) -> tuple[bool, str]:
    """Run bean-check on main.beancount. Returns (is_clean, output)."""
    main = os.path.join(workspace, "data", "main.beancount")
    result = subprocess.run(
        [_bean_bin(workspace, "bean-check"), main],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0, result.stdout + result.stderr


def run_bql(workspace: str, bql: str) -> tuple[int, str, str]:
    """Run an ad-hoc BQL query with CSV output. Returns (returncode, stdout, stderr)."""
    main = os.path.join(workspace, "data", "main.beancount")
    result = subprocess.run(
        [_bean_bin(workspace, "bean-query"), "-f", "csv", main, bql],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def run_bql_rows(workspace: str, bql: str) -> tuple[list[dict], str | None]:
    """Run a BQL query and return (rows_as_dicts, error_or_None).

    Parses CSV output into a list of dicts keyed by column header.
    Returns ([], error_message) on failure.
    """
    rc, stdout, stderr = run_bql(workspace, bql)
    if rc != 0:
        return [], stderr.strip()
    rows = list(csv.DictReader(io.StringIO(stdout)))
    return [{k: v.strip() for k, v in row.items()} for row in rows], None
