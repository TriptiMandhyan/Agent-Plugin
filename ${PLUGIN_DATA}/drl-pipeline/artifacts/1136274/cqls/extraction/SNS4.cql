library HEDIS_SNSE4_2027

/*
 * Internal ID: 27.HEDIS.Sns4
 * Subject Type: Patient
 * Inverse Measure: False
 */

using ADM

include "2027/HEDIS/shared/HospiceDuringPeriod.cql" called Hospice
include "2027/HEDIS/shared/LTIDuringPeriod.cql" called LTI
include "2027/HEDIS/shared/DeceasedDuringPeriod.cql" called DeceasedDuringPeriod
include "2027/HEDIS/shared/ContinuousEnrollmentShared.cql" called CEShared


// VALUESETS
valueset "HEDIS; HOMELESSNESS_PROCEDURES": '2.16.840.1.113883.3.464.1004.2410'
valueset "HEDIS; HOUSING_INADEQUACY_PROCEDURES": '2.16.840.1.113883.3.464.1004.2411'
valueset "HEDIS; HOUSING_INSTABILITY_PROCEDURES": '2.16.840.1.113883.3.464.1004.2412'

// DIRECT REFERENCE HEDIS VALUESETS
valueset "HEDIS; DIRECT_REFERENCE_CHILDREN_S_HEALTH_WATCH_HOUSING_STABILITY_VITAL_SIGNS_SCREENING"
valueset "HEDIS; DIRECT_REFERENCE_HOUSING_INADEQUACY_AHC_AND_AAFP_SCREENING"
valueset "HEDIS; DIRECT_REFERENCE_HOUSING_INADEQUACY_NCHC_SCREENING"
valueset "HEDIS; DIRECT_REFERENCE_HOUSING_INSTABILITY_AND_HOMELESSNESS_MULTI_SCREENING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_HOUSING_PARTIAL_INSTABILITY_AND_INSECURITY_SCREENING_TOOLS"
valueset "HEDIS; DIRECT_REFERENCE_OTHER_HOUSING_INADEQUACY_NCHC_SCREENING"
valueset "HEDIS; DIRECT_REFERENCE_LA31994_9_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA31996_4_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA32001_2_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA28580_1_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA31998_0_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA30190_5_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA32000_4_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA32695_1_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA31997_2_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA32693_6_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA31995_6_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA31999_8_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA33_6_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA32691_0_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA32694_4_POSITIVE_FINDING_CODE"
valueset "HEDIS; DIRECT_REFERENCE_LA32696_9_POSITIVE_FINDING_CODE"

parameter "Measurement Period" Interval<DateTime>

context Patient

// POPULATIONS
define "HEDIS Certification Eligible Population"  //should be included in HEDIS output
    AgeInYearsAt(start of "Measurement Period") > 0 //Age for HEDIS output should be age at start of "Measurement Period"
    and not exists(Patient.deceased_datetime before start of "Measurement Period")

define "Denominator":
    "HEDIS Certification Eligible Population"
    and exists("First Positive Housing Insecurity") //denominator event = true in the HEDIS output if exists("First Positive Housing Insecurity")
    ,
    if exists("Denominator")
    from "First Positive Housing Insecurity" FirstPositiveHousingInsecurity
    return
    ExtendedMeasureResultV2(
        FirstPositiveHousingInsecurity
        , 'First Evidence of Housing Insecurity' as name
        , display_date = start_date
        , calculated_record = false
        , population = Denominator
        , option_id = all
    )

define "Denominator Exclusion - Deceased":
/* OptionID: denominator_exclusion_deceased */
    exists(DeceasedDuringPeriod."Deceased During Period"("Measurement Period"))

define "Denominator Exclusion - LTI and SNP":
/* Option ID: denominator_exclusion_LTI */
    exists(   LTI."LTI During Period"("Measurement Period") or LTI."SNP"("Measurement Period"))LTIPeriod
    ,
    return
    ExtendedMeasureResultV2(
        LTIPeriod
        , 'Evidence of LTI or SNP' as name
        , display_date = start_date
        , calculated_record = false
        , population = Exclusion
        , option_id = denominator_exclusion_LTI
    )

