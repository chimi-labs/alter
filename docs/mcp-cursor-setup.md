# Setting up the Alter MCP Server in Cursor

## Prerequisites

- `alterdb` installed in your project's virtual environment (`pip install alterdb` or `uv add alterdb`)
- A project with SQLModel or SQLAlchemy models

---

## Step 1 — Initialize the schema file

```bash
uv run alter init
```

This scans your project for ORM model files and creates a `schema.alter` file at the project root. The MCP server requires this file to exist before it can start — it's the single source of truth for the schema.

If you're using plain `pip` instead of `uv`:

```bash
source .venv/bin/activate && alter init
```

---

## Step 2 — Add the MCP server config

Create (or edit) `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "alter": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/your/project", "alter", "mcp"]
    }
  }
}
```

Replace `/absolute/path/to/your/project` with the absolute path to your project root (where `schema.alter` lives).

> **Why `uv run` instead of calling `alter` directly?** `uv run` ensures the command runs inside your project's virtual environment, picking up the correct `alterdb` version and all its dependencies — no manual `source .venv/bin/activate` needed.

---

## Step 3 — Restart Cursor

The MCP server connects at startup, so **restart Cursor** (or reload the window) after adding the config. You can verify it's active by opening the Cursor chat and asking:

> _"What tools do you have available from alter?"_

You should see the alter tools listed in the response.

---

## What you can do once connected

Cursor can now interact with your schema directly. Example prompts:

- _"Show me the current schema"_
- _"Add a `tags` table with `id`, `name`, and a many-to-many join to `posts`"_
- _"Preview the migration SQL for pending changes"_
- _"Validate the schema for errors"_

Cursor will stage changes, show you a diff, and only commit them to `schema.alter` with your approval — nothing is written to your model files until you also run `alter apply`.
