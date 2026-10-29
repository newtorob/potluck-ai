# Investigation handoff

## Issue
How has the number of available workshop seats changed since the prior count?

Status: reviewed
Investigation: cd090904\-72ab\-4031\-98ef\-8c8a1cdc0042
Model: qwen3\-8b\-q4
Model SHA256: d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785
Created: 2026\-10\-29T22:40:39\.417813\+00:00

Workspace: synthetic\-eval
Collections: 7ece7f46\-16da\-4bac\-bcfb\-7e6f1c59b086
Template: investigation\-v9
Retrieval: unicode\-word\-overlap\-v1
Generation settings: \{'temperature': 0\.7, 'max\_tokens': 2000, 'timeout\_seconds': 240, 'seed': 7, 'timeout\_scope': 'shared across planning and answer generation'\}

## Questions and answers
### How has the number of available workshop seats changed since the prior count?
conflicting: Reviewer assessment \(conflicting\): Fixture\-script assessment: prior 30 workshop seats; current sources disagree at 42 and 48\. Neither is designated authoritative, so the change is unresolved\.
- Source excerpt (source:f9976f9c\-f483\-584a\-8633\-9cc0e831a6d2:fb088aad\-b9a2\-581a\-a413\-287da5572c9f): The prior count recorded 30 available workshop seats\. 
- Source excerpt (source:f9976f9c\-f483\-584a\-8633\-9cc0e831a6d2:fb088aad\-b9a2\-581a\-a413\-287da5572c9f): The scheduling ledger says 42 workshop seats are currently available\. 
- Source excerpt (source:f9976f9c\-f483\-584a\-8633\-9cc0e831a6d2:fb088aad\-b9a2\-581a\-a413\-287da5572c9f): The venue notice says 48 workshop seats are currently available\. 
- Source excerpt (source:f9976f9c\-f483\-584a\-8633\-9cc0e831a6d2:fb088aad\-b9a2\-581a\-a413\-287da5572c9f): Neither source has been designated authoritative for current availability\.

## Plan acknowledgement
fixture\_supplied
Plan confirmation is not approval of the answers.

## Quantity extraction notices
- S1 (source:f9976f9c\-f483\-584a\-8633\-9cc0e831a6d2:fb088aad\-b9a2\-581a\-a413\-287da5572c9f): 30 available workshop seats\.  · Number\-like source text was not extracted; its format or unit may be unsupported or incomplete\.

## Original machine calculations
These calculations belong to the original draft; reviewer corrections are separate.

### R1 · difference
Original request: How has the number of available workshop seats changed since the prior count?
Status: conflicting
Algorithm: cited\-calculations\-v3
Formula: unavailable
Evaluated expression: unavailable
Result: unavailable
Rounding: unavailable
Reason: Conflicting values for the number of available workshop seats are provided by different sources \(42 and 48\)\.
Limit: Arithmetic does not verify source selection or authority\.
- Input N1 (source:f9976f9c\-f483\-584a\-8633\-9cc0e831a6d2:fb088aad\-b9a2\-581a\-a413\-287da5572c9f, S2): 42 workshop seats
- Input N2 (source:f9976f9c\-f483\-584a\-8633\-9cc0e831a6d2:fb088aad\-b9a2\-581a\-a413\-287da5572c9f, S3): 48 workshop seats

## Reviewed calculation choices
Reviewer: fixture\-script \(not owner acceptance\)

### R1
Selection: conflicting
Inputs: none
Reason: Fixture\-script assessment: prior 30 workshop seats; current sources disagree at 42 and 48\. Neither is designated authoritative, so the change is unresolved\.
Result: Reviewer assessment \(conflicting\): Fixture\-script assessment: prior 30 workshop seats; current sources disagree at 42 and 48\. Neither is designated authoritative, so the change is unresolved\.
Algorithm: unavailable
Formula: unavailable
Evaluated expression: unavailable
Rounding: unavailable
Limit: This assessment does not verify source completeness or authority\.
- Source excerpt (source:f9976f9c\-f483\-584a\-8633\-9cc0e831a6d2:fb088aad\-b9a2\-581a\-a413\-287da5572c9f): The prior count recorded 30 available workshop seats\. 
- Source excerpt (source:f9976f9c\-f483\-584a\-8633\-9cc0e831a6d2:fb088aad\-b9a2\-581a\-a413\-287da5572c9f): The scheduling ledger says 42 workshop seats are currently available\. 
- Source excerpt (source:f9976f9c\-f483\-584a\-8633\-9cc0e831a6d2:fb088aad\-b9a2\-581a\-a413\-287da5572c9f): The venue notice says 48 workshop seats are currently available\. 
- Source excerpt (source:f9976f9c\-f483\-584a\-8633\-9cc0e831a6d2:fb088aad\-b9a2\-581a\-a413\-287da5572c9f): Neither source has been designated authoritative for current availability\.

## Facts
- None recorded

## Prior fixes
- None recorded

## Hypotheses (unverified)
- None recorded

## Recommended checks
- None recorded

## Model questions (unverified)

## Retrieval notices

## Evidence provenance
- source:f9976f9c\-f483\-584a\-8633\-9cc0e831a6d2:fb088aad\-b9a2\-581a\-a413\-287da5572c9f · source\.md · intro
  - source\.md · revision fb088aad\-b9a2\-581a\-a413\-287da5572c9f · chunk f9976f9c\-f483\-584a\-8633\-9cc0e831a6d2

## Review
correct by fixture\-script \(not owner acceptance\): Scripted qualification only; not actual owner review\.
The sections above use the reviewed correction. The original draft is retained in Potluck.

## Actual outcome
Not recorded
