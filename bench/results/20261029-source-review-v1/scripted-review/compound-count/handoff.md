# Investigation handoff

## Issue
How many more customer visits did the north studio record than the south studio?

Status: reviewed
Investigation: bdd00bc5\-e113\-4827\-bc1c\-f638ecd0e85b
Model: qwen3\-8b\-q4
Model SHA256: d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785
Created: 2026\-10\-29T22:41:12\.220372\+00:00

Workspace: synthetic\-eval
Collections: ef719aa5\-d72e\-4aab\-be3a\-c420b0ade493
Template: investigation\-v9
Retrieval: unicode\-word\-overlap\-v1
Generation settings: \{'temperature': 0\.7, 'max\_tokens': 2000, 'timeout\_seconds': 240, 'seed': 7, 'timeout\_scope': 'shared across planning and answer generation'\}

## Questions and answers
### How many more customer visits did the north studio record than the south studio?
supported: Signed difference: 13 customer visits\.
- Source excerpt (source:a9a92718\-e380\-506e\-899f\-bca9cbfdbf4b:20abed5a\-de17\-50ad\-bc32\-091472095aa1): The north studio recorded 37 customer visits\. 
- Source excerpt (source:a9a92718\-e380\-506e\-899f\-bca9cbfdbf4b:20abed5a\-de17\-50ad\-bc32\-091472095aa1): The south studio recorded 24 customer visits\. 

## Plan acknowledgement
fixture\_supplied
Plan confirmation is not approval of the answers.

## Quantity extraction notices

## Original machine calculations
These calculations belong to the original draft; reviewer corrections are separate.

### R1 · difference
Original request: How many more customer visits did the north studio record than the south studio?
Status: computed
Algorithm: cited\-calculations\-v3
Formula: after \- before
Evaluated expression: 24 customer visits \- 37 customer visits = \-13 customer visits \(rounded\)
Result: \-13 customer visits
Rounding: \{'mode': 'ROUND\_HALF\_UP', 'max\_fractional\_digits': 6\}
Reason: none
Limit: Arithmetic only; source selection, authority, completeness and comparability are not verified\.
- Input N1 (source:a9a92718\-e380\-506e\-899f\-bca9cbfdbf4b:20abed5a\-de17\-50ad\-bc32\-091472095aa1, S1): 37 customer visits
- Input N2 (source:a9a92718\-e380\-506e\-899f\-bca9cbfdbf4b:20abed5a\-de17\-50ad\-bc32\-091472095aa1, S2): 24 customer visits
- Normalized before (N1): 37 customer visits · conversion factor 1
- Normalized after (N2): 24 customer visits · conversion factor 1

## Reviewed calculation choices
Reviewer: fixture\-script \(not owner acceptance\)

### R1
Selection: calculate
Inputs: N2, N1
Reason: Fixture\-script selection: subtract south 24 customer visits from north 37; five meeting rooms is unrelated\.
Result: Signed difference: 13 customer visits\.
Algorithm: cited\-calculations\-v3
Formula: after \- before
Evaluated expression: 37 customer visits \- 24 customer visits = 13 customer visits \(rounded\)
Rounding: \{'mode': 'ROUND\_HALF\_UP', 'max\_fractional\_digits': 6\}
Limit: Arithmetic only; source selection, authority, completeness and comparability are not verified\.
- Normalized before (N2): 24 customer visits · conversion factor 1
- Normalized after (N1): 37 customer visits · conversion factor 1
- Source excerpt (source:a9a92718\-e380\-506e\-899f\-bca9cbfdbf4b:20abed5a\-de17\-50ad\-bc32\-091472095aa1): The north studio recorded 37 customer visits\. 
- Source excerpt (source:a9a92718\-e380\-506e\-899f\-bca9cbfdbf4b:20abed5a\-de17\-50ad\-bc32\-091472095aa1): The south studio recorded 24 customer visits\. 

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
- source:a9a92718\-e380\-506e\-899f\-bca9cbfdbf4b:20abed5a\-de17\-50ad\-bc32\-091472095aa1 · source\.md · intro
  - source\.md · revision 20abed5a\-de17\-50ad\-bc32\-091472095aa1 · chunk a9a92718\-e380\-506e\-899f\-bca9cbfdbf4b

## Review
correct by fixture\-script \(not owner acceptance\): Scripted qualification only; not actual owner review\.
The sections above use the reviewed correction. The original draft is retained in Potluck.

## Actual outcome
Not recorded
