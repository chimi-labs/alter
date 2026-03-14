"""Canvas auto-layout utilities.

This module is the single source of truth for placing tables on the canvas
grid.  Both the SQL importer and the CLI (init / sync / add) use it so that
every entry-point produces non-overlapping table positions without any manual
drag-and-drop required.

Grid geometry::

    col 0      col 1      col 2      col 3
    [table 0]  [table 1]  [table 2]  [table 3]   row 0
    [table 4]  [table 5]  [table 6]  [table 7]   row 1
    …

Each cell is ``GRID_COL_W`` pixels wide and ``GRID_ROW_H`` pixels tall.  A
50-pixel margin is added on all sides so no table is flush against the canvas
edge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alter.schema import Table

from alter.schema import Position

# ---------------------------------------------------------------------------
# Grid constants
# ---------------------------------------------------------------------------

GRID_COL_W: int = 300   # horizontal cell width (pixels)
GRID_ROW_H: int = 260   # vertical cell height (pixels)
GRID_COLS: int = 4      # number of columns before wrapping to the next row
GRID_MARGIN: int = 50   # left / top canvas margin (pixels)

# The sentinel value used by Position defaults — any table still at this
# exact coordinate pair is considered "not yet placed".
_ORIGIN_X: int = 0
_ORIGIN_Y: int = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def grid_position(index: int) -> Position:
    """Return the canvas ``Position`` for the *index*-th table on the grid.

    Tables are placed left-to-right, top-to-bottom in rows of ``GRID_COLS``
    columns, with a ``GRID_MARGIN`` pixel offset so no table touches the edge.

    Args:
        index: Zero-based table index in the layout sequence.

    Returns:
        A ``Position`` with ``x`` and ``y`` pixel coordinates.
    """
    col = index % GRID_COLS
    row = index // GRID_COLS
    return Position(
        x=col * GRID_COL_W + GRID_MARGIN,
        y=row * GRID_ROW_H + GRID_MARGIN,
    )


def auto_layout_tables(tables: list["Table"]) -> None:
    """Assign non-overlapping grid positions to tables still at the origin.

    Tables that already have a non-default position (i.e. ``x != 0`` or
    ``y != 0``) are left untouched.  Tables at ``(0, 0)`` are placed on the
    grid starting *after* all already-positioned tables, so new tables never
    overwrite manually placed ones.

    This function mutates the ``position`` attribute of each unpositioned
    table **in-place** and returns ``None``.

    Args:
        tables: The list of ``Table`` objects from an ``AlterSchema``.

    Examples::

        >>> auto_layout_tables(schema.tables)   # init — all tables positioned
        >>> auto_layout_tables(schema.tables)   # sync — only new (0,0) tables moved
    """
    # Count tables that already have a real position so the new ones start
    # after them in the grid sequence.
    placed_count = sum(
        1 for t in tables
        if t.position.x != _ORIGIN_X or t.position.y != _ORIGIN_Y
    )
    next_index = placed_count
    for tbl in tables:
        if tbl.position.x == _ORIGIN_X and tbl.position.y == _ORIGIN_Y:
            tbl.position = grid_position(next_index)
            next_index += 1
