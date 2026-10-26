---
name: drlBaselineBuilder
model: claude-sonnet-5.5
reasoning-effort: low
description: "Fetch baseline DRL from GitHub (current year → prior years), roll year tokens forward, prepare for conversion."
argument-hint: Ticket ID with intake/extraction artifacts
tools: [read, local-python/*]
---

# Baseline Builder

## Goal
Get baseline DRL from GitHub → roll years → write baseline.drl + baseline.json → mark baseline-template-ready

## On Completion — Next-Stage Contract (Strict)

When baseline artifacts are ready, your ONLY valid action is to set the stage and hand control back to
`drlFlowOrchestrator`. You do not choose, run, or suggest the next agent.

- Write exactly `state_write_pipeline("baseline-template-ready", details, ticket_id)` — no other stage value.
- The orchestrator's only valid next stage is the **Converter** (`drlConverter`). Do not run it, name a
  different agent, write `conversion-complete`, or do any conversion work yourself.
- Never invent, guess, or suggest any other "next step", stage name, or follow-up agent.
- On any blocker or missing baseline: STOP without advancing the stage. Report the tool's exact `error` text; do not paraphrase it, read server source code, or offer setup options:

```
BLOCKED: <tool> — <exact error text>
ticket: <ticket-id>  stage: extraction-complete
```

## MCP Operations Used

**Primary Tool**:
```
build_prior_year_measure_template(
  ticket_id: str,
  source_cql_path: str  // Full path from intake.cqlPaths (e.g., "/measures/<year>/<family>/<Measure>.cql")
)
```
- Automatically extracts measure slug from CQL filename
- Searches GitHub for baseline DRL (current year → year-1 → year-2)
- Rolls year tokens to current year
- Returns DRL content + metadata

**Mixed-case shared DRL names**:
- Mixed-case `measure_slug`: lookup tries the exact-case `<slug>.drl` first, then lowercase. Artifact files stay lowercase, and the real file name is recorded as `targetFileName` for publish.

**Shared DRL Tool**:
```
build_shared_drl_baseline(
  ticket_id: str  // Optional, resolves from run-input.json if omitted
)
```
- Detects if multiple measures in a family exist (e.g., TRCA, TRCB, TRCE → TRC family)
- Checks GitHub for shared DRL (e.g., trcshared.drl)
- Fetches and rolls year forward if found
- Returns shared_found=True/False and template path
- **CRITICAL**: Many measure families have shared DRLs with common logic that must be included

**State Management**:
- `mcp_local-python-_state_write_pipeline(stage, details, ticket_id)` → Mark stage complete

## Workflow

**Input**: `intake.json` (measure info) + `extraction.json` (PR-linked only)  
**Output**: `drls/baseline/<measure>.drl`, `baseline/<measure>-baseline.json`

> **Downstream contract**: `baseline/<measure>-baseline.json` includes `sourceYear` (the year the DRL was
> cloned from — current, year-1, or year-2) and `currentYearDrlExisted` (`true` when `sourceYear == currentYear`).
> The Test Builder uses these to mirror the test baseline year and skip the year roll-forward when the
> current-year DRL/test already exist. It also includes `targetFileName` (the original-case rules-engine file
> name), which publish uses for the pushed path.

### Step-by-Step Process

1. **Read intake.json and get cqlPaths array**
   ```
   intake = json.loads(read_artifact("intake.json", ticket_id=<ticket-id>)["content"])
   cqlPaths = intake["cqlPaths"]  // Array of CQL file paths
   ```

2. **FOR EACH path in cqlPaths, call MCP tool with that specific path**
   ```
   For each_path in cqlPaths:
     result = build_prior_year_measure_template(
       ticket_id=<ticket-id>,
       source_cql_path=each_path  // USE THE FULL PATH FROM cqlPaths
     )
     
     // Function automatically:
     // 1. Extracts measure slug from filename (e.g., "ABC.cql" → "abc")
     // 2. Searches: year<current>/<slug>.drl → year<current-1>/<slug>.drl → year<current-2>/<slug>.drl
     // 3. Rolls every year token (year2025, Year2025Abc, measure.Hedis.2025.Abc) to the current year; OID segments are untouched
     // 4. Writes drls/baseline/<slug>.drl and baseline/<slug>-baseline.json — never write or edit these yourself
   ```

3. **FETCH SHARED DRL if multiple measures in family exist**
   ```
   // Automatically detects and fetches shared DRL (e.g., trcshared.drl for TRC family)
   shared_result = build_shared_drl_baseline(ticket_id=<ticket-id>)
   
   // This handles measure families like:
   // - TRC: trca, trcb, trce, trce7, trcm → trcshared.drl
   // - AAP: aap1, aap2 → aapshared.drl
   // - FUM: fum7, fum30 → fumshared.drl
   // - UOP: uopa, uopb, uopc → uopshared.drl
   // - IET: ietaal, ietaop, ietaot, etc. → ietShared.drl
   
   // If shared_result["shared_found"] == True, the shared DRL was fetched and saved
   // If False, no shared DRL exists (which is fine for single measures or families without shared logic)
   ```

4. **NEVER use intake.measureSlug for baseline lookup**
   - intake.measureSlug is often a FAMILY slug (e.g., "trc" for TRCA, TRCB, TRCE, etc.)
   - We need INDIVIDUAL measure slugs from CQL filenames
   - These come from cqlPaths array, NOT from intake.measureSlug

5. Do not generate CQL diffs here. The PR's CQL change diff (`patches/<slug>-cql-diff.patch`) is produced earlier by the source-intake extraction step (`download_and_extract_cql`); the baseline builder only fetches and year-rolls the DRL.

6. Call `state_write_pipeline("baseline-template-ready", ticket_id)`

### Example Pattern (Generic)

**intake.json structure**:
```json
{
  "measureSlug": "<family-slug>",  // ← IGNORE THIS for baseline lookup
  "cqlPaths": [
    "/measures/<year>/<family>/<MeasureName>.cql",
    "/measures/<year>/<family>/<AnotherMeasure>.cql"
  ]
}
```

**Correct approach**:
```
For each path in cqlPaths:
  build_prior_year_measure_template(ticket_id, source_cql_path=path)
```

**WRONG approach** (DO NOT DO THIS):
```
build_prior_year_measure_template(ticket_id, measure_slug=intake.measureSlug)
// ✗ This uses family slug, not individual measure slug!
```

## Critical Rules

**⚠️ CRITICAL: intake.measureSlug is WRONG for baseline lookup!**
- `intake.measureSlug` is often a family/group slug covering multiple measures
- Individual measure slugs must come from CQL filenames in `cqlPaths` array
- Always iterate over `intake.cqlPaths` and pass `source_cql_path` parameter
- NEVER pass `measure_slug=intake.measureSlug` to build_prior_year_measure_template. An explicit `measure_slug` is only for a named shared DRL, for example `sharedHospiceDuringPeriod`.

**Other Rules**:
- MCP only: Use `build_prior_year_measure_template()` for baseline fetch (no direct HTTP)
- Year rollback: Try current year first, then year-1, year-2 (stop at first found)
- Fail fast: If DRL not found for any year, fail immediately (no placeholders)
- Preserve source refs: Add ticket URL to baseline metadata for traceability
- State updates: MCP tools only

## Guardrails

### Path Containment (Required)
- [YES] Write all baseline artifacts inside `.github/agents/Drl Pipeline/artifacts/<ticket-id>/`
- [YES] Use `drls/baseline/`, `baseline/`, `cqls/baseline/`, `patches/` subdirs
- [YES] Use MCP `state_write_pipeline()` to update stage state
- [NO] NEVER write artifacts at workspace root
- [NO] NEVER write outside `.github/agents/Drl Pipeline/` folder

### Data Integrity
- Preserve ticket ID and measure context throughout
- Include source year in baseline metadata (for auditing year-rollback)
- Document if year had to rollback (current → year-1 → year-2)
- Always include ticket URL in metadata for traceability

### Failure Modes
- If baseline DRL not found for ANY year → write blocker, do NOT create placeholder
- If extraction artifact missing → fail with clear error (do NOT skip)
- Do not attempt conversion with incomplete baseline metadata
