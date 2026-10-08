// Package peerstate maintains an in-memory cache of known household peers,
// keyed by node_id. This is required because roster_update.removed only
// carries node_id strings, not public keys, so we need a local map to look
// up the WireGuard public key before removing the peer from the WG device.
//
// Privacy: the cache holds only mesh-layer metadata (node_id, mesh_ip,
// wg_public_key, last_endpoint). No inference content, no member_id.
// Nothing is written to disk — the cache is rebuilt from the welcome roster
// on every fresh signaling connection.
//
// The store is also the gate for roster entries. Reset and Merge (and Upsert,
// which wraps Merge) validate each entry before storing it: mesh_ip must be a
// usable IPv4 host address inside the mesh range, not this node's own, and not
// shared with another node; wg_public_key must be a canonical 32-byte key. An
// entry that fails is dropped with one WARN line and never stored, so the
// tunnel config, the host routes and GET /peers (all built from this store)
// only ever see validated entries.
package peerstate

import (
	"errors"
	"net/netip"
	"sync"

	"github.com/potluck-ai/potluck-mesh/internal/signal"
)

// Store is a thread-safe in-memory map of node_id → RosterPeer.
type Store struct {
	mu    sync.RWMutex
	peers map[string]signal.RosterPeer // keyed by NodeID
	mesh  netip.Prefix                 // every stored mesh_ip falls inside this
	self  netip.Addr                   // this node's own mesh IP; zero until the first Reset
}

// New returns an empty Store.
func New() *Store {
	return &Store{
		peers: make(map[string]signal.RosterPeer),
		mesh:  meshRange(),
	}
}

// Reset replaces the entire peer map with the entries of peers that pass
// validation. Called on welcome to initialize the cache from the initial
// roster. selfMeshIP is this node's own address from the welcome; it is
// remembered so later Merge calls can refuse an entry that claims it.
//
// It returns the stored entries in roster order (what the caller should hand to
// the tunnel) and the entries it dropped. An address claimed by more than one
// node in the roster is dropped for all of them.
func (s *Store) Reset(selfMeshIP string, peers []signal.RosterPeer) (kept []signal.RosterPeer, rejected []Rejection) {
	s.mu.Lock()
	s.self, _ = netip.ParseAddr(selfMeshIP) // zero value when empty or malformed
	reasons, _ := judge(peers, nil, s.mesh, s.self)
	s.peers = make(map[string]signal.RosterPeer, len(peers))
	for i, p := range peers {
		if reasons[i] != "" {
			rejected = append(rejected, Rejection{NodeID: p.NodeID, Reason: reasons[i]})
			continue
		}
		s.peers[p.NodeID] = p
		kept = append(kept, p)
	}
	s.mu.Unlock()
	warnRejected(rejected)
	return kept, rejected
}

// Merge validates a roster_update's added and updated entries as one batch,
// stores the ones that pass, and returns them (in their original order, split
// back into the added and updated lists).
//
// Entries are judged against the peers already stored, so an incoming entry
// that claims a stored peer's address is dropped and the stored peer is
// evicted as well (see judge for why neither keeps the address). A stored peer
// whose new entry is dropped is evicted too: it was valid and no longer is.
//
// evict lists the node_ids that must be torn down. Those peers are still in the
// store when Merge returns, so the caller can fetch each one with Remove (which
// returns the entry) to learn its key and route, then remove it from the tunnel.
func (s *Store) Merge(added, updated []signal.RosterPeer) (keptAdded, keptUpdated []signal.RosterPeer, evict []string, rejected []Rejection) {
	s.mu.Lock()
	incoming := make([]signal.RosterPeer, 0, len(added)+len(updated))
	incoming = append(append(incoming, added...), updated...)
	held := make([]signal.RosterPeer, 0, len(s.peers))
	for _, p := range s.peers {
		held = append(held, p)
	}

	reasons, lostHeld := judge(incoming, held, s.mesh, s.self)
	admitted := make(map[string]bool, len(incoming))
	for i, p := range incoming {
		if reasons[i] == "" {
			admitted[p.NodeID] = true
		}
	}

	evicting := make(map[string]bool)
	markEvict := func(nodeID string) {
		if _, stored := s.peers[nodeID]; stored && !admitted[nodeID] && !evicting[nodeID] {
			evicting[nodeID] = true
			evict = append(evict, nodeID)
		}
	}
	for i, p := range incoming {
		if reasons[i] != "" {
			rejected = append(rejected, Rejection{NodeID: p.NodeID, Reason: reasons[i]})
			markEvict(p.NodeID)
			continue
		}
		s.peers[p.NodeID] = p
		if i < len(added) {
			keptAdded = append(keptAdded, p)
		} else {
			keptUpdated = append(keptUpdated, p)
		}
	}
	for _, nodeID := range lostHeld {
		markEvict(nodeID)
	}
	s.mu.Unlock()
	warnRejected(rejected)
	return keptAdded, keptUpdated, evict, rejected
}

// Upsert inserts or replaces a single peer entry through the same gate as
// Merge. A refused entry comes back as an error, and a stored peer that the
// refusal evicts is removed here, since a single-entry caller has no tunnel
// state of its own to unwind.
func (s *Store) Upsert(p signal.RosterPeer) error {
	_, _, evict, rejected := s.Merge([]signal.RosterPeer{p}, nil)
	for _, nodeID := range evict {
		s.Remove(nodeID)
	}
	if len(rejected) > 0 {
		return errors.New(rejected[0].Reason)
	}
	return nil
}

// Remove deletes the peer with the given node_id and returns the removed
// entry along with a bool indicating whether it was present.
func (s *Store) Remove(nodeID string) (signal.RosterPeer, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.peers[nodeID]
	if ok {
		delete(s.peers, nodeID)
	}
	return p, ok
}

// Get returns the peer for a node_id, if present.
func (s *Store) Get(nodeID string) (signal.RosterPeer, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	p, ok := s.peers[nodeID]
	return p, ok
}

// All returns a snapshot of all peers. Callers receive a copy; mutations
// to the returned slice do not affect the store.
func (s *Store) All() []signal.RosterPeer {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]signal.RosterPeer, 0, len(s.peers))
	for _, p := range s.peers {
		out = append(out, p)
	}
	return out
}
