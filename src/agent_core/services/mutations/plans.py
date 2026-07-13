"""Serializable, immutable descriptions of deterministic sidecar mutations."""

import hashlib
from dataclasses import dataclass, field
from typing import Literal

from .facts import SemanticFact

OperationKind = Literal["append", "open", "replace"]
_PLAN_SCHEMA_VERSION = 2
_LEGACY_PLAN_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class MutationOperation:
    """One ordered filesystem change, with all apply-time inputs explicit."""

    kind: OperationKind
    text: str = ""
    target_file: str | None = None
    account_name: str | None = None
    old_text: str | None = None

    def to_spec(self) -> dict[str, str | None]:
        return {
            "kind": self.kind,
            "text": self.text,
            "target_file": self.target_file,
            "account_name": self.account_name,
            "old_text": self.old_text,
        }

    @classmethod
    def from_spec(cls, value: dict[str, object]) -> "MutationOperation":
        kind = value.get("kind")
        if kind not in {"append", "open", "replace"}:
            raise ValueError("Unsupported mutation operation")
        return cls(
            kind=kind,
            text=str(value.get("text") or ""),
            target_file=value.get("target_file")
            if isinstance(value.get("target_file"), str)
            else None,
            account_name=value.get("account_name")
            if isinstance(value.get("account_name"), str)
            else None,
            old_text=value.get("old_text") if isinstance(value.get("old_text"), str) else None,
        )


@dataclass(frozen=True)
class FilePrecondition:
    path: str
    digest: str | None

    @classmethod
    def from_content(cls, path: str, content: str | None) -> "FilePrecondition":
        return cls(
            path, hashlib.sha256(content.encode()).hexdigest() if content is not None else None
        )

    def to_spec(self) -> dict[str, str | None]:
        return {"path": self.path, "digest": self.digest}


@dataclass(frozen=True)
class MutationPlan:
    """A replayable plan shared by preview validation and approved execution."""

    operations: tuple[MutationOperation, ...]
    commit_message: str
    remediation: str
    preconditions: tuple[FilePrecondition, ...] = field(default_factory=tuple)
    semantic_facts: tuple[SemanticFact, ...] = field(default_factory=tuple)
    schema_version: int = _PLAN_SCHEMA_VERSION

    @classmethod
    def from_operations(
        cls,
        operations: list[MutationOperation],
        *,
        commit_message: str,
        remediation: str,
    ) -> "MutationPlan":
        return cls(tuple(operations), commit_message, remediation)

    def with_preconditions(self, conditions: list[FilePrecondition]) -> "MutationPlan":
        return MutationPlan(
            self.operations,
            self.commit_message,
            self.remediation,
            tuple(conditions),
            self.semantic_facts,
        )

    def with_semantic_facts(self, facts: tuple[SemanticFact, ...]) -> "MutationPlan":
        return MutationPlan(
            self.operations,
            self.commit_message,
            self.remediation,
            self.preconditions,
            facts,
        )

    def to_spec(self) -> dict[str, object]:
        return {
            "version": self.schema_version,
            "operations": [operation.to_spec() for operation in self.operations],
            "commit_message": self.commit_message,
            "remediation": self.remediation,
            "preconditions": [condition.to_spec() for condition in self.preconditions],
            "semantic_facts": [fact.to_spec() for fact in self.semantic_facts],
        }

    @classmethod
    def from_spec(cls, value: dict[str, object]) -> "MutationPlan":
        version = value.get("version")
        if version not in {_LEGACY_PLAN_SCHEMA_VERSION, _PLAN_SCHEMA_VERSION}:
            raise ValueError("Unsupported mutation plan version")
        raw_operations = value.get("operations")
        raw_conditions = value.get("preconditions")
        if not isinstance(raw_operations, list) or not isinstance(raw_conditions, list):
            raise ValueError("Mutation plan is invalid")
        if not all(isinstance(operation, dict) for operation in raw_operations):
            raise ValueError("Mutation plan operation is invalid")
        conditions: list[FilePrecondition] = []
        for condition in raw_conditions:
            if not isinstance(condition, dict) or not isinstance(condition.get("path"), str):
                raise ValueError("Mutation plan precondition is invalid")
            digest = condition.get("digest")
            if digest is not None and not isinstance(digest, str):
                raise ValueError("Mutation plan precondition is invalid")
            conditions.append(FilePrecondition(condition["path"], digest))
        raw_facts = value.get("semantic_facts", [])
        if version == _PLAN_SCHEMA_VERSION and not isinstance(raw_facts, list):
            raise ValueError("Mutation plan semantic facts are invalid")
        if not isinstance(raw_facts, list) or not all(isinstance(fact, dict) for fact in raw_facts):
            raise ValueError("Mutation plan semantic facts are invalid")
        return cls(
            tuple(MutationOperation.from_spec(item) for item in raw_operations),
            str(value.get("commit_message") or ""),
            str(value.get("remediation") or ""),
            tuple(conditions),
            tuple(SemanticFact.from_spec(fact) for fact in raw_facts),
            schema_version=int(version),
        )
