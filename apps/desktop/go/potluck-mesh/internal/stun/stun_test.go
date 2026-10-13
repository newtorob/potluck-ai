package stun

import (
	"context"
	"net"
	"testing"
)

// ---------------------------------------------------------------------------
// portOfEndpoint - the comparison primitive ClassifyNAT is built on.
// ---------------------------------------------------------------------------

func TestPortOfEndpoint_IPv4(t *testing.T) {
	port, err := portOfEndpoint("203.0.113.7:51820")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if port != 51820 {
		t.Errorf("port = %d, want 51820", port)
	}
}

func TestPortOfEndpoint_IPv6Bracketed(t *testing.T) {
	port, err := portOfEndpoint("[2001:db8::1]:4500")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if port != 4500 {
		t.Errorf("port = %d, want 4500", port)
	}
}

func TestPortOfEndpoint_Malformed(t *testing.T) {
	cases := []string{"", "not-an-endpoint", "203.0.113.7", "203.0.113.7:abc"}
	for _, ep := range cases {
		if _, err := portOfEndpoint(ep); err == nil {
			t.Errorf("portOfEndpoint(%q): expected error, got none", ep)
		}
	}
}

// ---------------------------------------------------------------------------
// formatEndpoint / portOfEndpoint round-trip
// ---------------------------------------------------------------------------

func TestFormatEndpoint_PortOfEndpoint_RoundTrip(t *testing.T) {
	cases := []struct {
		ip   string
		port int
	}{
		{"203.0.113.7", 51820},
		{"::1", 4500},
		{"2001:db8::1", 12345},
	}
	for _, tc := range cases {
		ip := net.ParseIP(tc.ip)
		if ip == nil {
			t.Fatalf("bad IP literal %q", tc.ip)
		}
		ep := formatEndpoint(ip, tc.port)
		got, err := portOfEndpoint(ep)
		if err != nil {
			t.Fatalf("portOfEndpoint(%q): %v", ep, err)
		}
		if got != tc.port {
			t.Errorf("round-trip port = %d, want %d (endpoint %q)", got, tc.port, ep)
		}
	}
}

// ---------------------------------------------------------------------------
// NATType - the classification result's presentation
// ---------------------------------------------------------------------------

func TestNATType_String(t *testing.T) {
	cases := map[NATType]string{
		NATUnknown:             "unknown",
		NATEndpointIndependent: "endpoint-independent",
		NATSymmetric:           "symmetric",
	}
	for nt, want := range cases {
		if got := nt.String(); got != want {
			t.Errorf("NATType(%d).String() = %q, want %q", nt, got, want)
		}
	}
}

// ---------------------------------------------------------------------------
// ClassifyNAT / NewResolver - the input-validation edge (no network needed)
// ---------------------------------------------------------------------------

func TestNewResolver_DefaultsWhenEmpty(t *testing.T) {
	r := NewResolver(nil, 0)
	if len(r.servers) != len(DefaultServers) {
		t.Fatalf("expected DefaultServers to be used, got %v", r.servers)
	}
}

func TestNewResolver_LocalPortWired(t *testing.T) {
	r := NewResolver([]string{"a:1", "b:2"}, 51820)
	if r.localPort != 51820 {
		t.Errorf("localPort = %d, want 51820", r.localPort)
	}
}

func TestClassifyNAT_RequiresAtLeastTwoServers(t *testing.T) {
	r := NewResolver([]string{"only-one:3478"}, 0)
	if _, err := r.ClassifyNAT(context.Background()); err == nil {
		t.Errorf("expected an error with only one configured STUN server")
	}
}