define "Denominator Exclusion - Hospice":
/* Option ID: denominator_exclusion_hospice */
    exists(Hospice."Hospice During Period"("Measurement Period"))HospiceDuringPeriod
    ,
    return
    ExtendedMeasureResultV2(
        HospiceDuringPeriod
        , 'Evidence of Hospice' as name
        , display_date = start_date
        , calculated_record = false
        , population = Exclusion
        , option_id = denominator_exclusion_hospice
    )

define "Numerator Performance Met":
/* Option ID: performance_met */
  exists((["Clinical Activity": "HEDIS; HOMELESSNESS_PROCEDURES"]
          union ["Clinical Activity": "HEDIS; HOUSING_INSTABILITY_PROCEDURES"]) HomelessnessOrInstabilityIntervention
        with "First Positive Housing Insecurity"  PosScreen
             with "Housing Instability and Homelessness" HousingInsecurity
             such that   ( HomelessnessOrInstabilityIntervention.start_date during Interval[PosScreen.start_date, PosScreen.start_date + 30 days]
                           and PosScreen.start_date = HousingInsecurity.start_date) )HomelessnessorHousingInstabilityScreeningIntervention
    or exists(["Clinical Activity": "HEDIS; HOUSING_INADEQUACY_PROCEDURES"] InadequacyIntervention
              with "First Positive Housing Insecurity"  PosScreen
                   with "Housing Inadequacy" Inadequacy
                   such that   ( InadequacyIntervention.start_date during Interval[PosScreen.start_date, PosScreen.start_date + 30 days]
                                 and PosScreen.start_date = Inadequacy.start_date) )HousingInadequacyIntervention
    ,
    if exists (HomelessnessorHousingInstabilityScreeningIntervention)
    then return
    ExtendedMeasureResultV2(
        HomelessnessorHousingInstabilityScreeningIntervention
        , 'Intervention within 30 days of Homelessness or Housing Instability Screening' as name
        , display_date = start_date
        , calculated_record = false
        , population = Numerator Performance Met
        , option_id = performance_met
    )
    else if exists (HousingInadequacyIntervention)
    then return
    ExtendedMeasureResultV2(
        HousingInadequacyIntervention
        , 'Intervention within 30 days of Housing Inadequacy Finding' as name
        , display_date = start_date
        , calculated_record = false
        , population = Numerator Performance Met
        , option_id = performance_met
    )

define "Numerator Performance Not Met":
/* Option ID: performance_not_met */
    not "Denominator Exclusion - Deceased"
    and not "Denominator Exclusion - LTI and SNP"
    and not "Denominator Exclusion - Hospice"
    and not "Numerator Performance Met"

define "Continuous Enrollment - SNS":
    if start of "Measurement Period" is after "GetMaxCoverageEndDateForPayer"(P)
    return false,
    else
        CEShared."HasContinuousEnrollmentByBenefitTypes"(
            Interval[start of "Measurement Period", first("GetMaxCoverageEndDateForPayer"(P), end of "Measurement Period")]
            , {'medical'}
            , maxAllowedGapDays 45
            , minConsecutiveGapDays 45
            , maxAllowedGaps 1
        )
        and CEShared."HasEnrollmentOnDateByBenefitTypes"(first("GetMaxCoverageEndDateForPayer"(P), end of "Measurement Period"), {'medical'})

// DEFINITIONS
define "Screening Period":
    Interval[start of "Measurement Period", start of "Measurement Period" + 11 months]

define "First Positive Housing Insecurity":
  (First( ("Housing Instability and Homelessness"
           union "Housing Inadequacy")HousingInsecurity )
    sort by HousingInsecurity.start_date asc) FirstPositiveHousingInsecurity

