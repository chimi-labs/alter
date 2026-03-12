"""Shared diff-to-markdown formatting utilities.

This module has NO dependency on fastmcp, mcp_server, or any MCP-related
code so it can be imported freely by the CLI, the MCP server, and tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alter.diff import SchemaChange


def changes_to_markdown(changes: list["SchemaChange"]) -> str:
    """Return a markdown changelog string for *changes*.

    Args:
        changes: List of :class:`~alter.diff.SchemaChange` objects produced
                 by :func:`alter.diff.diff_schemas`.

    Returns:
        A markdown string with ``## Schema Changes`` header and per-category
        ``###`` sub-sections, or ``_No pending changes._`` when *changes* is
        empty.
    """
    if not changes:
        return "_No pending changes._"

    sections: dict[str, list[str]] = {
        "Added Tables": [],
        "Dropped Tables": [],
        "Added Columns": [],
        "Dropped Columns": [],
        "Modified Columns": [],
        "Added Relations": [],
        "Dropped Relations": [],
    }

    for ch in changes:
        if ch.type == "add_table":
            sections["Added Tables"].append(f"- `{ch.table}`")
        elif ch.type == "drop_table":
            sections["Dropped Tables"].append(f"- ~~`{ch.table}`~~ ⚠️ destructive")
        elif ch.type == "add_column":
            sections["Added Columns"].append(f"- `{ch.table}.{ch.column}`")
        elif ch.type == "drop_column":
            sections["Dropped Columns"].append(
                f"- ~~`{ch.table}.{ch.column}`~~ ⚠️ destructive"
            )
        elif ch.type == "modify_column":
            details = ", ".join(
                f"{k}: `{v[0]}` → `{v[1]}`" for k, v in ch.details.items()
            )
            sections["Modified Columns"].append(
                f"- `{ch.table}.{ch.column}` ({details})"
                + (" ⚠️ destructive" if ch.destructive else "")
            )
        elif ch.type == "add_relation":
            sections["Added Relations"].append(
                f"- `{ch.table}.{ch.column}` → `{ch.details.get('to', '?')}`"
            )
        elif ch.type == "drop_relation":
            sections["Dropped Relations"].append(
                f"- ~~`{ch.table}.{ch.column}`~~ ⚠️ destructive"
            )

    lines = ["## Schema Changes\n"]
    for heading, items in sections.items():
        if items:
            lines.append(f"### {heading}")
            lines.extend(items)
            lines.append("")

    return "\n".join(lines)
