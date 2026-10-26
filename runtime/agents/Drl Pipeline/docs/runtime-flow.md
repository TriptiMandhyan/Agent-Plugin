# Runtime Flow Guide

This guide covers the complete workflow for transforming CQL artifacts into DRL rules. Complete setup prerequisites first, then start with `/drlFlowOrchestrator`; it runs the pre-approval stages automatically through conversion and then pauses at the approval gate for explicit human approval.

## End-to-End Workflow Diagram

```mermaid
flowchart TD
    START([Start Run])
    ENV[Set env vars: ADO_PAT, GITHUB_TOKEN]
    INPUT[run-input.json]
    CFG[.github/orchestration/config.json]
    MCP[.vscode/mcp.json loaded by VS Code]

    ORCHCMD[/drlFlowOrchestrator]
    ORCHFILE[.github/agents/drl-flow-orchestrator.agent.md]

    SOURCECMD[/drlSourceIntake]
    SOURCEFILE[.github/agents/drl-source-intake.agent.md]
    INTAKEOUT[.github/agents/Drl Pipeline/artifacts/<ticket-id>/intake.json]
    PIPE[.github/agents/Drl Pipeline/artifacts/<ticket-id>/state/pipeline-status.json]

    EXTRACTOUT[.github/agents/Drl Pipeline/artifacts/<ticket-id>/extraction.json]

    BASELINECMD[/drlBaselineBuilder]
    BASELINEFILE[.github/agents/drl-baseline-builder.agent.md]
    GHFETCH[MCP tool: github-content/* file retrieval\n(baseline DRL current year, then year-1, then year-2)]
    BASELINEWRITE[MCP tool: local-python.build_prior_year_measure_template\n(ticket_id, source_year, source_path, source_content)]
    BASELINEOUT[.github/agents/Drl Pipeline/artifacts/<ticket-id>/drls/baseline/<measure-slug>.drl\n.github/agents/Drl Pipeline/artifacts/<ticket-id>/baseline/<measure-slug>-baseline.json\n.github/agents/Drl Pipeline/artifacts/<ticket-id>/cqls/baseline/<measure-slug>-source-cql-<year>.cql\n.github/agents/Drl Pipeline/artifacts/<ticket-id>/patches/<measure-slug>-cql-diff.patch]

    PLANOUT[.github/agents/Drl Pipeline/artifacts/<ticket-id>/plan.md]
    APPROVALSTATE[.github/agents/Drl Pipeline/artifacts/<ticket-id>/state/approval-status.json]

    APPROVECMD[/drlApprovalRecorder]
    APPROVEFILE[.github/agents/drl-approval-recorder.agent.md]

    CONVERTCMD[/drlConverter]
    CONVERTFILE[.github/agents/drl-converter.agent.md]
    MIPSSKILL[.github/skills/mips-drl/SKILL.md]
    HEDISSKILL[.github/skills/hedis-drl/SKILL.md]
    CQMSKILL[.github/skills/cqm-drl/SKILL.md]
    CONVERTOUT[.github/agents/Drl Pipeline/artifacts/<ticket-id>/drls/generated/<measure-slug>.drl\n.github/agents/Drl Pipeline/artifacts/<ticket-id>/mapping.md]
    VALIDATECMD[MCP tool: local-python.validate_conversion_artifacts\n(ticket_id=<ticket-id>)]

    APICMD[/drlApiBuilder]
    APIFILE[.github/agents/drl-api-builder.agent.md]
    APIOUT[.github/agents/Drl Pipeline/artifacts/<ticket-id>/pr-assets/<measure-slug>.json]

    TESTCMD[/drlTestBuilder]
    TESTFILE[.github/agents/drl-test-builder.agent.md]
    TESTOUT[.github/agents/Drl Pipeline/artifacts/<ticket-id>/pr-assets/Year<measureYear><measureFamilyPascal><measureNumber>Test.java]

    PRCMD[/drlPrSubmitter]
    PRFILE[.github/agents/drl-pr-submitter.agent.md]
    PROUT[.github/agents/Drl Pipeline/artifacts/<ticket-id>/pr.md]

    START --> ENV --> INPUT --> MCP --> ORCHCMD
    ORCHCMD --> ORCHFILE
    ORCHFILE --> CFG

    ORCHCMD --> SOURCECMD --> SOURCEFILE --> INTAKEOUT --> EXTRACTOUT --> PLANOUT --> APPROVALSTATE --> PIPE
    PIPE --> BASELINECMD --> BASELINEFILE --> GHFETCH --> BASELINEWRITE --> BASELINEOUT --> PIPE
    PIPE --> CONVERTCMD --> CONVERTFILE
    CONVERTFILE --> MIPSSKILL
    CONVERTFILE --> HEDISSKILL
    CONVERTFILE --> CQMSKILL
    CONVERTCMD --> CONVERTOUT --> VALIDATECMD --> PIPE
    PIPE --> APPROVECMD --> APPROVEFILE --> APPROVALSTATE --> PIPE
    PIPE --> APICMD --> APIFILE --> APIOUT --> PIPE
    PIPE --> TESTCMD --> TESTFILE --> TESTOUT --> PIPE
    PIPE --> PRCMD --> PRFILE --> PROUT --> PIPE
```

