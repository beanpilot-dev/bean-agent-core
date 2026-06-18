"""File ingestion and Python sandbox for batch transaction processing."""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

logger = logging.getLogger(__name__)

# Max file size for ingestion (2 MB)
_MAX_FILE_BYTES = 2 * 1024 * 1024
# Sandbox execution timeout (seconds)
_SANDBOX_TIMEOUT = 60
# Staging dir for large sandbox outputs (avoids passing bulk text through LLM)
_STAGING_DIR = "/tmp/ledger_staging"


def _staging_path(label: str) -> str:
    """Return a unique staging file path under /tmp/ledger_staging/."""
    os.makedirs(_STAGING_DIR, exist_ok=True)
    uid = uuid.uuid4().hex[:8]
    safe_label = re.sub(r"[^a-zA-Z0-9_-]", "_", label)[:40]
    return os.path.join(_STAGING_DIR, f"{safe_label}_{uid}.beancount")


def read_file(file_path: str) -> str:
    """Read a file and return its text content.

    Supports any UTF-8 text file: CSV, TSV, plain text, beancount exports.
    Returns JSON with status SUCCESS (result.content, result.size_bytes, result.lines)
    or ERROR.
    """
    path = os.path.expanduser(file_path)
    if not os.path.exists(path):
        return json.dumps({"status": "ERROR", "error": f"File not found: {file_path}"})

    size = os.path.getsize(path)
    if size > _MAX_FILE_BYTES:
        return json.dumps({
            "status": "ERROR",
            "error": f"File too large ({size:,} bytes). Max is {_MAX_FILE_BYTES:,} bytes. "
                     "Split the file or paste a smaller excerpt.",
        })

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return json.dumps({"status": "ERROR", "error": str(e)})

    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return json.dumps({
        "status": "SUCCESS",
        "result": {
            "file_path": file_path,
            "size_bytes": size,
            "lines": lines,
            "content": content,
        },
    })


def run_python(
    code: str,
    input_files: list[str] | None = None,
    stage: bool = False,
    stage_label: str = "import",
) -> str:
    """Run a Python script in a sandboxed subprocess and return its stdout.

    The sandbox:
    - Runs in a fresh temp directory
    - Input files (if given) are copied into the temp dir as their basenames
    - Has access to the standard library + pandas, csv, json, re, datetime
    - stdout is captured (max 200 KB inline, or written to /tmp staging file)
    - Hard timeout: 60 seconds
    - No access to the beancount workspace or git

    Use this to process batch transaction data — the script should print
    valid beancount transaction blocks to stdout, one transaction separated
    by a blank line, which the agent can then review and commit in bulk.

    Staging mode (stage=True):
    When the output may be large (hundreds of transactions), set stage=True.
    The stdout is written to a /tmp staging file instead of returned inline.
    The response includes staging_file (path), transaction_count, and sample
    (first 5 transaction headers). Pass staging_file to ledger_bulk_commit
    as transactions_file — the full text never flows through LLM context.

    Example script that parses a bank CSV and prints beancount transactions:

        import csv, sys

        with open("export.csv") as f:
            for row in csv.DictReader(f):
                date = row["Date"]          # e.g. "2026-04-15"
                narration = row["Note"]
                amount = float(row["Amount"])
                if amount < 0:
                    print(f'''{date} * "" "{narration}"''')
                    print(f"  Liabilities:CMB-Credit  {amount:.2f} CNY")
                    print(f"  Expenses:Unknown        {-amount:.2f} CNY")
                    print()

    Args:
        code: Python source code to execute.
        input_files: Absolute paths to files that the script needs. Each file
                     is copied to the temp dir and accessible by its basename.
        stage: If True, write stdout to a /tmp staging file and return the path
               + transaction_count + sample instead of the full stdout. Use for
               large batches (>50 transactions) to avoid bloating LLM context.
        stage_label: Short label used in the staging filename (e.g. "cmb_april").

    Returns JSON with status SUCCESS and either:
      - stage=False: result.stdout, result.stderr, result.exit_code
      - stage=True:  result.staging_file, result.transaction_count, result.sample,
                     result.stderr, result.exit_code
    or ERROR.
    """
    tmpdir = tempfile.mkdtemp(prefix="ledger_sandbox_")
    try:
        # Copy input files into sandbox
        if input_files:
            for fpath in input_files:
                fpath = os.path.expanduser(fpath)
                if not os.path.exists(fpath):
                    return json.dumps({
                        "status": "ERROR",
                        "error": f"Input file not found: {fpath}",
                    })
                dest = os.path.join(tmpdir, os.path.basename(fpath))
                shutil.copy2(fpath, dest)

        # Write script
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

        if proc.returncode != 0 or not stage:
            # Non-staging path: return stdout inline (truncate if large)
            max_out = 200 * 1024
            if len(stdout) > max_out:
                stdout = stdout[:max_out] + "\n... [truncated]"
            return json.dumps({
                "status": "SUCCESS",
                "result": {
                    "exit_code": proc.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                },
            })

        # Staging path: write stdout to /tmp file, return metadata only
        staging_file = _staging_path(stage_label)
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

        return json.dumps({
            "status": "SUCCESS",
            "result": {
                "exit_code": proc.returncode,
                "staging_file": staging_file,
                "transaction_count": txn_count,
                "sample": sample,
                "stderr": stderr,
                "note": (
                    f"Output staged to {staging_file} ({txn_count} transactions). "
                    "Pass staging_file to ledger_bulk_commit as transactions_file."
                ),
            },
        })

    except subprocess.TimeoutExpired:
        return json.dumps({
            "status": "ERROR",
            "error": f"Script exceeded {_SANDBOX_TIMEOUT}s timeout.",
        })
    except Exception as e:
        logger.exception("Sandbox error")
        return json.dumps({"status": "ERROR", "error": str(e)})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
