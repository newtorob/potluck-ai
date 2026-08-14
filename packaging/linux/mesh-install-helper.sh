#!/usr/bin/env bash
#
# mesh-install-helper.sh — the ONE fixed script the Potluck desktop app is
# allowed to run as root via polkit/pkexec on Linux.
#
# SECURITY MODEL — READ THIS BEFORE CHANGING ANYTHING
# ------------------------------------------------------
# This script is the entire privilege boundary for "app-driven mesh install"
# on Linux. Electron (running as the logged-in user, unprivileged) invokes:
#
#   pkexec /usr/lib/potluck/mesh-install-helper.sh install \
#       --coordinator-url <url> --derp-url <url> \
#       --uid <desktop-user-uid> --gid <desktop-user-gid>
#
# via main.js's mesh:enable handler (apps/desktop/electron/main.js). Two
# independent things keep this from becoming an arbitrary-root-exec hole:
#
#   1. pkexec itself refuses to run anything that isn't (a) an absolute
#      path, (b) owned by root, (c) not writable by group/other. A
#      compromised Electron process cannot substitute a different script —
#      it can only ask pkexec to run THIS file, at THIS path, as shipped by
#      the .deb/.rpm postinst (root:root, 0755, under /usr/lib/potluck/).
#   2. Every argument this script accepts is validated below (subcommand
#      whitelist, URL shape, numeric uid/gid, mesh-binary path pinned to an
#      allowlist of trusted install prefixes) BEFORE it touches disk or
#      systemd. Nothing here is eval'd, sourced, or passed through a shell
#      that could reinterpret metacharacters — see render_unit() below,
#      which writes the unit file with a fixed template and substituted
#      values, never by executing caller-supplied text.
#
# This is a SEPARATE, simpler cousin of scripts/systemd/install-persistence.sh
# (the headless dogfood/server installer): that script creates a dedicated
# non-root 'potluck' user + CAP_NET_ADMIN/CAP_NET_RAW ambient capabilities,
# which is the right minimum-privilege shape for an unattended server with
# no GUI ever needing to reach the IPC socket. The app-driven path here
# needs a DIFFERENT shape: a desktop user must be able to reach the mesh's
# local IPC socket the moment the daemon starts, with NO extra "log out and
# back in" step (a shared-group model would require exactly that, because
# Linux resolves a process's supplementary groups at login/session start,
# not per file-open — see the long comment in this repo's Linux mesh plan
# for why that was rejected). So this daemon runs as ROOT, same as the
# macOS LaunchDaemon (ai.trypotluck.mesh.plist) — and reuses the EXACT SAME
# already-tested reconciliation code that daemon relies on:
# internal/localapi/listener_unix.go's platformListen() chowns the IPC
# socket to the active console/login-session user whenever it's running as
# root and no SUDO_UID is set (see consoleUsernameLinux() in that file,
# which shells out to loginctl/who/logname). Nothing in THIS script needs
# to know or care who the console user is — the Go binary works it out at
# socket-bind time, every time it (re)starts, so it self-heals across
# reboots/relogins without re-running this installer.
#
# FIXED SOCKET PATH
# -------------------
# We deliberately do NOT use a per-user XDG_RUNTIME_DIR-derived path (that
# breaks the moment this system-level unit starts before first login, or
# survives a relogin that tears down /run/user/$UID). Instead we pin
# POTLUCK_LOCAL_API_SOCKET=/run/potluck-mesh.sock — a flat path under /run,
# which always exists once the OS has booted, independent of any login
# session. This matches the path already declared (but not yet wired to an
# actual reachability story) in packaging/daemons/potluck-mesh.service's
# Environment=POTLUCK_LOCAL_API_SOCKET=/var/run/potluck-mesh.sock (/var/run
# is a symlink to /run on every systemd distro) — same canonical location,
# so a future .deb/.rpm unattended install and this app-driven install agree
# on where the socket lives, and main.js's meshSocketPath() (Linux branch)
# only has to check ONE fixed path.
#
# WHAT THIS SCRIPT DOES NOT DO
# -------------------------------
# - Does not create a 'potluck' system user (root needs none).
# - Does not accept an arbitrary --mesh-bin path from the caller — see
#   ALLOWED_MESH_BIN_PREFIXES below. The renderer/Electron process cannot
#   point ExecStart at an attacker-controlled binary.
# - Does not run any caller-supplied string through a shell. Every
#   substitution below goes through printf '%s' into a heredoc or sed with
#   a fixed delimiter, never `eval`/`bash -c "$USER_INPUT"`.
#
# USAGE
# ------
#   mesh-install-helper.sh install --coordinator-url URL --derp-url URL \
#       --uid UID --gid GID [--node-name NAME] [--mesh-bin PATH]
#   mesh-install-helper.sh uninstall
#   mesh-install-helper.sh status
#
# STDOUT CONTRACT (kept in sync with main.js's runLinuxHelper() parsing —
# see the big comment block in main.js's Linux mesh-daemon section):
#   install    -> prints exactly "installed" on success, exit 0.
#                 On failure: message to stderr, exit 1.
#   uninstall  -> prints exactly "uninstalled" on success, exit 0.
#   status     -> prints one of: active | inactive | not-installed
#                 (this subcommand does NOT require root and is provided
#                 for operator/CLI convenience; main.js's daemon-status
#                 handler queries systemctl directly instead of shelling
#                 through pkexec, since reading unit state needs no
#                 privilege — see computeLinuxDaemonStatus() in main.js).