## Running from the Actions Tab

You can trigger the pipeline directly from the **GitHub Actions tab** without a local environment.

### One-time repository setup

1. Go to **Settings → Environments** and create an environment named **`copilot`**.
2. Add the following **Environment secrets**:

   | Secret | Value |
   |---|---|
   | `ADO_PAT` | Azure DevOps personal access token (Work Items: Read, Code: Read) |
   | `API_AUTH_BASIC_TOKEN` | Basic auth token for `librarian.admin.ablehealth.com` |
   | `GIT_TOKEN_RULES_ENGINE` | Fine-grained (or classic) PAT for cross-repo access to `AbleHealth/rules-engine` |

3. Verify the Copilot cloud-agent environment by running **Actions → Copilot Setup Steps → Run workflow** and confirming the job completes (all green).

### Starting a pipeline run from Actions

1. Navigate to **Actions → CQL to DRL Orchestrator – Run**.
2. Click **Run workflow** and fill in the inputs:
   - `ticket_url` – full ADO work-item URL, **or**
   - `ticket_id` – numeric ADO ticket ID
   - `resume_stage` – optional canonical pipeline stage override (for recovery/resume scenarios)
3. Click **Run workflow**.

The workflow will:
- Install Python dependencies and start the local Python MCP server.
- Initialize `run-input.json` and `tickets-index.json` (and update `run-input.json` from workflow inputs when provided).
- Validate that `run-input.json` contains a usable `ticketUrl` or `ticketId`.
- Verify MCP server health.
- Print a job summary with the current pipeline stage and next-step instructions.

4. When the job completes, open **GitHub Copilot Chat** in the repository and type:

```
/drlFlowOrchestrator
```

The orchestrator reads the pipeline stage written by the Actions workflow and picks up from there.

### Approval gate

The approval gate remains enforced after conversion/DRL generation. Record approval in Copilot Chat with `/drlApprovalRecorder` after reviewing conversion artifacts (DRL, mapping.md, patches).

---

## Setup Prerequisites (local VS Code)

Before running any workflow locally, complete the following one-time setup.

### 1. Install Dependencies

Install the local MCP server requirements:

```powershell
python -m pip install -r config/mcp/requirements-local-mcp.txt
```

### 2. Set Environment Variables

Set your authentication tokens in your PowerShell session:

```powershell
$env:ADO_PAT = "<your-ado-personal-access-token>"
$env:GITHUB_TOKEN = "<your-github-personal-access-token>"
$env:API_AUTH_BASIC_TOKEN = "<your-basic-auth-token-for-auth-api>"
```

To persist these across sessions, use:

