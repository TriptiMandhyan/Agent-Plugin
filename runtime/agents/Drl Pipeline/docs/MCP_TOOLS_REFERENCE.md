# MCP Tools Reference: Local Python Server

> **Last Updated**: 2026-06-09  
> **Server**: `local-python-tools` (via FastAPI at `http://127.0.0.1:8766`)

This document catalogs all MCP tools available from the local Python server, defined in `scripts/local_mcp_server.py`. Tools for non-DRL agents (`publish_ado_changes`, Librarian downloads) are on the separate `shared-tools` server; see [.github/tools/shared_mcp/README.md](../../../tools/shared_mcp/README.md).

## Tool Invocation Pattern

All tools are prefixed with `mcp_local-python-_` in the MCP namespace. For example:
- Local tool name: `read_rules_engine_file`
- MCP invocation: `mcp_local-python-_read_rules_engine_file`

## Tools By Category

### State Management

#### `state_get_current_ticket()`
**Purpose**: Resolve current ticket ID from run-input.json  
**When to use**: Need to determine active ticket context  
**Returns**: `{ok, ticketId}`

#### `state_read_pipeline(ticket_id="")`
**Purpose**: Read pipeline status for a ticket  
**When to use**: Check current stage before proceeding  
**Parameters**:
- `ticket_id` (optional): Ticket ID, defaults to current ticket
**Returns**: `{ok, ticketId, pipeline: {stage, details, timestamp}}`

#### `state_write_pipeline(stage, details="", ticket_id="")`
**Purpose**: Write pipeline status using canonical stage validation  
**When to use**: Mark stage completion or transition  
**Parameters**:
- `stage` (required): One of: `intake-start`, `extraction-complete`, `baseline-template-ready`, `awaiting-approval`, `conversion-ready`, `conversion-complete`, `api-artifact-ready`, `test-artifact-ready`, `pr-ready`, `pr-submitted`
- `details` (optional): Additional context
- `ticket_id` (optional): Target ticket
**Returns**: `{ok, ticketId, stage, path, artifactBundle}`

#### `state_read_approval(ticket_id="")`
**Purpose**: Read approval status for a ticket  
**When to use**: Check if PR submission is authorized  
**Returns**: `{ok, ticketId, approval: {approved, approvedBy, notes, timestamp}}`

#### `state_write_approval(approved, approved_by="", notes="", ticket_id="")`
**Purpose**: Write approval status for a ticket  
**When to use**: Record explicit human approval for PR submission  
**Parameters**:
- `approved` (required): Boolean approval flag
- `approved_by` (optional): Name of approver
- `notes` (optional): Approval notes
- `ticket_id` (optional): Target ticket
**Returns**: `{ok, ticketId, approved, path}`

#### `state_reset_ticket_baseline(ticket_id="")`
**Purpose**: Reset a ticket to first-time baseline (removes all artifacts and state)  
**When to use**: Only from `/drlTicketReset` after the user explicitly requests a clean restart/reset for a ticket workflow. Never use for source intake, first run, normal orchestration, or stage completion.
**Returns**: `{ok, ticketId, deleted: [...], skipped: [...], baselineState}`

---

### Intake & Extraction

#### `write_intake_json(intake, ticket_id="")`

**Purpose**: Write `artifacts/<ticket-id>/intake.json`. This is the only supported way for source intake to create it; agents do not need a file-edit tool.

**Validation**: `ticketId` must match the resolved ticket; `projectUrl` is required. With `cqlPaths`, `resolvedPullRequests` must be a non-empty list of objects that each have `prId` and `sourceCommit`. With no `cqlPaths`, `measureFamily`, `measureSlug`, and `measureYear` are required.

**Returns**: `{ok, ticketId, path, cqlPathCount}` or `{ok: false, errors: [...]}`

---

#### `write_intake_plan(content, ticket_id="")`

**Purpose**: Write `plan.md` and reset `state/approval-status.json` to `approved=false` for the fresh run.

**Prerequisite**: `extraction.json` exists.

**Returns**: `{ok, ticketId, planPath, approvalPath}`

---

#### `download_and_extract_cql(ticket_id="")`

