"""IngestionService — file reading and sandboxed Python execution.

Handles CSV/TSV/text file ingestion and Python sandbox execution for
batch transaction parsing. No LLM dependency.
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

from .types import FileReadResult, SandboxResult

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 2 * 1024 * 1024
_SANDBOX_TIMEOUT = 60
_STAGING_DIR = "/tmp/ledger_staging"


class IngestionServiceError(Exception):
    """Unrecoverable ingestion error."""


class IngestionService:
    """File reading and Python sandbox for batch transaction processing."""

    @staticmethod
    def _staging_path(label: str) -> str:
        os.makedirs(_STAGING_DIR, exist_ok=True)
        uid = uuid.uuid4().hex[:8]
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", label)[:40]
        return os.path.join(_STAGING_DIR, f"{safe}_{uid}.beancount")

    @staticmethod
    def read_file(file_path: str) -> FileReadResult:
        path = os.path.expanduser(file_path)
        if not os.path.exists(path):
            return FileReadResult(
                status="ERROR", error=f"File not found: {file_path}",
            )

        size = os.path.getsize(path)
        if size > _MAX_FILE_BYTES:
            return FileReadResult(
                status="ERROR",
                error=f"File too large ({size:,} bytes). Max {_MAX_FILE_BYTES:,}.",
            )

        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            return FileReadResult(status="ERROR", error=str(e))

        lines = (
            content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        )
        return FileReadResult(
            status="SUCCESS",
            file_path=file_path,
            size_bytes=size,
            lines=lines,
            content=content,
        )

    @staticmethod
    def run_python(
        code: str,
        input_files: list[str] | None = None,
        stage: bool = False,
        stage_label: str = "import",
    ) -> SandboxResult:
        tmpdir = tempfile.mkdtemp(prefix="ledger_sandbox_")
        try:
            if input_files:
                for fpath in input_files:
                    fpath = os.path.expanduser(fpath)
                    if not os.path.exists(fpath):
                        return SandboxResult(
                            status="ERROR",
                            error=f"Input file not found: {fpath}",
                        )
                    dest = os.path.join(tmpdir, os.path.basename(fpath))
                    shutil.copy2(fpath, dest)

            script_path = os.path.join(tmpdir, "_script.py")
            with open(script_path, "w") as f:
                f.write(code)

            proc = subprocess.run(
                [sys.executable, "_script.py"],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=_SANDBOX_TIMEOUT,
            )

            stdout = proc.stdout
            stderr = proc.stderr[:4096] if proc.stderr else ""

            if proc.returncode != 0:
                return SandboxResult(
                    status="ERROR",
                    exit_code=proc.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    error=stderr.strip() or f"Script exited with status {proc.returncode}.",
                )

            if not stage:
                max_out = 200 * 1024
                if len(stdout) > max_out:
                    stdout = stdout[:max_out] + "\n... [truncated]"
                return SandboxResult(
                    status="SUCCESS",
                    exit_code=proc.returncode,
                    stdout=stdout,
                    stderr=stderr,
                )

            staging_file = IngestionService._staging_path(stage_label)
            with open(staging_file, "w", encoding="utf-8") as f:
                f.write(stdout)

            txn_lines = [
                line for line in stdout.splitlines()
                if re.match(r"^\d{4}-\d{2}-\d{2}\s+[*!]", line)
            ]
            txn_count = len(txn_lines)
            sample = "\n".join(txn_lines[:5])
            if txn_count > 5:
                sample += f"\n... ({txn_count - 5} more)"

            return SandboxResult(
                status="SUCCESS",
                exit_code=proc.returncode,
                stderr=stderr,
                staging_file=staging_file,
                transaction_count=txn_count,
                sample=sample,
            )

        except subprocess.TimeoutExpired:
            return SandboxResult(
                status="ERROR",
                error=f"Script exceeded {_SANDBOX_TIMEOUT}s timeout.",
            )
        except Exception as e:
            logger.exception("Sandbox error")
            return SandboxResult(status="ERROR", error=str(e))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
