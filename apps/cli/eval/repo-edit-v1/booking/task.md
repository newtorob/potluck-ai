Fix interval conflict detection and booking availability in this repository.

Intervals are half-open [start, end), with start <= end. Adjacent intervals do not
conflict. Empty intervals never conflict, even inside another interval. Both
lists and tuples are valid intervals. is_available(existing, candidate) returns
True exactly when candidate conflicts with none of the existing intervals;
existing may be a one-shot iterator. Do not mutate inputs. Preserve the API.

Read the implementation and tests, edit the source files, and use the automatic
test feedback to finish the repair. Do not change tests. Shell commands are
disabled; the CLI runs the tests automatically after each edit.
