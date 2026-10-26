<!--- Apply WIP Label if work in progress. Remove when ready for review. -->

<!-- Add link to Tracker Story -->
ADO Work Item: <paste ticketUrl here>

***

<!-- Add additional information here -->
## Summary of Changes

### For PR Reviewers
Review [*Effective Code Reviews*](https://app.tettra.co/teams/ablehealth/pages/peer-review-process-1) to maximize the effectiveness of your review.

### Development checks:
- [ ] Check null for optional fields
- [ ] Performance optimization using Exists
- [ ] Correct use of MeasureStatus
- [ ] Use CQL statement as Javadoc
- [ ] Rule should have enough logic in order to avoid create unnecessary facts
- [ ] Test has been properly updated
- [ ] The implementation matchs with subject type (EncounterDenominator/PatientDenominator)
- [ ] Verify if Org/group/provider versions are needed
- [ ] Use only primitives facts (ClinicalActivities, Medications, Diagnosis) on queries
- [ ] On queries use general exists when we need to use an OR 
- [ ] Do not use the same query to verify if exists more than one object with a different attribute i.e startDate

### Deployment
#### Subtasks added to the Deploy ticket if:
- [ ] ENV VARS need to be added/removed to Aptible enviroments and [ENV VAR list](https://healthcatalyst.box.com/s/2e7d5jce9my3696nbcc5sxp9f0t7p6ut) needs to be updated. Please create a card on the "Ops Tickets" Asana board stating an ENV var needs to be added.