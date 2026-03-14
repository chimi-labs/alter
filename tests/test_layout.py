"""Tests for src/alter/layout.py — shared canvas auto-layout utilities."""

from __future__ import annotations

import pytest

from alter.layout import (
    GRID_COL_W,
    GRID_COLS,
    GRID_MARGIN,
    GRID_ROW_H,
    auto_layout_tables,
    grid_position,
)
from alter.schema import Column, Position, Table


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_table(name: str, x: int = 0, y: int = 0) -> Table:
    """Create a minimal Table with the given canvas position."""
    return Table(
        name=name,
        position=Position(x=x, y=y),
        columns=[Column(name="id", type="uuid", primary_key=True, nullable=False)],
    )


# ---------------------------------------------------------------------------
# grid_position — unit tests
# ---------------------------------------------------------------------------


class TestGridPosition:
    def test_index_0_returns_margin_offset(self) -> None:
        pos = grid_position(0)
        assert pos.x == GRID_MARGIN
        assert pos.y == GRID_MARGIN

    def test_index_1_advances_one_column(self) -> None:
        pos = grid_position(1)
        assert pos.x == GRID_COL_W + GRID_MARGIN
        assert pos.y == GRID_MARGIN

    def test_index_grid_cols_wraps_to_next_row(self) -> None:
        """The (GRID_COLS)-th table starts a new row."""
        pos = grid_position(GRID_COLS)
        assert pos.x == GRID_MARGIN          # back to column 0
        assert pos.y == GRID_ROW_H + GRID_MARGIN  # row 1

    def test_index_grid_cols_plus_1_is_second_col_second_row(self) -> None:
        pos = grid_position(GRID_COLS + 1)
        assert pos.x == GRID_COL_W + GRID_MARGIN
        assert pos.y == GRID_ROW_H + GRID_MARGIN

    def test_all_positions_in_first_row_have_same_y(self) -> None:
        ys = {grid_position(i).y for i in range(GRID_COLS)}
        assert len(ys) == 1, "All first-row tables must share the same y"

    def test_two_rows_have_distinct_y(self) -> None:
        y0 = grid_position(0).y
        y1 = grid_position(GRID_COLS).y
        assert y1 > y0

    def test_positions_are_positive(self) -> None:
        for i in range(GRID_COLS * 3):
            pos = grid_position(i)
            assert pos.x > 0
            assert pos.y > 0

    def test_all_positions_unique_for_first_two_rows(self) -> None:
        positions = [grid_position(i) for i in range(GRID_COLS * 2)]
        coords = [(p.x, p.y) for p in positions]
        assert len(coords) == len(set(coords)), "All grid positions must be unique"


# ---------------------------------------------------------------------------
# auto_layout_tables — all tables at origin
# ---------------------------------------------------------------------------


class TestAutoLayoutAllAtOrigin:
    def test_single_table_gets_positioned(self) -> None:
        tables = [_make_table("users")]
        auto_layout_tables(tables)
        pos = tables[0].position
        assert pos.x != 0 or pos.y != 0

    def test_single_table_gets_index_0_position(self) -> None:
        tables = [_make_table("users")]
        auto_layout_tables(tables)
        assert tables[0].position == grid_position(0)

    def test_multiple_tables_all_positioned(self) -> None:
        tables = [_make_table(f"t{i}") for i in range(6)]
        auto_layout_tables(tables)
        for tbl in tables:
            assert tbl.position.x != 0 or tbl.position.y != 0

    def test_no_two_tables_share_a_position(self) -> None:
        tables = [_make_table(f"t{i}") for i in range(10)]
        auto_layout_tables(tables)
        coords = [(t.position.x, t.position.y) for t in tables]
        assert len(coords) == len(set(coords)), "Tables must not overlap"

    def test_positions_match_grid_sequence(self) -> None:
        """Tables receive positions grid_position(0), grid_position(1), … in order."""
        n = GRID_COLS + 2
        tables = [_make_table(f"t{i}") for i in range(n)]
        auto_layout_tables(tables)
        for i, tbl in enumerate(tables):
            assert tbl.position == grid_position(i), (
                f"Table {i} position mismatch: expected {grid_position(i)}, "
                f"got {tbl.position}"
            )

    def test_empty_list_is_noop(self) -> None:
        auto_layout_tables([])  # must not raise


# ---------------------------------------------------------------------------
# auto_layout_tables — mixed: some tables already placed
# ---------------------------------------------------------------------------


class TestAutoLayoutMixed:
    def test_positioned_tables_not_moved(self) -> None:
        """Tables with an existing non-(0,0) position must not be changed."""
        placed = _make_table("existing", x=150, y=300)
        new = _make_table("new_table")
        auto_layout_tables([placed, new])
        assert placed.position.x == 150
        assert placed.position.y == 300

    def test_unpositioned_tables_placed_after_positioned_ones(self) -> None:
        """New tables must not overlap with already-placed tables."""
        placed = _make_table("existing", x=grid_position(0).x, y=grid_position(0).y)
        new = _make_table("new_table")
        auto_layout_tables([placed, new])
        # There is one already-placed table, so the new one goes to index 1.
        assert new.position == grid_position(1)

    def test_multiple_positioned_offset_new_correctly(self) -> None:
        """Two already-placed tables → new table starts at index 2."""
        p1 = _make_table("a", x=100, y=100)
        p2 = _make_table("b", x=200, y=200)
        n = _make_table("c")
        auto_layout_tables([p1, p2, n])
        assert n.position == grid_position(2)

    def test_all_tables_unique_positions_in_mixed_scenario(self) -> None:
        p1 = _make_table("p1", x=50, y=50)
        p2 = _make_table("p2", x=350, y=50)
        new_tables = [_make_table(f"n{i}") for i in range(5)]
        all_tables = [p1, p2] + new_tables
        auto_layout_tables(all_tables)
        coords = [(t.position.x, t.position.y) for t in all_tables]
        assert len(coords) == len(set(coords)), "All positions must be unique"

    def test_idempotent_on_already_positioned_schema(self) -> None:
        """Calling auto_layout_tables twice must not move any table."""
        tables = [_make_table(f"t{i}") for i in range(4)]
        auto_layout_tables(tables)
        positions_after_first = [(t.position.x, t.position.y) for t in tables]
        auto_layout_tables(tables)
        positions_after_second = [(t.position.x, t.position.y) for t in tables]
        assert positions_after_first == positions_after_second

    def test_only_origin_tables_are_moved(self) -> None:
        """Tables at exactly (0, 0) are moved; all others stay put."""
        at_origin = _make_table("origin")          # (0, 0) → should be moved
        off_origin = _make_table("off", x=1, y=0)  # x=1 → should NOT be moved
        auto_layout_tables([at_origin, off_origin])
        # off_origin must not have been touched
        assert off_origin.position.x == 1
        assert off_origin.position.y == 0
        # at_origin must have been given a real position
        assert at_origin.position.x != 0 or at_origin.position.y != 0