```powershell
setx ADO_PAT "<your-ado-personal-access-token>"
setx GITHUB_TOKEN "<your-github-personal-access-token>"
setx API_AUTH_BASIC_TOKEN "<your-basic-auth-token-for-auth-api>"
```

### 3. Start the Local MCP Server

Open a separate PowerShell terminal and run:

```powershell
python .github/agents/Drl Pipeline/scripts/local_mcp_server.py
```

You should see output like:

```
INFO:     Uvicorn running on http://127.0.0.1:8766
```

**This server must be running before you execute any workflow stage.** The server provides local-python MCP tools for state management, CQL processing, and validation.

If you modify `.github/agents/Drl Pipeline/scripts/local_mcp_server.py` or `.github/agents/Drl Pipeline/scripts/state_manager.py`, restart this process to load your changes.

### 4. Prepare Your Input

Create `run-input.json` with your ticket ID:

```json
{
  "ticketId": "1024330"
}
```

Alternatively, use a full ticket URL:

```json
{
  "ticketUrl": "https://dev.azure.com/healthcatalyst/MeasureAble%202.0/_workitems/edit/1024330/"
}
```

## Workflow Configuration

The workflow is configured by:

- **Manifest**: `.github/orchestration/config.json` (defines agents, states, guardrails)
- **Cloud-agent MCP**: `.github/copilot/mcp.json` (Copilot cloud agent discovers MCP servers here)
- **Local MCP (VS Code)**: `.vscode/mcp.json` (VS Code loads MCP servers from here)
- **Orchestrator Agent**: `.github/agents/drl-flow-orchestrator.agent.md` (coordinates all stages)

## Running the Workflow

### Start the Orchestrator

In VS Code Copilot Chat, run:

```
/drlFlowOrchestrator
```

The orchestrator will:
1. Read your ticket ID from `run-input.json`
2. Check/resolve current pipeline status
3. Run all valid pre-approval stages automatically through `/drlConverter`
4. Pause once and request `/drlApprovalRecorder` with explicit approver identity

### Manual Stage Commands (fallback / debugging)

Use manual stage commands only for troubleshooting, partial reruns, or targeted recovery:

1. **`/drlSourceIntake`** - Extracts PR links/CQL paths, fetches CQL, and generates extraction artifacts + plan
2. **`/drlBaselineBuilder`** - Fetches baseline DRL (current year first, then fallback), rolls year to current year when needed, writes baseline artifacts
3. **`/drlConverter`** - Applies CQL deltas to baseline DRL and writes conversion outputs
4. **`/drlApprovalRecorder`** - Records human approval (only step that requires user input)
5. **`/drlApiBuilder`** - Retrieves auth token and writes canonical measure JSON artifact
6. **`/drlTestBuilder`** - Clones previous-year Java test file from rules-engine and applies year-only roll-forward
7. **`/drlPrSubmitter`** - Generates PR draft and opens a pull request

## Verification: What to Check After Each Stage

| Stage | Expected Outputs | Location |
|-------|-----------------|----------|
| **Intake** | Resolved PR links and CQL paths | `.github/agents/Drl Pipeline/artifacts/<ticket-id>/intake.json` |
| **Extraction** | Parsed CQL rules, generation plan | `.github/agents/Drl Pipeline/artifacts/<ticket-id>/extraction.json` <br/> `.github/agents/Drl Pipeline/artifacts/<ticket-id>/plan.md` |
| **Baseline Template** | Baseline DRL template (prefer current year) rolled to current year when needed, metadata, baseline-year CQL snapshot, and CQL diff | `.github/agents/Drl Pipeline/artifacts/<ticket-id>/drls/baseline/<measure-slug>.drl` <br/> `.github/agents/Drl Pipeline/artifacts/<ticket-id>/baseline/<measure-slug>-baseline.json` |
| **Conversion** | Generated DRL files and mapping | `.github/agents/Drl Pipeline/artifacts/<ticket-id>/drls/generated/*.drl` <br/> `.github/agents/Drl Pipeline/artifacts/<ticket-id>/mapping.md` |
| **API Artifact** | Authenticated payload captured as measure JSON | `.github/agents/Drl Pipeline/artifacts/<ticket-id>/pr-assets/<measure-slug>.json` |
| **Test Artifact** | Previous-year Java test cloned and year-rolled for current year | `.github/agents/Drl Pipeline/artifacts/<ticket-id>/pr-assets/Year<measureYear><measureFamilyPascal><measureNumber>Test.java` |
| **Approval** | Approval decision recorded and PR unlock stage set | `.github/agents/Drl Pipeline/artifacts/<ticket-id>/state/approval-status.json` <br/> (`approved: true`) |
| **PR Submission** | Draft PR with validation summary | `.github/agents/Drl Pipeline/artifacts/<ticket-id>/pr.md` |