**Purpose**: Download every intake CQL fully, write `extraction.json`, and write each changed CQL's PR base→head diff to `patches/<slug>-cql-diff.patch` in one deterministic step (Step 1).

**When to use**: The source-intake CQL step. This is the authoritative producer of the raw CQL files, `extraction.json`, and the CQL diff patches; agents must never hand-write CQL or `extraction.json`. The diff patches reflect the actual merged change and are never empty regardless of PR completion/merge status.

**Parameters**:
- `ticket_id` (optional): Target ticket

**Returns**: `{ok, returnCode, command, stdout, stderr}` — non-zero if any cqlPath is unresolved, empty, undecodable, or truncated

---

#### `check_drl_already_updated(ticket_id="")`

**Purpose**: Step 2 — decide whether the local rules-engine DRL already contains the PR's CQL change.

**When to use**: Immediately after `download_and_extract_cql`, before baseline. For each changed CQL it matches distinctive added (green) lines from `patches/<slug>-cql-diff.patch` against the local DRL (each rule carries its CQL as a `/* define ... */` comment). Fails toward conversion (missing DRL/patch/green lines => not updated).

**Side effect**: When `alreadyUpdated=true`, writes the terminal pipeline stage `already-implemented`.

**Parameters**:
- `ticket_id` (optional): Target ticket

**Returns**: `{ok, ticketId, alreadyUpdated, recommendedAction, stageWritten, rulesEngineRoot, measures: [{slug, cqlPath, drlPath, drlExists, patchExists, alreadyUpdated, matchedLine, reason}]}`

---

### Baseline & Template Building

#### `build_prior_year_measure_template(ticket_id="", measure_slug="", current_year="", source_year="", source_path="", source_content="", source_cql_path="")`
**Purpose**: Build baseline DRL artifacts by fetching prior-year DRL content and rolling year tokens forward to the current year  
**When to use**: Need to fetch prior-year DRL and roll forward to current year  
**Parameters**:
- `ticket_id` (optional): Target ticket
- `source_cql_path` (preferred): CQL path; the slug comes from its file name
- `measure_slug` (optional): Explicit slug. Case is preserved for the GitHub lookup: `year<YYYY>/<exact-case>.drl` is tried first, then the lowercase name (so `sharedHospiceDuringPeriod` resolves). Artifact files stay lowercase.
- `source_path` (optional): Baseline DRL source path hint
- `source_year` (optional): Explicit source year (falls back to year-1, year-2 if omitted)
- `source_content` (optional): Supply baseline DRL text directly

**Returns**: `{ok, ticketId, measureSlug, targetFileName, currentYear, sourceYear, sourcePath, templatePath, metadataPath, yearReplacementCount, lookbackYears}`

**Baseline json**: `targetFileName` is the original-case rules-engine file name (for example `sharedHospiceDuringPeriod.drl`). `publish_approved_pr_files`, `validate_drl_changes` (mapper key), `generate_drl_patch` (returned `targetFileName`), and `write_mapping_md` (accepts slug or the targetFileName stem) use `targetFileName` when present; otherwise they fall back to the `sourcePath` file name, then `<slug>.drl`.

**Mixed-case generated DRLs**: `drls/generated/` files may use rules-engine casing (for example `sharedLTIDuringPeriod.drl`); slugs, `drls/baseline/<slug>.drl`, `baseline/<slug>-baseline.json`, and patches stay lowercase. Every tool and `validate_conversion_artifacts.py` resolve generated files by case-insensitive stem match (the server runs on a case-sensitive Linux mount) and derive slugs by lowercasing the stem. `write_generated_drl` and `initialize_generated_drls` write to the existing file in its current casing (no lowercase duplicate; `only_missing` counts it as present). Patch `+++` headers always use the lowercase `drls/generated/<slug>.drl` path, so renaming a generated file never changes its patch. `publish_approved_pr_files` refuses when two generated files differ only by case.

---

### Conversion & Validation

