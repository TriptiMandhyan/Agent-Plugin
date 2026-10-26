# Local Runbook

> **Tip:** This runbook covers local VS Code setup.  For the GitHub Actions / Copilot cloud-agent path, see [runtime-flow.md → Running from the Actions Tab](runtime-flow.md#running-from-the-actions-tab).

## 0) GitHub environment secrets (one-time)

If you intend to use the GitHub Actions entry point or Copilot cloud agent, configure these secrets under **Settings → Environments → copilot** first:

| Secret | Purpose |
|---|---|
| `ADO_PAT` | Azure DevOps personal access token (Work Items: Read, Code: Read) |
| `API_AUTH_BASIC_TOKEN` | Basic auth token for `librarian.admin.ablehealth.com` |
| `GIT_TOKEN_RULES_ENGINE` | Fine-grained PAT for cross-repo access to `AbleHealth/rules-engine` |

> **Local sessions:** locally, set `GITHUB_TOKEN` in your shell (the scripts read that env var directly).  `GIT_TOKEN_RULES_ENGINE` is the *GitHub environment secret name* used by Actions workflows, which map it to `GITHUB_TOKEN` at runtime.

## 1) Create ADO PAT

In Azure DevOps:

1. Open User Settings > Personal access tokens.
2. Select New Token.
3. Set organization to `healthcatalyst` and a short expiry window.
4. Scopes minimum needed for this local flow:
	- Work Items: Read
	- Code: Read
5. Create token and copy it once.

## 2) Configure local secret

Set environment variable in your shell session:

```bash
export ADO_PAT="<pat>"
```

Windows persistent equivalent:

```text
setx ADO_PAT "<pat>"
```

If your session still cannot reach Azure DevOps after setting the PAT, verify DNS/network first:

```bash
getent hosts dev.azure.com
curl -I https://dev.azure.com/healthcatalyst/_apis/projects?api-version=7.1-preview.4
```

- If DNS lookup fails, fix local DNS/VPN/firewall settings before running agents.
- If DNS works but the HTTP request returns 401/403, fix the PAT scopes or token value.
- The Task Agent sandbox cannot override blocked outbound DNS; use VS Code or GitHub Actions when you need a network-enabled environment.

## 3) Set ticket URL placeholder

Edit `run-input.json` and set:

- `ticketUrl`: `https://dev.azure.com/healthcatalyst/MeasureAble%202.0/_workitems/edit/1024330/`

The intake step will parse ticket description and resolve PR links such as:

- `https://dev.azure.com/healthcatalyst/MeasureAble%202.0/_git/measures/pullrequest/<pr-id>`

The workflow uses PR source branch refs, not main branch, when PR links are present.

If no PR links are present, the flow skips CQL retrieval entirely and uses GitHub DRL baseline selection only.

## 3.1) MANDATORY: Start local FastAPI MCP server

For first-time VS Code versus CLI setup, Docker image creation, and credentials, start with the
[bundle README](../README.md#first-time-local-setup). Selecting a DRL agent in VS Code does not
automatically launch Docker Desktop or this HTTP server; start the task before selecting the agent.
The task builds the image on first use only when Docker's Linux engine is already running. Otherwise
it runs Python locally and does not build an image.

Use the VS Code task `Start Local Python MCP Server`, or run from the workspace root:

```powershell
.\.github\agents\Drl Pipeline\scripts\start_local_mcp.ps1        # auto: Docker if running, else Python
.\.github\agents\Drl Pipeline\scripts\start_local_mcp.ps1 -Mode python   # no Docker
```

Python mode uses `.venv`, installs `config/mcp/requirements-local-mcp.txt` when missing, and points
`RULES_ENGINE_REPO_PATH` at a sibling `../rules-engine` checkout when present. The bundle README documents
credentials, the optional rules-engine mount, and stopping the server.

This process must be running before invoking slash agents that depend on local-python MCP tools.

If you change MCP/state code (for example `.github/agents/Drl Pipeline/scripts/local_mcp_server.py` or `.github/agents/Drl Pipeline/scripts/state_manager.py`), restart this process before making MCP calls.

The local MCP URL configured in `.vscode/mcp.json` is:

- `http://127.0.0.1:8766/mcp`

## 3.2) MCP Call Discipline (Lesson from Ticket 1024340)

**Golden rule:** Before calling any MCP tool, ask: "Do I already have this data from an earlier successful call in this conversation?"

**Anti-patterns to avoid:**
- Do NOT make test/noop MCP calls (e.g., `mcp_ado_repo_create_pull_request(..., title="noop")`).
- Do NOT re-call ADO MCP to re-validate PR data already fetched in intake.
- Do NOT make MCP calls without a clear reason for progress; unused calls waste tokens and surface as auth errors.

**Proven pattern (Ticket 1024340 intake-to-extraction):**
1. **Intake stage:** Use ADO MCP once:
  - `mcp_azure_devops__wit_work_item` -> capture ticket URL, description, linked PR.
  - `mcp_azure_devops__repo_pull_request` -> capture PR source branch, source commit.
  - Store in `.github/agents/Drl Pipeline/artifacts/<ticket-id>/intake.json` (persisted state).
2. **Extraction stage:** Use local-python MCP only:
  - `mcp_local-python_download_and_extract_cql(ticket_id)` -> wraps script, uses stored PR commit, downloads all raw CQL and writes extraction.json.
   - No need to re-call ADO MCP; ticket_id is the lookup key to all prior PR metadata.
3. **Why this works:** State artifacts (`.github/agents/Drl Pipeline/artifacts/<ticket-id>/intake.json`, `.github/agents/Drl Pipeline/artifacts/<ticket-id>/extraction-fetched.json`) carry forward all necessary context; MCP calls are unidirectional (fetch once, reuse context).

**Implementation check:**
-  Before any ADO MCP call outside intake: check if data already exists in `.github/agents/Drl Pipeline/artifacts/<ticket-id>/intake.json`.
-  Before any local-python MCP call: check if `.github/agents/Drl Pipeline/artifacts/<ticket-id>/intake.json` exists (required input).
-  If a required tool is unavailable, document the blocker in pipeline state and stop the blocked stage instead of retrying noop calls.

## 4) Initialize ticket state directory

Before running agents, ensure the root state files exist. You can either:

**Option A: Use the initialization script (recommended)**

```powershell
python scripts/init_state_structure.py
```

This will:
- Create `tickets-index.json` (master index of all tickets)
- Create template `run-input.json` if missing

**Option B: Manual setup**

```powershell
# Create the root state files if they don't exist
if (!(Test-Path tickets-index.json)) { '{"currentTicketId": null, "lastUpdated": "", "tickets": {}}' | Out-File -Encoding utf8 tickets-index.json }
if (!(Test-Path run-input.json)) { '{"ticketUrl": ""}' | Out-File -Encoding utf8 run-input.json }
```

The state files used by the workflow are:

```
.github/agents/Drl Pipeline/state/run-input.json                  (current ticket input)
.github/agents/Drl Pipeline/state/tickets-index.json              (master index, maintains all ticket IDs and stages)
.github/agents/Drl Pipeline/artifacts/<ticket-id>/state/
  pipeline-status.json          (created by source intake and subsequent stages)
  approval-status.json          (initialized by source intake)
  history.json                  (optional, created by agents)
```

These ticket-scoped files are part of the workflow output. They are no longer ignored by git, so they can be reviewed or committed when needed for a run.

## 5) Execute stages with agents

Run stages from Copilot chat using slash agents:

1. `/drlSourceIntake`
2. `/drlBaselineBuilder`
3. `/drlConverter`
4. `/drlApprovalRecorder` (single call with approver identity and optional note)
5. `/drlApiBuilder`
6. `/drlTestBuilder`
7. `/drlPrSubmitter`

## 5.1) Local MCP helper tools

When MCP is preferred but a stage needs HTTPS fallback for capabilities not currently exposed in ADO MCP, use these local MCP tools:

- `local-python.get_pr_changes(project_url, repository, pull_request_id)` - Fetch PR metadata including commit details
- `local-python.download_and_extract_cql(ticket_id="<ticket-id>")` - Download all CQL fully and write extraction.json in one deterministic call
- `local-python.state_read_pipeline(ticket_id?)` / `local-python.state_write_pipeline(...)`
- `local-python.state_read_approval(ticket_id?)` / `local-python.state_write_approval(...)`
- `local-python.state_reset_ticket_baseline(ticket_id)`

Reset policy:

- Use `local-python.state_reset_ticket_baseline(...)` as the primary and preferred reset mechanism.
- Do not use shell-based cleanup for ticket reset unless explicitly approved as a break-glass action.
- If reset reports JSON/BOM parsing failures, restart local MCP server first, then retry reset.
- Keep state JSON files UTF-8 clean; the local MCP/state readers are BOM-tolerant (`utf-8-sig`) for resilience.

Outputs:

- Updated `.github/agents/Drl Pipeline/artifacts/<ticket-id>/intake.json` (`cqlPaths` enriched in place)
- Raw CQL files under `.github/agents/Drl Pipeline/artifacts/<ticket-id>/cqls/extraction/...` for PR-linked tickets only
- Fetch manifest at `.github/agents/Drl Pipeline/artifacts/<ticket-id>/extraction-fetched.json` for PR-linked tickets only

Notes:

- `ADO_PAT` must be set in environment.
- `.github/agents/Drl Pipeline/artifacts/<ticket-id>/intake.json` must exist.
- `get_pr_changes` is the primary tool for fetching PR metadata during intake; it returns complete commit information.
- `download_and_extract_cql` maps to `scripts/ado_extraction_fallback.py download-and-extract`: it downloads every commit-pinned raw `.cql` and writes `extraction.json`, failing closed on missing/empty/truncated content.
- `state_*` tools map to `.github/agents/Drl Pipeline/scripts/state_manager.py` and are required for state reads/writes.
- The CQL helper does not update pipeline state; stage transitions stay with `state_*` tools.

## 5.2) CQL Fetch and Extraction Flow (from Ticket 1024340)

**Prerequisite:** `.github/agents/Drl Pipeline/artifacts/<ticket-id>/intake.json` must exist. In the standard flow it is written by `/drlSourceIntake`, which then fetches CQL and writes extraction outputs in the same stage.

This section applies only when intake found PR links. In no-PR mode, the workflow does not download CQL and proceeds directly to GitHub DRL baseline selection.

**Flow:**
1. Call `local-python.download_and_extract_cql(ticket_id="<ticket-id>")` via MCP. This single deterministic call:
  - Produces: `.github/agents/Drl Pipeline/artifacts/<ticket-id>/cqls/extraction/...` (full raw CQL files)
  - Produces: `.github/agents/Drl Pipeline/artifacts/<ticket-id>/extraction-fetched.json` (fetch manifest with PR commit and raw file paths)
  - Produces: `.github/agents/Drl Pipeline/artifacts/<ticket-id>/extraction.json` (normalized definitions/includes/terminology, with source commit preserved)
  - Fails closed (non-zero) if any cqlPath is unresolved, empty, undecodable, or truncated.
2. `/drlSourceIntake` does not hand-write CQL or `extraction.json`; it only verifies the tool returned `ok=true` and that `extraction-fetched.json` has no `unresolvedCqlPaths`/`invalidRawFiles`.

**Example from Ticket 1024340:**
```json
{
  "ticketId": "1024340",
  "sourceType": "pr-changed-cql-files",
  "files": [{
    "prId": "109928",
    "repository": "measures",
    "path": "/measures/2026/MIPS/050.cql",
    "sourceCommitId": "62b743dc6b482a3c6f49dd99aced8a3a9511bf10",
    "definitions": [
      { "name": "Denominator", "line": 28 },
      { "name": "Denominator Exclusion G9694", "line": 55 }
    ],
    "includes": [
      { "library": "2026/MIPS/shared/sharedHospice.cql", "alias": "sharedHospice", "line": 11 }
    ],
    "terminology": [
      { "kind": "valueset", "name": "MIPS; 050_ENCOUNTER", "identifier": null, "line": 14 }
    ]
  }],
  "status": "extraction-complete"
}
```

**Why this pattern works:**
- [x] Source commit is persisted in extraction artifact for DRL conversion reference.
- [x] Extracted definitions (with line numbers) enable direct CQL-to-DRL rule mapping.
- [x] No repeat ADO MCP calls; all PR metadata already in intake artifact.
- [x] Raw CQL files available for offline review/validation.

### 5.2.1) When CQL is downloaded (and when it is not)

- CQL download happens in source intake in the standard flow: `/drlSourceIntake` calls `local-python.download_and_extract_cql(ticket_id)` after writing intake metadata.
- The fetch is commit-pinned to the PR source commit stored in intake (`sourceCommitId`).
- On each extraction run, files are written to `.github/agents/Drl Pipeline/artifacts/<ticket-id>/cqls/extraction/...` and the fetch manifest is rewritten at `.github/agents/Drl Pipeline/artifacts/<ticket-id>/extraction-fetched.json`.
- If the pipeline is already at `baseline-template-ready`, `conversion-ready`, or later, and you continue from that stage, extraction is not re-run, so no new CQL download occurs.
- If intake found no PR links, extraction is skipped entirely and no CQL files or extraction manifest are created.

To force a fresh CQL download for the same ticket:
1. Run `/drlTicketReset`.
2. Run `/drlSourceIntake`.

This guarantees intake metadata is refreshed and commit-pinned raw CQL is fetched again before conversion.

## 5.3) Approval gate

`/drlSourceIntake` writes extraction artifacts for PR-linked tickets. The planning/approval stage initializes approval to false before `/drlApprovalRecorder` is allowed.

Approval and pipeline status are ticket-scoped at:

- `.github/agents/Drl Pipeline/artifacts/<ticket-id>/state/approval-status.json`
- `.github/agents/Drl Pipeline/artifacts/<ticket-id>/state/pipeline-status.json`

`/drlApprovalRecorder` performs one action only: record explicit human approval details and move pipeline to `pr-ready`.

## 6) Validate outputs

Check for generated files:

- `.github/agents/Drl Pipeline/artifacts/<ticket-id>/drls/generated/<measure-slug>.drl`
- `.github/agents/Drl Pipeline/artifacts/<ticket-id>/mapping.md`


Run conversion artifact validation via MCP tool (if available):

- `local-python.validate_conversion_artifacts(ticket_id="<ticket-id>")`

Validation checks naming, EMR fire-once guards, and reference-style fact modeling.

If the tool is disabled or unavailable, document in pipeline state (`details: "...validation tool unavailable..."`); do not retry noop calls.

For ticket `1024340`, expect:
- `.github/agents/Drl Pipeline/artifacts/<ticket-id>/drls/generated/<measure-slug>.drl` (example: `.github/agents/Drl Pipeline/artifacts/1024340/drls/generated/ecqm050rate1.drl`)
- `.github/agents/Drl Pipeline/artifacts/1024340/mapping.md`

