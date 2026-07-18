"""Read-only semantic facts sealed into new mutation plans."""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date

from beancount import loader

from ..beancount import _cfg, _repo_path
from ..price_directives import (
    PriceIdentity,
    price_fact_subject,
    price_state,
    price_state_digest,
)
from ..queries import LedgerQueryService
from ..reconciliation import ReconciliationCalculator, format_decimal
from ..transaction_index import TransactionIndex, parse_transaction_ref
from ..types import LedgerConfig

_INCLUDE_RE = re.compile(r'^\s*include\s+"([^"]+)"\s*$')
_ACCOUNT_RE = re.compile(
    r"^(Assets|Liabilities|Equity|Income|Expenses)(:[A-Za-z][A-Za-z0-9\-]+)+$"
)
_CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9\-]*$")
_LEGACY_ACCOUNT_PRESENT_DIGEST = hashlib.sha256(b"present").hexdigest()
_LEGACY_ACCOUNT_ABSENT_DIGEST = hashlib.sha256(b"absent").hexdigest()
_LEGACY_ACCOUNT_DIGESTS = {
    _LEGACY_ACCOUNT_PRESENT_DIGEST,
    _LEGACY_ACCOUNT_ABSENT_DIGEST,
}


@dataclass(frozen=True)
class SemanticFact:
    """A deterministic read-set observation used by plan replay."""

    kind: str
    subject: str
    digest: str | None

    def to_spec(self) -> dict[str, str | None]:
        return {"kind": self.kind, "subject": self.subject, "digest": self.digest}

    @classmethod
    def from_spec(cls, value: dict[str, object]) -> "SemanticFact":
        kind = value.get("kind")
        subject = value.get("subject")
        digest = value.get("digest")
        if not isinstance(kind, str) or not isinstance(subject, str):
            raise ValueError("Mutation plan semantic fact is invalid")
        if digest is not None and not isinstance(digest, str):
            raise ValueError("Mutation plan semantic fact is invalid")
        return cls(kind, subject, digest)


def _file_digest(content: str | None) -> str | None:
    return hashlib.sha256(content.encode()).hexdigest() if content is not None else None


def _included_paths(workspace: str, entry_path: str) -> tuple[str, ...]:
    """Resolve the ledger's textual include graph without mutating a workspace."""
    seen: set[str] = set()

    def visit(relative_path: str) -> None:
        normalized = os.path.normpath(relative_path).replace(os.sep, "/")
        if normalized in seen:
            return
        seen.add(normalized)
        try:
            with open(_repo_path(workspace, normalized), encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            return
        parent = os.path.dirname(normalized)
        for line in lines:
            match = _INCLUDE_RE.match(line)
            if match:
                visit(os.path.join(parent, match.group(1)))

    visit(entry_path)
    return tuple(sorted(seen))


def capture_ledger_read_facts(
    workspace: str, ledger_config: LedgerConfig | None = None
) -> tuple[SemanticFact, ...]:
    """Capture included ledger files, excluding unrelated repository files.

    A handler may append narrower account, balance, checkpoint, or locator facts.
    This base include-graph fact prevents a plan from crossing any semantic
    ledger-input change while preserving unrelated README and asset edits.
    """
    config = _cfg(ledger_config)
    facts: list[SemanticFact] = []
    for relative_path in _included_paths(workspace, config.entry_path):
        try:
            with open(_repo_path(workspace, relative_path), encoding="utf-8") as handle:
                content: str | None = handle.read()
        except FileNotFoundError:
            content = None
        facts.append(SemanticFact("included_file_digest", relative_path, _file_digest(content)))
    return tuple(facts)


def capture_account_state_fact(
    workspace: str, account_name: str, ledger_config: LedgerConfig | None = None
) -> SemanticFact:
    """Hash the active open/close lifecycle directives for one account."""
    config = _cfg(ledger_config)
    entries, _errors, _options = loader.load_file(_repo_path(workspace, config.entry_path))
    lifecycle: list[dict[str, object]] = []
    for entry in entries:
        if getattr(entry, "account", None) != account_name:
            continue
        entry_type = entry.__class__.__name__
        if entry_type == "Open":
            lifecycle.append(
                {
                    "type": "open",
                    "date": entry.date.isoformat(),
                    "currencies": list(entry.currencies or ()),
                    "booking": str(entry.booking) if entry.booking is not None else None,
                }
            )
        elif entry_type == "Close":
            lifecycle.append({"type": "close", "date": entry.date.isoformat()})
    serialized = json.dumps(lifecycle, sort_keys=True, separators=(",", ":"))
    return SemanticFact("account_state", account_name, _file_digest(serialized))


def capture_transaction_revision_fact(
    workspace: str,
    transaction_ref: str,
    ledger_config: LedgerConfig | None = None,
) -> SemanticFact:
    """Capture the exact directive revision addressed by an opaque reference."""
    revision: str | None = None
    if parse_transaction_ref(transaction_ref) is not None:
        try:
            index = TransactionIndex.build(workspace, ledger_config)
            code, transaction = index.resolve(transaction_ref)
            if code == "OK" and transaction is not None:
                revision = transaction.revision_fingerprint
        except Exception:
            revision = None
    return SemanticFact("transaction_revision", transaction_ref, revision)


def capture_price_state_fact(
    workspace: str,
    identity: PriceIdentity,
    ledger_config: LedgerConfig | None = None,
) -> SemanticFact:
    """Capture the exact same-date/base/quote price state for replay."""
    return SemanticFact(
        "price_state",
        price_fact_subject(identity),
        price_state_digest(price_state(workspace, identity, ledger_config)),
    )


def _encode_subject(**fields: str) -> str:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


def _decode_subject(subject: str, expected_keys: frozenset[str]) -> dict[str, str] | None:
    try:
        decoded = json.loads(subject)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict) or set(decoded) != expected_keys:
        return None
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()):
        return None
    return decoded


