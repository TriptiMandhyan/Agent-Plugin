# Workspace Instructions: DRL Pipeline Agents

> **Bundle Location**: This file is part of the `Drl Pipeline` bundle in `.github/agents/Drl Pipeline/`.  
> All paths are relative to the bundle folder unless otherwise specified.

## Mission

Build and operate a local multi-agent workflow that transforms CQL artifacts into DRL deliverables with strict approval and traceability gates.

## Available MCP Tools (Quick Reference)

**CRITICAL: Use these tools directly. Never ask users to run PowerShell/REST commands manually.**

### ADO Operations
- `mcp_azure_devops__wit_work_item` - Get one or more work items
- `mcp_azure_devops__search_workitem(searchText, project)` - Search work items
- `mcp_azure_devops__search_code(searchText, project, repository)` - Search code
- `mcp_azure_devops__repo_repository` - List or get repositories

### Local Python MCP (Pipeline State & Operations)
- `mcp_local-python-_state_get_current_ticket()` - Get ticket ID from run-input.json
- `mcp_local-python-_state_read_pipeline(ticket_id)` - Read pipeline stage
- `mcp_local-python-_state_write_pipeline(stage, ticket_id)` - Update pipeline stage
- `mcp_local-python-_state_read_approval(ticket_id)` - Read approval status
- `mcp_local-python-_state_write_approval(approved, approved_by, notes, ticket_id)` - Set approval (approver required when approved=true)
- `mcp_local-python-_write_intake_json(intake, ticket_id)` - Validate and write intake.json (source intake only)
- `mcp_local-python-_download_and_extract_cql(ticket_id)` - Download all CQL fully and write extraction.json
- `mcp_local-python-_write_intake_plan(content, ticket_id)` - Write plan.md and reset approval to false (source intake only)
- `mcp_local-python-_write_mapping_md(content, ticket_id)` - Validate and write mapping.md (converter only)
- `mcp_local-python-_build_current_year_tests(ticket_id)` - Build all needed current-year Java tests (test builder only)
- `mcp_local-python-_open_drl_pull_request(ticket_id)` - Open/reuse the PR from the template and write pr.md (PR submitter only)
- `mcp_local-python-_generate_drl_patch(ticket_id, measure_slug)` - Generate DRL patch

**Never suggest PowerShell commands like `Invoke-RestMethod` when these MCP tools exist.**

## Core Rules