#### `validate_conversion_artifacts(ticket_id)`
**Purpose**: Run conversion artifact validation for a ticket  
**When to use**: After conversion stage, before PR submission  
**Parameters**:
- `ticket_id` (required): Target ticket
**Returns**: `{ok, returnCode, command, stdout, stderr}` (delegates to `validate_conversion_artifacts.py`)

**Accepted cases (warnings, not errors)**:
- Query-only shared DRLs (at least one `query` block and no `rule` blocks) produce `[INFO] Query-only shared DRL ... rule checks skipped`. A DRL with neither rules nor queries still fails with `No rules found`.
- A CQL comment block or patch line containing `...` is accepted, with an `[INFO]` warning, when that text appears verbatim in the ticket's source CQL (`artifacts/<ticket>/cqls/**/*.cql`, for example `cqls/extraction/`). This match is whitespace-insensitive (runs of spaces, tabs, and newlines collapse to one space) and ignores comment markers (`/*`, `*/`, leading `*` or `//`) on both sides. A bare `...` never matches. Every other `...` remains an error.

#### `initialize_generated_drls(ticket_id="", only_missing=false)`
**Purpose**: Copy every baseline DRL into `drls/generated/` as the conversion starting point  
**Parameters**:
- `only_missing` (optional, default `false`): The default overwrites every generated DRL with its baseline, which is the documented reset. With `true`, only baselines with no generated counterpart are copied, so in-progress edits survive (for example after adding a shared baseline mid-conversion).
**Returns**: `{ok, ticketId, copied: [...], skipped: [...], onlyMissing, ...}`; `skipped` lists generated DRLs that were kept

#### `validate_drl_changes(ticket_id, measure_slug)`
**Purpose**: Validate one generated DRL against its baseline  
**When to use**: Converter, after each `write_generated_drl`  
**Returns**: `{ok, ticketId, measureSlug, issues, warnings, generatedLines, baselineLines, message}`. `issues` fail `ok`. `warnings` prefixed `[rules-engine review <tag>]` flag new or changed code that the rules-engine PR reviewer would reject; they never fail `ok` (see `skills/drl-review-rules/SKILL.md`).

#### `generate_drl_patch(ticket_id="", slug="")`
**Purpose**: Generate unified diff patch from baseline to generated DRL  
**When to use**: After conversion, need patch artifact for PR  
**Parameters**:
- `ticket_id` (optional): Target ticket
- `slug` (optional): Measure slug
**Returns**: `{ok, ticketId, measureSlug, targetFileName, baselinePath, generatedPath, patchPath, patchSizeBytes}`

---

### Artifact Organization

#### `organize_ticket_artifacts(ticket_id="", clean=false)`
**Purpose**: Ensure ticket-first artifact directories exist under `.github/agents/Drl Pipeline/artifacts/<ticket-id>/`  
**When to use**: Before any stage writes artifacts, to guarantee directory structure  
**Parameters**:
- `ticket_id` (optional): Target ticket
- `clean` (optional): If true, removes existing artifacts before rebuilding
**Returns**: `{ok, ticketId, root, created: [...], present: [...]}`

**Creates**:
- `.github/agents/Drl Pipeline/artifacts/<ticket-id>/cqls/{baseline,extraction}`
- `.github/agents/Drl Pipeline/artifacts/<ticket-id>/drls/{baseline,generated}`
- `.github/agents/Drl Pipeline/artifacts/<ticket-id>/patches/`
- `.github/agents/Drl Pipeline/artifacts/<ticket-id>/baseline/`
- `.github/agents/Drl Pipeline/artifacts/<ticket-id>/pr-assets/`
- `.github/agents/Drl Pipeline/artifacts/<ticket-id>/state/`

---

### GitHub Operations

#### `read_rules_engine_file(path, ref="main")`
**Purpose**: Read a rules-engine file, local working copy first, GitHub API as fallback  
**When to use**: Read current/prior-year DRL or prior-year test source by repo-relative path  
**Parameters**:
- `path` (required): Repo-relative file path (e.g., `src/test/java/.../Year2025HedisTrcaTest.java`)
- `ref` (optional): GitHub fallback branch/ref, defaults to `main`
**Returns**: `{ok, path, content, contentLength, source}`

