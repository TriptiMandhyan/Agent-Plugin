---
name: hedis-drl
description: "Use when converting, reviewing, or updating HEDIS CQL-to-DRL logic in rules-engine, including HEDIS 2025 to 2026 roll-forward, continuous enrollment, max coverage end date, shared HEDIS DRLs, HedisMeasureStatus, PatientDenominator/EncounterDenominator, HEDIS EMR, MapperUseEnrollmentsRules, W30, WCC, URI, TRC, DRR, IET, or other HEDIS measure families."
---

# HEDIS DRL Conversion Skill

Use this skill for HEDIS DRL conversion and review. HEDIS includes continuous enrollment and payer max coverage updates.

## Source Of Truth

When this agent bundle is opened as the `measureable_content_agent` submodule inside rules-engine, inspect the parent rules-engine workspace before applying patterns.

Preferred lookup order:

1. Current target DRL: `src/main/resources/com/ablehealth/measure/hedis/year2026/<measure>.drl`
2. Current-year shared DRL for the family, if present: `src/main/resources/com/ablehealth/measure/hedis/year2026/<family>shared.drl` or known camel-case variant such as `ietShared.drl`
3. Prior-year same measure: `src/main/resources/com/ablehealth/measure/hedis/year2025/<measure>.drl`
4. Peer 2026 DRLs with the same pattern, subject type, or shared family
5. Config mappers: `src/main/resources/config_load_rules/MapperUseEnrollmentsRules.json`, `MapperSharedRules.json`, and `MapperUseProviderRules.json` only when relevant
6. Rules-engine `copilot-instructions.md` (PR-review rules) via the `drl-review-rules` skill

Do not rely on bundled examples when rules-engine DRLs are available. Use existing DRL style and helper APIs from the parent workspace.

## 2026 HEDIS Shape

- HEDIS uses a dual status model:
  - Certification: `controlSet.addOrUpdateHedisMeasureStatus(...)`
  - Production: `addPatientMeasureStatus(...)` or `addEncounterMeasureStatus(...)`
- HEDIS usually writes HEDIS and production measure status rather than org/group/provider EMR rows. When HEDIS EMR appears, follow the parent DRL for that family. HEDIS 2026 baselines mostly pass `null` as the `addPatientEMR` attribution; new or changed calls use `""` (behavior-neutral, required by PR review).
- HEDIS has no group/provider attribution loops by default.
- HEDIS commonly uses family shared DRLs for enrollment, shared exclusions, and repeated logic.
- Shared DRL changes should be made once in the shared DRL when the same CQL delta appears across every measure in a family.
- Value sets use `HedisYear2026` (most rules) and `AbleYear2026`. Valid namespaces are the files in rules-engine `src/main/resources/value_sets/`.

Observed 2026 main-branch patterns:

- Encounter HEDIS measures commonly call `addEncounterMeasureStatus(...)` and then `addOrUpdateHedisMeasureStatus(...)` for the same logical outcome.
- Patient HEDIS measures commonly call `addPatientMeasureStatus(...)` and then `addOrUpdateHedisMeasureStatus(...)`.
- Standard CE rules often use `addContinuousEnrollment($program, $patient, $measure, payer, hasCE, optionId)` or the encounter equivalent.
- Max-coverage CE rules add the extra product/context and coverage-end-date parameters.

## Subject Type

Choose subject type from output grain:

- Patient-subject: one result per patient; use `PatientDenominator` and patient status/EMR APIs.
- Encounter-subject: one result per qualifying encounter; use `EncounterDenominator` and encounter status/EMR APIs.

Do not infer encounter-subject only because encounter facts appear in qualification logic.

## Continuous Enrollment

HEDIS CE rules use either `salience -100` or no salience (34 and 39 rules in 2026); keep the parent rule's salience. They use `EnrollmentHelper` over payer-grouped enrollment periods. Current 2026 parent DRLs include both older CE signatures and newer max-coverage signatures; preserve the parent-family style unless source CQL requires the max-coverage change. Extract all CE parameters from CQL:

- enrollment interval start/end
- benefit type filters
- max allowed gap days
- min consecutive gap days
- max allowed gaps
- enrollment-on-date requirements
- option IDs affected by CE output

Benefit type filtering belongs in the `when` collect clause, for example `Enrollment(hasMedicalCoverage == true)`, unless the existing parent DRL uses a different verified local pattern.

Preserve the exact CQL benefit expression:

