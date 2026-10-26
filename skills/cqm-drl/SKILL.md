---
name: cqm-drl
description: "Use when converting, reviewing, or updating CQM, CMS, or eCQM CQL-to-DRL logic in rules-engine, including CMS 2025 to 2026 migration, CQM shared DRLs, groupExternalIds migration, EMR v2, org/group/provider attribution, CMS measure status, VsacYear2026/Ecqm_Direct_Reference_CodesYear2026 value sets, and CQM/CMS rate/shared files."
---

# CQM/CMS DRL Conversion Skill

Use this skill for CQM, CMS, and eCQM DRL conversion and review.

## Source Of Truth

When running as the `measureable_content_agent` submodule inside rules-engine, inspect parent workspace DRLs first.

Preferred lookup order:

1. Target 2026 DRL: `src/main/resources/com/ablehealth/measure/cms/year2026/<measure>.drl`
2. Shared/rate family files, for example `cms128shared.drl`, `cms136shared.drl`, or `sharedHospice.drl`
3. Prior-year same measure in `src/main/resources/com/ablehealth/measure/cms/year2025/`
4. 2026 peer CMS/CQM DRLs with the same subject type and attribution pattern
5. Config mappers under `src/main/resources/config_load_rules/` when shared/provider/enrollment loading changes
6. Rules-engine `copilot-instructions.md` (PR-review rules) via the `drl-review-rules` skill

Do not hardcode measure-specific details unless the inspected parent DRLs show that the example represents the same CQM/CMS type.

## 2025 To 2026 Priorities

Always validate these migration points:

- `groupExternalId` singular -> `groupExternalIds` collection
- query filters using direct equality -> `memberOf groupExternalIds` where applicable
- group attribution loops over `groupExternalIds` or `getGroupExternalIds()` with `isPresent(groupExternalIds)` guard; follow the parent target DRL style
- provider attribution has `isPresent(providerExternalId)` guard
- org EMR contextId is `""`, never `null`
- EMR APIs use EMR v2 signatures with `PopulationEnum` and `SupplementalInfoEnum.DISPLAY_DATE`
- EMR logging has declare-fact dedupe guards for org/group/provider contexts
- nullable date/string comparisons use Drools `==` rather than Java `.equals(...)`

## File Shape

Typical 2026 CQM/CMS DRL structure:

- package `com.ablehealth.measure.cms.year2026`
- model/payload/results/enum imports as needed
- CQL comments
- declares
- queries
- denominator/status rules
- pure EMR rules at `salience -10` (rules that also set a status keep default salience)
- exclusion/exception/success/gap rules

Preserve existing parent DRL ordering when updating a measure.

## Attribution And EMR

Observed 2026 main-branch CMS/CQM patterns:

- Org-scope `addPatientEMR` uses `""` for the context parameter.
- Group scopes guard with `$encounter.isPresent($encounter.groupExternalIds)` and may iterate either `$encounter.groupExternalIds` or `$encounter.getGroupExternalIds()` depending on the parent file.
- Provider scopes guard with `$encounter.isPresent($encounter.providerExternalId)`.
- CMS/CQM 2026 DRLs use `VsacYear2026` (1361 uses) and `Ecqm_Direct_Reference_CodesYear2026` (394 uses). There is no `CmsYear2026` value-set file.

Org context:

```drools
controlSet.addPatientEMR($program, $patient, $measure, "", evidenceName, optionId, PopulationEnum.denominator, $evidence, SupplementalInfoEnum.DISPLAY_DATE.displayName(), $evidence.startDate.toString());
```

Group context:

```drools
if ($encounter.isPresent($encounter.groupExternalIds)) {
    for (String groupExternalId : $encounter.groupExternalIds) {
        controlSet.addPatientEMR($program, $patient, $measure, groupExternalId, evidenceName, optionId, PopulationEnum.denominator, $evidence, SupplementalInfoEnum.DISPLAY_DATE.displayName(), $evidence.startDate.toString());
    }
}
```

Provider context:

```drools
if ($encounter.isPresent($encounter.providerExternalId)) {
    controlSet.addPatientEMR($program, $patient, $measure, $encounter.providerExternalId, evidenceName, optionId, PopulationEnum.denominator, $evidence, SupplementalInfoEnum.DISPLAY_DATE.displayName(), $evidence.startDate.toString());
}
```

Use `addEncounterEMR` only when output grain is encounter/episode, and follow the parent measure's established status APIs.

## Value Sets

Use year-specific namespaces from current-year parent DRLs. Valid namespaces are the files in rules-engine `src/main/resources/value_sets/`:

- `VsacYear2026` for VSAC sets
- `Ecqm_Direct_Reference_CodesYear2026` for direct reference codes
- `AbleYear2026` for Able-maintained sets (exists, but no CMS 2026 DRL uses it yet)

There is no `CmsYear2026`; never emit it. Do not blindly replace prefixes. Confirm against target CQL and current parent DRL usage.

## Shared Files

CQM/CMS shared files often hold hospice, palliative care, frailty, outpatient encounter, or rate-shared logic. If the same CQL delta applies across rates, update the shared DRL once instead of duplicating into rate files.

## Validation Checklist

- Parent rules-engine DRLs inspected and cited in mapping.
- 2026 group/provider attribution uses collection/guard patterns.
- Org EMR contextId is `""` for new or changed calls (198 `null` calls remain in CMS 2026 baselines; leave untouched ones).
- EMR rules are deduped with keyed declare markers.
- Nullable comparisons are Drools-null-safe.
- Value-set namespaces match 2026 parent DRL/CQL context.
- Shared/rate architecture is preserved.
- `drl-review-rules` checks pass, or each exception is justified in mapping.md.