#### `publish_approved_pr_files(ticket_id="", branch="", owner="", repo="", message="")`
**Purpose**: The ONLY sanctioned repo write. Reads approved artifacts off disk, validates each changed DRL against its baseline AND its human-approved patch, rejects truncated/placeholder content, and commits all files (DRLs + test JSON + Java test + any modified config mappers) in ONE commit via the Git Data API. Config mappers (`config_load_rules/Mapper*.json`) are **ticket-scoped**: only registration entries referencing this ticket's measure slugs are pushed, merged onto the remote branch's mapper (never the whole local file, so branch/main drift is never carried). Files in the ticket's `pr-extra-files.json` are pushed whole when they differ from the branch. Never creates an empty/message-only commit (returns `noChanges` instead).
**When to use**: PR submission, after approval, to push the approved changes to the remote branch  
**Parameters**:
- `ticket_id` (optional): Target ticket
- `branch` (optional): Remote PR branch. When omitted it is resolved deterministically from config `githubRepo.branchPattern` + the ticket's intake metadata, and **created from the base branch (`main`) if it does not exist yet**. Pass a value only to override the resolved name.
- `owner`/`repo` (optional): Default from config `githubRepo`
- `message` (optional): Commit subject. The final commit message is always normalized to `[AB#<ticket_number>] <subject>` (an existing `[AB#...]` prefix is not duplicated; when omitted, a default subject is used)
**Returns**: `{ok, branch, branchCreated, noChanges, commitSha, committed: [...], skipped: [...]}` — refuses the whole push on approval failure, patch drift, or placeholder/truncated content; `noChanges: true` means every file already matched the branch so no commit was made

**Paths and mapper**: changed DRLs are always pushed to `<drlTargetPath>/<family>/year<currentYear>/`, never to a prior-year baseline source path. The file name is the baseline json `targetFileName` (original case, for example `sharedHospiceDuringPeriod.drl`), then the `sourcePath` file name, then `<slug>.drl`. Every changed DRL that declares `global ... maxCoverageDateByPayer` is registered in `MapperUseEnrollmentsRules.json` (`"measure/<family>/year<YYYY>/<file>.drl": {"rulesPath": [same]}`) on the branch in the same commit.

**Unchanged DRLs**: a generated DRL identical to its baseline is skipped (`skipped[]`, reason `unchanged (generated == baseline)`) only when its current-year target file already exists. Existence is checked on the GitHub base branch first, then by exact-case name in the local rules-engine checkout, then from baseline json `currentYearDrlExisted == false`; when none can decide, the file is assumed to exist. A missing year file is still published (no patch required, since the diff is empty) and appears in `published[]` with `note: "new (year file missing; unchanged vs baseline)"` and `existenceCheck` (`github:<base>`, `local-rules-engine`, or `baseline-metadata`). It is registered in the enrollment mapper like a changed DRL.

#### `open_drl_pull_request(ticket_id="")`
**Purpose**: Open (or reuse the already-open) PR for the published branch. The body is `templates/pr-template.md` with only the ADO ticket URL filled in, and the title is `[AB#<ticket>] <FAMILY> <MEASURES> DRL update <year>`. Writes `pr.md`.
**When to use**: Immediately after `publish_approved_pr_files` succeeds
**Returns**: `{ok, prUrl, number, branch, reusedExisting, prMd}` — refuses without approval or when the branch does not exist

---

### Test Artifact Generation

#### `fetch_librarian_test_json(ticket_id="", source_cql_path="", test_version="published")`
**Purpose**: Download a measure's test JSON from the Librarian API and write `pr-assets/<slug>.json`  
**When to use**: API Builder stage (post-approval), once per measure  
**Parameters**:
- `ticket_id` (optional): Target ticket
- `source_cql_path` (optional): CQL path used to derive the measure slug/specification
- `test_version` (optional): `published` (default) or `draft`
**Returns**: `{ok, measureSpecification, outputPath, bytes, source}` — fails closed on 404/empty (no placeholder)

