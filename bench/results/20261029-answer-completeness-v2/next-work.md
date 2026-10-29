# Proposed next implementation

The v7 candidate is frozen. Its fresh failures show that exact issue spans and
additional calculation instructions do not ensure a complete numerical answer.
Copied punctuation also caused a valid-intent plan to fail, while a different
plan fragmented one request into clauses that were not independently meaningful.
This proposal is not implemented or evaluated.

1. Give each requested operation its own required result. A model can propose
   operation types from a closed list, bound to exact request text; preserve the
   original issue and permit explicit owner-supplied operations. Multiple
   operations can belong to one question. Validate the structure and reject
   invented operation names. Prefer server-issued request IDs over requiring the
   model to reproduce question punctuation. Exact text still cannot prove intent
   coverage or that a selected fragment is a meaningful requested operation.
2. Select operands using snippet IDs, exact numeric substrings and explicit roles
   such as before/after or part/total. Validate source membership, ambiguity,
   numeric magnitude/precision and compatible units. Start with a small unit and
   operation set, without expression evaluation or inferred source authority.
3. Compute accepted operations with Decimal and deterministic formulas. Keep
   percentage change separate from percentage-point difference. Missing operands,
   zero denominators, incompatible units and unresolved source authority must
   remain explicit limitations. Never silently choose among conflicting values.
4. Render each calculation and cited inputs directly, with its formula, units
   and rounding. Retain original draft provenance through correction and export;
   reviewer-edited prose must not inherit a calculation-verification claim.
   Preserve source invalidation and scope checks.
5. Use the consumed v1/v2 cases for development and a separately authored new
   holdout for evaluation. Measure omitted operations, wrong operand selection,
   arithmetic errors and evidence precision separately. Retain every attempt and
   independent semantic review before considering a preview change.

Deterministic arithmetic verifies a calculation from selected inputs. It cannot
establish that the selected inputs answer the question, share the same real-world
scope, or come from an authoritative source. Those remain review/evaluation gates.
