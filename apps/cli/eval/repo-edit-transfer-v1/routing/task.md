Repair path normalization and longest-prefix routing, preserving the APIs.

normalize_path(path) strips any query or fragment beginning at the first '?' or
'#', ensures one leading slash, collapses repeated slashes, and removes trailing
slashes except for the root '/'. An empty path becomes '/'. Keep letter case,
percent escapes and literal '.' or '..' segments unchanged; do not URL-decode.

select_route(path, routes) receives a path and an ordered list of (prefix, target)
pairs. Normalize the path and each prefix. A prefix matches if it equals the
path or ends at a segment boundary; '/api' must not match '/apix'. Root matches
all paths. Return the target of the longest matching normalized prefix. For
equal normalized lengths choose the earliest route. Return None if no route
matches. Do not mutate the input list. Targets may be any object, including
false values; return the original target object without copying it.

Read implementation and tests, edit both source files, and finish using the
automatic test feedback. Do not change tests. Shell commands are disabled;
the CLI runs tests automatically after each edit.
