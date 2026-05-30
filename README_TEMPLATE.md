# Databricks agent-on-apps genie-mcp hackathon

<p align="center">
  <img src="assets/databricks-mark.svg" alt="Databricks" height="40" />
</p>


https://github.com/user-attachments/assets/b8d78543-a91e-4b22-973c-a7fd568d9b40


Based on the [agent-langgraph-advanced app template](https://github.com/databricks/app-templates/tree/main/agent-langgraph-advanced).

## Local development

### Prerequisites

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- The [Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/install) authenticated with a profile (`databricks auth login --profile <profile>`)
- A Lakebase instance (autoscaling) — required for agent memory

### 1. Bootstrap the environment

`quickstart` writes a `.env`, configures auth, and points the agent at your Lakebase.

```bash
uv run quickstart
```

Arguments:

- `--profile <profile>` — Databricks CLI profile to write into `.env`
- `--lakebase-autoscaling-project <project>` — Lakebase autoscaling project name
- `--lakebase-autoscaling-branch <branch>` — branch within the autoscaling project

Verify the profile is valid afterwards:

```bash
databricks auth profiles
```

> All `databricks` CLI commands need the profile from `.env` — either `--profile <name>` or `DATABRICKS_CONFIG_PROFILE=<name> databricks ...`.

### 2. Wire up the MCP tools (optional)

Genie space (creates or reuses, then rewrites `databricks.yml` + `utils.py` URL):

```bash
uv run create-genie-space
```

Arguments:

- `--profile <profile>` — Databricks CLI profile
- `--title "<space title>"` — Genie space title to create or reuse
- `--warehouse-id <id>` *(optional)* — SQL warehouse ID; falls back to the workspace default

The script edits `databricks.yml` (replaces the matching resource block) and `agent_server/utils.py` (rewrites the URL on the matching `DatabricksMCPServer` entry) in place — reruns are idempotent.

### 3. Run the agent locally

```bash
uv run start-app
```

This starts the FastAPI agent server on port 8000 and the chat UI on port 3000. With `DATABRICKS_APP_NAME` unset, the user workspace client falls back to the `DATABRICKS_CONFIG_PROFILE` profile from `.env`, so you can talk to MCP tools end-to-end without the Apps OBO header.

To explore what tools/resources are available in the workspace:

```bash
uv run discover-tools
```

### 4. Deploy to Databricks Apps

Deploy the bundle:

```bash
databricks bundle deploy
```

Arguments:

- `--profile <profile>` — Databricks CLI profile

Grant the app's SP the Lakebase grants it needs for memory tables:

```bash
uv run grant-lakebase-permissions
```

Arguments:

- `--profile <profile>` — Databricks CLI profile
- `--app-name <app>` — the app whose service principal gets the grants
- `--memory-type langgraph` — schema set to grant against (matches this template)

Run the bundle:

```bash
databricks bundle run
```

Arguments:

- `agent_langgraph_advanced_genie` *(positional)* — bundle resource name to run
- `--profile <profile>` — Databricks CLI profile

> `grant-lakebase-permissions` has to run *after* the bundle deploy because the app's service principal client ID only exists once the app is created.
