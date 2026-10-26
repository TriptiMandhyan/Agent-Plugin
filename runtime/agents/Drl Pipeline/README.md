# DRL Pipeline Agent Bundle

This bundle provides a complete multi-agent workflow for transforming CQL (Clinical Quality Language) artifacts into DRL (Drools Rule Language) deliverables with strict approval and traceability gates.

## Bundle Structure

- **Agent definitions**: Individual `.agent.md` files in the parent `.github/agents/` folder
- **Shared DRL Pipeline instructions**: `copilot-instructions.md` in this folder
- **Workflow documentation**: `WORKFLOW.md` in this folder
- **Runtime scripts**: `scripts/` subfolder
- **Configuration**: `config/` subfolder
- **Templates**: `templates/` subfolder
- **State files**: `state/` subfolder
- **Documentation**: `docs/` subfolder
- **DRL Examples**: `drl-examples/` subfolder

## How This Is Organized

- Agent definitions (`*.agent.md`) live in `.github/agents/` (parent folder)
- Shared DRL Pipeline rules and runtime constraints live in `copilot-instructions.md` (this folder)
- Workflow and file dependency documentation in `WORKFLOW.md` (this folder)
- Agent runtime/tooling lives in this `Drl Pipeline/` folder
- State files and artifacts are generated under `.github/agents/Drl Pipeline/artifacts/<ticket-id>/`

## Expected Usage

From VS Code:
1. Ensure `.github/agents/Drl Pipeline/state/run-input.json` exists with `ticketId` or `ticketUrl`
2. Ensure `.github/agents/Drl Pipeline/state/tickets-index.json` exists
3. Start the local MCP server with the VS Code task `Start Local Python MCP Server` before selecting an agent
4. Open Chat in Agent mode
5. Select `/drlFlowOrchestrator` from the agent picker to run the full pipeline
6. Or select individual agents for specific stages

Primary agents:
- `drlFlowOrchestrator`: End-to-end stage coordination with approval gate
- `drlSourceIntake`: Combined ticket normalization, PR/source resolution, CQL fetch, extraction, and plan output
- `drlBaselineBuilder`: Prior-year DRL template retrieval and year roll-forward baseline creation
- `drlApprovalRecorder`: Explicit approval capture before PR submission
- `drlConverter`: DRL generation and clause-to-rule mapping
- `drlApiBuilder`: Local-python MCP auth+data API retrieval into canonical measure JSON artifact
- `drlTestBuilder`: Java test file generation by cloning the previous-year file and applying year-only updates
- `drlPrSubmitter`: PR draft generation and final stage update
- `drlTicketReset`: Ticket reset to baseline state
- `drlLearning`: Optional non-blocking metrics and pattern aggregation

Notes:
- Keep agent names stable because slash commands and config references depend on them.
- Keep DRL generation logic in conversion flow and skill files; wrapper metadata should remain orchestration-only.

## First-Time Local Setup

- Clone this repository and, for local DRL lookups, place the `rules-engine` checkout beside it (or set
	`RULES_ENGINE_HOST_PATH` to the checkout). Configure credentials such as `ADO_PAT` and `GITHUB_TOKEN` in
	the environment that launches the server/client; do not commit them. See the local runbook for ticket input.
- **VS Code Chat:** Copilot CLI is not needed. VS Code reads `.vscode/mcp.json`; run the
	`Start Local Python MCP Server` task before selecting a DRL agent. Selecting an agent does **not** start
	Docker Desktop or the HTTP `local-python` server. If Docker Desktop's Linux engine is already running,
	the task builds the local image on first use and starts the container. If Docker is absent or stopped,
	the task instead starts Python from the workspace `.venv` (or system Python) and installs the bundle's
	MCP requirements if needed; **no Docker image is built**. Keep the task terminal open in Python mode.
- **Copilot CLI:** Install Copilot CLI, Docker Desktop, and Node.js for the ADO MCP server. CLI reads
	per-user `~/.copilot/mcp-config.json`, not `.vscode/mcp.json`; configure `local-python`, `github-content`,
	and `ado-content` there without storing tokens in the file. From the repository root, run
	`& ".github/agents/Drl Pipeline/scripts/start_copilot_cli.ps1"`. The launcher starts Docker Desktop
	if necessary, builds/starts local-python, checks `/health`, pulls the GitHub MCP image if missing,
	and opens the DRL agent. The first build/download needs network access; later launches reuse images.
	`-PreflightOnly` checks prerequisites without opening CLI. Confirm registrations with `copilot mcp list`;
	a listing alone does not prove server authentication.

