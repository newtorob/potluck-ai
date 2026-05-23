#!/usr/bin/env bash
#
# install-firewall-rules.sh
#
# Idempotently insert the nftables accept rules that let Potluck mesh
# traffic survive in the presence of Tailscale and/or Mullvad VPN.
#
# WHY THIS EXISTS
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
# inserted at the TOP of each chain. We do NOT modify the broader VPN
# rule sets — they keep working exactly as they did.
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
#
# Idempotency:
#   --install is safe to re-run. We check for an existing rule with the
#   same predicate before inserting. The systemd unit calls it on every
#   boot; re-runs are no-ops.

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

tailscale_rule_present() {
    nft list chain ip filter ts-input 2>/dev/null \
        | grep -q "iifname \"$POTLUCK_IFACE\" accept"
}

mullvad_input_rule_present() {
    nft list chain inet mullvad input 2>/dev/null \
        | grep -q "iif \"$POTLUCK_IFACE\" accept"
}

mullvad_output_rule_present() {
    nft list chain inet mullvad output 2>/dev/null \
        | grep -q "oif \"$POTLUCK_IFACE\" accept"
}

# ----------------------------------------------------------------------------
# Install / uninstall
# ----------------------------------------------------------------------------

do_install() {
    require_root
    require_nft

    local any_inserted=0

    if tailscale_present; then
        if tailscale_rule_present; then
            log "tailscale: ts-input accept for $POTLUCK_IFACE already present"
        else
            log "tailscale: inserting ts-input accept for $POTLUCK_IFACE"
            nft insert rule ip filter ts-input iifname "$POTLUCK_IFACE" accept
            any_inserted=1
        fi
    else
        log "tailscale: not detected (no ts-input chain) — skipping"
    fi

    if mullvad_present; then
        if mullvad_input_rule_present; then
            log "mullvad: input accept for $POTLUCK_IFACE already present"
        else
            log "mullvad: inserting input accept for $POTLUCK_IFACE"
            nft insert rule inet mullvad input iif "$POTLUCK_IFACE" accept
            any_inserted=1
        fi

        if mullvad_output_rule_present; then
            log "mullvad: output accept for $POTLUCK_IFACE already present"
        else
            log "mullvad: inserting output accept for $POTLUCK_IFACE"
            nft insert rule inet mullvad output oif "$POTLUCK_IFACE" accept
            any_inserted=1
        fi
    else
        log "mullvad: not detected (no inet mullvad table) — skipping"
    fi

    if [ "$any_inserted" -eq 0 ]; then
        log "no changes needed"
    else
        log "done"
    fi
}

# Remove a rule by handle. We re-list the chain, find the handle for the rule
# matching our predicate, then delete it. Handles are unique per chain.
delete_rule_by_predicate() {
    local family="$1"      # ip / inet
    local table="$2"       # filter / mullvad
    local chain="$3"       # ts-input / input / output
    local predicate="$4"   # substring to match

    local handle
    handle=$(nft -a list chain "$family" "$table" "$chain" 2>/dev/null \
        | grep "$predicate" \
        | grep -oE 'handle [0-9]+' \
        | awk '{print $2}' \
        | head -1 || true)

    if [ -z "$handle" ]; then
        return 1
    fi

    nft delete rule "$family" "$table" "$chain" handle "$handle"
    return 0
}

do_uninstall() {
    require_root
    require_nft

    if tailscale_present && tailscale_rule_present; then
        log "tailscale: removing ts-input accept for $POTLUCK_IFACE"
        delete_rule_by_predicate ip filter ts-input \
            "iifname \"$POTLUCK_IFACE\" accept" \
            || log "  (rule disappeared between check and delete; ok)"
    fi

    if mullvad_present; then
        if mullvad_input_rule_present; then
            log "mullvad: removing input accept for $POTLUCK_IFACE"
            delete_rule_by_predicate inet mullvad input \
                "iif \"$POTLUCK_IFACE\" accept" \
                || log "  (rule disappeared between check and delete; ok)"
        fi
        if mullvad_output_rule_present; then
            log "mullvad: removing output accept for $POTLUCK_IFACE"
            delete_rule_by_predicate inet mullvad output \
                "oif \"$POTLUCK_IFACE\" accept" \
                || log "  (rule disappeared between check and delete; ok)"
        fi
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
# Entry
# ----------------------------------------------------------------------------

mode="${1:---install}"
case "$mode" in
    --install)   do_install ;;
    --uninstall) do_uninstall ;;
    --status)    do_status ;;
    -h|--help)
        sed -n '2,38p' "$0" | sed 's/^# \?//'
        ;;
    *)
        fail "unknown mode: $mode (try --install / --uninstall / --status)"
        ;;
esac
