library HEDIS_SNS5_2027

/*
 * Internal ID: 27.HEDIS.Sns5
 * Subject Type: Patient
 * Inverse Measure: False
 */

using ADM

include "2027/HEDIS/shared/HospiceDuringPeriod.cql" called Hospice
include "2027/HEDIS/shared/LTIDuringPeriod.cql" called LTI
include "2027/HEDIS/shared/DeceasedDuringPeriod.cql" called DeceasedDuringPeriod
include "2027/HEDIS/shared/ContinuousEnrollmentShared.cql" called CEShared


// DIRECT REFERENCE HEDIS VALUESETS
valueset "HEDIS; DIRECT_REFERENCE_TRANSPORTATION_AAFP_SHORT_FORM"
valueset "HEDIS; DIRECT_REFERENCE_PARTIAL_TRANSPORTATION_SCREENING_TOOLS"
valueset "HEDIS; DIRECT_REFERENCE_TRANSPORTATION_CUBS_SCREENING"
valueset "HEDIS; DIRECT_REFERENCE_TRANSPORTATION_PROMIS_SCREENING"
valueset "HEDIS; DIRECT_REFERENCE_MULTI_TRANSPORTATION_SCREENING_CODE"


parameter "Measurement Period" Interval<DateTime>

context Patient

// POPULATIONS
define "HEDIS Certification Eligible Population"  //should be included in HEDIS output
    AgeInYearsAt(start of "Measurement Period") > 0 //Age for HEDIS output should be age at start of "Measurement Period"
    and not exists(Patient.deceased_datetime before start of "Measurement Period")

define "Denominator":
    "HEDIS Certification Eligible Population"   //denominator event = false as no event exists in HEDIS output

define "Denominator Exclusion - Deceased":
/* OptionID: denominator_exclusion_deceased */
    exists(DeceasedDuringPeriod."Deceased During Period"("Measurement Period"))

define "Denominator Exclusion - LTI and SNP":
/* Option ID: denominator_exclusion_LTI */
    exists (   LTI."LTI During Period"("Measurement Period") or LTI."SNP"("Measurement Period"))LTIPeriod
    ,
    return
    ExtendedMeasureResultV2(
        LTIPeriod // Return only 1 record even if multiple LTI or SNP evidence exist
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
        HospiceDuringPeriod // Return only 1 record even if multiple hospice evidence exist
        , 'Evidence of Hospice' as name
        , display_date = start_date
        , calculated_record = false
        , population = Exclusion
        , option_id = denominator_exclusion_hospice
    )

define "Numerator Performance Met":
/* Option ID: performance_met */
   exists("Transportation Insecurity Screening") TransportationInsecurityScreening
   ,
    return
    ExtendedMeasureResultV2(
        TransportationInsecurityScreening // Return only 1 record even if multiple screenings exist
        , 'Transportation Insecurity Screening' as name
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

define "Transportation Insecurity Screening":
  exists(  ["Clinical Activity": "HEDIS; DIRECT_REFERENCE_TRANSPORTATION_AAFP_SHORT_FORM"] 
           union ["Clinical Activity": "HEDIS; DIRECT_REFERENCE_PARTIAL_TRANSPORTATION_SCREENING_TOOLS"]
           union ["Clinical Activity": "HEDIS; DIRECT_REFERENCE_TRANSPORTATION_CUBS_SCREENING"]
           union ["Clinical Activity": "HEDIS; DIRECT_REFERENCE_TRANSPORTATION_PROMIS_SCREENING"]
           union ["Clinical Activity": "HEDIS; DIRECT_REFERENCE_MULTI_TRANSPORTATION_SCREENING_CODE"] Insecurity
            where   (   Insecurity.start_date during "Screening Period"
                        and Insecurity.result is not null or empty   )) TransportationInsecurityScreening