Docker Desktop stores the images in its own image store, **not** in the repository. Run `docker image ls`
or open Docker Desktop's Images view to see `drlpipeline-local-python:latest` and the pinned
`ghcr.io/github/github-mcp-server` image. The latter serves CLI `github-content`; it is separate from
the long-running local-python container. The Dockerfile installs Python dependencies into the image,
but scripts come from the host bundle bind-mounted at `/app`, and ticket state/artifacts remain on disk.

## Starting the Local MCP Server

The VS Code task `Start Local Python MCP Server` runs `scripts/start_local_mcp.ps1 -Mode auto`. Only DRL agents need this server; non-DRL agents use the on-demand `shared-tools` server ([.github/tools/shared_mcp](../../tools/shared_mcp/README.md)).

- If the server on `127.0.0.1:8766` is already healthy, it does nothing.
- It loads the repo-root `.env` into its own process. Variables already set win, and placeholder `replace_with…` values are skipped.
- It resolves the rules-engine checkout from `RULES_ENGINE_HOST_PATH` / `RULES_ENGINE_REPO_PATH`. If that path does not exist, it falls back to sibling `../rules-engine` and warns if none is found.
- If the Docker daemon is running, it starts the container (below).
- Otherwise it runs the server with Python from `.venv` (Python 3.10+), installing `config/mcp/requirements-local-mcp.txt` if needed. The task terminal must stay open; use **Terminate Task** to stop it. The `Stop Local Python MCP Server` task only stops the Docker container.

