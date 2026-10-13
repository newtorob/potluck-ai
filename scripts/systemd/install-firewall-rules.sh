#!/usr/bin/env bash
#
# install-firewall-rules.sh
#
# Idempotently insert the nftables accept rules that let Potluck mesh
# traffic survive in the presence of Tailscale and/or Mullvad VPN.
#
# Why this exists
# ----------------
# Both Tailscale and Mullvad install nftables rule chains that, by default,
# drop traffic on interfaces they don't recognize:
#
#   - Tailscale: chain ip filter ts-input has
#       `iifname != "tailscale0" ip saddr 100.64.0.0/10 drop`
#     This drops every Potluck mesh packet (we use 100.64.0.0/10 too).
#
#   - Mullvad: table inet mullvad has `policy drop;` on input/output/forward,
#     with explicit accepts only for the Mullvad WG interface itself. Our
#     potluck0 interface falls into the default drop.
#
# The fix is a narrowly-scoped accept rule for the potluck0 interface,
# inserted at the top of each chain. We do not modify the broader VPN
# rule sets - they keep working unchanged.
#
# This script is called by potluck-firewall.service at boot, so the rules
# get reinstalled every time. They live in the runtime-only ruleset (not
# /etc/nftables.conf) and would otherwise vanish on reboot.
#
# MODES
# ------
#   $0 --install     # default: detect VPN(s), insert accept rules
#   $0 --uninstall   # remove the rules we added
#   $0 --status      # show whether our rules are present
#   $0 --watch       # install, then self-heal: reapply whenever the VPN
#                    # flushes our rules (e.g. Mullvad reconnect)
#
# Idempotency:
#   --install is safe to re-run. We check for an existing rule with the
#   same predicate before inserting. The systemd unit calls it on every
#   boot; re-runs are no-ops.
#
# SELF-HEALING (--watch)
# ----------------------
# Mullvad/Proton/Nord recreate their entire nftables ruleset on every
# (re)connect, silently wiping our accept rules. A one-shot install at boot
# therefore breaks the moment the VPN cycles - the symptom is a mesh peer
# that was reachable suddenly going dark ("No reachable peers"). --watch
# treats rule disappearance as the primary firewall-block signal: it polls
# the ruleset and reapplies our accepts within a few seconds of them
# vanishing. Idle cost is one `nft list chain` per tick; reapply is the same
# idempotent insert as --install.

set -euo pipefail

POTLUCK_IFACE="${POTLUCK_IFACE:-potluck0}"

log()  { printf '[potluck-firewall] %s\n' "$*"; }
fail() { printf '[potluck-firewall] FAIL: %s\n' "$*" >&2; exit 1; }

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        fail "must run as root (nftables operations require it)"
    fi
}

require_nft() {
    command -v nft >/dev/null || fail "nft not installed; install nftables package"
}

# ----------------------------------------------------------------------------
# Detectors
# ----------------------------------------------------------------------------

tailscale_present() {
    # ts-input chain exists in ip filter table = Tailscale's ruleset is loaded.
    nft list chain ip filter ts-input >/dev/null 2>&1
}

mullvad_present() {
    # inet mullvad table exists = Mullvad's kill-switch ruleset is loaded.
    nft list table inet mullvad >/dev/null 2>&1
}

# ----------------------------------------------------------------------------
# Rule-presence checks (so we don't double-insert)
# ----------------------------------------------------------------------------

# These use bash's native [[ "$var" == *substring* ]] rather than piping nft
# to grep. The grep approach was hitting a `set -euo pipefail` interaction
# that made the predicate return non-zero even when the substring was clearly
# present in nft's output (verified by hand). Variable-capture sidesteps it.

tailscale_rule_present() {
    local out
    out=$(nft list chain ip filter ts-input 2>/dev/null) || return 1
    [[ "$out" == *"iifname \"$POTLUCK_IFACE\" accept"* ]]
}

mullvad_input_rule_present() {
    local out
    out=$(nft list chain inet mullvad input 2>/dev/null) || return 1
    [[ "$out" == *"iifname \"$POTLUCK_IFACE\" accept"* ]]
}