set -euo pipefail

SERVICE_NAME="potluck-mesh.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"
SOCKET_PATH="/run/potluck-mesh.sock"
KEY_DIR="/var/lib/potluck/mesh"
KEY_PATH="${KEY_DIR}/wg.key"
SIGNING_KEY_PATH="${KEY_DIR}/signing.key"
LOG_TAG="potluck-mesh-install-helper"

# Trusted locations the mesh binary is allowed to live at. Anything outside
# these prefixes is refused (see validate_mesh_bin below). The dev-checkout
# prefix exists ONLY for local development/testing of this script before a
# real .deb/.rpm packaging pipeline exists; a shipped build of this script
# should have that entry removed (tracked as a packaging TODO — see the
# report this script shipped with).
ALLOWED_MESH_BIN_PREFIXES=(
  "/opt/Potluck AI/resources/mesh/"
  "/usr/lib/potluck/mesh/"
  # --- dev-only, remove before shipping a packaged build ---
  "/usr/local/lib/potluck/"
)

log() { logger -t "$LOG_TAG" -- "$*" 2>/dev/null || true; printf '%s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

require_root() {
  [ "$(id -u)" -eq 0 ] || die "must run as root (invoke via pkexec, not directly)"
}

require_systemd() {
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found; this helper is systemd-only"
}

# ----------------------------------------------------------------------------
# Argument validation — every value that ends up in the rendered unit file
# or on disk is checked here BEFORE do_install runs. Fail closed.
# ----------------------------------------------------------------------------

# Accepts http(s):// or tcp(+tls|+insecure):// URLs with a plain
# host[:port][/path] — no shell metacharacters, no whitespace, no newlines.
# This is deliberately conservative (rejects some technically-valid URLs)
# rather than trying to be a complete URL grammar.
validate_url() {
  local val="$1" label="$2"
  [[ "$val" =~ ^(https?|tcp(\+tls|\+insecure)?)://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%/-]*)?$ ]] \
    || die "invalid $label: $val"
}

validate_uid_gid() {
  local val="$1" label="$2"
  [[ "$val" =~ ^[0-9]+$ ]] || die "invalid $label: $val (must be numeric)"
  [ "$val" -gt 0 ] || die "invalid $label: $val (uid/gid 0 is not a desktop user)"
  [ "$val" -lt 65534 ] || die "invalid $label: $val (out of normal user range)"
}

# Node name ends up as a --node-name CLI argument and (via the mesh binary)
# in roster data other households can see — keep it to a conservative
# charset so it can never be mistaken for a flag or break the unit file.
validate_node_name() {
  local val="$1"
  [[ "$val" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die "invalid node name: $val"
}

# Refuses anything outside ALLOWED_MESH_BIN_PREFIXES, anything not owned by
# root, and anything group/other-writable (mirrors pkexec's own rule for
# THIS script, applied to the binary the rendered unit will exec).
validate_mesh_bin() {
  local val="$1"
  [ -f "$val" ] || die "mesh binary not found: $val"
  local resolved
  resolved="$(readlink -f -- "$val")" || die "could not resolve mesh binary path: $val"
  local ok=0
  for prefix in "${ALLOWED_MESH_BIN_PREFIXES[@]}"; do
    case "$resolved" in
      "$prefix"*) ok=1; break ;;
    esac
  done
  [ "$ok" -eq 1 ] || die "mesh binary path not in an allowed install location: $resolved"
  local owner writable
  owner="$(stat -c '%u' -- "$resolved")"
  [ "$owner" -eq 0 ] || die "mesh binary must be owned by root: $resolved (owner uid=$owner)"
  local perm
  perm="$(stat -c '%a' -- "$resolved")"
  # Reject if group or other write bits are set (2nd/3rd octal digit odd/>=2
  # in the write position — simplest robust check: forbid perm strings
  # ending in a digit with the write bit, i.e. anything other than the
  # owner-write-only shapes we expect from an installed package).
  case "$perm" in
    *[2367]|*[2367][0-7]) die "mesh binary must not be group/other-writable: $resolved (mode $perm)" ;;
  esac
  printf '%s\n' "$resolved"
}

default_mesh_bin() {
  local candidate
  for prefix in "${ALLOWED_MESH_BIN_PREFIXES[@]}"; do
    candidate="${prefix%/}/potluck-mesh"
    [ -f "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
  done
  return 1
}

# ----------------------------------------------------------------------------
# Legacy-unit handoff — if the headless dogfood installer
# (scripts/systemd/install-persistence.sh) already has a potluck-mesh.service
# running as the non-root 'potluck' user, stop+disable it before installing
# the app-driven root unit, and reuse its WireGuard key so the machine keeps
# the same household identity (no re-enrollment, no "new node" surprise for
# peers). Safe to call when no legacy unit exists (no-op).
# ----------------------------------------------------------------------------
handoff_legacy_unit_if_present() {
  [ -f "$UNIT_PATH" ] || return 0
  local existing_user
  existing_user="$(grep -E '^User=' "$UNIT_PATH" 2>/dev/null | head -1 | cut -d= -f2 | tr -d '[:space:]')"
  if [ "$existing_user" = "potluck" ]; then
    log "found existing headless potluck-mesh.service (User=potluck) — stopping it before app-driven install; WG key at $KEY_PATH will be reused as-is (root can read it regardless of its potluck:potluck ownership)"
    systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
  elif [ -z "$existing_user" ]; then
    log "found existing potluck-mesh.service already running as root — will be overwritten with the current rendered unit"
  else
    log "found existing potluck-mesh.service running as unexpected user '$existing_user' — overwriting"
  fi
}

# ----------------------------------------------------------------------------
# Install
# ----------------------------------------------------------------------------

do_install() {
  local coord_url="" derp_url="" node_name="" mesh_bin="" target_uid="" target_gid=""

  while [ $# -gt 0 ]; do
    case "$1" in
      --coordinator-url) shift; coord_url="${1:-}" ;;
      --derp-url)        shift; derp_url="${1:-}" ;;
      --node-name)       shift; node_name="${1:-}" ;;
      --mesh-bin)        shift; mesh_bin="${1:-}" ;;
      --uid)             shift; target_uid="${1:-}" ;;
      --gid)             shift; target_gid="${1:-}" ;;
      *) die "unknown install argument: $1" ;;
    esac
    shift
  done

  [ -n "$coord_url" ] || die "--coordinator-url is required"
  [ -n "$derp_url" ] || die "--derp-url is required"
  [ -n "$target_uid" ] || die "--uid is required"
  [ -n "$target_gid" ] || die "--gid is required"

  validate_url "$coord_url" "coordinator URL"
  validate_url "$derp_url" "DERP URL"
  validate_uid_gid "$target_uid" "uid"
  validate_uid_gid "$target_gid" "gid"
  [ -z "$node_name" ] || validate_node_name "$node_name"

  if [ -z "$mesh_bin" ]; then
    mesh_bin="$(default_mesh_bin)" || die "no --mesh-bin given and no default mesh binary found in allowed locations"
  fi
  mesh_bin="$(validate_mesh_bin "$mesh_bin")"

  handoff_legacy_unit_if_present

  install -d -m 0700 -o root -g root "$KEY_DIR"

  local extra_args=""
  if [ -n "$node_name" ]; then
    extra_args="--node-name ${node_name}"
  fi

  # Rendered with a fixed heredoc + substituted, pre-validated values only —
  # no caller-controlled string is ever interpreted as shell syntax here.
  cat > "$UNIT_PATH" <<UNIT