define "Housing Instability and Homelessness":
    (   (   ["Clinical Activity": "HEDIS; DIRECT_REFERENCE_HOUSING_PARTIAL_INSTABILITY_AND_INSECURITY_SCREENING_TOOLS"] Partial
            where Partial.result_code_type, Partial.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA33_6_POSITIVE_FINDING_CODE"]   )
        or  (   ["Clinical Activity":  "HEDIS; DIRECT_REFERENCE_CHILDREN_S_HEALTH_WATCH_HOUSING_STABILITY_VITAL_SIGNS_SCREENING"] CHWVS
                where CHWVS.result_as_decimal >= '2'    )
        or  (   ["Clinical Activity": "HEDIS; DIRECT_REFERENCE_HOUSING_INSTABILITY_AND_HOMELESSNESS_MULTI_SCREENING_CODE"] Multi
                where   (   Multi.result_code_type, Multi.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA31994_9_POSITIVE_FINDING_CODE"]
                            or Multi.result_code_type, Multi.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA31995_6_POSITIVE_FINDING_CODE"]
                            or Multi.result_code_type, Multi.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA30190_5_POSITIVE_FINDING_CODE"]   )   )
    ) Instability
    where Instability.start_date during "Screening Period"

define "Housing Inadequacy":
    (   (   ["Clinical Activity": "HEDIS; DIRECT_REFERENCE_HOUSING_INADEQUACY_AHC_AND_AAFP_SCREENING"] AHC
            where   (   AHC.result_code_type, AHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA31996_4_POSITIVE_FINDING_CODE"]
                        or AHC.result_code_type, AHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA28580_1_POSITIVE_FINDING_CODE"]
                        or AHC.result_code_type, AHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA31997_2_POSITIVE_FINDING_CODE"]
                        or AHC.result_code_type, AHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA31998_0_POSITIVE_FINDING_CODE"]
                        or AHC.result_code_type, AHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA31999_8_POSITIVE_FINDING_CODE"]
                        or AHC.result_code_type, AHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA32000_4_POSITIVE_FINDING_CODE"]
                        or AHC.result_code_type, AHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA32001_2_POSITIVE_FINDING_CODE"]
                        or AHC.result_code_type, AHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA32691_0_POSITIVE_FINDING_CODE"]
                        or AHC.result_code_type, AHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA32693_6_POSITIVE_FINDING_CODE"]
                        or AHC.result_code_type, AHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA32694_4_POSITIVE_FINDING_CODE"]
                        or AHC.result_code_type, AHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA32695_1_POSITIVE_FINDING_CODE"]
                        or AHC.result_code_type, AHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA32696_9_POSITIVE_FINDING_CODE"]   )   )
        or  (   ["Clinical Activity": "HEDIS; DIRECT_REFERENCE_HOUSING_INADEQUACY_NCHC_SCREENING"] NCHC
                where   (   NCHC.result_code_type, NCHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA31996_4_POSITIVE_FINDING_CODE"]
                            or NCHC.result_code_type, NCHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA28580_1_POSITIVE_FINDING_CODE"]
                            or NCHC.result_code_type, NCHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA31997_2_POSITIVE_FINDING_CODE"]
                            or NCHC.result_code_type, NCHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA31998_0_POSITIVE_FINDING_CODE"]
                            or NCHC.result_code_type, NCHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA31999_8_POSITIVE_FINDING_CODE"]
                            or NCHC.result_code_type, NCHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA32000_4_POSITIVE_FINDING_CODE"]
                            or NCHC.result_code_type, NCHC.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA32001_2_POSITIVE_FINDING_CODE"] )   )
        or (    ["Clinical Activity": "HEDIS; DIRECT_REFERENCE_OTHER_HOUSING_INADEQUACY_NCHC_SCREENING"] NCHC2
                where NCHC2.result_code_type, NCHC2.result_code_value in ["HEDIS; DIRECT_REFERENCE_LA33_6_POSITIVE_FINDING_CODE"]   )
    ) Inadequacy
    where Inadequacy.start_date during "Screening Period"