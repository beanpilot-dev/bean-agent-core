"""Parser-backed transaction identity, lookup, and source extraction.

The index deliberately retains transaction facts and the exact directive bytes,
but never retains a complete source file.  Both search and mutation lookup use
this module so they cannot disagree about where a directive ends.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import PurePosixPath

from .beancount import Beancount, LedgerServiceError
from .types import LedgerConfig

_REFERENCE_PREFIX = "txn_v1_"
_REFERENCE_RE = re.compile(r"^txn_v1_([A-Za-z0-9_-]+)_([0-9a-f]{16})$")
_RESERVED_METADATA = {"filename", "lineno"}
_NATIVE_ACCOUNT_ROOTS = {"Assets", "Liabilities", "Equity", "Income", "Expenses"}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(value: object) -> object:
    """Convert parser values to bounded JSON-safe values."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    return str(value)


def _user_metadata(meta: object) -> dict[str, object]:
    if not isinstance(meta, dict):
        return {}
    return {
        str(key): _json_value(value)
        for key, value in meta.items()
        if key not in _RESERVED_METADATA and not str(key).startswith("__")
    }


def _units(units: object) -> dict[str, str] | None:
    number = getattr(units, "number", None)
    currency = getattr(units, "currency", None)
    if number is None or not isinstance(currency, str):
        return None
    return {"number": str(number), "currency": currency}


def _posting_facts(posting: object, include_metadata: bool) -> dict[str, object]:
    facts: dict[str, object] = {"account": str(getattr(posting, "account", ""))}
    units = _units(getattr(posting, "units", None))
    if units is not None:
        facts["units"] = units
    flag = getattr(posting, "flag", None)
    if flag is not None:
        facts["flag"] = str(flag)
    for field_name in ("cost", "price"):
        value = getattr(posting, field_name, None)
        if value is not None:
            facts[field_name] = str(value)
    if include_metadata:
        metadata = _user_metadata(getattr(posting, "meta", None))
        if metadata:
            facts["metadata"] = metadata
    return facts


def _directive_facts(entry: object, include_metadata: bool = True) -> dict[str, object]:
    return {
        "date": getattr(entry, "date").isoformat(),
        "flag": str(getattr(entry, "flag", "")),
        "payee": getattr(entry, "payee", None),
        "narration": getattr(entry, "narration", None),
        "tags": sorted(str(tag) for tag in (getattr(entry, "tags", None) or ())),
        "links": sorted(str(link) for link in (getattr(entry, "links", None) or ())),
        "metadata": _user_metadata(getattr(entry, "meta", None)) if include_metadata else {},
        "postings": [
            _posting_facts(posting, include_metadata)
            for posting in (getattr(entry, "postings", None) or ())
        ],
    }


def _normalize_source_path(workspace: str, filename: object) -> str | None:
    if not isinstance(filename, str):
        return None
    workspace_abs = os.path.realpath(workspace)
    filename_abs = os.path.realpath(
        filename if os.path.isabs(filename) else os.path.join(workspace, filename)
    )
    relative = os.path.relpath(filename_abs, workspace_abs)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return None
    return PurePosixPath(relative).as_posix()


def _source_bytes(workspace: str, relative_path: str) -> bytes:
    path = os.path.join(workspace, *PurePosixPath(relative_path).parts)
    with open(path, "rb") as source:
        return source.read()


def _extract_directive(source: bytes, start_line: int) -> tuple[bytes, int, int]:
    """Extract one directive from its parser line without normalizing bytes.

    A directive starts at its parser-provided header line and continues through
    contiguous indented lines.  Blank lines and unindented comments belong to
    surrounding source, not to the directive.  This covers metadata, comments
    inside a transaction, and multi-line postings while keeping adjacent
    directives separate.
    """
    lines = source.splitlines(keepends=True)
    start = start_line - 1
    if start < 0 or start >= len(lines):
        raise LedgerServiceError("Transaction source location is unavailable")
    end = start
    for index in range(start + 1, len(lines)):
        body = lines[index].rstrip(b"\r\n")
        if not body.strip():
            break
        if body.startswith((b" ", b"\t")):
            end = index
            continue
        break
    return b"".join(lines[start : end + 1]), start_line, end + 1


def _reference_payload(
    relative_path: str, start_line: int, occurrence: int, directive_identity: str
) -> dict[str, object]:
    return {
        "version": 1,
        "path": relative_path,
        "start_line": start_line,
        "occurrence": occurrence,
        "directive_identity": directive_identity,
    }


def mint_transaction_ref(payload: dict[str, object]) -> str:
    canonical = _canonical_json(payload).encode("utf-8")
    encoded = base64.urlsafe_b64encode(canonical).decode("ascii").rstrip("=")
    checksum = hashlib.sha256(canonical).hexdigest()[:16]
    return f"{_REFERENCE_PREFIX}{encoded}_{checksum}"


def parse_transaction_ref(transaction_ref: str) -> dict[str, object] | None:
    """Validate and decode the documented opaque reference grammar."""
    if not isinstance(transaction_ref, str):
        return None
    match = _REFERENCE_RE.fullmatch(transaction_ref)
    if match is None:
        return None
    encoded, checksum = match.groups()
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        return None
    if not isinstance(payload, dict):
        return None
    if hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:16] != checksum:
        return None
    if (
        payload.get("version") != 1
        or not isinstance(payload.get("path"), str)
        or not isinstance(payload.get("start_line"), int)
        or not isinstance(payload.get("occurrence"), int)
        or not isinstance(payload.get("directive_identity"), str)
        or payload["start_line"] < 1
        or payload["occurrence"] < 1
        or PurePosixPath(payload["path"]).is_absolute()
        or ".." in PurePosixPath(payload["path"]).parts
    ):
        return None
    return payload


