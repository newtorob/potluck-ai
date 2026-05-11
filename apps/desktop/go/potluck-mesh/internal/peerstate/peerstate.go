// Package peerstate maintains an in-memory cache of known household peers,
// keyed by node_id. This is required because roster_update.removed only
// carries node_id strings, not public keys, so we need a local map to look
// up the WireGuard public key before removing the peer from the WG device.
//
// Privacy: the cache holds only mesh-layer metadata (node_id, mesh_ip,
// wg_public_key, last_endpoint). No inference content, no member_id.
// Nothing is written to disk — the cache is rebuilt from the welcome roster
// on every fresh signaling connection.
package peerstate

import (
	"sync"

	"github.com/potluck-ai/potluck-mesh/internal/signal"
)

// Store is a thread-safe in-memory map of node_id → RosterPeer.
type Store struct {
	mu    sync.RWMutex
	peers map[string]signal.RosterPeer // keyed by NodeID
}

// New returns an empty Store.
func New() *Store {
	return &Store{
		peers: make(map[string]signal.RosterPeer),
	}
}

// Reset replaces the entire peer map with the given slice.
// Called on welcome to initialize the cache from the initial roster.
func (s *Store) Reset(peers []signal.RosterPeer) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.peers = make(map[string]signal.RosterPeer, len(peers))
	for _, p := range peers {
		s.peers[p.NodeID] = p
	}
}

// Upsert inserts or replaces a peer entry.
// Called on roster_update.added and roster_update.updated.
func (s *Store) Upsert(p signal.RosterPeer) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.peers[p.NodeID] = p
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
