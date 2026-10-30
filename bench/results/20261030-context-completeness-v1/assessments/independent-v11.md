# Independent semantic review: 8b-v11

Frozen required-plus-forbidden rubric: **5/6 cases pass**. Harbor, Cedar, Mosaic, Atlas and Birch contain all required facets with support in their attached excerpts; none makes a forbidden claim. Quill fails because no validated answer or attached excerpts were returned. All six cases remain in the denominator.

Quill’s raw generated response mentions both departure times and says approval is not established. It repeats parts and labels some conflicting and others supported; the attempt returned `ValueError: Invalid model format` at inference or validation. The raw response is not an accepted answer, so its content does not convert the case into a pass. No answer-level status was returned.

Compared with v10, Harbor changes from fail to pass because the attached quotations now include the complete delay and calibration conditions. Quill remains a fail but changes from an answer with incomplete required attachment support to a validation error with no returned answer. Total passes increase from **4/6 to 5/6**; complete factual text in returned answers decreases from **6/6 to 5/6**. This is a six-case comparison, not evidence of a broader rate.

This independent review used the frozen rubric and answer excerpts resolved through result evidence; it did not inspect implementation, prompts, or root scores. Status and extra-claim concerns remain separate from the rubric.
