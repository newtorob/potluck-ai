# Investigation prompt encoding

The internal `investigation-v21` service supports opt-in
`prompt_encoding="compact-calculation-v1"`. Public routes and defaults are unchanged.

Calculation extraction warnings repeat citation IDs and reason strings. The compact
encoding keeps each warning's snippet ID, source text, offsets and other fields,
replaces the reason with a zero-based index into `extraction_notice_reasons`, and
omits the redundant citation ID. The original warning list, snippet-to-citation
mapping and evidence remain in saved provenance. Nothing is removed from the
source passages, quantity catalog, extraction warnings or evidence warnings.

Requests without extraction warnings retain the original prompt bytes. Encoded
requests record the encoding version, before/after SHA-256, character counts and
warning counts. Character counts are not token counts. The model context, output
allowance and deadlines are unchanged; this encoding does not guarantee that all
possible requests fit a context window.

Compact encoding alone does not add formula evaluation. The additional internal
`formula_mode="source-formula-ratio-v1"` supports reviewed percentage requests
that explicitly mention a formula or equation. It uses the same displayed source
formula for two scenarios, evaluates their derived values, and computes part /
total × 100. Requests outside this mode keep the existing direct-operand path.

The source formula must be a complete scalar assignment with ASCII variable names,
addition, subtraction, multiplication, division and bounded integer powers.
Both `^` and `**` mean exponentiation; bitwise XOR is unsupported.
Functions, attribute access, indexing, code execution and arbitrary model-generated
expressions are rejected. Limits are 240 expression characters, 48 AST nodes,
8 variables and 8 formula candidates. Intermediate nonzero values must lie between
1e-18 and 1e18; integer exponents are bounded to ±64. Arithmetic uses Decimal with
60 digits of precision, with the final percentage rounded half-up to 6 places.
Undefined or out-of-bounds arithmetic saves an explicit unknown result.

The model selects a source formula and binds variables to extracted source quantity
IDs or exact numeric spans in the user's issue. Units must match for each variable
across the two scenarios; this mode does not infer unit conversions. Arithmetic
is checked, but formula choice, variable meaning, dimensional correctness and
source authority remain unverified model assessments. Source constants should be
selected from the source quantities. User scenario values are labeled separately
from document facts in the answer, saved trace and export.

Corrections rebuild the original formula catalog, bindings and arithmetic before
accepting an owner-edited answer. Original computed values and provenance remain
separate from the owner's correction. No public route or default is promoted by
these internal opt-in modes.
