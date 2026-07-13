"""Read-only semantic facts sealed into new mutation plans."""

import hashlib
import os
import re
from dataclasses import dataclass

from ..beancount import _cfg, _repo_path
from ..queries import LedgerQueryService
from ..types import LedgerConfig

_INCLUDE_RE = re.compile(r'^\s*include\s+"([^"]+)"\s*$')


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
    """Capture the account-lifecycle observation used by an action handler."""
    present = account_name in set(LedgerQueryService.get_accounts(workspace, ledger_config))
    return SemanticFact(
        "account_state", account_name, _file_digest("present" if present else "absent")
    )


def _current_fact(
    workspace: str, fact: SemanticFact, ledger_config: LedgerConfig | None
) -> SemanticFact:
    if fact.kind == "included_file_digest":
        try:
            with open(_repo_path(workspace, fact.subject), encoding="utf-8") as handle:
                content: str | None = handle.read()
        except FileNotFoundError:
            content = None
        return SemanticFact(fact.kind, fact.subject, _file_digest(content))
    if fact.kind == "account_state":
        return capture_account_state_fact(workspace, fact.subject, ledger_config)
    # Unknown fact kinds are integrity failures rather than a permissive replay.
    return SemanticFact("unsupported", fact.subject, None)


def semantic_facts_hold(
    workspace: str,
    facts: tuple[SemanticFact, ...],
    ledger_config: LedgerConfig | None = None,
) -> bool:
    """Recompute canonical include-graph facts before applying a v2 plan."""
    return all(_current_fact(workspace, fact, ledger_config) == fact for fact in facts)