[Unit]
Description=Potluck WireGuard mesh sidecar (app-driven install)
Documentation=https://trypotluck.ai/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# Root — creates the potluck0 TUN device the same way the macOS
# LaunchDaemon (ai.trypotluck.mesh.plist) runs as root for utun. The IPC
# socket below is then chowned to the active desktop user by
# internal/localapi/listener_unix.go's platformListen() at bind time (no
# ambient-capability / dedicated-user / group-membership dance needed, and
# critically no "log out and back in" step for the desktop user).
Environment=POTLUCK_WG_PRIVATE_KEY_PATH=${KEY_PATH}
Environment=POTLUCK_SIGNING_KEY_PATH=${SIGNING_KEY_PATH}
Environment=POTLUCK_LOCAL_API_SOCKET=${SOCKET_PATH}
# SUDO_UID/SUDO_GID are the invoking desktop user's own uid/gid, passed by
# main.js's mesh:enable handler (its own process.getuid()/getgid() — the
# most authoritative source available, since that IS the user who clicked
# "Enable mesh networking"). internal/localapi/listener_unix.go's
# decideSocketOwner() checks these FIRST, before falling back to the
# loginctl/who/logname console-user heuristic, so the socket is chowned to
# exactly the right user without depending on session-manager quirks.
Environment=SUDO_UID=${target_uid}
Environment=SUDO_GID=${target_gid}
ExecStart=${mesh_bin} --coordinator-url ${coord_url} --derp-url ${derp_url} --log-format json ${extra_args}

