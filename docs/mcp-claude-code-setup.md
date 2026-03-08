# Setting up the Alter MCP Server in Claude Code

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

## Step 2 — Register the MCP server in Claude Code

```bash
claude mcp add alter -- uv run --directory /path/to/your/project alter mcp
```

Replace `/path/to/your/project` with the absolute path to your project root (where `schema.alter` lives).

This command registers a named MCP server called `alter` in Claude Code's project-level config (`~/.claude.json`). It tells Claude Code to launch the alter MCP server via stdio whenever a session starts in this project.

The resulting config entry looks like this:

```json
{
  "mcpServers": {
    "alter": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/your/project", "alter", "mcp"]
    }
  }
}
```

> **Why `uv run` instead of calling `alter` directly?** `uv run` ensures the command runs inside your project's virtual environment, picking up the correct `alterdb` version and all its dependencies — no manual `source .venv/bin/activate` needed.

---

## Step 3 — Start a new Claude Code session

The MCP server connects at session startup, so **open a new Claude Code session** in your project after registering it. You can verify it's active by asking Claude:

> _"What tools do you have available from alter?"_

---

## What you can do once connected

Claude Code can now interact with your schema directly. Example prompts:

- _"Show me the current schema"_
- _"Add a `tags` table with `id`, `name`, and a many-to-many join to `posts`"_
- _"Preview the migration SQL for pending changes"_
- _"Validate the schema for errors"_

Claude will stage changes, show you a diff, and only commit them to `schema.alter` with your approval — nothing is written to your model files until you also run `alter apply`.
