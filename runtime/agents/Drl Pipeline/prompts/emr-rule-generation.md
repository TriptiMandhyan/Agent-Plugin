# EMR Rule Generation Pattern

This module provides the pattern for generating DRL EMR evidence logging rules from CQL ExtendedMeasureResultV2 outputs.

## Core Principle

**CQL Return Statement → DRL EMR Rule**

Every CQL `return ExtendedMeasureResultV2(...)` statement must generate a corresponding EMR evidence logging rule in DRL.

Preserve the exact `option_id` text from CQL everywhere it is emitted. Do not lowercase or otherwise normalize option codes.

---

## Pattern Recognition

### CQL Pattern to Detect
```cql
return ExtendedMeasureResultV2(
    <evidence_expression>
    , '<evidence_name>' as name
    , display_date = <date_field>
    , calculated_record = <boolean>
    , population = <population_type>
    , option_id = <option_id>
)
```

### Required DRL Output
```drools
rule 'Year{year}.{Measure}.EMR.{option_id}.{evidence_name_normalized}'
salience -10
when
    // Status guard: EMR only fires if status already set
    EncounterMeasureStatus(
        program == $program,
        clinicalActivity == $encounter,
        measure == $measure,
        statusValue == {status_value},
        optionId == "{option_id}"
    )
    
    // Evidence binding: capture actual record for EMR evidence field
    $evidence: {evidence_type}({evidence_conditions}) from accumulate (
        $c: {evidence_type}({query_conditions}),
        {accumulate_function}($c)
    )
then
    controlSet.addEncounterEMR(
        $program,
        $patient,
        $measure,
        $encounter,
        "{evidence_name}",
        "{option_id}",
        PopulationEnum.{population_enum},
        $evidence,
        SupplementalInfoEnum.DISPLAY_DATE.displayName(),
        ${date_binding}.toString()
    );
end
```

---

## Mapping Rules

### 1. CQL Evidence Expression → DRL Evidence Binding

| CQL Pattern | DRL Binding Pattern |
|-------------|---------------------|
| `QualifyingEncounter` | `$encounter` (already bound) |
| `Last([...] Resource where ...)` | `$evidence: Resource(...) from accumulate ($c: Resource(...), latestClinicalActivity($c))` |
| `First([...] Resource where ...)` | `$evidence: Resource(...) from accumulate ($c: Resource(...), earliestClinicalActivity($c))` |
| Direct reference to resource | `$evidence: Resource(...)` |
| Nested query result | Bind the innermost resource, use accumulate if aggregation involved |

### 2. CQL Population → DRL PopulationEnum

| CQL Population | DRL PopulationEnum |
|----------------|---------------------|
| Denominator | `PopulationEnum.denominator` |
| Numerator + `MeasureStatusValue.SUCCESS` | `PopulationEnum.numerator_performance_met` |
| Numerator + `MeasureStatusValue.GAP` | `PopulationEnum.numerator_performance_not_met` |
| Numerator + `MeasureStatusValue.EXCLUSION` | `PopulationEnum.numerator_exclusion` |
| Numerator + other status/no status | `PopulationEnum.numerator` |
| Numerator Performance Met | `PopulationEnum.numerator_performance_met` |
| Exclusion | `PopulationEnum.denominator_exclusion` |
| Exception | `PopulationEnum.denominator_exception` |

### 3. CQL Display Date → DRL Date Binding

| CQL display_date | DRL Date Expression |
|------------------|---------------------|
| start_date | `$evidence.startDate.toString()` or `$encounter.startDate.toString()` |
| stop_date | `$evidence.stopDate.toString()` |
| author_datetime | `$evidence.authorDatetime.toString()` |
| author_date | `$evidence.authorDate.toString()` |

### 4. CQL Retrieve → DRL Query Conditions

| CQL Pattern | DRL Pattern |
|-------------|-------------|
| `["Clinical Activity": "VSAC; VALUE_SET_NAME"]` | `withValueSet("VsacYear2026", "VALUE_SET_NAME")` |
| `["Clinical Activity": "eCQM Direct Reference Codes; CODE_NAME"]` | `withValueSet("Ecqm_Direct_Reference_CodesYear2026", "CODE_NAME")` |
| `where X during day of Interval[E.start, E.stop]` | `(stopDate != null && endsDuring($encounter)) \|\| (stopDate == null && startsDuring($encounter))` |
| `where X.result in Interval[A, B]` | `resultAsDecimal >= A && resultAsDecimal <= B` |

### 5. Status Value Determination

| CQL Population | Expected Status Value |
|----------------|----------------------|
| Numerator / Numerator Performance Met | `MeasureStatusValue.SUCCESS` |
| Exclusion | `MeasureStatusValue.EXCLUSION` |
| Exception | `MeasureStatusValue.EXCEPTION` |
| Denominator | `MeasureStatusValue.DENOMINATOR` (rare, usually no status guard) |

