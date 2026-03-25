<p align="center">
  <img src="https://raw.githubusercontent.com/chimi-labs/alter/main/docs/alter_logo.png" alt="Alter" width="320" />
</p>

<h3 align="center">Lightweight, always up-to-date DB context for your AI. Query your database in human language.</h3>

<p align="center">
  <a href="#installation">Install</a> · <a href="#quick-start">Quick Start</a> · <a href="#mcp-server">MCP Server</a> · <a href="#the-canvas">Canvas</a> · <a href="#commands">Commands</a>
</p>

---

## What is Alter?

Alter creates a `schema.alter` file, a lightweight JSON that captures your entire database structure: tables, columns, types, relations, indexes, and layout. It lives in your repo, gets versioned with your code, and stays in sync with your ORM models in both directions.

```json
{
  "version": 1,
  "orm": "sqlmodel",
  "tables": [
    {
      "name": "users",
      "file_path": "app/models.py",
      "columns": [
        { "name": "id",    "type": "uuid",   "primary_key": true,  "default": "uuid4" },
        { "name": "email", "type": "string", "nullable": false,    "unique": true     }
      ]
    }
  ]
}
```

Everything runs on your machine. No cloud, no account, no data leaves your laptop.

---

## Why Alter?

### Your AI gets full database context without wasting tokens

This is the core value.

When an AI assistant needs to work with your database, it typically has to inspect your DB connection, read migration files, or parse dozens of model files just to understand your schema. That costs tokens, takes time, and the result is often incomplete.

With `schema.alter` in your repo, the AI gets the **full picture instantly**: every table, every column, every relation, every foreign key, in a single, compact, structured JSON. No inspection needed. No guessing.

This means your AI can:

- **Modify your schema** with full awareness of what exists and how tables relate
- **Suggest indexes** based on actual column types and foreign key patterns
- **Evaluate query performance** knowing the real structure, not an approximation
- **Propose database refactors** with complete context of every dependency
- **Generate efficient multi-join queries** that are correct on the first try

All through Alter's MCP server, which works with **Claude Code**, **Cursor**, and **Windsurf**.

### Talk to your database in plain language

This is what surprises people the most.

Connect the MCP server and you can ask questions about your actual data in natural language:

> _"Which customers placed orders last month but never came back?"_

The AI already knows your schema. It sees the `customers`, `orders`, and `order_items` tables, understands the foreign keys, writes the correct JOINs, runs the query **read-only**, and gives you the answer. No SQL required. No context-switching to a SQL client. No remembering column names.

> _"How many users signed up in the last 30 days?"_

> _"Which plan do most of our paying customers use?"_

> _"Why is this query slow?"_

It works because Alter exposes three database tools: `query_db` for running SELECT queries, `describe_table_data` for exploring table structure and sample rows, and `explain_query` for analyzing execution plans. All enforced as **read-only at the database level**.

### Design schemas through conversation

The MCP server isn't just for querying, it's a full schema design interface:

> _"Add a payments table with id, amount, currency, and a foreign key to users."_

The AI stages the change, shows you a diff, and waits for your approval. Nothing touches `schema.alter` until you commit. Nothing touches your code until you apply. You get undo, redo, migration SQL preview, and validation, all through conversation.

### A visual diagram that lives in your code

`schema.alter` also powers an interactive browser-based ERD canvas. But unlike standalone diagram tools, this diagram is **always in sync with your code** because it reads from the same JSON file that your AI uses and that your ORM models generate.

```
  ┌─────────────┐                       ┌───────────────┐
  │  Your Code  │  ── alter sync ─────► │    Canvas     │
  │ (models.py) │                       │ (visual ERD)  │
  │             │  ◄── alter apply ──── │               │
  └─────────────┘                       └───────────────┘
                    schema.alter
                  (keeps both in sync)
```