The task chooses Docker only if its engine is already running; it does not launch Docker Desktop.
To use the container in VS Code, start Docker Desktop first, then run the task. CLI startup is different:
its launcher attempts to start Docker Desktop and requires Docker (the CLI's GitHub MCP server is a Docker image). It reuses a server already running on 8766 instead of starting a second one.

Force a mode with the task `Start Local Python MCP Server (Python, no Docker)` or
`.\.github\agents\Drl Pipeline\scripts\start_local_mcp.ps1 -Mode python|docker`. Both modes serve the same URL,
so MCP config and agents need no changes. Credentials come from the environment that starts the server.

### Without Docker

Everything works the same in Python mode: all DRL tools, the same URL, and the same paths. Python mode was verified end to end (health, tool list, a tool call over MCP). The differences:

| Topic | Docker mode | Python mode |
|---|---|---|
| Requirements | Docker Desktop (Linux engine) | `.venv` with Python 3.10+; MCP packages install on first start |
| Lifetime | Keeps running after the task ends; stop with the Stop task | Lives in the task terminal; stop with **Terminate Task** (closing VS Code stops it) |
| Code changes | `docker compose ... restart` | Terminate and start the task again |
| File access | Only mounted folders: this bundle, orchestration, and the optional `rules-engine` mount | Whole machine |
| Secrets | From the environment running Compose (plus `.env` via the start script) | Same |

Edge cases:

- **`.env` is read only by the start scripts.** VS Code-launched MCP servers such as `ado-content` (`${env:ADO_PAT}`) and `github-content` (`${env:GITHUB_TOKEN}`) read VS Code's own environment. Set those as user environment variables, or start VS Code from a shell that has them.
- **Port 8766 already in use** by another program: the task reports it as unhealthy. Stop that program first.
- **Two windows or developers on one machine** share one server. The second start reuses the first.
- **macOS/Linux:** tasks and agent commands assume Windows (`powershell`, `.venv\Scripts\python.exe`). Use Docker mode, or run `.venv/bin/python ".github/agents/Drl Pipeline/scripts/local_mcp_server.py"` with the env vars set. On Linux, files the container writes into `runs/` are owned by root.

## Local Docker MCP Server

Start Docker Desktop with the Linux engine, then run the VS Code task `Start Local Python MCP Server`.
From the workspace root, the equivalent command is:

```powershell
docker compose -f ".github/agents/Drl Pipeline/compose.yaml" -f ".github/agents/Drl Pipeline/compose.rules-engine.yaml" up -d --build
```

Drop the override file when no rules-engine checkout is available. `start_local_mcp.ps1` and `start_copilot_cli.ps1` add it automatically when they find `../rules-engine` or `RULES_ENGINE_HOST_PATH` / `RULES_ENGINE_REPO_PATH`.

`up` creates/starts the container, `-d` leaves it running after the task exits, and `--build` rebuilds the
Python image when dependencies change. On a fresh Docker machine, Compose builds this image locally;
it is not shipped in the git checkout. The bundle is bind-mounted at `/app`, so local changes to scripts,
state, and artifacts are visible on both sides. After changing Python code, run
`docker compose -f ".github/agents/Drl Pipeline/compose.yaml" restart` to reload it; rebuilding alone
does not restart an unchanged container. After changing the Dockerfile or MCP dependency requirements,
run the Compose `up -d --build` command above to rebuild the image. The CLI launcher normally uses
`up -d --wait` without forcing a rebuild on each session.
The port is published only to `127.0.0.1:8766`, preserving the existing VS Code MCP URL.

Check `http://127.0.0.1:8766/health` or run `docker compose -f ".github/agents/Drl Pipeline/compose.yaml" ps`.
Use the VS Code task `Stop Local Python MCP Server` (or `docker compose -f ".github/agents/Drl Pipeline/compose.yaml" down`)
to stop it. `down` does not delete the host bundle's state or artifacts. Do not start the Python mode while the
container is running; both use port 8766.

The container receives `ADO_PAT`, `GITHUB_TOKEN`, `API_AUTH_BASIC_TOKEN`, `LIBRARIAN_API_KEY`, and
`LIBRARIAN_API_SECRET` from the environment running Compose, when set. Set only the credentials your tools
need; do not put secrets in Compose files or commit a `.env` file. VS Code tasks inherit VS Code's process
environment, not variables set in another terminal after VS Code was opened. For a one-off run, set variables
in the terminal where you invoke the Compose command instead. Anyone with access to your local Docker daemon
can inspect container environment variables; this setup is for a trusted local machine.

The start task mounts the sibling `rules-engine` checkout read-only at `/rules-engine` and sets
`RULES_ENGINE_REPO_PATH` so MCP lookups read local DRLs first. If that checkout lives elsewhere,
set `RULES_ENGINE_HOST_PATH` in the environment running Compose before starting it:

```powershell
$env:RULES_ENGINE_HOST_PATH = "C:\dev\rules-engine"
docker compose -f ".github/agents/Drl Pipeline/compose.yaml" -f ".github/agents/Drl Pipeline/compose.rules-engine.yaml" up -d --build
```

The checkout must exist; Compose will not create an empty one. If no checkout is available, run with
only `compose.yaml` (without the override); lookups can fall back to GitHub when configured.
The stop task works for either variant. File paths passed to MCP tools must refer to paths inside the
container (for example `/app/artifacts/...`), not `C:\...` paths on the host.

Start the server once when beginning a DRL work session and leave it running for agent calls;
stop it when finished. Agents should not manage Docker: they need this MCP server running before
they can call its tools. Neither Compose nor the VS Code task starts it automatically at login.

## Parent Rules-Engine Reference

A local rules-engine checkout is recommended (not required). Without it, `read_rules_engine_file` and baseline
lookups fall back to GitHub (`GITHUB_TOKEN`), but `check_drl_already_updated` cannot detect already-implemented
changes and `publish_approved_pr_files` skips mapper JSON merges. The DRL Pipeline does **not** need a local
`measures` checkout; it downloads CQL through the ADO API.

This bundle can run alongside or as a submodule of the **rules-engine** repo. The already-shipped DRLs under
`src/main/resources/com/ablehealth/measure/<program>/year<YYYY>/` are the authoritative source of truth and
outrank broad guide material. The local-python MCP tool `read_rules_engine_file(path, ref="main")` reads those
files **local working copy first** (no token, no network) and falls back to the GitHub API only when the file is
absent on disk. In Docker, Compose mounts the sibling checkout at `/rules-engine` and sets
`RULES_ENGINE_REPO_PATH` to that path. Without Docker, the server can find a parent rules-engine checkout
in the submodule layout or use `RULES_ENGINE_REPO_PATH` to point to a separate checkout.

The converter (`drlConverter`) references the parent rules-engine at three points:
1. **Understand patterns (before editing)** — read the matching current/prior-year peer DRL to learn the real rule shape, signatures, and option-id handling.
2. **Confirm patterns (after applying)** — re-read the same peer DRL and verify the applied rule matches its shipped shape; fix any divergence not justified by a CQL delta.
3. **Confirm (after conversion)** — `conversion-complete` is gated on having confirmed each changed rule against a same-program/same-year parent peer, with the peer paths recorded in `mapping.md`.