1. **[MCP DIRECT USAGE]** Always call MCP tools directly. NEVER ask users to run PowerShell, curl, or manual REST commands. All ADO, GitHub, and pipeline operations have MCP tools available - use them.
2. Never proceed to PR submission without explicit approval state set to approved.
3. Preserve ticket id, source CQL file paths, and commit references in every output artifact.
4. Do not fabricate unknown business logic. Mark assumptions explicitly.
5. **[PATH CONTAINMENT]** All generated artifacts MUST be created inside `.github/agents/Drl Pipeline/` only. **NEVER** create files at workspace root or outside this agent folder. Use MCP tools for all file operations to enforce path containment. See [Path Guardrails](#path-guardrails) section.
6. Never store secrets in files. Use environment variables only.
7. Use MCP tools only for ADO metadata/content/state operations; do not use direct REST URL/PAT calls from agent workflows.
8. Intake cannot complete unless either (a) `.github/agents/Drl Pipeline/artifacts/<ticket-id>/intake.json` has non-empty `cqlPaths`, or (b) the ticket is an explicit no-PR/no-CQL flow (for example CMS partial-fix) with explicit measure context (`measureSlug`, `measureFamily`, `measureYear`). Extraction cannot complete unless raw CQL content is fetched for required paths when `cqlPaths` are present; in explicit no-CQL flows, extraction may be skipped and the pipeline proceeds to baseline selection.
9. Never assume approval from prior runs or existing state; PR submission requires an explicit approval command in the current run with an approver name provided at approval time.
10. PR packaging must include supplemental measure JSON (from authenticated API response) and measure Java test file required by rules-engine runtime, unless the current run explicitly requests a testing-phase `case for tests` and librarian JSON is unavailable.
11. Before PR creation, the ticket URL from intake input must be placed under the `Add link to Tracker Story` comment in PR content using `ADO Work Item: <ticketUrl>`.
12. **[MCP-ONLY PUBLISH]** Publish only through `local-python` MCP tools. Never run `git commit`, `git push`, or branch changes in the `rules-engine` checkout, even though non-DRL agents publish with git.

## Case For Tests

- `case for tests` is a non-default testing-phase exception.
- Use it only when the current run explicitly asks for it. Never infer or prioritize it automatically.
- In a `case for tests`, PR packaging may omit the supplemental measure JSON only when librarian JSON is unavailable, the Java test artifact is present, and the PR clearly documents the missing JSON as a testing blocker.
- A `case for tests` does not relax approval, traceability, DRL validation, or test artifact requirements.

## Resume From Any Stage (Read State First — Trust Existing Artifacts)

The pipeline is resumable: a run may start from ANY agent depending on the status files. Every agent —
especially small models — MUST follow this contract instead of re-doing finished work:

1. **Read `pipeline-status.json` first** (`state_read_pipeline`). It is the single source of truth for where
   the ticket is. Also read `approval-status.json` (`state_read_approval`) when your stage needs approval.
2. **Confirm your precondition stage** from the table below. If the current stage is already at or past your
   output stage, you have nothing to do — report and stop (or hand back to the orchestrator).
3. **TRUST the on-disk artifacts.** Inputs from earlier stages already exist under
   `artifacts/<ticket-id>/`. Read them; never re-fetch, re-extract, re-baseline, or re-convert to "verify"
   them. Local state files (not ADO / source PR) are the authority for approval and progress.
4. **Do ONLY your stage's work** and write ONLY your stage's outputs + the next stage marker. Never call an
   upstream stage's tools — doing so is the #1 cause of wrong behavior and wasted work.

| Agent | Precondition stage(s) it reads | Reads (input artifacts) | Writes / advances to |
| --- | --- | --- | --- |
| drlSourceIntake | (empty/error) | run-input.json | intake.json, extraction.json → `extraction-complete` |
| drlBaselineBuilder | `extraction-complete` | intake.json, extraction.json | drls/baseline, baseline/*.json → `baseline-template-ready` |
| drlConverter | `baseline-template-ready` / `conversion-ready` | baseline DRLs, CQL diffs | drls/generated, patches, mapping.md → `conversion-complete` |
| drlApprovalRecorder | `conversion-complete` / `awaiting-approval` | mapping.md, patches | approval-status.json → `pr-ready` (human gate) |
| drlApiBuilder | `pr-ready` | intake.json (approved) | pr-assets/<slug>.json → `api-artifact-ready` |
| drlTestBuilder | `api-artifact-ready` | pr-assets, baseline meta | pr-assets/Year*Test.java → `test-artifact-ready` |
| drlPrSubmitter | `pr-ready` or later | approval + generated DRLs + pr-assets | pushes commit + opens PR → `pr-submitted` |

**Hard rule:** never re-run a stage whose output already exists. If `pipeline-status.json` says `pr-ready`,
the conversion and approval are DONE — downstream agents publish/build from disk and never touch ADO, CQL,
or baseline tools again.

## Agent Handoff Contract

Each phase must produce machine-readable handoff outputs. All artifacts live under `.github/agents/Drl Pipeline/artifacts/<ticket-id>/`:

- Intake: `.github/agents/Drl Pipeline/artifacts/<ticket-id>/intake.json`
- Extraction: `.github/agents/Drl Pipeline/artifacts/<ticket-id>/extraction.json`
- Plan: `.github/agents/Drl Pipeline/artifacts/<ticket-id>/plan.md` and `.github/agents/Drl Pipeline/artifacts/<ticket-id>/state/approval-status.json`
- Baseline Template:
   - `.github/agents/Drl Pipeline/artifacts/<ticket-id>/drls/baseline/<measure-slug>.drl` (baseline DRL rolled to current year)
   - `.github/agents/Drl Pipeline/artifacts/<ticket-id>/cqls/baseline/<measure-slug>-source-cql-<year>.cql` (baseline source CQL)
   - `.github/agents/Drl Pipeline/artifacts/<ticket-id>/patches/<measure-slug>-cql-diff.patch` (year-over-year CQL diff)
   - `.github/agents/Drl Pipeline/artifacts/<ticket-id>/baseline/<measure-slug>-baseline.json` (metadata JSON — used by validator)
- Conversion:
   - `.github/agents/Drl Pipeline/artifacts/<ticket-id>/drls/generated/<measure-slug>.drl` (one per changed CQL)
   - `.github/agents/Drl Pipeline/artifacts/<ticket-id>/patches/<ticket-id>-<measure-slug>-drl.patch` (baseline DRL to generated DRL)
   - `.github/agents/Drl Pipeline/artifacts/<ticket-id>/mapping.md`
- API Artifact: `.github/agents/Drl Pipeline/artifacts/<ticket-id>/pr-assets/<measure-slug>.json` (default path; optional only for an explicit `case for tests`)
- Test Artifact: `.github/agents/Drl Pipeline/artifacts/<ticket-id>/pr-assets/Year<measureYear><measureFamilyPascal><measureNumber>Test.java`
- PR: `.github/agents/Drl Pipeline/artifacts/<ticket-id>/pr.md`

## Definition of Done

A ticket is done only if:

- Plan is approved by human reviewer.
- Baseline DRL template was fetched (current year first, then year-1, then year-2 fallback) and rolled to current year when needed before delta conversion.
- DRL compiles against local validation checks (or known blockers are documented).
- Conversion DRL patch artifacts exist and show baseline-to-generated deltas for each changed CQL.
- Baseline CQL comment blocks in DRL are preserved and only ticket-relevant CQL changes are reflected in corresponding comment text.
- Mapping from CQL clauses to DRL rules is documented.
- Measure JSON file is generated from authenticated API response and prepared for PR, unless the ticket is being handled as an explicit `case for tests` with the missing librarian JSON documented in the PR.
- Measure Java test file is generated in canonical year/measure/rate naming pattern and prepared for PR.
- PR draft content is generated with validation summary.

## Orchestrator Execution Mode

When the user invokes `/drlFlowOrchestrator`, treat it as a full pipeline execution command (not a next-step suggestion prompt):

1. Run environment preflight first:
   - Ensure `.github/agents/Drl Pipeline/state/run-input.json` and `.github/agents/Drl Pipeline/state/tickets-index.json` exist (initialize if missing).
   - Ensure required secrets/environment variables are present for MCP-backed operations.
   - Check local Python MCP server health at `http://<LOCAL_MCP_HOST>:<LOCAL_MCP_PORT>/health` (defaults: `127.0.0.1:8766`) and require response `{"status":"ok"}`. Keep host loopback-only (`127.0.0.1`/`localhost`) unless explicitly configured for remote access in trusted environment configuration.
   - If health check fails, start the server with `.github/agents/Drl Pipeline/scripts/start_local_mcp.ps1` (Docker when running, otherwise Python from `.venv`).
   - After startup, run a blocking health retry loop (default: 20 attempts, 2 seconds between attempts; max wait ~40 seconds, aligned with workflow setup defaults). If health still fails, stop and report setup failure in the form `MCP preflight failed: /health did not return {"status":"ok"} after 20 attempts` and include troubleshooting steps: verify server process is running, verify host/port, and check local MCP server logs.
2. Resolve the current ticket from `.github/agents/Drl Pipeline/state/run-input.json` and read ticket-scoped pipeline state.
3. **Execute the valid stages automatically and directly** (do NOT ask for permission; call the stage agent immediately) in-order until conversion is complete:
   - `/drlSourceIntake`
   - `/drlBaselineBuilder`
   - `/drlConverter`
4. Stop exactly once at the approval gate and ask for explicit human approval using `/drlApprovalRecorder approvedBy='<name>' note='<optional note>'` (double quotes are also acceptable depending on chat shell parsing).

**Critical**: Do NOT ask the user for permission to run stages 1-4. Execute them directly. Only pause and ask for approval after stage 4 (conversion-complete).
If approval is not granted, do not continue to post-approval stages; remain at approval gate state and allow user-directed rework (for example rerun conversion inputs or run `/drlTicketReset`).

---

## Path Guardrails

### Critical Rule: Agent Folder Containment

**All DRL Pipeline agents MUST create files only inside `.github/agents/Drl Pipeline/`.**

### Enforcement Mechanisms

1. **MCP Tool Path Resolution**: The local Python MCP server enforces path containment:
   ```python
   ROOT_DIR = Path(__file__).resolve().parent.parent
   # Evaluates to: .github/agents/Drl Pipeline/
   ```
   All MCP tools construct paths as `ROOT_DIR / "artifacts" / ticket_id / ...`

2. **Agent File Creation Rules**:
   - [YES] **ALWAYS** use MCP tools for file operations (`write_intake_json`, `write_intake_plan`, `write_generated_drl`, `write_mapping_md`, `build_current_year_tests`, `open_drl_pull_request`, `state_write_pipeline`, etc.)
   - [YES] If an agent has no MCP writer for a file it must produce, report it as a blocker; never delegate the write to a generic agent or a shell script
   - [NO] **NEVER** use relative paths that could resolve outside the agent folder
   - [NO] **NEVER** create files at workspace root (e.g., `artifacts/`, `state/` without `.github/agents/Drl Pipeline/` prefix)

3. **Path Validation Checklist**:
   - [ ] Does the path start with `.github/agents/Drl Pipeline/`?
   - [ ] Are you using an MCP tool that enforces path containment?
   - [ ] For GitHub publishing, is `workspace_file_path` inside agent folder?

### Standard Artifact Paths

All artifacts follow this structure:

```
.github/agents/Drl Pipeline/
├── artifacts/
│   └── <ticket-id>/
│       ├── state/
│       │   ├── pipeline-status.json
│       │   ├── approval-status.json
│       │   └── history.json
│       ├── cqls/
│       │   ├── baseline/<measure>-source-cql-<year>.cql
│       │   └── extraction/<measure>.cql
│       ├── drls/
│       │   ├── baseline/<measure>.drl
│       │   └── generated/<measure>.drl
│       ├── patches/
│       │   ├── <ticket-id>-<measure>-drl.patch
│       │   └── <measure>-cql-diff.patch
│       ├── baseline/
│       │   └── <measure>-baseline.json
│       ├── pr-assets/
│       │   ├── <measure>.json
│       │   └── Year<year><family><num>Test.java
│       ├── intake.json
│       ├── extraction.json
│       ├── plan.md
│       ├── mapping.md
│       └── pr.md
├── state/
│   ├── run-input.json
│   └── tickets-index.json
└── templates/
    └── (static templates)
```

### Troubleshooting Wrong Paths

**Symptom**: Files created at workspace root instead of `.github/agents/Drl Pipeline/`

**Diagnosis**:
1. Check if agent bypassed MCP tools
2. Look for hardcoded relative paths in agent prompts
3. Verify Python server working directory
4. Check for direct `create_file` calls without full path prefix

**Fix**:
- Use MCP tool `organize_ticket_artifacts(ticket_id)` at stage start
- Prefix all workspace file operations with `.github/agents/Drl Pipeline/`
- Never assume current working directory is agent folder

### MCP Tool Reference

For complete MCP tool catalog with path guarantees, see:  
**[MCP Tools Reference](docs/MCP_TOOLS_REFERENCE.md)**

---

## Specialized Conversion Patterns

### Program-Specific DRL Skills

Keep specialized conversion patterns out of static context. Source intake should tag relevant program skills in `plan.md`, and conversion should load only the matching skill when needed:

- HEDIS, including continuous enrollment and max coverage: `.github/skills/hedis-drl/SKILL.md`
- MIPS: `.github/skills/mips-drl/SKILL.md`
- CQM/CMS/eCQM: `.github/skills/cqm-drl/SKILL.md`
- All programs (pointer to rules-engine PR-review rules): `.github/skills/drl-review-rules/SKILL.md`

When this agent repo runs as a rules-engine submodule, read the parent rules-engine DRLs with `read_rules_engine_file` (local working copy first, GitHub fallback) for concrete examples before applying a pattern, and re-read the same peer DRL to confirm the applied rule matches the shipped shape after editing. The parent repo is the source of truth and outranks broad guide material.

### Shared DRL Pattern for Measure Families

**CRITICAL**: Many measure families have a shared DRL file containing common logic used by all measures in the family. These MUST be included in baseline fetching and conversion.

**Examples of Shared DRL Patterns**:
- **TRC** family: trca, trcb, trce, trce7, trcm → **trcshared.drl**
- **AAP** family: aap1, aap2 → **aapshared.drl**
- **FUM** family: fum7, fum30 → **fumshared.drl**
- **UOP** family: uopa, uopb, uopc → **uopshared.drl**
- **IET** family: ietaal, ietaop, ietaot, ietbt → **ietShared.drl** (note camelCase)
- **GSD** family: gsd1, gsd2 → **gsdshared.drl**
- **HDO** family: hdo → **hdoshared.drl**
- **CIS** family: ciscmb10, cisdtp, etc. → **CISshared.cql** (ADO), **cisshared.drl** (GitHub)
- **DRR** family: drra, drrb, drrc → **DRRshared.cql** (ADO), **drrshared.drl** (GitHub)

**Naming Convention**:
- ADO Measures repo: `{FamilyPrefix}shared.cql` (e.g., TRCshared.cql, UOPshared.cql)
- GitHub rules-engine: `{familyprefix}shared.drl` (e.g., trcshared.drl, uopshared.drl) — usually all lowercase

**Why Shared DRLs Exist**:
- Common enrollment logic across all measures in family
- Shared exclusion rules (hospice, deceased, etc.)
- Common demographic/population filters
- Shared helper functions/rules referenced by individual measures

**Detection and Handling**:
1. **Baseline Builder**: Call `build_shared_drl_baseline(ticket_id)` after fetching individual measure baselines
   - Automatically detects if multiple measures in a family exist
   - Checks GitHub for shared DRL pattern (e.g., trcshared.drl)
   - Fetches and rolls year forward if found
   - Saves to `.github/agents/Drl Pipeline/artifacts/<ticket-id>/drls/baseline/trcshared.drl`

2. **Converter**: Process ALL .drl files in baseline directory
   - Scan `.github/agents/Drl Pipeline/artifacts/<ticket-id>/drls/baseline/*.drl`
   - This includes both individual measures AND shared DRLs
   - Call `generate_drl_patch(ticket_id, "trcshared")` for the shared DRL

**Critical Rules**:
- [YES] Always call `build_shared_drl_baseline()` after individual baseline fetching
- [YES] Process shared DRL files in converter alongside individual measures
- [YES] Generate patches for shared DRLs just like individual measures
- [NO] **NEVER skip shared DRLs** - they contain critical common logic
- [NO] **NEVER assume no shared DRL exists** - always check

**If Shared DRL Not Found**:
- Normal for single-measure families (e.g., URI, BCS, CBP)
- Normal for families that don't use shared pattern
- Document in mapping.md that no shared DRL exists for this family

**Example Workflow (TRC Family)**:
```
1. Baseline Builder:
   - build_prior_year_measure_template(ticket_id, source_cql_path="/measures/2026/HEDIS/TRCA.cql")
   - build_prior_year_measure_template(ticket_id, source_cql_path="/measures/2026/HEDIS/TRCB.cql")
   - build_prior_year_measure_template(ticket_id, source_cql_path="/measures/2026/HEDIS/TRCE.cql")
   - build_prior_year_measure_template(ticket_id, source_cql_path="/measures/2026/HEDIS/TRCE7.cql")
   - build_prior_year_measure_template(ticket_id, source_cql_path="/measures/2026/HEDIS/TRCM.cql")
   - build_shared_drl_baseline(ticket_id) → Fetches trcshared.drl

2. Result in `.github/agents/Drl Pipeline/artifacts/<ticket-id>/drls/baseline/`:
   - trca.drl
   - trcb.drl
   - trce.drl
   - trce7.drl
   - trcm.drl
   - trcshared.drl ← Shared DRL!

3. Converter:
   - Scan baseline directory → Find 6 .drl files
   - generate_drl_patch(ticket_id, "trca")
   - generate_drl_patch(ticket_id, "trcb")
   - generate_drl_patch(ticket_id, "trce")
   - generate_drl_patch(ticket_id, "trce7")
   - generate_drl_patch(ticket_id, "trcm")
   - generate_drl_patch(ticket_id, "trcshared") ← Don't forget this!
```