Restart=on-failure
RestartSec=5
StartLimitIntervalSec=300
StartLimitBurst=5

StandardOutput=journal
StandardError=journal
SyslogIdentifier=potluck-mesh

[Install]
WantedBy=multi-user.target
UNIT
  chmod 0644 "$UNIT_PATH"

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null

  if ! systemctl restart "$SERVICE_NAME"; then
    journalctl -u "$SERVICE_NAME" -n 20 --no-pager >&2 || true
    die "$SERVICE_NAME failed to start"
  fi

  # Confirm it actually came up rather than optimistically declaring
  # success — same discipline as computeMacDaemonStatus() on the app side.
  sleep 1
  systemctl is-active --quiet "$SERVICE_NAME" || die "$SERVICE_NAME is not active after start"

  printf 'installed\n'
}

# ----------------------------------------------------------------------------
# Uninstall
# ----------------------------------------------------------------------------

do_uninstall() {
  systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
  rm -f "$UNIT_PATH"
  systemctl daemon-reload
  printf 'uninstalled\n'
}

# ----------------------------------------------------------------------------
# Status (no root required; provided for parity/manual use)
# ----------------------------------------------------------------------------

do_status() {
  if [ ! -f "$UNIT_PATH" ]; then
    printf 'not-installed\n'
    return 0
  fi
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    printf 'active\n'
  else
    printf 'inactive\n'
  fi
}

# ----------------------------------------------------------------------------
# Entry
# ----------------------------------------------------------------------------

require_systemd

case "${1:-}" in
  install)
    shift
    require_root
    do_install "$@"
    ;;
  uninstall)
    require_root
    do_uninstall
    ;;
  status)
    do_status
    ;;
  *)
    die "usage: $0 <install|uninstall|status> [args]"
    ;;
esac
