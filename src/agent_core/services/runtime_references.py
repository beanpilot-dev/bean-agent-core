"""Keep authoritative transaction bindings stable through internal plan replay."""

from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path

from .mutations.applier import MutationApplier
from .mutations.facts import SemanticFact
from .mutations.plans import MutationPlan
from .transaction_index import TransactionIndex
from .types import LedgerConfig


class RuntimeReferenceError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RuntimeTransactionBindings:
    """Validate against the source, then follow exact source spans in the draft."""

    def __init__(self, workspace: str, config: LedgerConfig) -> None:
        self.config = config
        self.original = TransactionIndex.build(workspace, config)
        self.positions = {
            item.transaction_ref: (item.start_line - 1, item.end_line)
            for item in self.original.transactions
        }
        self.original_refs: dict[str, str] = {}

    def rebase(self, operation: dict, draft: str) -> dict:
        ref = operation.get("transaction_ref", "")
        code, original = self.original.resolve(ref)
        if original is None:
            raise RuntimeReferenceError(code)
        if operation.get("revision_fingerprint") != original.revision_fingerprint:
            raise RuntimeReferenceError("STALE_TRANSACTION_REVISION")
        position = self.positions.get(ref)
        if position is None:
            raise RuntimeReferenceError("STALE_TRANSACTION_REF")
        matches = [
            item
            for item in TransactionIndex.build(draft, self.config).transactions
            if item.relative_path == original.relative_path
            and item.start_line - 1 == position[0]
            and item.revision_fingerprint == original.revision_fingerprint
        ]
        if len(matches) != 1:
            raise RuntimeReferenceError("STALE_TRANSACTION_REF")
        current = matches[0]
        self.original_refs[current.transaction_ref] = ref
        return {**operation, "transaction_ref": current.transaction_ref}

    def original_facts(self, plan: MutationPlan) -> MutationPlan:
        return plan.with_semantic_facts(
            tuple(
                SemanticFact(
                    fact.kind, self.original_refs.get(fact.subject, fact.subject), fact.digest
                )
                if fact.kind == "transaction_revision"
                else fact
                for fact in plan.semantic_facts
            )
        )

    def replay(self, draft: str, plan: MutationPlan, applier: MutationApplier) -> None:
        paths = {item.relative_path for item in self.original.transactions}
        for operation in plan.operations:
            before = {path: (Path(draft) / path).read_text() for path in paths}
            applier.apply(draft, replace(plan, operations=(operation,)), self.config)
            for path, old in before.items():
                new = (Path(draft) / path).read_text()
                if old == new:
                    continue
                old_lines, new_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
                if operation.kind in {"delete", "replace"} and operation.target_file == path:
                    # Use the exact splice selected by the applier, including duplicates.
                    start = (
                        operation.target_start_line - 1
                        if operation.kind == "delete" and operation.target_start_line
                        else old[: old.index(operation.old_text or "")].count("\n")
                    )
                    end = start + len((operation.old_text or "").splitlines(keepends=True))
                    edits = [(start, end, len(new_lines) - len(old_lines))]
                else:
                    edits = [
                        (i, j, (b - a) - (j - i))
                        for tag, i, j, a, b in SequenceMatcher(
                            None, old_lines, new_lines, autojunk=False
                        ).get_opcodes()
                        if tag != "equal"
                    ]
                for item in self.original.transactions:
                    ref = item.transaction_ref
                    position = self.positions.get(ref)
                    if item.relative_path != path or position is None:
                        continue
                    start, end = position
                    if any(i < end and j > start for i, j, _ in edits):
                        self.positions.pop(ref, None)
                        continue
                    delta = sum(delta for i, j, delta in edits if j <= start)
                    self.positions[ref] = (start + delta, end + delta)
