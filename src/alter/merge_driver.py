"""Git merge driver for .alter files.

Performs a three-way merge of two ``.alter`` branches against a common base.
Registered in ``.gitattributes`` as ``*.alter merge=alter`` and in
``.git/config`` / ``~/.gitconfig`` as::

    [merge "alter"]
        name = Alter schema merge driver
        driver = alter merge-driver %O %A %B

Exit codes follow git convention:  0 = clean merge,  1 = conflicts present.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass, field
from pathlib import Path

from alter.errors import SchemaFileError
from alter.schema import AlterSchema, EnumDef, Relation, Table


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class MergeResult:
    """Outcome of a three-way merge."""

    schema: AlterSchema
    conflicts: list[str] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def merge_schemas(
    base: AlterSchema,
    ours: AlterSchema,
    theirs: AlterSchema,
) -> MergeResult:
    """Three-way merge of two AlterSchema branches against a common base.

    Strategy
    --------
    - **Independent additions**: tables/relations/enums added on one side
      but not the other are included automatically.
    - **Independent deletions**: entities dropped on one side but *unchanged*
      on the other are removed automatically.
    - **Modify-vs-delete**: if one side modified an entity and the other
      deleted it, a conflict is recorded and the modified version is kept
      (matching standard git merge semantics for this case).
    - **Both sides added the same entity differently**: take *ours*, record
      a conflict.
    - **Both sides modified the same existing entity**: take *ours*, record
      a conflict.
    - **Both sides modified the same entity identically**: auto-merge (no
      conflict).
    - **Positions**: preserve *ours* positions; use *theirs* positions only
      for tables that exist only in *theirs*.
    """
    conflicts: list[str] = []

    # ── Tables ──────────────────────────────────────────────────────────────
    merged_tables = _merge_entities(
        base_map={t.name: t for t in base.tables},
        our_map={t.name: t for t in ours.tables},
        their_map={t.name: t for t in theirs.tables},
        entity_type="Table",
        conflicts=conflicts,
    )

    # ── Relations ───────────────────────────────────────────────────────────
    def _rel_key(r: Relation) -> str:
        return f"{r.from_table}.{r.from_column}→{r.to_table}.{r.to_column}"

    merged_relations = _merge_entities(
        base_map={_rel_key(r): r for r in base.relations},
        our_map={_rel_key(r): r for r in ours.relations},
        their_map={_rel_key(r): r for r in theirs.relations},
        entity_type="Relation",
        conflicts=conflicts,
    )

    # ── Enums ────────────────────────────────────────────────────────────────
    merged_enums = _merge_entities(
        base_map={e.name: e for e in base.enums},
        our_map={e.name: e for e in ours.enums},
        their_map={e.name: e for e in theirs.enums},
        entity_type="Enum",
        conflicts=conflicts,
    )

    merged = copy.deepcopy(ours)
    merged.tables = list(merged_tables.values())
    merged.relations = list(merged_relations.values())
    merged.enums = list(merged_enums.values())

    return MergeResult(schema=merged, conflicts=conflicts)


def run_merge_driver(base_path: str, ours_path: str, theirs_path: str) -> int:
    """Entry point for the git merge driver.

    Reads three .alter files, merges them, writes the result to *ours_path*
    (git convention for the working copy), and returns 0 for a clean merge
    or 1 if conflicts were detected.
    """
    try:
        base = AlterSchema.load(Path(base_path))
        ours = AlterSchema.load(Path(ours_path))
        theirs = AlterSchema.load(Path(theirs_path))
    except (SchemaFileError, Exception) as exc:
        print(f"alter merge-driver: could not parse .alter files: {exc}", file=sys.stderr)
        return 1

    result = merge_schemas(base, ours, theirs)
    result.schema.save(Path(ours_path))

    if result.has_conflicts:
        for msg in result.conflicts:
            print(f"CONFLICT: {msg}", file=sys.stderr)
        print(
            "\nResolve conflicts by editing the .alter file, then run "
            "'alter sync' to regenerate from current code.",
            file=sys.stderr,
        )
        return 1

    return 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _merge_entities(
    base_map: dict,
    our_map: dict,
    their_map: dict,
    entity_type: str,
    conflicts: list[str],
) -> dict:
    """Generic three-way merge for a dict of named entities.

    Returns an ordered dict of the merged entities (keyed by their name/key).
    All values are deep-copied so the caller can freely mutate them.
    """
    merged: dict = {}
    all_keys = sorted(set(our_map) | set(their_map))

    for key in all_keys:
        in_base = key in base_map
        in_ours = key in our_map
        in_theirs = key in their_map

        if in_ours and in_theirs:
            our_json = our_map[key].model_dump_json()
            their_json = their_map[key].model_dump_json()

            if our_json == their_json:
                # Identical on both sides — no conflict
                merged[key] = copy.deepcopy(our_map[key])
            elif not in_base:
                # Added on both sides with different content — conflict
                conflicts.append(
                    f"{entity_type} '{key}' was added on both branches with "
                    f"different definitions (keeping ours)"
                )
                merged[key] = copy.deepcopy(our_map[key])
            else:
                # Modified — check whether one or both sides changed
                base_json = base_map[key].model_dump_json()
                our_changed = our_json != base_json
                their_changed = their_json != base_json

                if our_changed and their_changed and our_json != their_json:
                    # Both sides diverged — conflict, keep ours
                    conflicts.append(
                        f"{entity_type} '{key}' was modified on both branches "
                        f"(keeping ours)"
                    )
                    merged[key] = copy.deepcopy(our_map[key])
                elif their_changed and not our_changed:
                    # Only theirs changed — accept their version
                    merged[key] = copy.deepcopy(their_map[key])
                else:
                    # Only ours changed, or both identical — keep ours
                    merged[key] = copy.deepcopy(our_map[key])

        elif in_ours and not in_theirs:
            if in_base:
                # Theirs deleted it.  Check whether ours modified it first.
                if our_map[key].model_dump_json() != base_map[key].model_dump_json():
                    # Ours changed it AND theirs deleted it — conflict.
                    # Keep the modified version so no work is silently lost.
                    conflicts.append(
                        f"{entity_type} '{key}': modified in ours but deleted "
                        f"in theirs (keeping ours)"
                    )
                    merged[key] = copy.deepcopy(our_map[key])
                # else: ours was unchanged — honour the deletion (omit)
            else:
                # Only ours added it — include
                merged[key] = copy.deepcopy(our_map[key])

        else:  # in_theirs and not in_ours
            if in_base:
                # Ours deleted it.  Check whether theirs modified it first.
                if their_map[key].model_dump_json() != base_map[key].model_dump_json():
                    # Theirs changed it AND ours deleted it — conflict.
                    # Keep the modified version so no work is silently lost.
                    conflicts.append(
                        f"{entity_type} '{key}': deleted in ours but modified "
                        f"in theirs (keeping theirs)"
                    )
                    merged[key] = copy.deepcopy(their_map[key])
                # else: theirs was unchanged — honour the deletion (omit)
            else:
                # Only theirs added it — include
                merged[key] = copy.deepcopy(their_map[key])

    return merged
