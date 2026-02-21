"""Preview/staging state manager.

All schema modifications go through ``StagingManager``. This implements
the propose → preview → commit/discard workflow described in the architecture:

- ``current_schema`` — loaded from the ``.alter`` file on disk
- ``proposed_schema`` — in-memory, uncommitted changes
- ``_undo_stack`` — previous proposed states (for Ctrl+Z)
- ``_redo_stack`` — undone states (for Ctrl+Y)

Callers (MCP tools, CLI commands, canvas API) interact exclusively through
this class and never write to the ``.alter`` file directly.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Callable

from alter.diff import SchemaChange, diff_schemas
from alter.schema import AlterSchema


class StagingManager:
    """Manages the propose/commit/discard/undo/redo cycle for schema changes."""

    def __init__(self, alter_file_path: Path) -> None:
        """Load *alter_file_path* as the current schema.

        Args:
            alter_file_path: Path to the ``.alter`` JSON file.
                             The file is created empty if it does not exist.
        """
        self._alter_file_path = alter_file_path
        if alter_file_path.exists():
            self.current_schema: AlterSchema = AlterSchema.load(alter_file_path)
        else:
            self.current_schema = AlterSchema(orm="sqlmodel")

        self.proposed_schema: AlterSchema | None = None
        self._undo_stack: list[AlterSchema] = []
        self._redo_stack: list[AlterSchema] = []

    # ------------------------------------------------------------------
    # Core staging operations
    # ------------------------------------------------------------------

    def propose(
        self, change_fn: Callable[[AlterSchema], AlterSchema]
    ) -> AlterSchema:
        """Apply *change_fn* to the proposed schema.

        If there is no pending proposal yet, starts from a copy of
        ``current_schema``.  The pre-change state is pushed onto the undo
        stack.  The redo stack is cleared (a new proposal after undo
        invalidates the redo history).

        Args:
            change_fn: A function that takes the current proposed schema and
                       returns a new (modified) schema. Should not mutate its
                       argument in place — return a new object.

        Returns:
            The new proposed schema after applying *change_fn*.
        """
        base = self.proposed_schema if self.proposed_schema is not None else self.current_schema

        # Save the pre-change proposed state for undo
        self._undo_stack.append(copy.deepcopy(base))
        self._redo_stack.clear()

        self.proposed_schema = change_fn(copy.deepcopy(base))
        return self.proposed_schema

    def undo(self) -> AlterSchema | None:
        """Revert the most recent proposal.

        Pops the last state from the undo stack, pushes the current proposed
        state onto the redo stack.

        Returns:
            The reverted proposed schema, or ``None`` if the undo stack is empty.
        """
        if not self._undo_stack:
            return None

        # Push current proposed to redo
        current_proposed = self.proposed_schema if self.proposed_schema is not None else self.current_schema
        self._redo_stack.append(copy.deepcopy(current_proposed))

        # Pop from undo
        reverted = self._undo_stack.pop()

        # If the undo stack is now empty and reverted matches current_schema,
        # there are no more pending proposals
        if not self._undo_stack and reverted == self.current_schema:
            self.proposed_schema = None
        else:
            self.proposed_schema = reverted

        return self.proposed_schema

    def redo(self) -> AlterSchema | None:
        """Re-apply the most recently undone proposal.

        Pops from the redo stack and pushes the current proposed state onto
        the undo stack.

        Returns:
            The re-applied proposed schema, or ``None`` if the redo stack is empty.
        """
        if not self._redo_stack:
            return None

        # Push current proposed to undo
        current_proposed = self.proposed_schema if self.proposed_schema is not None else self.current_schema
        self._undo_stack.append(copy.deepcopy(current_proposed))

        # Pop from redo
        self.proposed_schema = self._redo_stack.pop()
        return self.proposed_schema

    def commit(self) -> None:
        """Write proposed schema to disk. Proposed becomes current. Clears both stacks."""
        if self.proposed_schema is None:
            return  # nothing to commit

        self.current_schema = self.proposed_schema
        self.proposed_schema = None
        self._undo_stack.clear()
        self._redo_stack.clear()

        self.current_schema.save(self._alter_file_path)

    def discard(self) -> None:
        """Throw away the proposed schema and both stacks."""
        self.proposed_schema = None
        self._undo_stack.clear()
        self._redo_stack.clear()

    # ------------------------------------------------------------------
    # Read-only helpers
    # ------------------------------------------------------------------

    def has_pending(self) -> bool:
        """Return ``True`` if there are uncommitted proposals."""
        return self.proposed_schema is not None

    def get_diff(self) -> list[SchemaChange]:
        """Return the diff between current and proposed schemas.

        Returns an empty list if there is no pending proposal.
        """
        if self.proposed_schema is None:
            return []
        return diff_schemas(self.current_schema, self.proposed_schema)

    def effective_schema(self) -> AlterSchema:
        """Return the proposed schema if pending, else the current schema."""
        return self.proposed_schema if self.proposed_schema is not None else self.current_schema
