Repair the CSV contact importer without changing its APIs.

parse_rows(text) returns row dictionaries using the CSV header. Support standard
CSV quoting, escaped quotes, commas and newlines inside quoted fields, and LF
or CRLF records. Empty input returns [].

import_contacts(text) returns dictionaries with exactly email and name. Trim and
casefold email addresses, skip rows with missing or blank email, and trim names
(missing names become ""). Deduplicate by normalized email: the last record's
name wins, but output order stays the order of each email's first appearance.
Ignore other columns. Do not change the original text. Preserve the APIs.

Read the implementation and tests, edit the source files, and use the automatic
test feedback to finish the repair. Do not change tests. Shell commands are
disabled; the CLI runs the tests automatically after each edit.
