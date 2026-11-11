"""Rollback journal: every mutating action registers its inverse.

`undo_last()` replays the inverse of the most recent change through the same
tool registry, so the undo itself is policy-checked and audited.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UndoStep:
    action_id: str
    description: str
    tool: str
    tool_input: dict


@dataclass
class RollbackJournal:
    steps: list[UndoStep] = field(default_factory=list)

    def push(self, step: UndoStep) -> None:
        self.steps.append(step)

    def pop(self) -> UndoStep | None:
        return self.steps.pop() if self.steps else None

    def peek(self) -> UndoStep | None:
        return self.steps[-1] if self.steps else None