mullvad_output_rule_present() {
    local out
    out=$(nft list chain inet mullvad output 2>/dev/null) || return 1
    [[ "$out" == *"oifname \"$POTLUCK_IFACE\" accept"* ]]
}

# ----------------------------------------------------------------------------
# Install / uninstall
# ----------------------------------------------------------------------------

# Counters set by do_install on each run, read back by the caller. We track
# failures EXPLICITLY rather than leaning on `set -e`: do_install is invoked as
# `do_install || log ...` inside the watch loop, and bash suppresses errexit for
# the whole body of a function called in a `||`/`&&`/`if` condition. Without
# explicit tracking, an early `nft insert` failure would neither abort nor be
# caught - and a later success would make the function return 0 and log "done"
# while a rule was actually missing. These let us attempt every rule, log each
# failure by name, and return non-zero iff something genuinely failed.
INSTALL_INSERTED=0
INSTALL_FAILED=0

# Attempt one nft insert. Logs the action, then success-implicit / failure
# explicitly. Never aborts the caller regardless of set -e context. Bumps the
# module-level counters so do_install can report an accurate aggregate.
try_insert() {
    local label="$1"; shift
    log "$label: inserting"
    if "$@"; then
        INSTALL_INSERTED=$((INSTALL_INSERTED + 1))
        return 0
    fi
    log "$label: FAILED to insert (rule still missing; will retry next tick)"
    INSTALL_FAILED=$((INSTALL_FAILED + 1))
    return 1
}

# Returns 0 if all owed rules are in place after the run, non-zero if any insert
# failed. Safe to call from a `||` condition (see try_insert note above).
do_install() {
    require_root
    require_nft

    INSTALL_INSERTED=0
    INSTALL_FAILED=0

    if tailscale_present; then
        if tailscale_rule_present; then
            log "tailscale: ts-input accept for $POTLUCK_IFACE already present"
        else
            try_insert "tailscale ts-input accept for $POTLUCK_IFACE" \
                nft insert rule ip filter ts-input iifname "$POTLUCK_IFACE" accept || true
        fi
    else
        log "tailscale: not detected (no ts-input chain) - skipping"
    fi

    if mullvad_present; then
        if mullvad_input_rule_present; then
            log "mullvad: input accept for $POTLUCK_IFACE already present"
        else
            # iifname (not iif) - string match, doesn't require the interface
            # to exist at insert time. At boot, potluck0 isn't up yet.
            try_insert "mullvad input accept for $POTLUCK_IFACE" \
                nft insert rule inet mullvad input iifname "$POTLUCK_IFACE" accept || true
        fi

        if mullvad_output_rule_present; then
            log "mullvad: output accept for $POTLUCK_IFACE already present"
        else
            try_insert "mullvad output accept for $POTLUCK_IFACE" \
                nft insert rule inet mullvad output oifname "$POTLUCK_IFACE" accept || true
        fi
    else
        log "mullvad: not detected (no inet mullvad table) - skipping"
    fi

    if [ "$INSTALL_FAILED" -ne 0 ]; then
        log "completed with errors: $INSTALL_FAILED rule(s) failed to insert"
        return 1
    elif [ "$INSTALL_INSERTED" -eq 0 ]; then
        log "no changes needed"
    else
        log "done"
    fi
    return 0
}

# Remove a rule by handle. We re-list the chain, find the handle for the
# FIRST rule matching our predicate, then delete it. Caller can call us in
# a loop to remove duplicates. Same variable-capture approach as the
# rule-presence checks for the same robustness reason.
delete_rule_by_predicate() {
    local family="$1"      # ip / inet
    local table="$2"       # filter / mullvad
    local chain="$3"       # ts-input / input / output
    local predicate="$4"   # substring to match

    local out
    out=$(nft -a list chain "$family" "$table" "$chain" 2>/dev/null) || return 1

    local line handle=""
    while IFS= read -r line; do
        if [[ "$line" == *"$predicate"* ]]; then
            if [[ "$line" =~ handle[[:space:]]+([0-9]+) ]]; then
                handle="${BASH_REMATCH[1]}"
                break
            fi
        fi
    done <<< "$out"

    if [ -z "$handle" ]; then
        return 1
    fi

    nft delete rule "$family" "$table" "$chain" handle "$handle"
    return 0
}

