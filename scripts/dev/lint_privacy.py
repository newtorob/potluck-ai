#!/usr/bin/env python3
"""Privacy-invariant linter.

Enforces the manifesto-level privacy rules at CI time
(memory 42a7e671 + 1fc85a49):

  #20 — Coordinator-side: a per-peer identity token (member_id, peer_id,
        node_name, mesh_ip, hostname) MUST NOT appear in any payload
        passed to a peer-bound message constructor or peer WebSocket send.
        Specifically scrutinized:
          * JobAssignedMessage(...) — the schema doesn't have these
            fields, but we still want to catch attempts to smuggle one in
            via **kwargs or string content.
          * <anything>.ws.send_json(...) / .ws.send(...) / .ws.send_text(...)
            — these are the WS write paths to peers.

  #20b — Public aggregate responses (transparency.py) MUST be pure
        counts. A per-peer identity token MUST NOT appear in their
        `return` payloads (only the return is scanned, so SQL aggregations
        like func.count(Peer.peer_id) above the return are unaffected).

  #21 — Contributor-side: prompt content and generated tokens MUST NOT
        be written to disk, logs, or telemetry. The contributor sidecar
        (`apps/desktop/python/potluck_local/contributor/`) is the
        enforcement zone.

  #22 — Peer/pool-facing relay code MUST NOT write the strangers' prompt
        or response content to disk, logs, or telemetry. These are the
        runtime surfaces that actually receive and forward other people's
        prompts/tokens — they live OUTSIDE the contributor tree:
          * routers/peer.py — peer_chat_completions / peer_memory_search
          * services/pool_routing.py — stream/complete_pool_openai
          * services/gateway_routing.py — stream/complete_peer_openai
        Same content-leak heuristic as #21 (sensitive name inside a
        logger / dump / open-write context, unless metadata-wrapped).

Scope is deliberately tight. The lints have whitelists for files that
have a legitimate reason to violate the heuristic (keys.py writes Ed25519
keys; the privacy-tests file references `member_id` in strings as part of
the very assertion that it must not appear).

Run locally:
    python3 scripts/dev/lint_privacy.py

Run in CI: see `.github/workflows/ci-privacy-lint.yml`.

Exits 0 when clean, non-zero with per-finding output when violations
are detected.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# This file lives in scripts/dev/, so the repository root is three levels up.
# With two, every path below pointed into scripts/ and the lint scanned nothing
# while reporting a pass. A missing target is now a failure (see below).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Lint #20 — member_id forbidden in peer-bound payloads
# ---------------------------------------------------------------------------

# Files where matching `member_id` is intentional (e.g. the privacy test
# that asserts member_id MUST NOT appear — it references the string itself).
PEER_LINT_WHITELIST = {
    "services/coordinator/tests/test_privacy_invariants.py",
    "scripts/dev/lint_privacy.py",
}

# WS-send method names we want to scrutinize.
WS_SEND_METHODS = {"send_json", "send_text", "send_bytes", "send"}

# Per-peer identity tokens that MUST NOT cross to a peer-bound message
# constructor or WS send. `member_id` is the original (memory 42a7e671);
# the rest are the broader peer-identity invariant the docs claim is
# guarded — a peer learning another peer's node_name / mesh_ip / hostname
# / peer_id from a job assignment is the same class of de-anonymization
# leak. These are matched by bare name, attribute access (`x.peer_id`),
# keyword arg (`peer_id=`), and string literal (dict key smuggled via
# **kwargs).
PEER_IDENTITY_TOKENS = {
    "member_id",
    "peer_id",
    "node_name",
    "mesh_ip",
    "hostname",
}


class PeerBoundLeakChecker(ast.NodeVisitor):
    """Walks an AST and flags any call to JobAssignedMessage(...) or
    *.ws.send_*(...) whose argument tree mentions a per-peer identity
    token (member_id / peer_id / node_name / mesh_ip / hostname)."""

    def __init__(self, file: Path):
        self.file = file
        self.errors: list[tuple[int, str, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_job_assigned_call(node):
            self._scan_subtree(node, "JobAssignedMessage(...)")
        elif self._is_ws_send_call(node):
            method = node.func.attr  # type: ignore[attr-defined]
            self._scan_subtree(node, f".ws.{method}(...)")
        self.generic_visit(node)

    def _is_job_assigned_call(self, node: ast.Call) -> bool:
        # JobAssignedMessage(...) — bare name
        if isinstance(node.func, ast.Name) and node.func.id == "JobAssignedMessage":
            return True
        # peer.JobAssignedMessage(...) — qualified
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "JobAssignedMessage"
        ):
            return True
        return False

    def _is_ws_send_call(self, node: ast.Call) -> bool:
        # Pattern: <expr>.ws.<send_method>(...)
        if not isinstance(node.func, ast.Attribute):
            return False
        if node.func.attr not in WS_SEND_METHODS:
            return False
        # The thing before .send_* must itself end in `.ws`
        inner = node.func.value
        if isinstance(inner, ast.Attribute) and inner.attr == "ws":
            return True
        return False

    def _scan_subtree(self, node: ast.Call, ctx: str) -> None:
        """Walk the subtree and flag any reference to a peer-identity token."""
        for sub in ast.walk(node):
            # Bare name reference: peer_id
            if isinstance(sub, ast.Name) and sub.id in PEER_IDENTITY_TOKENS:
                self.errors.append(
                    (node.lineno, ctx, f"name reference '{sub.id}'")
                )
            # Attribute access: foo.peer_id, req.member_id, etc.
            elif isinstance(sub, ast.Attribute) and sub.attr in PEER_IDENTITY_TOKENS:
                self.errors.append(
                    (node.lineno, ctx, f"attribute access '.{sub.attr}'")
                )
            # Keyword argument: peer_id=...
            elif isinstance(sub, ast.keyword) and sub.arg in PEER_IDENTITY_TOKENS:
                self.errors.append(
                    (node.lineno, ctx, f"keyword argument '{sub.arg}='")
                )
            # String literal that exactly equals a token (would catch dict
            # literal keys passed via **kwargs or constructed payloads).
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                for token in PEER_IDENTITY_TOKENS:
                    if token in sub.value:
                        self.errors.append(
                            (node.lineno, ctx, f"string literal contains '{token}'")
                        )
                        break


def lint_peer_bound() -> list[str]:
    errors: list[str] = []
    coordinator_root = REPO_ROOT / "services" / "coordinator" / "coordinator"
    if not coordinator_root.is_dir():
        return [f"{coordinator_root}: directory to scan is missing, so nothing was checked"]
    for path in sorted(coordinator_root.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in PEER_LINT_WHITELIST:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            errors.append(f"{rel}:{e.lineno}: parse error: {e.msg}")
            continue
        checker = PeerBoundLeakChecker(path)
        checker.visit(tree)
        for line, ctx, what in checker.errors:
            errors.append(f"{rel}:{line}: peer-bound leak in {ctx}: {what}")
    return errors


# ---------------------------------------------------------------------------
# Lint #20b — per-peer identity forbidden in public aggregate responses
# ---------------------------------------------------------------------------

# Public, unauthenticated aggregate surfaces. Their response bodies must be
# pure counts/aggregates — never a per-peer identity field. We scan only the
# `return` expressions (the response payload), NOT the SQL aggregation
# expressions above them, because the latter legitimately reference
# Peer.peer_id inside func.count(...) without exposing it.
AGGREGATE_RESPONSE_FILES = (
    "services/coordinator/coordinator/routers/transparency.py",
)


class AggregateResponseChecker(ast.NodeVisitor):
    """Flag a per-peer identity token appearing inside a `return` payload
    of a public aggregate endpoint. String-literal dict keys count too —
    they are what a JSON consumer sees."""

    def __init__(self) -> None:
        self.errors: list[tuple[int, str]] = []

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is None:
            return
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Name) and sub.id in PEER_IDENTITY_TOKENS:
                self.errors.append((node.lineno, f"name '{sub.id}'"))
            elif isinstance(sub, ast.Attribute) and sub.attr in PEER_IDENTITY_TOKENS:
                self.errors.append((node.lineno, f"attribute '.{sub.attr}'"))
            elif isinstance(sub, ast.keyword) and sub.arg in PEER_IDENTITY_TOKENS:
                self.errors.append((node.lineno, f"keyword '{sub.arg}='"))
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                # Exact dict-key match only — avoids flagging an aggregate
                # key like "online_peer_count" that merely contains "peer".
                if sub.value in PEER_IDENTITY_TOKENS:
                    self.errors.append(
                        (node.lineno, f"string-literal key '{sub.value}'")
                    )
        # Do not recurse into nested functions' returns here; generic_visit
        # would, which is fine — they are still inside the public module.
        self.generic_visit(node)


def lint_aggregate_responses() -> list[str]:
    errors: list[str] = []
    for relpath in AGGREGATE_RESPONSE_FILES:
        path = REPO_ROOT / relpath
        if not path.is_file():
            errors.append(f"{relpath}: listed file is missing, so it was not checked")
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            errors.append(f"{rel}:{e.lineno}: parse error: {e.msg}")
            continue
        checker = AggregateResponseChecker()
        checker.visit(tree)
        for line, what in checker.errors:
            errors.append(
                f"{rel}:{line}: aggregate-response leak: per-peer identity {what} "
                f"in a public response payload"
            )
    return errors


# ---------------------------------------------------------------------------
# Lint #21 — In-memory-only invariant on contributor
# ---------------------------------------------------------------------------

# Files in the contributor module that legitimately persist (keys, config,
# metadata-only logging structure). Anything not in this list cannot write
# anything resembling prompt/response content.
CONTRIBUTOR_LINT_WHITELIST = {
    "apps/desktop/python/potluck_local/contributor/keys.py",
    "apps/desktop/python/potluck_local/contributor/config.py",
    # __init__.py + schemas.py + README.md don't contain logic
}

# Words that, if they appear inside a file-write or log-emit expression,
# suggest the prompt or generated tokens are being persisted.
SENSITIVE_VAR_TOKENS = {
    "prompt",
    "response",
    "completion",
    "content",
    "output_text",
    "generated",
}

# Function calls that produce metadata-only summaries of their input.
# When a sensitive variable appears wrapped in one of these, the value
# isn't actually exposed — only a length / id / hash / type is logged.
# We explicitly DO NOT include `str` or `repr` here — those serialize
# the value content.
SAFE_METADATA_WRAPPERS = {"len", "id", "hash", "type", "bool"}

# Regex patterns that catch the obvious cases.
LOG_CALL_PATTERN = re.compile(
    r"\blogger\.\w+\s*\(([^)]*)\)"
)
OPEN_WRITE_PATTERN = re.compile(
    r"\bopen\s*\([^)]*?,\s*['\"](?:w|a|x|wb|ab|xb|w\+|a\+)['\"]"
)
DUMP_PATTERN = re.compile(
    r"\b(?:json|pickle|yaml|toml)\.dump\s*\("
)


class ContributorWriteChecker(ast.NodeVisitor):
    """AST-based check: find file writes / dump calls in the contributor
    module that pass any variable whose name is in SENSITIVE_VAR_TOKENS."""

    def __init__(self, file: Path, sensitive_tokens: set[str] | None = None):
        self.file = file
        self.errors: list[tuple[int, str]] = []
        # The set of names that count as content. Defaults to the
        # contributor set; lint #22 passes a wider set for relay surfaces.
        self.sensitive_tokens = (
            sensitive_tokens if sensitive_tokens is not None else SENSITIVE_VAR_TOKENS
        )

    def visit_Call(self, node: ast.Call) -> None:
        # 1. open(..., 'w'|'a'|'x') with a sensitive var anywhere in the
        #    enclosing With/Assign context — caught by regex below; here
        #    we focus on direct argument inspection of dump() calls.
        if self._is_dump_call(node):
            for arg in node.args:
                if self._arg_leaks_sensitive(arg):
                    self.errors.append(
                        (node.lineno, "dump-call references sensitive name")
                    )
        # 2. logger.X(...) with a sensitive var as a positional or kw arg
        elif self._is_logger_call(node):
            for arg in node.args:
                if self._arg_leaks_sensitive(arg):
                    self.errors.append(
                        (node.lineno, "logger call references sensitive name")
                    )
            for kw in node.keywords:
                if kw.value and self._arg_leaks_sensitive(kw.value):
                    self.errors.append(
                        (
                            node.lineno,
                            f"logger call references sensitive name via kw '{kw.arg}'",
                        )
                    )
        self.generic_visit(node)

    def _arg_leaks_sensitive(self, arg: ast.AST) -> bool:
        """Return True if this argument exposes a sensitive value.

        Returns False when the sensitive name is *only* mentioned inside
        a safe metadata wrapper like `len(prompt)` or `id(response)` —
        in those cases only a length / id / etc. is exposed, not the
        underlying content.
        """
        # Whole arg is a safe metadata wrapper call — even if its inner
        # expression mentions a sensitive name, only the metadata escapes.
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name):
            if arg.func.id in SAFE_METADATA_WRAPPERS:
                return False

        # Otherwise: walk the subtree and flag any sensitive reference.
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Name) and sub.id in self.sensitive_tokens:
                return True
            if (
                isinstance(sub, ast.Attribute)
                and sub.attr in self.sensitive_tokens
            ):
                return True
        return False

    def _is_logger_call(self, node: ast.Call) -> bool:
        if not isinstance(node.func, ast.Attribute):
            return False
        # logger.info / .warning / .debug / .error / .exception / .log
        return node.func.attr in {
            "info",
            "warning",
            "debug",
            "error",
            "exception",
            "log",
            "critical",
        } and (
            (isinstance(node.func.value, ast.Name) and node.func.value.id == "logger")
            or (
                isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "logger"
            )
        )

    def _is_dump_call(self, node: ast.Call) -> bool:
        if not isinstance(node.func, ast.Attribute):
            return False
        if node.func.attr != "dump":
            return False
        if not isinstance(node.func.value, ast.Name):
            return False
        return node.func.value.id in {"json", "pickle", "yaml", "toml"}



def _scan_content_leaks(
    path: Path, label: str, sensitive_tokens: set[str]
) -> list[str]:
    """Scan one file for prompt/response content reaching a write/log sink.

    Shared by lint #21 (contributor tree) and lint #22 (peer/pool relay
    surfaces). `label` is the prefix used in the error message; the only
    difference between the two lints is the file set + the sensitive-token
    set, so the heuristic itself lives here once.
    """
    errors: list[str] = []
    rel = path.relative_to(REPO_ROOT).as_posix()
    source = path.read_text(encoding="utf-8")

    # AST-level checks (logger calls + dump calls with sensitive names).
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        errors.append(f"{rel}:{e.lineno}: parse error: {e.msg}")
        return errors
    checker = ContributorWriteChecker(path, sensitive_tokens)
    checker.visit(tree)
    for line, what in checker.errors:
        errors.append(f"{rel}:{line}: {label}: {what}")

    # Line-level checks (catches things AST might miss — e.g. open()
    # with a write mode immediately followed by .write() of a sensitive
    # name on the next line).
    split = source.splitlines()
    for i, raw in enumerate(split, start=1):
        line = raw.strip()
        if line.startswith("#"):
            continue
        if OPEN_WRITE_PATTERN.search(raw):
            # Look for sensitive var name on this line or the next 5
            # lines (catches `with open(p, 'w') as f: f.write(prompt)`).
            window = "\n".join(split[i - 1 : i + 5])
            for token in sensitive_tokens:
                if re.search(rf"\b{token}\b", window):
                    errors.append(
                        f"{rel}:{i}: {label}: open() with write-mode "
                        f"near sensitive name '{token}' in surrounding lines"
                    )
                    break
        if DUMP_PATTERN.search(raw):
            # Allow if dump is of a variable NOT in the sensitive set
            # (we already AST-check the args; the regex is a backstop).
            pass
    return errors


def lint_contributor() -> list[str]:
    errors: list[str] = []
    contributor_root = (
        REPO_ROOT / "apps" / "desktop" / "python" / "potluck_local" / "contributor"
    )
    if not contributor_root.is_dir():
        return [f"{contributor_root}: directory to scan is missing, so nothing was checked"]
    for path in sorted(contributor_root.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in CONTRIBUTOR_LINT_WHITELIST:
            continue
        errors.extend(
            _scan_content_leaks(path, "contributor-leak", SENSITIVE_VAR_TOKENS)
        )
    return errors


# ---------------------------------------------------------------------------
# Lint #22 — content-leak guard on peer/pool relay surfaces
# ---------------------------------------------------------------------------

# Runtime files (outside the contributor tree) that receive and forward
# strangers'/peers' prompts and responses. The same "no content to a
# write/log sink" rule applies here.
PEER_RELAY_FILES = (
    "apps/desktop/python/potluck_local/routers/peer.py",
    "apps/desktop/python/potluck_local/services/pool_routing.py",
    "apps/desktop/python/potluck_local/services/gateway_routing.py",
)

# Sensitive names for the relay surfaces: the contributor set plus the
# request-field names these surfaces actually carry (the latest user
# message and the memory-search query).
PEER_RELAY_SENSITIVE_TOKENS = SENSITIVE_VAR_TOKENS | {"messages", "query"}


def lint_peer_relay() -> list[str]:
    errors: list[str] = []
    for relpath in PEER_RELAY_FILES:
        path = REPO_ROOT / relpath
        if not path.is_file():
            errors.append(f"{relpath}: listed file is missing, so it was not checked")
            continue
        errors.extend(
            _scan_content_leaks(path, "relay-leak", PEER_RELAY_SENSITIVE_TOKENS)
        )
    return errors


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    peer_errors = lint_peer_bound()
    aggregate_errors = lint_aggregate_responses()
    contributor_errors = lint_contributor()
    relay_errors = lint_peer_relay()

    total = (
        len(peer_errors)
        + len(aggregate_errors)
        + len(contributor_errors)
        + len(relay_errors)
    )

    if peer_errors:
        print("=" * 72)
        print(f"Lint #20 — peer-bound identity leaks ({len(peer_errors)})")
        print("=" * 72)
        for e in peer_errors:
            print(f"  {e}")
        print()

    if aggregate_errors:
        print("=" * 72)
        print(
            f"Lint #20b — per-peer identity in aggregate responses "
            f"({len(aggregate_errors)})"
        )
        print("=" * 72)
        for e in aggregate_errors:
            print(f"  {e}")
        print()

    if contributor_errors:
        print("=" * 72)
        print(f"Lint #21 — contributor write/log leaks ({len(contributor_errors)})")
        print("=" * 72)
        for e in contributor_errors:
            print(f"  {e}")
        print()

    if relay_errors:
        print("=" * 72)
        print(f"Lint #22 — peer/pool relay content leaks ({len(relay_errors)})")
        print("=" * 72)
        for e in relay_errors:
            print(f"  {e}")
        print()

    if total == 0:
        print(
            "✓ privacy invariants pass: 0 peer-bound leaks, "
            "0 aggregate-response leaks, 0 contributor leaks, 0 relay leaks"
        )
        return 0

    print(f"✗ privacy lint FAIL: {total} violation(s)")
    print(
        "  If the flagged code is intentional + safe, either:\n"
        "    - add the file to the appropriate whitelist in scripts/dev/lint_privacy.py\n"
        "    - refactor to not mention the sensitive name in the flagged context\n"
        "  These invariants are manifesto-level (memory 42a7e671 + 1fc85a49)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