- `{'medical' and 'pharmacy'}` -> collect enrollments with both `hasMedicalCoverage == true` and `hasPharmacyCoverage == true`.
- `{'medical', 'pharmacy'}` or explicit `or` -> collect enrollments matching either benefit only when the parent helper pattern supports that OR semantics.
- `HasEnrollmentOnDateByBenefitTypes(...)` is a separate anchor-date predicate. If its benefit expression differs from the continuous interval expression, create a separate enrollment list/map for the anchor check rather than reusing the continuous list.

2026 examples from rules-engine:

- CWP/AAB: CQL only has `HasContinuousEnrollmentByBenefitTypes(..., {'medical' and 'pharmacy'}, ...)`; one combined medical+pharmacy enrollment list is correct.
- SAA: CQL has `HasContinuousEnrollmentByBenefitTypes(..., {'medical' and 'pharmacy'}, ...)` plus `HasEnrollmentOnDateByBenefitTypes(..., {'medical'})`; keep `ceEnrollmentList` for the combined interval and a separate `medicalEnrollmentList` for the medical-only anchor-date check.
- UOP/SSD: both continuous interval and anchor-date checks use `{'medical' and 'pharmacy'}`; the same combined list may be used for both checks.

Do not remove a seemingly redundant medical-only list until the source CQL and peer 2026 DRLs prove the anchor-date benefit expression is identical to the interval expression.

## Max Coverage End Date

Apply this pattern when CQL or ticket context references `GetMaxCoverageEndDateForPayer(P)`, payer max coverage, max enrollment, or `maxCoverageDateByPayer`.

Required contract:

- `maxCoverageDateByPayer` is a Drools global populated by the framework. Never initialize it locally.
- Add `global Map<String, LocalDate> maxCoverageDateByPayer;` only if missing.
- Add `import java.time.LocalDate;` and `import static com.ablehealth.helper.DateHelper.getEarliestDate;` when needed.
- Register the measure in `src/main/resources/config_load_rules/MapperUseEnrollmentsRules.json` in the same PR.
- Read `coverageEndDate` inside the payer loop.
- Cap the planned interval end with `getEarliestDate(plannedEnd, coverageEndDate)` when a coverage date exists.
- Build CE `optionIds` from every concrete `/* Option ID: ... */` in the source CQL, including exclusion options such as deceased even when that option has no EMR payload. Do not hand-copy only the EMR-bearing options.
- Use the subject-correct max-coverage `addContinuousEnrollment` signature only for max-coverage CQL changes.
- Preserve any explicit guard such as `if visitDate is after "GetMaxCoverageEndDateForPayer"(P) return false` as `coverageEndDate != null && !visitDate.isAfter(coverageEndDate)` before continuity checks. Do not replace that guard with a nullable fallback end date.

Patient-subject signature (pass `""` as lineOfBusiness unless the parent DRL passes a variable, as `ccs.drl` does):

```drools
controlSet.addContinuousEnrollment($program, $patient, $measure, payer, "", coverageEndDate, hasCE, optionId);
```

Encounter-subject signature:

```drools
controlSet.addContinuousEnrollment($program, $patient, $encounter, $measure, payer, "", coverageEndDate, hasCE, optionId);
```

Non-max-coverage CE should keep the parent-family signature, for example:

```drools
controlSet.addContinuousEnrollment($program, $patient, $measure, payer, hasCE, optionId);
controlSet.addContinuousEnrollment($program, $patient, $encounter, $measure, payer, hasCE, optionId);
```

### Reference template — patient-subject (mirror `wcca.drl`)

```drools
import java.time.LocalDate;
import com.ablehealth.helper.EnrollmentHelper;
import static com.ablehealth.helper.DateHelper.getEarliestDate;

global Map<String, LocalDate> maxCoverageDateByPayer;

then
    LocalDate coverageEndDate = (LocalDate) maxCoverageDateByPayer.get(payer);
    boolean hasContinuousEnrollment =
        coverageEndDate != null
        && !$program.measurementPeriodStartDate.isAfter(coverageEndDate)
        && EnrollmentHelper.hasEnrollmentOnDateByBenefitTypes(payerEnrollments, getEarliestDate($program.measurementPeriodEndDate, coverageEndDate))
        && EnrollmentHelper.isContinuous(EnrollmentHelper.consolidatePeriods(payerEnrollments), $program.measurementPeriodStartDate, getEarliestDate($program.measurementPeriodEndDate, coverageEndDate), 45, 45, 1);
    for (String optionId : optionIds) {
        controlSet.addContinuousEnrollment($program, $patient, $measure, payer, "", coverageEndDate, hasContinuousEnrollment, optionId);
    }
```

### Reference template — encounter-subject (based on `uri.drl`)

