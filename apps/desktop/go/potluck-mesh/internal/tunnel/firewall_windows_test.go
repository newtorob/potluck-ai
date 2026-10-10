//go:build windows

package tunnel

import (
	"strings"
	"testing"
)

// The interface rule has to be made again on every start: it is bound to the
// adapter that existed when it was made, and Wintun makes a new one each time.
func TestAllowMeshInboundScriptReplacesTheInterfaceRule(t *testing.T) {
	ps := allowMeshInboundScript("Potluck Mesh inbound (Wintun)", "Wintun", "Potluck Mesh WG transport (UDP 51820)", 51820)

	remove := strings.Index(ps, "Remove-NetFirewallRule -DisplayName 'Potluck Mesh inbound (Wintun)'")
	add := strings.Index(ps, "New-NetFirewallRule -DisplayName 'Potluck Mesh inbound (Wintun)' -Direction Inbound -InterfaceAlias 'Wintun'")
	if remove < 0 || add < 0 || remove > add {
		t.Fatalf("the interface rule must be removed and then added; got: %s", ps)
	}
	// Nothing may make the add conditional on the rule being absent.
	if strings.Contains(ps[:add], "if (-not (Get-NetFirewallRule -DisplayName 'Potluck Mesh inbound (Wintun)'") {
		t.Fatalf("the interface rule is still only added when missing: %s", ps)
	}
	// The port rule has no adapter binding and stays add-if-missing.
	if !strings.Contains(ps, "if (-not (Get-NetFirewallRule -DisplayName 'Potluck Mesh WG transport (UDP 51820)'") {
		t.Fatalf("the port rule should be added only when missing: %s", ps)
	}
	if !strings.Contains(ps, "-LocalPort 51820") {
		t.Fatalf("the port rule lost its port: %s", ps)
	}
}
