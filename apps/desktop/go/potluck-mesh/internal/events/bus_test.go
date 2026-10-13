package events_test

import (
	"sync"
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/events"
)

func TestBus_PublishAndReceive(t *testing.T) {
	b := events.NewBus()

	ch, unsub := b.Subscribe()
	defer unsub()

	want := events.NewStatusChanged("running")
	b.Publish(want)

	select {
	case got := <-ch:
		if got.Type != events.TypeStatusChanged {
			t.Fatalf("got type %q want %q", got.Type, events.TypeStatusChanged)
		}
		if got.Status != "running" {
			t.Fatalf("got status %q want %q", got.Status, "running")
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for event")
	}
}

func TestBus_MultipleSubscribers(t *testing.T) {
	b := events.NewBus()

	const n = 5
	channels := make([]<-chan events.Event, n)
	unsubs := make([]func(), n)
	for i := range n {
		channels[i], unsubs[i] = b.Subscribe()
		defer unsubs[i]()
	}

	b.Publish(events.NewPeerAdded(events.PeerSnapshot{NodeID: "abc", NodeName: "test", MeshIP: "100.64.0.1"}))

	for i, ch := range channels {
		select {
		case got := <-ch:
			if got.Type != events.TypePeerAdded {
				t.Errorf("subscriber %d: got type %q", i, got.Type)
			}
		case <-time.After(time.Second):
			t.Errorf("subscriber %d: timeout", i)
		}
	}
}

func TestBus_Unsubscribe(t *testing.T) {
	b := events.NewBus()

	ch, unsub := b.Subscribe()
	unsub() // unsubscribe before any publish

	// The channel should be closed.
	select {
	case _, ok := <-ch:
		if ok {
			t.Fatal("expected closed channel after unsubscribe")
		}
	case <-time.After(time.Second):
		t.Fatal("channel not closed after unsubscribe")
	}

	// Publishing to a bus with no subscribers should not panic.
	b.Publish(events.NewStatusChanged("stopped"))
}

func TestBus_SlowSubscriberDropped(t *testing.T) {
	b := events.NewBus()

	// Subscribe but never drain - the channel will fill.
	_, unsub := b.Subscribe()
	defer unsub()

	// Publish more events than the buffer capacity (256).
	// This must not block or panic.
	done := make(chan struct{})
	go func() {
		for i := 0; i < 300; i++ {
			b.Publish(events.NewStatusChanged("running"))
		}
		close(done)
	}()

	select {
	case <-done:
		// all publishes completed without blocking
	case <-time.After(5 * time.Second):
		t.Fatal("Publish blocked on slow subscriber")
	}
}

func TestBus_Close(t *testing.T) {
	b := events.NewBus()

	ch, _ := b.Subscribe()

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		// Drain until the channel is closed.
		for range ch {
		}
	}()

	b.Publish(events.NewStatusChanged("stopping"))
	b.Close()

	// After Close the goroutine should finish (channel closed).
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("goroutine did not finish after bus Close")
	}
}

func TestBus_ClosedBusSubscribe(t *testing.T) {
	b := events.NewBus()
	b.Close()

	// Subscribing to a closed bus should give a closed channel immediately.
	ch, unsub := b.Subscribe()
	defer unsub()

	select {
	case _, ok := <-ch:
		if ok {
			t.Fatal("expected closed channel from closed bus")
		}
	case <-time.After(time.Second):
		t.Fatal("channel was not already closed")
	}
}

func TestBus_PeerEvents(t *testing.T) {
	b := events.NewBus()
	ch, unsub := b.Subscribe()
	defer unsub()

	peer := events.PeerSnapshot{
		NodeID:   "node-1",
		NodeName: "laptop",
		MeshIP:   "100.64.0.2",
	}

	evts := []events.Event{
		events.NewPeerAdded(peer),
		events.NewPeerUpdated(peer),
		events.NewPeerRemoved("node-1"),
		events.NewEndpointChanged("203.0.113.7:51820"),
		events.NewError("test_code", "test message"),
	}

	for _, e := range evts {
		b.Publish(e)
	}

	for i, want := range evts {
		select {
		case got := <-ch:
			if got.Type != want.Type {
				t.Errorf("event %d: got type %q want %q", i, got.Type, want.Type)
			}
		case <-time.After(time.Second):
			t.Fatalf("event %d: timeout", i)
		}
	}
}

func TestBus_UnsubscribeIdempotent(t *testing.T) {
	b := events.NewBus()
	_, unsub := b.Subscribe()

	// Calling unsub twice should not panic.
	unsub()
	unsub()
}