After each stage completes successfully, check the output files to confirm the next stage's inputs are correct.

## State Management

The workflow maintains per-ticket state in `.github/agents/Drl Pipeline/artifacts/<ticket-id>/state/`:

- **`pipeline-status.json`** â€“ Current stage and progress
- **`approval-status.json`** â€“ Approval decision and timestamp
- **`history.json`** â€“ Timeline of stage transitions
- **`.github/agents/Drl Pipeline/state/tickets-index.json`** â€“ Master index tracking all tickets (shared)

You can view the current status:

```powershell
Get-Content ".github/agents/Drl Pipeline/artifacts/<ticket-id>/state/pipeline-status.json"
```

## Resetting a Ticket

If you need to restart a ticket from the beginning:

```
/drlTicketReset
```

This command will:
- Reset pipeline stage to `intake-start`
- Clear approval status
- Remove ticket-scoped workflow artifacts and ticket-scoped state so rerun starts clean

## MCP Tools Reference

These tools are available during workflow execution:

| Tool | Purpose | Example Use |
|------|---------|-------------|
| `local-python.get_pr_changes` | Fetch PR metadata including commit details | Called during intake |
| `local-python.download_and_extract_cql` | Download all CQL fully and write extraction.json | Called during extraction |
| `local-python.read_rules_engine_file` | Read any rules-engine file by path/ref, local working copy first with GitHub fallback (current/prior-year DRL and prior-year test source) | Called during baseline and test artifact stages |
| `local-python.build_prior_year_measure_template` | Write rolled baseline artifacts and required baseline CQL diff assets; `targetFileName` keeps the mixed-case DRL name for publish | Called during baseline template stage |
| `local-python.validate_conversion_artifacts` | Validate generated DRL | Called after conversion |
| `local-python.state_read_pipeline` | Read current pipeline status | For reporting/debugging |
| `local-python.state_read_approval` | Read approval status | For reporting/debugging |
| `local-python.state_write_pipeline` | Update pipeline stage | Used by agents automatically |
| `local-python.state_write_approval` | Record approval decision | Used by approval agent |
| `local-python.learning_record_execution` | Log stage timing and success (optional) | Background metrics only |

All state operations use MCP toolsâ€”**do not edit state files directly**.

## Troubleshooting

### "MCP tool not found" Error

**Cause**: Local MCP server is not running.  
**Fix**: Start the server in a separate terminal: `python .github/agents/Drl Pipeline/scripts/local_mcp_server.py`

### "ticket-id not found in state files"

**Cause**: `.github/agents/Drl Pipeline/state/run-input.json` is missing or doesn't contain `ticketId`.  
**Fix**: Create/update `.github/agents/Drl Pipeline/state/run-input.json` with a valid ticket ID.

### "PR submission blocked: not approved"

**Cause**: Approval status is `false`; you skipped or didn't complete `/drlApprovalRecorder`.  
**Fix**: Run `/drlApprovalRecorder` and provide approver details to unlock PR submission.

### "CQL did not download again"

**Cause**: CQL fetching is performed by `/drlSourceIntake` in the standard flow. If you resume from `baseline-template-ready`, `conversion-ready`, or later, extraction is skipped and no new fetch occurs.  
**Fix**: Re-run source intake for the ticket. If you want a clean re-run, execute `/drlTicketReset`, then run `/drlSourceIntake` or `/drlFlowOrchestrator`.