do_uninstall() {
    require_root
    require_nft

    # Loop to clean up duplicates - a repeated install may have
    # inserted the same rule more than once.
    if tailscale_present; then
        while tailscale_rule_present; do
            log "tailscale: removing ts-input accept for $POTLUCK_IFACE"
            delete_rule_by_predicate ip filter ts-input \
                "iifname \"$POTLUCK_IFACE\" accept" \
                || { log "  (could not delete; bailing to avoid infinite loop)"; break; }
        done
    fi

    if mullvad_present; then
        while mullvad_input_rule_present; do
            log "mullvad: removing input accept for $POTLUCK_IFACE"
            delete_rule_by_predicate inet mullvad input \
                "iifname \"$POTLUCK_IFACE\" accept" \
                || { log "  (could not delete; bailing to avoid infinite loop)"; break; }
        done
        while mullvad_output_rule_present; do
            log "mullvad: removing output accept for $POTLUCK_IFACE"
            delete_rule_by_predicate inet mullvad output \
                "oifname \"$POTLUCK_IFACE\" accept" \
                || { log "  (could not delete; bailing to avoid infinite loop)"; break; }
        done
    fi

    log "uninstall done"
}

do_status() {
    require_nft
    printf 'potluck-firewall-rules status (iface: %s)\n' "$POTLUCK_IFACE"
    printf '  tailscale detected      : %s\n' "$(tailscale_present && echo yes || echo no)"
    if tailscale_present; then
        printf '  ts-input accept rule    : %s\n' \
            "$(tailscale_rule_present && echo present || echo MISSING)"
    fi
    printf '  mullvad detected        : %s\n' "$(mullvad_present && echo yes || echo no)"
    if mullvad_present; then
        printf '  mullvad input accept    : %s\n' \
            "$(mullvad_input_rule_present && echo present || echo MISSING)"
        printf '  mullvad output accept   : %s\n' \
            "$(mullvad_output_rule_present && echo present || echo MISSING)"
    fi
}

# ----------------------------------------------------------------------------
# Self-healing watch
# ----------------------------------------------------------------------------

# Poll interval (seconds) between integrity checks. Override with
# POTLUCK_FW_WATCH_INTERVAL. Reapply latency after a VPN reconnect is bounded
# by this; 5s keeps a peer-dark window short without meaningful idle cost.
WATCH_INTERVAL="${POTLUCK_FW_WATCH_INTERVAL:-5}"

# Return 0 if every accept rule we owe (for each VPN currently present) is in
# place, 1 if any is missing. A VPN that isn't loaded owes nothing - so this
# stays quiet when, e.g., Mullvad is disconnected, and fires the instant it
# reconnects and recreates its table without our accept.
rules_intact() {
    if tailscale_present && ! tailscale_rule_present; then
        return 1
    fi
    if mullvad_present; then
        mullvad_input_rule_present  || return 1
        mullvad_output_rule_present || return 1
    fi
    return 0
}

do_watch() {
    require_root
    require_nft

    log "watch: installing initial rules"
    # Don't let a transient nft error abort the whole watcher.
    do_install || log "watch: initial install hit an error; will retry on next tick"

    log "watch: self-heal loop active (interval ${WATCH_INTERVAL}s)"
    while true; do
        sleep "$WATCH_INTERVAL"
        if ! rules_intact; then
            log "watch: accept rule(s) missing (VPN likely reconnected) - reapplying"
            do_install || log "watch: reapply hit an error; will retry on next tick"
        fi
    done
}

# ----------------------------------------------------------------------------
# Entry
# ----------------------------------------------------------------------------

mode="${1:---install}"
case "$mode" in
    --install)   do_install ;;
    --uninstall) do_uninstall ;;
    --status)    do_status ;;
    --watch)     do_watch ;;
    -h|--help)
        sed -n '2,38p' "$0" | sed 's/^# \?//'
        ;;
    *)
        fail "unknown mode: $mode (try --install / --uninstall / --status / --watch)"
        ;;
esac