---

## Examples

### Example 1: Simple BP Reading Evidence

**CQL:**
```cql
define "Encounter with Normal Blood Pressure Reading":
    "Qualifying Encounter" QualifyingEncounter
    let EncounterLastSystolicBP:
    Last(   ["Clinical Activity": "eCQM Direct Reference Codes; SYSTOLIC_BLOOD_PRESSURE"] SystolicBP
            where SystolicBP.stop_date during day of Interval[QualifyingEncounter.start_date, QualifyingEncounter.stop_date]
            sort by SystolicBP.start_date asc
        )
    where EncounterLastSystolicBP.result in Interval[1 'mm[Hg]', 119 'mm[Hg]']

// option_id: performance_met_normal
return ExtendedMeasureResultV2(
    EncounterLastSystolicBP
    , 'Normal BP Systolic Reading' as name
    , display_date = start_date
    , calculated_record = false
    , population = Numerator Performance Met
    , option_id = performance_met_normal
)
```

**DRL Status Rule:**
```drools
rule 'Year2026.Cms22.Success.performance_met_normal'
when
    EncounterDenominator($program: program, $encounter: clinicalActivity, $measure: measure == "Year2026.Cms22")
    Year2026Cms22NormalBPEncounter(record == $encounter)
then
    controlSet.addEncounterMeasureStatus($program, $encounter, $measure, MeasureStatusValue.SUCCESS, "performance_met_normal");
end
```

**DRL EMR Rule:**
```drools
rule 'Year2026.Cms22.EMR.performance_met_normal.SystolicReading'
salience -10
when
    EncounterMeasureStatus(
        $program: program,
        $encounter: clinicalActivity,
        $measure: measure == "Year2026.Cms22",
        statusValue == MeasureStatusValue.SUCCESS,
        optionId == "performance_met_normal"
    )
    $patient: Patient()
    $lastSystolic: ClinicalActivity(resultAsDecimal >= 1 && resultAsDecimal <= 119) from accumulate (
        $c: ClinicalActivity(
            withValueSet("Ecqm_Direct_Reference_CodesYear2026", "SYSTOLIC_BLOOD_PRESSURE"),
            (stopDate != null && endsDuring($encounter)) || (stopDate == null && startsDuring($encounter))
        ),
        latestClinicalActivity($c)
    )
then
    controlSet.addEncounterEMR(
        $program,
        $patient,
        $measure,
        $encounter,
        "Normal BP Systolic Reading",
        "performance_met_normal",
        PopulationEnum.numerator_performance_met,
        $lastSystolic,
        SupplementalInfoEnum.DISPLAY_DATE.displayName(),
        $lastSystolic.startDate.toString()
    );
end
```

### Example 2: Referral Evidence with Reason Code

**CQL:**
```cql
define "Encounter with Referral for Hypertension":
    "Qualifying Encounter" QualifyingEncounter
    with ["Clinical Activity": "VSAC; REFERRAL_TO_PRIMARY_CARE"] Referral
    such that Referral.reason_code in "VSAC; FINDING_OF_ELEVATED_BLOOD_PRESSURE"
        and Referral.author_datetime during day of Interval[QualifyingEncounter.start_date, QualifyingEncounter.stop_date]

return ExtendedMeasureResultV2(
    Referral
    , 'Referral for Elevated BP' as name
    , display_date = author_datetime
    , calculated_record = false
    , population = Numerator Performance Met
    , option_id = performance_met_elevated
)
```

**DRL EMR Rule:**
```drools
rule 'Year2026.Cms22.EMR.performance_met_elevated.Referral'
salience -10
when
    EncounterMeasureStatus(
        $program: program,
        $encounter: clinicalActivity,
        $measure: measure == "Year2026.Cms22",
        statusValue == MeasureStatusValue.SUCCESS,
        optionId == "performance_met_elevated"
    )
    $patient: Patient()
    $referral: ClinicalActivity(
        withValueSet("VsacYear2026", "REFERRAL_TO_PRIMARY_CARE_OR_ALTERNATE_PROVIDER"),
        withReasonCodeValueInValueSet("VsacYear2026", "FINDING_OF_ELEVATED_BLOOD_PRESSURE_OR_HYPERTENSION"),
        authoredDuring($encounter.startDate, $encounter.stopDate)
    )
then
    controlSet.addEncounterEMR(
        $program,
        $patient,
        $measure,
        $encounter,
        "Referral for Elevated BP",
        "performance_met_elevated",
        PopulationEnum.numerator_performance_met,
        $referral,
        SupplementalInfoEnum.DISPLAY_DATE.displayName(),
        $referral.authorDatetime.toString()
    );
end
```

### Example 3: Multiple EMR Rules for One Option (Systolic AND Diastolic)