### "Source intake ran but I still see old CQL content"

**Cause**: Fetch is commit-pinned to intake PR metadata (`sourceCommitId`). If intake is stale, source intake may re-fetch the same commit snapshot.  
**Fix**: Re-run `/drlSourceIntake` to refresh PR metadata and fetched CQL together.

### "Empty DRL file generated"

**Cause**: CQL extraction found no valid rules or conversion has placeholder-only logic.  
**Fix**: Check `.github/agents/Drl Pipeline/artifacts/<ticket-id>/extraction.json` for parsed rules. If empty, review the source CQL for syntax errors.

### "PR creation failed"

**Cause**: GitHub token is invalid or the target branch already exists.  
**Fix**: Verify `GITHUB_TOKEN` is set correctly. Check if you need to reset the ticket and try again.

### Changes Not Reflecting After Code Update

**Cause**: You edited `.github/agents/Drl Pipeline/scripts/local_mcp_server.py` or `.github/agents/Drl Pipeline/scripts/state_manager.py` but didn't restart the server.  
**Fix**: Stop the local MCP server (Ctrl+C) and restart it to load your changes.

## Key Files

| File | Purpose |
|------|---------|
| `.github/orchestration/config.json` | Manifest defining agents, states, and guardrails |
| `.github/agents/drl-flow-orchestrator.agent.md` | Main DRL orchestrator agent |
| `.github/agents/drl-source-intake.agent.md` | Canonical combined ticket/CQL source stage |
| `.github/agents/*.agent.md` | Individual stage agents |
| `.github/skills/mips-drl/SKILL.md` | Dynamic MIPS conversion skill |
| `.github/skills/hedis-drl/SKILL.md` | Dynamic HEDIS conversion skill, including CE and max coverage |
| `.github/skills/cqm-drl/SKILL.md` | Dynamic CQM/CMS/eCQM conversion skill |
| `.github/skills/drl-review-rules/SKILL.md` | Pointer to the rules-engine PR-review rules |
| `scripts/local_mcp_server.py` | Local MCP server implementation (DRL tools) |
| `.github/tools/shared_mcp/` | Tools for non-DRL agents, served by the separate on-demand `shared-tools` server (not this one) |
| `.github/agents/Drl Pipeline/scripts/state_manager.py` | Per-ticket state management |
| `scripts/learning_engine.py` | Background metrics aggregation |
| `.vscode/mcp.json` | MCP server configuration for VS Code |

## Directory Structure

```
devopsToPRAgents/
|- .github/
|  |- orchestration/              # Workflow configuration
|  |- agents/                     # Stage agents and orchestrator
|  `- skills/<name>/SKILL.md      # Program DRL skills (hedis-drl, mips-drl, cqm-drl, drl-review-rules)
|- scripts/                       # Helper scripts and MCP server
|- artifacts/                     # Ticket-scoped generated outputs
|  `- <ticket-id>/
|     |- cqls/{baseline,extraction}/
|     |- drls/{baseline,generated}/
|     |- patches/
|     |- baseline/
|     |- pr-assets/
|     `- state/
|- run-input.json                 # Current ticket input
|- tickets-index.json             # Master ticket index
|- state/learning/                # Aggregated metrics and patterns
|  |- execution-metrics.json
|  |- cql-patterns.json
|  `- error-patterns.json
`- config/mcp/                    # MCP dependencies and configuration
```

## Advanced: Parallel Ticket Processing

The workflow supports processing multiple tickets concurrently. Each ticket maintains its own state in `.github/agents/Drl Pipeline/artifacts/<ticket-id>/state/`:

1. Create multiple `run-input.json` snapshots or use a job queue
2. Each orchestrator invocation reads its own `run-input.json`
3. Per-ticket state directories prevent conflicts
4. You can run `/drlFlowOrchestrator` for different tickets in parallel windows

Ticket state is isolated; one ticket's failure does not affect others.