def capture_balance_fact(
    workspace: str,
    account_name: str,
    as_of_date: str,
    ledger_config: LedgerConfig | None = None,
) -> SemanticFact:
    """Capture the exact descendant-inclusive balance used by reconciliation."""
    result = ReconciliationCalculator.get_balance(
        workspace, account_name, as_of_date, ledger_config
    )
    state = {
        "status": result.status,
        "balance": result.balance if result.status == "SUCCESS" else None,
    }
    return SemanticFact(
        "balance_state",
        _encode_subject(account=account_name, as_of=as_of_date),
        _file_digest(json.dumps(state, sort_keys=True, separators=(",", ":"))),
    )


def capture_checkpoint_fact(
    workspace: str,
    account_name: str,
    assertion_date: str,
    currency: str,
    ledger_config: LedgerConfig | None = None,
) -> SemanticFact:
    """Capture the amount or absence of one balance assertion identity."""
    amount = ReconciliationCalculator.existing_balance_assertion(
        workspace, assertion_date, account_name, currency, ledger_config
    )
    state = "absent" if amount is None else f"present:{format_decimal(amount)}"
    return SemanticFact(
        "checkpoint_state",
        _encode_subject(account=account_name, currency=currency, date=assertion_date),
        _file_digest(state),
    )


def _legacy_account_state_fact(
    workspace: str, account_name: str, ledger_config: LedgerConfig | None
) -> SemanticFact:
    present = account_name in set(LedgerQueryService.get_accounts(workspace, ledger_config))
    return SemanticFact(
        "account_state", account_name, _file_digest("present" if present else "absent")
    )


def _current_fact(
    workspace: str, fact: SemanticFact, ledger_config: LedgerConfig | None
) -> SemanticFact | None:
    if fact.kind == "included_file_digest":
        try:
            with open(_repo_path(workspace, fact.subject), encoding="utf-8") as handle:
                content: str | None = handle.read()
        except FileNotFoundError:
            content = None
        return SemanticFact(fact.kind, fact.subject, _file_digest(content))
    if fact.kind == "account_state":
        if not _ACCOUNT_RE.fullmatch(fact.subject):
            return None
        if fact.digest in _LEGACY_ACCOUNT_DIGESTS:
            return _legacy_account_state_fact(workspace, fact.subject, ledger_config)
        return capture_account_state_fact(workspace, fact.subject, ledger_config)
    if fact.kind == "balance_state":
        fields = _decode_subject(fact.subject, frozenset({"account", "as_of"}))
        if fields is None or not _ACCOUNT_RE.fullmatch(fields["account"]):
            return None
        try:
            date.fromisoformat(fields["as_of"])
        except ValueError:
            return None
        return capture_balance_fact(
            workspace, fields["account"], fields["as_of"], ledger_config
        )
    if fact.kind == "checkpoint_state":
        fields = _decode_subject(
            fact.subject, frozenset({"account", "currency", "date"})
        )
        if (
            fields is None
            or not _ACCOUNT_RE.fullmatch(fields["account"])
            or not _CURRENCY_RE.fullmatch(fields["currency"])
        ):
            return None
        try:
            date.fromisoformat(fields["date"])
        except ValueError:
            return None
        return capture_checkpoint_fact(
            workspace,
            fields["account"],
            fields["date"],
            fields["currency"],
            ledger_config,
        )
    if fact.kind == "transaction_revision":
        if parse_transaction_ref(fact.subject) is None:
            return None
        try:
            index = TransactionIndex.build(workspace, ledger_config)
            code, transaction = index.resolve(fact.subject)
        except Exception:
            return None
        if code != "OK" or transaction is None:
            return None
        return SemanticFact(
            "transaction_revision", fact.subject, transaction.revision_fingerprint
        )
    if fact.kind == "price_state":
        try:
            fields = json.loads(fact.subject)
            if not isinstance(fields, dict):
                return None
            identity = PriceIdentity(
                str(fields["date"]),
                str(fields["base"]),
                str(fields["quote"]),
                str(fields["value"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return SemanticFact(
            "price_state",
            fact.subject,
            price_state_digest(price_state(workspace, identity, ledger_config)),
        )
    # Unknown fact kinds are integrity failures rather than a permissive replay.
    return None


def semantic_facts_hold(
    workspace: str,
    facts: tuple[SemanticFact, ...],
    ledger_config: LedgerConfig | None = None,
) -> bool:
    """Recompute persisted facts, failing closed on malformed or unreadable input."""
    try:
        return all(
            (current := _current_fact(workspace, fact, ledger_config)) is not None
            and current == fact
            for fact in facts
        )
    except Exception:
        return False