**CQL:**
```cql
return ExtendedMeasureResultV2(
    NormalBPEncounter.NormalSys
    , 'Normal BP Systolic Reading' as name
    , display_date = start_date
    , calculated_record = false
    , population = Numerator Performance Met
    , option_id = performance_met_normal
)
and return ExtendedMeasureResultV2(
    NormalBPEncounter.NormalDias
    , 'Normal BP Diastolic Reading' as name
    , display_date = start_date
    , calculated_record = false
    , population = Numerator Performance Met
    , option_id = performance_met_normal
)
```

**DRL EMR Rules (Two Separate Rules):**
```drools
rule 'Year2026.Cms22.EMR.performance_met_normal.SystolicReading'
salience -10
when
    EncounterMeasureStatus(..., optionId == "performance_met_normal")
    $patient: Patient()
    $systolic: ClinicalActivity(...) from accumulate (...)
then
    controlSet.addEncounterEMR(..., "Normal BP Systolic Reading", "performance_met_normal", ..., $systolic, ...);
end

rule 'Year2026.Cms22.EMR.performance_met_normal.DiastolicReading'
salience -10
when
    EncounterMeasureStatus(..., optionId == "performance_met_normal")
    $patient: Patient()
    $diastolic: ClinicalActivity(...) from accumulate (...)
then
    controlSet.addEncounterEMR(..., "Normal BP Diastolic Reading", "performance_met_normal", ..., $diastolic, ...);
end
```

---

## Implementation Checklist

When converting CQL to DRL, for each ExtendedMeasureResultV2 return:

- [ ] Identify the evidence expression (what record is being returned)
- [ ] Identify the evidence name ('...' as name)
- [ ] Identify the option_id
- [ ] Identify the population type
- [ ] Identify the display_date field
- [ ] Determine if evidence uses Last/First (needs accumulate)
- [ ] Determine the status value that should guard the EMR rule
- [ ] Preserve the exact CQL option_id text and case in status and EMR output
- [ ] Generate status rule (if not already done)
- [ ] Generate EMR rule with:
  - [ ] Status guard (EncounterMeasureStatus or PatientMeasureStatus)
  - [ ] Evidence binding ($evidence: Type(...) from accumulate if needed)
  - [ ] addEncounterEMR or addPatientEMR call
  - [ ] Correct evidence parameter (bound variable, not null)
    - [ ] No exclusion/suppression guard unless the owning CQL definition already contains it
  - [ ] Correct date field extraction
- [ ] If CQL has multiple return statements for same option_id, generate multiple EMR rules
- [ ] Validate EMR rule salience is -10 (standard)
- [ ] Validate evidence is not `null` or placeholder

---

## Common Mistakes to Avoid

### [NO] Wrong: Using Exists Instead of Binding
```drools
when
    exists ClinicalActivity(...) from accumulate (...)
then
    controlSet.addEncounterEMR(..., null, ...);  // No evidence!
```

### [YES] Correct: Evidence Binding
```drools
when
    $evidence: ClinicalActivity(...) from accumulate (...)
then
    controlSet.addEncounterEMR(..., $evidence, ...);
```

### [NO] Wrong: Missing Status Guard
```drools
rule 'Year2026.Cms22.EMR.option_x'
when
    EncounterDenominator(...)
    $evidence: ClinicalActivity(...)
then
    controlSet.addEncounterEMR(...);
end
```
*Problem:* EMR will fire even if status didn't pass (e.g., exclusion)

### [YES] Correct: Status Guard Present
```drools
rule 'Year2026.Cms22.EMR.option_x'
when
    EncounterMeasureStatus(..., statusValue == MeasureStatusValue.SUCCESS, optionId == "option_x")
    $evidence: ClinicalActivity(...)
then
    controlSet.addEncounterEMR(...);
end
```

### [NO] Wrong: Only One EMR Rule for Multiple CQL Returns
```cql
return ExtendedMeasureResultV2(Sys, 'Systolic', ..., option_id = X)
and return ExtendedMeasureResultV2(Dias, 'Diastolic', ..., option_id = X)
```
```drools
// Only one rule generated - missing Diastolic!
rule 'EMR.option_x.Systolic' ...
```

### [YES] Correct: Multiple EMR Rules
```drools
rule 'EMR.option_x.Systolic' ... end
rule 'EMR.option_x.Diastolic' ... end
```

---

## Validation

After generating EMR rules, validate:

1. **Coverage**: Every option_id in CQL ExtendedMeasureResultV2 has at least one EMR rule
2. **Evidence**: No EMR rule has `null` or placeholder in evidence parameter
3. **Status Guard**: Every EMR rule checks for appropriate status value
4. **Salience**: All EMR rules have salience -10
5. **Count**: Number of EMR rules ≥ number of ExtendedMeasureResultV2 returns (can be more if one return needs multiple evidence records)
6. **Parity**: EMR population enum and suppression predicates match the owning CQL definition and resulting status semantics