@dataclass(frozen=True)
class IndexedTransaction:
    """Facts retained for one parsed transaction, never its containing file."""

    transaction_ref: str
    relative_path: str
    start_line: int
    end_line: int
    occurrence: int
    directive: str
    revision_fingerprint: str
    facts: dict[str, object]
    directive_identity: str

    def summary(self) -> dict[str, object]:
        return {
            "transaction_ref": self.transaction_ref,
            "date": self.facts["date"],
            "payee": self.facts["payee"],
            "narration": self.facts["narration"],
            "postings": [
                {
                    "account": posting["account"],
                    "amount": " ".join(
                        (posting.get("units") or {}).get(key, "") for key in ("number", "currency")
                    ).strip(),
                }
                for posting in self.facts["postings"]
            ],
        }

    def detail(self) -> dict[str, object]:
        return {
            "transaction_ref": self.transaction_ref,
            "directive": self.directive,
            "source_path": self.relative_path,
            "source_start_line": self.start_line,
            "source_end_line": self.end_line,
            "payee": self.facts["payee"],
            "narration": self.facts["narration"],
            "tags": self.facts["tags"],
            "links": self.facts["links"],
            "metadata": self.facts["metadata"],
            "postings": self.facts["postings"],
            "revision_fingerprint": self.revision_fingerprint,
        }


class TransactionIndex:
    """Build and resolve the current parser-backed transaction index."""

    def __init__(self, transactions: list[IndexedTransaction]) -> None:
        self.transactions = transactions
        self._by_ref: dict[str, list[IndexedTransaction]] = {}
        for transaction in transactions:
            self._by_ref.setdefault(transaction.transaction_ref, []).append(transaction)

    @classmethod
    def build(
        cls, workspace: str, ledger_config: LedgerConfig | None = None
    ) -> "TransactionIndex":
        parsed = Beancount.parsed_ledger(workspace, ledger_config)
        if parsed.errors:
            raise LedgerServiceError("Ledger could not be parsed")
        file_cache: dict[str, bytes] = {}
        occurrences: dict[str, int] = {}
        transactions: list[IndexedTransaction] = []
        for entry in parsed.entries:
            if entry.__class__.__name__ != "Transaction":
                continue
            meta = getattr(entry, "meta", {})
            relative_path = _normalize_source_path(workspace, meta.get("filename"))
            start_line = meta.get("lineno")
            if relative_path is None or not isinstance(start_line, int):
                continue
            if relative_path not in file_cache:
                file_cache[relative_path] = _source_bytes(workspace, relative_path)
            directive_bytes, source_start, source_end = _extract_directive(
                file_cache[relative_path], start_line
            )
            facts = _directive_facts(entry)
            semantic = _directive_facts(entry, include_metadata=True)
            directive_identity = hashlib.sha256(
                _canonical_json(semantic).encode("utf-8")
            ).hexdigest()
            occurrences[relative_path] = occurrences.get(relative_path, 0) + 1
            occurrence = occurrences[relative_path]
            payload = _reference_payload(
                relative_path, source_start, occurrence, directive_identity
            )
            transactions.append(
                IndexedTransaction(
                    transaction_ref=mint_transaction_ref(payload),
                    relative_path=relative_path,
                    start_line=source_start,
                    end_line=source_end,
                    occurrence=occurrence,
                    directive=directive_bytes.decode("utf-8"),
                    revision_fingerprint="sha256:" + hashlib.sha256(directive_bytes).hexdigest(),
                    facts=facts,
                    directive_identity=directive_identity,
                )
            )
        return cls(transactions)

    def resolve(self, transaction_ref: str) -> tuple[str, IndexedTransaction | None]:
        payload = parse_transaction_ref(transaction_ref)
        if payload is None:
            return "MALFORMED_TRANSACTION_REF", None
        matches = self._by_ref.get(transaction_ref, [])
        if len(matches) > 1:
            return "AMBIGUOUS_TRANSACTION_REF", None
        if matches:
            return "OK", matches[0]

        path = payload["path"]
        start_line = payload["start_line"]
        occurrence = payload["occurrence"]
        identity = payload["directive_identity"]
        if any(
            transaction.relative_path == path
            and (
                transaction.occurrence == occurrence
                or transaction.start_line == start_line
                or transaction.directive_identity == identity
            )
            for transaction in self.transactions
        ):
            return "STALE_TRANSACTION_REF", None
        return "TRANSACTION_NOT_FOUND", None

    def search(
        self,
        account: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        narration_contains: str | None = None,
    ) -> list[IndexedTransaction]:
        account_pattern = re.compile(account) if account else None
        result: list[IndexedTransaction] = []
        for transaction in self.transactions:
            transaction_date = date.fromisoformat(str(transaction.facts["date"]))
            if date_from and transaction_date < date_from:
                continue
            if date_to and transaction_date > date_to:
                continue
            narration = str(transaction.facts.get("narration") or "")
            if narration_contains and narration_contains not in narration:
                continue
            accounts = [str(posting["account"]) for posting in transaction.facts["postings"]]
            if account_pattern and not any(account_pattern.search(value) for value in accounts):
                continue
            result.append(transaction)
        return sorted(
            result,
            key=lambda item: (
                -date.fromisoformat(str(item.facts["date"])).toordinal(),
                item.relative_path,
                item.start_line,
            ),
        )