#### `build_current_year_tests(ticket_id="")`
**Purpose**: Build every needed `pr-assets/Year<YYYY><Family><Slug>Test.java`. Skips shared DRLs and measures with `currentYearDrlExisted=true`. Otherwise finds the prior-year test by case-insensitive name (local rules-engine first, GitHub `main` fallback), rolls all year tokens, and verifies the class name and package.
**When to use**: Test Builder stage (the only call it makes besides state)
**Returns**: `{ok, built: [...], skipped: [...]}`, or `{ok: false, missing: [{slug, directory, expectedNames}]}`

#### `write_mapping_md(content, ticket_id="")`
**Purpose**: Write `mapping.md`. Refuses unless every changed generated DRL has a non-empty patch and is named in the content, and no placeholder text is present.
**When to use**: Converter, after patches and `validate_conversion_artifacts`

---

### Learning Engine (Non-Blocking)

#### `learning_record_execution(ticket_id, stage, elapsed_seconds, success, details="")`
**Purpose**: Record execution metrics for a pipeline stage (passive, non-blocking)  
**When to use**: At end of each stage for analytics  
**Parameters**:
- `ticket_id` (required): Ticket being processed
- `stage` (required): Pipeline stage
- `elapsed_seconds` (required): Time taken
- `success` (required): Boolean success flag
- `details` (optional): Context info
**Returns**: `{ok: true, recorded}` (always returns ok=true)

#### `learning_record_cql_patterns(ticket_id, cql_content)`
**Purpose**: Analyze CQL and record patterns (passive, non-blocking)  
**When to use**: After CQL extraction  
**Parameters**:
- `ticket_id` (required): Ticket being processed
- `cql_content` (required): CQL text to analyze
**Returns**: `{ok: true, recorded}`

#### `learning_record_error(ticket_id, stage, error_type, error_message)`
**Purpose**: Record error pattern for troubleshooting (passive, non-blocking)  
**When to use**: When any stage fails  
**Parameters**:
- `ticket_id` (required): Ticket where error occurred
- `stage` (required): Pipeline stage
- `error_type` (required): Error category
- `error_message` (required): Error description
**Returns**: `{ok: true, recorded}`

#### `learning_get_summary()`
**Purpose**: Get current learning summary (aggregated metrics/patterns/errors)  
**When to use**: For analytics review  
**Returns**: `{ok, summary: {...}}`

---

### PR-Specific Operations

#### `get_pr_changes(owner, repo, pr_number, ticket_id="")`
**Purpose**: Fetch changed files from a GitHub PR for CQL path resolution  
**When to use**: During intake, when ticket references a GitHub PR  
**Parameters**:
- `owner` (required): Repository owner
- `repo` (required): Repository name
- `pr_number` (required): PR number
- `ticket_id` (optional): Context ticket
**Returns**: `{ok, owner, repo, prNumber, files: [...], ticketId}`

---

## Critical Path Guarantees

### Path Containment Rule

**All file operations MUST resolve inside `.github/agents/Drl Pipeline/`.**

The server enforces this via `ROOT_DIR` which is set to:
```python
ROOT_DIR = Path(__file__).resolve().parent.parent
# Evaluates to: .github/agents/Drl Pipeline/
```

All artifact paths are constructed as:
```python
ROOT_DIR / "artifacts" / ticket_id / ...
```

This guarantees:
- [YES] `artifacts/` inside DRL Pipeline folder
- [YES] `state/` inside DRL Pipeline folder  
- [YES] `templates/` inside DRL Pipeline folder
- [NO] **NEVER** creates files at workspace root

### Verification

If files appear outside `.github/agents/Drl Pipeline/`, check:
1. Python server working directory when started
2. Agent file creation bypassing MCP tools
3. Hardcoded absolute paths in agent prompts

---

## Tool Selection Decision Tree

