---
name: mips-drl
description: "Use when converting, reviewing, or updating MIPS CQL-to-DRL logic in rules-engine, including MIPS 2025 to 2026 roll-forward, groupExternalIds migration, EMR v2, addPatientEMR, addEncounterEMR, PopulationEnum, option_id fan-out, patient-subject vs encounter-subject, shared MIPS DRLs, MIPS rate/shared files, or MIPS measure attribution."
---

# MIPS DRL Conversion Skill

Use this skill for MIPS DRL conversion and review.

## Source Of Truth

When running as the `measureable_content_agent` submodule inside rules-engine, inspect parent workspace DRLs before applying any pattern.

Preferred lookup order:

1. Target 2026 DRL: `src/main/resources/com/ablehealth/measure/mips/year2026/<measure>.drl`
2. Shared/rate family files, for example `mips226shared.drl`, `mips497rate6shared.drl`, or `mips005shared.drl`
3. Prior-year same measure in `src/main/resources/com/ablehealth/measure/mips/year2025/`
4. 2026 peer DRLs with same output grain: patient, encounter, episodic encounter, rate/shared architecture
5. Rules-engine `copilot-instructions.md` (PR-review rules) via the `drl-review-rules` skill

Do not hardcode a measure-specific pattern unless the inspected parent DRLs prove that pattern applies to the same measure family/type.

## 2025 To 2026 Priorities

Always check these migration concerns:

- `groupExternalId` singular -> `groupExternalIds` collection
- query filters using `== groupExternalId` -> `memberOf groupExternalIds`
- group output uses `isPresent(groupExternalIds)` and loops over `getGroupExternalIds()`
- old extended result APIs -> `addPatientEMR` or `addEncounterEMR` with EMR v2 signature
- org-scope `addPatientEMR` uses `""` as the attribution in 2026 (764 `""` vs 44 `null` in MIPS 2026; 2025 used `null`). New or changed calls use `""`; both are behavior-neutral at runtime
- pure EMR rules use `salience -10` and keyed declare-fact guards; rules that also set a status keep default salience. Helper/fact-insert rules may keep `salience 30`/`100` as in the baseline (44 and 9 remain in MIPS 2026); do not use them to order EMR rules
- value sets use `MipsYear2026`, `AbleYear2026`, `VsacYear2026`, and `Ecqm_Direct_Reference_CodesYear2026`; valid namespaces are the files in rules-engine `src/main/resources/value_sets/`
- option IDs come from CQL and remain exact case/spelling
- `option_id = all` fans out to concrete option codes

## Subject Type

Choose subject type from output grain:

- Patient-subject: one result per patient; use patient status APIs and `addPatientEMR`.
- Encounter-subject: one result per encounter; use encounter status APIs and `addEncounterEMR`.
- Episodic encounter-subject: one result per episode/window; carry episode boundaries and use the parent DRL's established episode pattern.

Patient-subject can still use encounter facts as evidence. Do not switch to encounter-subject unless output rows are encounter-grained.

## EMR v2 Contract

Observed 2026 main-branch MIPS patterns:

- Org-scope `addPatientEMR` uses `""` for the attribution parameter. A minority of 2026 baselines still pass `null`; leave untouched calls as they are.
- Group status can pass `clinicalActivity.groupExternalIds` directly to `addGroupPatientMeasureStatus(...)` when that API accepts the collection.
- Group EMR rows iterate concrete IDs with either `$groupId: String() from $encounter.getGroupExternalIds()` or `from $encounter.groupExternalIds` (both common in 2026); match the file.
- Provider rows guard with `clinicalActivity.isPresent(clinicalActivity.providerExternalId)` and use `$encounter.providerExternalId`.
- Calculated-value EMR rows can use `null` as the evidence parameter at org scope only, paired with `SupplementalInfoEnum.CALCULATION_VALUE` and `CALCULATED_VALUE_TYPE`. Group/provider rows pass `$encounter`.

Patient EMR, org scope:

```drools
controlSet.addPatientEMR($program, $patient, $measure, "", evidenceName, optionId, PopulationEnum.denominator, $evidence, SupplementalInfoEnum.DISPLAY_DATE.displayName(), $evidence.startDate.toString());
```

Patient EMR, group/provider scope:

```drools
controlSet.addPatientEMR($program, $patient, $measure, groupOrProviderExternalId, evidenceName, optionId, PopulationEnum.denominator, $evidence, SupplementalInfoEnum.DISPLAY_DATE.displayName(), $evidence.startDate.toString());
```

Encounter EMR:

```drools
controlSet.addEncounterEMR($program, $patient, $measure, $encounter, evidenceName, optionId, PopulationEnum.denominator, $evidence, SupplementalInfoEnum.DISPLAY_DATE.displayName(), $evidence.startDate.toString());
```

PopulationEnum should match the emitted status and CQL population:

- denominator -> `PopulationEnum.denominator`
- exclusion -> `PopulationEnum.denominator_exclusion`
- exception -> `PopulationEnum.denominator_exception`
- success/performance met -> `PopulationEnum.numerator_performance_met`
- gap/performance not met -> `PopulationEnum.numerator_performance_not_met`

## Marker Guards

EMR APIs append rows, so guard EMR rules with declare facts. Multi-scope measures need org/group/provider marker variants with keys for group/provider.

Example shapes (2026 files mix `...EMROrg`, `...OrgEmr`, and `...GroupEMR` suffixes; follow the file you are editing):

```drools
declare Year2026Mips123DenominatorEMROrg end
declare Year2026Mips123DenominatorEMRGroup
    groupExternalId: String
end
declare Year2026Mips123DenominatorEMRProvider
    providerExternalId: String
end
```

Use `not (exists ...Group(groupExternalId == $groupExternalId))` before writing group EMR and insert the same keyed marker after writing.

Do not treat the marker field name `groupExternalId` as a failed migration by itself. The migration concern is model/API usage of singular encounter group fields; marker fields may remain singular because they store one iterated ID.

## Shared And Rate Files

Many MIPS measures are multi-rate and use shared DRL files. Before editing a rate file, check for a sibling `shared.drl` and inspect current-year peer rates. Shared collectors, bounded facts, and most-recent encounter logic often belong in the shared file.

Do not create placeholder shared logic. If a shared dependency is missing, document a blocker.

Query-only shared DRLs (only `query` blocks, no `rule`) are valid; `validate_conversion_artifacts` reports them as `[INFO]` and skips rule checks. For a mixed-case shared DRL, pass the exact-case `measure_slug` (for example `sharedHospiceDuringPeriod`) to `build_prior_year_measure_template` so publish uses the correct file name.

## Validation Checklist

- Parent rules-engine DRLs inspected and cited in mapping.
- Subject type matches output grain.
- Group logic uses `groupExternalIds` collection in 2026.
- New or changed org EMR calls pass `""` as attribution.
- Pure EMR rules have keyed marker guards and `salience -10`; status rules keep default salience.
- Gap rules have no SUCCESS/EXCEPTION or EMR-marker guards; status precedence handles them.
- Option IDs are CQL-derived and exact.
- Shared/rate architecture is preserved.
- `drl-review-rules` checks pass, or each exception is justified in mapping.md.
