Repair configuration layering and option validation, preserving the APIs.

merge_layers(*layers) combines dictionaries from left to right, with later
values overriding earlier values. Merge recursively only when both old and
new values are dictionaries. Lists and scalars are replaced. Preserve explicit
None, False, zero and empty strings. Return a deep independent copy: modifying
any nested dictionary or list in the result must not change any input.

load_options(defaults, project, overrides) uses merge_layers, then validates the
merged result. workers must be an integer from 1 through 64 (not a boolean).
timeout must be an int or float greater than or equal to zero (not a boolean);
None disables the timeout. Missing workers defaults to 1; missing timeout to
30. Invalid values raise ValueError. Preserve all other keys and nested values.

Read implementation and tests, edit both source files, and finish using the
automatic test feedback. Do not change tests. Shell commands are disabled;
the CLI runs tests automatically after each edit.