```
Need to...
  ├─ Check current ticket? → state_get_current_ticket()
  ├─ Check pipeline stage? → state_read_pipeline()
  ├─ Mark stage complete? → state_write_pipeline()
  ├─ Check approval? → state_read_approval()
  ├─ Record approval? → state_write_approval()
  ├─ Download CQL + build extraction.json + CQL diff patches? → download_and_extract_cql()
  ├─ DRL already contains the CQL change? → check_drl_already_updated()
  ├─ Build baseline? → build_prior_year_measure_template()
  ├─ Validate conversion? → validate_conversion_artifacts()
  ├─ Generate patch? → generate_drl_patch()
  ├─ Ensure artifact dirs exist? → organize_ticket_artifacts()
  ├─ Read a rules-engine file? → read_rules_engine_file()
  ├─ Push approved files + open PR? → publish_approved_pr_files()
  ├─ Get measure test JSON? → fetch_librarian_test_json()
  ├─ Record metrics? → learning_record_execution()
  └─ User explicitly requested reset? → state_reset_ticket_baseline() via /drlTicketReset only
```

---

## Common Patterns

### Pattern: Stage Transition

```
1. Call organize_ticket_artifacts(ticket_id) to ensure dirs exist
2. Perform stage work (conversion, extraction, etc.)
3. Validate outputs exist
4. Call state_write_pipeline(stage="<next-stage>", ticket_id=ticket_id)
```

### Pattern: Approval Gate

```
1. Call state_read_approval(ticket_id)
2. Check if approved=true
3. If false, block and request approval
4. After approval command, call state_write_approval(approved=true, approved_by="<name>", ticket_id=ticket_id)
5. Proceed to post-approval stages
```

### Pattern: Read a Rules-Engine File

```
1. Call read_rules_engine_file(path="<repo-relative path>", ref="<branch>")
2. If ok=true, use content (served from local working copy, GitHub fallback if absent)
3. If ok=false, surface the error as a blocker
```

### Pattern: Artifact Validation

```
1. Call validate_conversion_artifacts(ticket_id)
2. Check returnCode (0 = success, non-zero = failure)
3. Parse stdout for detailed validation results
4. Block PR submission if validation fails
```

---

## Troubleshooting

### Tool Not Found

**Symptom**: `Tool 'mcp_local-python-_<name>' not found`  
**Fix**: 
1. Check MCP server is running: `http://127.0.0.1:8766/health` should return `{"status":"ok"}`
2. Verify tool name matches this reference (case-sensitive)
3. Use `tool_search` to load deferred tools before calling

### Path Outside Agent Folder

**Symptom**: Files created at workspace root instead of `.github/agents/Drl Pipeline/artifacts/`  
**Fix**:
1. Check if agent bypassed MCP tools and used direct file creation
2. Verify Python server was started from `.github/agents/Drl Pipeline/` directory
3. Check for hardcoded absolute paths in agent prompts

### Approval Not Persisting

**Symptom**: Approval gate keeps asking for approval  
**Fix**:
1. Ensure `state_write_approval(approved=true, approved_by="<name>")` was called
2. Check `.github/agents/Drl Pipeline/artifacts/<ticket-id>/state/approval-status.json` exists
3. Verify approver name was provided (required for approval=true)

---

## Version History

- **2026-06-09**: Initial comprehensive reference created
- All tools verified against `local_mcp_server.py` v1.0.0
- **2026-10-09**: Shared tools (`publish_ado_changes`, Librarian downloads) moved off this server to the on-demand `shared-tools` stdio server in `.github/tools/shared_mcp/`
- **2026-10-13**: `build_prior_year_measure_template` gained a mixed-case slug lookup and `targetFileName` (honored by publish/patch/mapping). `initialize_generated_drls` gained `only_missing`. The validator accepts query-only shared DRLs and `...` that appears verbatim in source CQL.
- **2026-10-13**: `publish_approved_pr_files` publishes an unchanged DRL when its current-year file is missing in rules-engine (note `new (year file missing; unchanged vs baseline)`); it still skips unchanged DRLs whose year file exists.
- **2026-10-13**: Generated DRLs may use rules-engine (mixed-case) file names; all generated-DRL tools and the validator resolve them case-insensitively, and patch headers keep the lowercase slug path.
---

## Related Documentation

- [DRL Pipeline Workflow](../WORKFLOW.md)
- [Agent Protocol](../prompts/agent_protocol.md)
- [State Manager](../scripts/state_manager.py)
- [Local MCP Server Source](../scripts/local_mcp_server.py)
- [Shared Tools Source](../../../tools/shared_mcp/)