![Canvas](https://raw.githubusercontent.com/chimi-labs/alter/main/docs/Canvas.png)

### Code generation that doesn't destroy your code

`alter apply` is surgical. It doesn't rewrite your file; it patches exactly what changed. Your docstrings, your `Relationship()` definitions, your trailing comments, your hand-written `Field()` kwarg order, your `default={}` on that one column: all preserved. New tables are appended as new classes. Modified columns are rebuilt in-place. Deleted tables are removed cleanly.

This is the difference between a code generator you try once and one you actually keep using.

### Your team won't fight over merge conflicts

`schema.alter` is a JSON file, and JSON files are merge conflict magnets. Alter ships a **custom Git merge driver** that resolves conflicts structurally (by table name, not by line number). Two developers add different tables on different branches? Auto-merged. No manual conflict resolution, no broken JSON.

---

## Installation

```bash
pip install alterdb
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add alterdb
```

**Requirements:** Python 3.11+

> **Dependency conflicts?** Install as an isolated CLI tool instead:
>
> ```bash
> uv tool install alterdb
> ```
>
> This keeps Alter's dependencies completely separate from your project while making the `alter` command available on your `PATH`.

> **Live database features** (`query_db`, `describe_table_data`, `explain_query`): require `psycopg2-binary`. Install with `pip install alterdb[db]`.

## Quick Start

### From existing models

```bash
alter init      # scan your ORM models → create schema.alter
alter canvas    # open the visual ERD in your browser
```

Your browser opens with an interactive diagram of every table, column, and relation in your project.

### From an existing database

```bash
pg_dump --schema-only --no-owner mydb > schema.sql
alter import schema.sql     # parse DDL → merge tables into schema.alter
alter canvas                # open the visual editor
```

Or start the canvas first and use **Paste SQL** in the toolbar to paste DDL directly.

### Starting from scratch

```bash
alter init      # creates an empty schema.alter
alter canvas    # open the canvas, click "Templates" to pick a starter
```

Choose from **saas-base**, **auth**, **cms**, or **ecommerce**. Tables are proposed on the canvas so you can review and customize before committing anything.

---

## MCP Server

Alter exposes your schema to any MCP-compatible AI assistant so it can read, modify, and query your database through natural language.

### Setup

**1. Initialize the schema** (if you haven't already):

```bash
alter init
```

**2. Ensure `mcp>=1.2.0` is installed:**

```bash
pip install 'mcp>=1.2.0'
```

**3. Register the server in your editor:**

```json
{
  "mcpServers": {
    "alter": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/project", "alter", "mcp"]
    }
  }
}
```

| Editor | Config file |
| --- | --- |
| Claude Desktop | `claude_desktop_config.json` |
| Cursor | `.cursor/mcp.json` |
| Windsurf | `.windsurf/mcp.json` |

For **Claude Code**, register via the CLI:

```bash
claude mcp add alter -- uv run --directory /path/to/project alter mcp
```

**4. Restart your editor** and verify:

> _"What tools do you have available from alter?"_

### What your AI assistant can do

**Explore and understand:**
- _"Show me the current schema"_
- _"What tables reference the users table?"_
- _"Export the schema as a Mermaid diagram I can paste into our wiki"_

**Design and modify:**
- _"Add a payments table with id, amount, currency, and a foreign key to users"_
- _"Add a tags table and a many-to-many join table linking it to posts"_
- _"Add created_at and updated_at timestamp columns to every table"_
- _"Remove the legacy_notes column from orders"_

**Review and validate:**
- _"Show me the diff of what changed"_
- _"Preview the migration SQL for the pending changes"_
- _"Validate the schema, are there any broken foreign keys?"_
- _"Undo the last change"_

**Import and bootstrap:**
- _"Parse app/legacy/models.py and add its tables to the schema"_
- _"I have a SQL dump, import it into the schema"_

The assistant stages changes, shows you a diff, and only commits to `schema.alter` with your approval. Nothing touches your model files until you run `alter apply`.

### Querying live data

Connect a PostgreSQL database and your AI assistant can answer questions about real data, not just schema structure.

**1. Install the database extra:**

```bash
pip install alterdb[db]
```

**2. Set the `DATABASE_URL` environment variable:**

```bash
export DATABASE_URL="postgresql://user:password@localhost:5432/mydb"
```

Or add it to your MCP config:

```json
{
  "mcpServers": {
    "alter": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/project", "alter", "mcp"],
      "env": {
        "DATABASE_URL": "postgresql://user:password@localhost:5432/mydb"
      }
    }
  }
}
```

**The three data tools:**

| Tool | What it does |
|------|-------------|
| `query_db` | Execute a SELECT query (results as table, JSON, or CSV) |
| `describe_table_data` | Row count, column types, relationships, sample rows |
| `explain_query` | PostgreSQL execution plan (without running the query) |

All queries run in a **read-only transaction**. INSERT, UPDATE, DELETE, and DDL are blocked at the database level. Results are capped at 1,000 rows.

**Example prompts:**

> _"How many users signed up in the last 30 days?"_

The assistant sees your schema, writes the query, runs it read-only, and tells you: _"847 users signed up in the last 30 days."_

> _"Which plan do most of our paying customers use?"_

```
| plan    | count |
| pro     | 1,204 |
| starter | 891   |
| team    | 342   |
```

> _"Tell me about the orders table before I write a query against it"_

The assistant calls `describe_table_data` and returns row count, column types, foreign key relationships, and sample data: everything you need to understand the table before touching it.

> _"Why is my query slow?"_

The assistant runs `explain_query` and walks you through the execution plan, pointing out sequential scans and suggesting indexes.

### Introspecting a live database

Already have a running PostgreSQL instance? Import its schema directly.

The `introspect_db` MCP tool reads tables, columns, constraints, indexes, and foreign key relationships from a live database and merges them into your schema. No `pg_dump` required.

---

## The Canvas

The canvas is a browser-based interactive ERD that runs locally on your machine. It gives you a visual overview of your entire schema and lets you design tables, columns, and relations by pointing and clicking.

What you can do on the canvas:

- **Create and edit tables**: add columns, set types, toggle nullable/unique/primary key, set defaults and max lengths, add indexes.
- **Draw relations**: foreign keys with crow's-foot notation, supporting one-to-one, one-to-many, many-to-one, and many-to-many. Set `ON DELETE` rules (CASCADE, SET NULL, RESTRICT, NO ACTION, SET DEFAULT).
- **Paste SQL**: paste a `CREATE TABLE` statement and Alter parses it into a table on the canvas.
- **Load templates**: start from saas-base, auth, cms, or ecommerce and customize from there.
- **See enums**: enum types from your code are displayed as read-only reference cards so you can see available types when wiring up columns.
- **Preview migration SQL**: the Migrations tab shows the pending DDL at any time.
- **Commit, discard, undo**: a staging workflow that lets you review changes before they hit your schema file.
- **Live reload**: the canvas watches your `.alter` file and updates in real time when it changes.

```bash
alter canvas                # open on default port
alter canvas --port 9000    # custom port
alter canvas --demo         # open with a sample schema
```

---

## The Two-Way Workflow

Two commands keep `schema.alter` synchronized with your code.

### Canvas → Code

Design on the canvas, then apply to your model files:

```bash
alter apply --preview   # see exactly what will change (unified diff)
alter apply             # write the changes to your model files
```

Or click **Apply to Code** directly in the canvas. It auto-commits pending changes before writing, so you never accidentally apply a stale snapshot.

What `alter apply` does (and doesn't do):

- **Additions**: new tables are appended as new classes; new columns are inserted after the last existing field.
- **Modifications**: changed `Field()` kwargs are rebuilt in-place, preserving your original kwarg order, multi-line formatting, and inline comments.
- **Deletions**: removed tables have their class deleted; removed columns have their `Field()` line removed.
- **Untouched**: your docstrings, `Relationship()` definitions, custom methods, trailing comments, and mutable defaults (`default={}`, `default=[]`) are left exactly as you wrote them.

### Code → Canvas

Edit your models by hand, then sync:

```bash
alter sync    # re-parse your models, update schema.alter
```

Or click **Sync from Code** in the canvas toolbar. The canvas picks up the change via live reload, no restart needed.

### Preview changes before committing

```bash
alter diff                    # see what changed (text summary)
alter diff --format markdown  # PR-ready changelog
```

---

## Complete Workflow Example

From a fresh project to running migrations:

```bash
# 1. Initialise
alter init                            # scan models → schema.alter

# 2. Create the initial migration (with your migration tool)
alembic init alembic
alembic revision --autogenerate -m "initial schema"
alembic upgrade head

# 3. Open the canvas and design
alter canvas
# add a "payments" table, click Commit
# the Migrations tab shows the SQL that needs to run

# 4. Apply canvas changes to code
alter apply --preview                 # review the unified diff
alter apply                           # write class Payments to models.py

# 5. Run the migration
alembic revision -m "add payments table"
# paste the SQL from the canvas Migrations tab into upgrade()
alembic upgrade head
```

---

## Migrations

Alter generates the SQL. You run it with whatever migration tool you already use (Alembic, Django, Flyway, raw SQL, anything).

The **Migrations tab** in the canvas shows the pending DDL at any time. The `preview_migration` MCP tool returns the same SQL to AI assistants. Copy it into your migration manager of choice.

> **Column rename detection:** Alter's diff engine is name-based and cannot distinguish a column rename from a drop + add. When it detects a `DROP COLUMN` paired with an `ADD COLUMN` of the same type on the same table, the generated SQL includes a comment pointing out the potential rename and showing the equivalent `ALTER TABLE … RENAME COLUMN` statement.

### With Alembic (one-time setup)

```bash
alembic init alembic
```

Edit `alembic/env.py` so Alembic knows about your models:

```python
from sqlmodel import SQLModel
import app.models  # ensure all models are imported

target_metadata = SQLModel.metadata
```

Then create migrations as needed:

```bash
alembic revision --autogenerate -m "initial schema"
alembic upgrade head
```

After that, use the canvas Migrations tab or the `preview_migration` MCP tool to get the SQL for any change, and apply it with `alembic revision` + `alembic upgrade head`.

---

## `schema.alter`: The Schema File

`schema.alter` is a JSON file that captures your table definitions, column types, relations, enum types, and canvas layout positions. It's the single source of truth that your code, your canvas, and your AI all read from: human-readable, git-diffable, and versioned alongside your code.

### Supported column types

| Type | Python | SQL |
| ---- | ------ | --- |
| `uuid` | `uuid.UUID` | `UUID` |
| `string` | `str` | `VARCHAR(N)` / `TEXT` |
| `text` | `str` | `TEXT` |
| `int` | `int` | `INTEGER` |
| `bigint` | `int` | `BIGINT` |
| `float` | `float` | `DOUBLE PRECISION` |
| `decimal` | `Decimal` | `NUMERIC` |
| `bool` | `bool` | `BOOLEAN` |
| `datetime` | `datetime` | `TIMESTAMPTZ` |
| `date` | `date` | `DATE` |
| `time` | `time` | `TIME` |
| `json` | `dict` | `JSONB` |
| `json_array` | `list` | `JSONB` |
| `bytes` | `bytes` | `BYTEA` |

Plus any **enum types** defined in your code.

---

## Multi-File and Cross-File Support

Alter understands real-world project structures out of the box:

- **Enums in separate files**: if `Role` is defined in `app/enums.py`, Alter tracks its `file_path` and applies changes to the right file. No duplicate enum classes.
- **Base class inheritance**: columns inherited from mixin classes (e.g. `UUIDBase`, `TimestampedBase`) are tracked as inherited and never re-injected when applying to code.
- **Multi-file models**: tables can live in different files; `alter apply` writes each table to its correct file independently.
- **Adding files incrementally**: got a new module with its own models? Add it without touching your existing schema:

```bash
alter add app/legacy/models.py    # parse and merge new tables into schema.alter
alter add lib/plugins/billing.py  # already-tracked tables are skipped
```

Safe to run multiple times.

---

## PostgreSQL Schema Support

Tables in a non-default PostgreSQL schema (with `__table_args__ = {"schema": "..."}`) are fully supported end-to-end:

- **Parsing**: both `{"schema": "billing"}` dict form and tuple form `({"schema": "billing"}, UniqueConstraint(...))` are recognized.
- **SQL export**: `CREATE TABLE` and `REFERENCES` use qualified `schema.table` names.
- **Mermaid export**: entity names use `schema_table` (underscore-joined) for valid Mermaid identifiers.
- **Validation**: foreign keys can be `table.column` or `schema.table.column`.

---

## Schema Validation

Run `alter validate` to check for structural problems before applying or exporting:

```bash
alter validate
```

**Errors** (must fix):

- Broken FK references: target table or column doesn't exist
- Duplicate table or column names
- Unknown column types
- Invalid SQL identifiers: names starting with a digit or containing hyphens, spaces, or special characters

**Warnings** (advisory):

- Tables without a primary key
- FK columns missing an index
- SQL reserved words used as names (`select`, `from`, `table`, …)

Exits with code 1 if errors are found, safe to use in CI.

---

## Import and Export

### Import

```bash
alter import schema.sql             # SQL DDL (pg_dump, hand-written, etc.)
alter import other.alter            # another schema.alter file
alter import templates/saas.alter   # a built-in template
```

Tables already in the schema are skipped, safe to run multiple times. Format is auto-detected from the file extension.

### Export

```bash
alter export                              # SQL DDL to stdout
alter export --format mermaid             # Mermaid ERD to stdout
alter export --format alter               # raw JSON to stdout
alter export --output schema.sql          # write to a file
alter export --proposed                   # export staged changes
```

The Mermaid output can be pasted directly into GitHub Markdown, Notion, or any tool that renders ` ```mermaid ` fences:

````markdown
```mermaid
erDiagram
  users {
    uuid id PK
    string email
  }
  posts {
    uuid id PK
    uuid author_id FK
  }
  users ||--o{ posts : "author_id"
```
````

---

## Templates

Four starter templates, accessible from the canvas toolbar or the CLI:

| Template    | Tables                                      |
| ----------- | ------------------------------------------- |
| `saas-base` | users, organizations, memberships, sessions |
| `auth`      | users, sessions, tokens, oauth_accounts     |
| `cms`       | posts, categories, tags, media              |
| `ecommerce` | products, orders, order_items, customers    |

```bash
alter import templates/ecommerce.alter
```

---

## Git Merge Driver

A custom merge driver that resolves `schema.alter` conflicts structurally (by table name, not by line number). Two developers add different tables on different branches? Clean merge. No manual conflict resolution.

**One-time setup (per machine):**

```bash
git config --global merge.alter.name   "Alter schema merge driver"
git config --global merge.alter.driver "alter merge-driver %O %A %B"
```

Add to your repo's `.gitattributes`:

```
*.alter merge=alter
```

After that, Git uses the driver automatically whenever a `.alter` file is involved in a merge or rebase.

**How it resolves conflicts:**

- **Independent additions**: auto-merged.
- **Independent deletions**: auto-merged.
- **Identical edits on both sides**: auto-merged.
- **One side modifies, other deletes**: conflict (keeps the modified version).
- **Both sides modify differently**: conflict (keeps yours).
- **Canvas positions**: yours are preserved; theirs are used for new tables.

---

## Commands

| Command              | What it does                                                  |
| -------------------- | ------------------------------------------------------------- |
| `alter init`         | Create `schema.alter` from existing ORM model files (`--force` to overwrite) |
| `alter canvas`       | Open the interactive ERD in your browser                      |
| `alter apply`        | Write schema changes to your ORM model files (`--preview` for dry run) |
| `alter sync`         | Update `schema.alter` from your ORM model files               |
| `alter add`          | Add tables from a model file to the schema                    |
| `alter diff`         | Show pending changes (`--format markdown` for PR-ready output) |
| `alter validate`     | Check your schema for errors and warnings                     |
| `alter export`       | Export as SQL DDL, Mermaid ERD, or `.alter` JSON              |
| `alter import`       | Import tables from a `.sql` or `.alter` file                  |
| `alter merge-driver` | Git merge driver for `.alter` files                           |
| `alter mcp`          | Start the MCP server                                          |

---

## Supported ORMs

- **SQLModel**: auto-detected from `from sqlmodel import ...`
- **SQLAlchemy 2.0** (declarative): auto-detected from `from sqlalchemy import ...`

---

## License

MIT