`uri.drl` itself computes the interval end with a null fallback (`coverageEndDate == null ? measurementPeriodEndDate : getEarliestDate(...)`). Use the short-circuit form below when the CQL has an explicit `is after GetMaxCoverageEndDateForPayer(P) return false` guard.

```drools
then
    LocalDate coverageEndDate = (LocalDate) maxCoverageDateByPayer.get(payer);
    LocalDate startDateMinus30 = $encounter.startDate.minusDays(30);
    boolean hasContinuousEnrollment =
        coverageEndDate != null
        && !startDateMinus30.isAfter(coverageEndDate)
        && EnrollmentHelper.isContinuous(EnrollmentHelper.consolidatePeriods(forPayer), startDateMinus30, getEarliestDate($encounter.startDate.plusDays(3), coverageEndDate), 0, 1, 0);
    for (String optionId : optionIds) {
        controlSet.addContinuousEnrollment($program, $patient, $encounter, $measure, payer, "", coverageEndDate, hasContinuousEnrollment, optionId);
    }
```

(Encounter day offsets such as `plusDays(3)` and gap parameters `(0, 1, 0)` are measure-specific — copy them from the source CQL / peer 2026 DRL, not from URI blindly.)

Anti-patterns (these are real defects seen in generated DRLs — never emit them):

- Do NOT call the plain overload (`..., payer, hasCE, optionId`) while a `coverageEndDate` is computed. That drops the max coverage date. Always pass `"", coverageEndDate` before `hasCE`.
- Do NOT use `LocalDate.now()` (or `LocalDate.now().lengthOfMonth()` / `withDayOfMonth`) to build the interval. The reference DRLs use program/encounter dates only, so results stay deterministic.
- Do NOT wrap `maxCoverageDateByPayer.get(payer)` in `try/catch` or guard the global with `maxCoverageDateByPayer != null`. The framework always injects it; read it directly.
- Do NOT translate an explicit `visitDate is after GetMaxCoverageEndDateForPayer(P) return false` guard into `coverageEndDate == null ? plannedEnd : getEarliestDate(...)`; that makes null coverage behave as eligible and loses the false branch. Use `coverageEndDate != null && !visitDate.isAfter(coverageEndDate) && EnrollmentHelper.isContinuous(...)`.
- Do NOT use an `if (coverageEndDate != null) { ... } else { ... }` block that evaluates CE in the else branch when the coverage date is absent. When the CQL has an explicit `is after GetMaxCoverageEndDateForPayer(P) return false` guard, a null coverage date must produce `hasContinuousEnrollment = false` — implemented by the short-circuit `&&` chain starting with `coverageEndDate != null`. Never add an else branch that evaluates CE without the cap.
- Do NOT omit exclusion option IDs from CE fan-out because they lack EMR records. CE output is option-scoped and must cover concrete CQL option IDs such as `denominator_exclusion_deceased`.
- Do NOT omit `import java.time.LocalDate;` or `import static com.ablehealth.helper.DateHelper.getEarliestDate;` when those symbols are used.

Guardrails:

- Do not use `new HashMap<>()` for `maxCoverageDateByPayer`.
- Do not derive max coverage by streaming enrollments.
- Do not hardcode option IDs. Extract from CQL comments and EMRv2 calls.
- If `option_id = all`, fan out to concrete applicable option IDs from CQL/baseline context.
- If CQL requires enrollment on the last day, preserve that check in DRL.

## Option IDs

Extract from source CQL:

- `/* Option ID: ... */`
- `option_id = ...` in `ExtendedMeasureResultV2`
- `option_id = all` means all applicable concrete option IDs, not a literal DRL shortcut unless parent DRL already proves that convention.

## Shared Files

Known HEDIS shared patterns in rules-engine include family files such as `trcshared.drl`, `drrshared.drl`, `uopshared.drl`, `cisshared.drl`, and camel-case variants like `ietShared.drl`, plus shared exclusion files such as `sharedHospiceDuringPeriod.drl` and `sharedPalliativeCareDuringPeriod.drl`.

Before editing an individual DRL, check whether the baseline family has a shared DRL that owns the changed logic.

## Validation Checklist

- Source CQL delta maps to the edited DRL rule/query.
- Shared-family changes are made in shared DRL when appropriate.
- HEDIS status APIs follow parent DRL family style.
- CE output uses CQL-derived option IDs.
- Max coverage changes add MapperUseEnrollmentsRules registration.
- `maxCoverageDateByPayer` is declared as a global and never locally initialized.
- `drl-review-rules` checks pass, or each exception is justified in mapping.md.
- Mapping cites this skill and the parent rules-engine DRL files inspected.
