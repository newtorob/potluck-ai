#!/usr/bin/env python3
"""Privacy-invariant linter.

Enforces two manifesto-level rules at CI time (memory 42a7e671 + 1fc85a49):

  #20 — Coordinator-side: `member_id` MUST NOT appear in any payload
        passed to a peer-bound message constructor or peer WebSocket send.
        Specifically scrutinized:
          * JobAssignedMessage(...) — the schema doesn't have a member_id
            field, but we still want to catch attempts to smuggle one in
            via **kwargs or string content.
          * <anything>.ws.send_json(...) / .ws.send(...) / .ws.send_text(...)
            — these are the WS write paths to peers.

  #21 — Contributor-side: prompt content and generated tokens MUST NOT
        be written to disk, logs, or telemetry. The contributor sidecar
        (`apps/desktop/python/potluck_local/contributor/`) is the
        enforcement zone.

Scope is deliberately tight. Both lints have whitelists for files that
have a legitimate reason to violate the heuristic (keys.py writes Ed25519
keys; the privacy-tests file references `member_id` in strings as part of
the very assertion that it must not appear).

Run locally:
    python3 scripts/lint_privacy.py

Run in CI: see `.github/workflows/ci-privacy-lint.yml`.

Exits 0 when clean, non-zero with per-finding output when violations
are detected.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Lint #20 — member_id forbidden in peer-bound payloads
# ---------------------------------------------------------------------------

# Files where matching `member_id` is intentional (e.g. the privacy test
# that asserts member_id MUST NOT appear — it references the string itself).
PEER_LINT_WHITELIST = {
    "services/coordinator/tests/test_privacy_invariants.py",
    "scripts/lint_privacy.py",
}

# WS-send method names we want to scrutinize.
WS_SEND_METHODS = {"send_json", "send_text", "send_bytes", "send"}


class PeerBoundLeakChecker(ast.NodeVisitor):
    """Walks an AST and flags any call to JobAssignedMessage(...) or
    *.ws.send_*(...) whose argument tree mentions `member_id` anywhere."""

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
        """Walk every node in the subtree and flag any reference to member_id."""
        for sub in ast.walk(node):
            # Bare name reference: member_id
            if isinstance(sub, ast.Name) and sub.id == "member_id":
                self.errors.append((node.lineno, ctx, "name reference 'member_id'"))
            # Attribute access: foo.member_id, req.member_id, etc.
            elif isinstance(sub, ast.Attribute) and sub.attr == "member_id":
                self.errors.append((node.lineno, ctx, "attribute access '.member_id'"))
            # Keyword argument: member_id=...
            elif isinstance(sub, ast.keyword) and sub.arg == "member_id":
                self.errors.append((node.lineno, ctx, "keyword argument 'member_id='"))
            # String literal that contains 'member_id' (would catch dict
            # literal keys passed via **kwargs or constructed payloads).
            elif (
                isinstance(sub, ast.Constant)
                and isinstance(sub.value, str)
                and "member_id" in sub.value
            ):
                self.errors.append(
                    (node.lineno, ctx, f"string literal contains 'member_id'")
                )


def lint_peer_bound() -> list[str]:
    errors: list[str] = []
    coordinator_root = REPO_ROOT / "services" / "coordinator" / "coordinator"
    if not coordinator_root.is_dir():
        return errors
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

    def __init__(self, file: Path):
        self.file = file
        self.errors: list[tuple[int, str]] = []

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
            if isinstance(sub, ast.Name) and sub.id in SENSITIVE_VAR_TOKENS:
                return True
            if (
                isinstance(sub, ast.Attribute)
                and sub.attr in SENSITIVE_VAR_TOKENS
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



def lint_contributor() -> list[str]:
    errors: list[str] = []
    contributor_root = (
        REPO_ROOT / "apps" / "desktop" / "python" / "potluck_local" / "contributor"
    )
    if not contributor_root.is_dir():
        return errors
    for path in sorted(contributor_root.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in CONTRIBUTOR_LINT_WHITELIST:
            continue
        source = path.read_text(encoding="utf-8")

        # AST-level checks (logger calls + dump calls with sensitive names)
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            errors.append(f"{rel}:{e.lineno}: parse error: {e.msg}")
            continue
        checker = ContributorWriteChecker(path)
        checker.visit(tree)
        for line, what in checker.errors:
            errors.append(f"{rel}:{line}: contributor-leak: {what}")

        # Line-level checks (catches things AST might miss — e.g. open()
        # with a write mode immediately followed by .write() of a sensitive
        # name on the next line).
        for i, raw in enumerate(source.splitlines(), start=1):
            line = raw.strip()
            if line.startswith("#"):
                continue
            if OPEN_WRITE_PATTERN.search(raw):
                # Look for sensitive var name on this line or the next 5
                # lines (catches `with open(p, 'w') as f: f.write(prompt)`).
                window_lines = source.splitlines()[i - 1 : i + 5]
                window = "\n".join(window_lines)
                for token in SENSITIVE_VAR_TOKENS:
                    if re.search(rf"\b{token}\b", window):
                        errors.append(
                            f"{rel}:{i}: contributor-leak: open() with write-mode "
                            f"near sensitive name '{token}' in surrounding lines"
                        )
                        break
            if DUMP_PATTERN.search(raw):
                # Allow if dump is of a variable NOT in SENSITIVE_VAR_TOKENS
                # (we already AST-check the args; the regex is a backstop)
                pass
    return errors


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    peer_errors = lint_peer_bound()
    contributor_errors = lint_contributor()

    total = len(peer_errors) + len(contributor_errors)

    if peer_errors:
        print("=" * 72)
        print(f"Lint #20 — peer-bound member_id leaks ({len(peer_errors)})")
        print("=" * 72)
        for e in peer_errors:
            print(f"  {e}")
        print()

    if contributor_errors:
        print("=" * 72)
        print(f"Lint #21 — contributor write/log leaks ({len(contributor_errors)})")
        print("=" * 72)
        for e in contributor_errors:
            print(f"  {e}")
        print()

    if total == 0:
        print("✓ privacy invariants pass: 0 peer-bound leaks, 0 contributor leaks")
        return 0

    print(f"✗ privacy lint FAIL: {total} violation(s)")
    print(
        "  If the flagged code is intentional + safe, either:\n"
        "    - add the file to the appropriate whitelist in scripts/lint_privacy.py\n"
        "    - refactor to not mention the sensitive name in the flagged context\n"
        "  These invariants are manifesto-level (memory 42a7e671 + 1fc85a49)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
