// Package election implements ZooKeeper leader election for the watcher-service.
//
// Protocol — ephemeral sequential znode recipe (avoids thundering herd):
//
//  1. Create an ephemeral sequential znode at {path}/candidate-.
//  2. List all children of the election path and sort them.
//  3. If this instance owns the lowest-sequence znode → it is the leader.
//  4. Otherwise, watch the znode with the next lower sequence number.
//  5. When that znode disappears, go back to step 2.
//
// Only one replica polls datasources and triggers heals at any time.
// When the leader crashes its ephemeral znode is deleted by ZooKeeper
// automatically (session expiry), and the next candidate takes over.
package election

import (
	"context"
	"fmt"
	"log/slog"
	"path"
	"sort"
	"strings"
	"time"

	"github.com/go-zookeeper/zk"
)

const (
	candidatePrefix    = "candidate-"
	zkSessionTimeout   = 10 * time.Second
	reenrollRetryDelay = 5 * time.Second
)

// LeaderElector participates in a ZooKeeper leader election and invokes a
// callback when this instance holds leadership.
type LeaderElector struct {
	conn         *zk.Conn
	electionPath string
}

// New connects to ZooKeeper and returns a LeaderElector.
// electionPath is the ZooKeeper path used for the election (e.g.
// "/vartrack/watcher/election"). The path is created automatically if it
// does not exist.
func New(hosts []string, electionPath string) (*LeaderElector, error) {
	conn, _, err := zk.Connect(hosts, zkSessionTimeout, zk.WithLogInfo(false))
	if err != nil {
		return nil, fmt.Errorf("election: zk connect %v: %w", hosts, err)
	}
	return &LeaderElector{conn: conn, electionPath: electionPath}, nil
}

// Run participates in leader election until ctx is cancelled.
//
// When this instance wins the election, fn is called with a derived context.
// That context is cancelled when leadership is lost (predecessor znode
// reappears, session expires, or the parent ctx is cancelled). Run then
// re-enrolls and waits for the next leadership opportunity.
func (e *LeaderElector) Run(ctx context.Context, fn func(ctx context.Context)) error {
	if err := e.ensurePath(e.electionPath); err != nil {
		return err
	}

	for {
		if ctx.Err() != nil {
			return nil
		}

		myNode, err := e.enroll()
		if err != nil {
			slog.Error("election: enroll failed, retrying",
				"error", err, "retry_in", reenrollRetryDelay)
			select {
			case <-ctx.Done():
				return nil
			case <-time.After(reenrollRetryDelay):
				continue
			}
		}

		slog.Info("election: enrolled", "node", myNode)

		if err := e.waitForLeadership(ctx, myNode, fn); err != nil {
			if ctx.Err() != nil {
				return nil
			}
			slog.Warn("election: re-enrolling", "reason", err)
		}
	}
}

// Close releases the underlying ZooKeeper connection.
func (e *LeaderElector) Close() {
	e.conn.Close()
}

// ─── internal ─────────────────────────────────────────────────────────────────

// enroll creates the ephemeral sequential candidate znode and returns its
// base name (e.g. "candidate-0000000003").
func (e *LeaderElector) enroll() (string, error) {
	nodePath := path.Join(e.electionPath, candidatePrefix)
	created, err := e.conn.Create(
		nodePath, nil,
		zk.FlagEphemeral|zk.FlagSequence,
		zk.WorldACL(zk.PermAll),
	)
	if err != nil {
		return "", fmt.Errorf("create candidate: %w", err)
	}
	return path.Base(created), nil
}

// waitForLeadership blocks until this instance becomes the leader or ctx is
// cancelled. On leadership, fn is called with a cancellable child context.
func (e *LeaderElector) waitForLeadership(
	ctx context.Context,
	myNode string,
	fn func(ctx context.Context),
) error {
	for {
		if ctx.Err() != nil {
			return nil
		}

		isLeader, predecessor, err := e.checkLeadership(myNode)
		if err != nil {
			return err
		}

		if isLeader {
			slog.Info("election: became leader", "node", myNode)
			leaderCtx, leaderCancel := context.WithCancel(ctx)
			fn(leaderCtx)
			leaderCancel()
			return nil
		}

		// Not the leader — watch the predecessor and wait.
		slog.Info("election: standby", "node", myNode, "watching", predecessor)

		watchPath := path.Join(e.electionPath, predecessor)
		exists, _, watchCh, err := e.conn.ExistsW(watchPath)
		if err != nil {
			return fmt.Errorf("watch predecessor %s: %w", watchPath, err)
		}
		if !exists {
			// Predecessor already gone — re-check immediately.
			continue
		}

		select {
		case <-ctx.Done():
			return nil
		case ev := <-watchCh:
			slog.Debug("election: predecessor event",
				"type", ev.Type.String(), "path", ev.Path)
			// Loop: re-list children and re-check leadership.
		}
	}
}

// checkLeadership returns:
//   - isLeader=true when myNode has the lowest sequence number.
//   - predecessor: the node with the next lower sequence number when not leader.
func (e *LeaderElector) checkLeadership(myNode string) (isLeader bool, predecessor string, err error) {
	children, _, err := e.conn.Children(e.electionPath)
	if err != nil {
		return false, "", fmt.Errorf("list candidates: %w", err)
	}

	sort.Strings(children) // sequential nodes sort lexicographically by suffix

	if len(children) == 0 {
		return false, "", fmt.Errorf("no candidates found")
	}

	if children[0] == myNode {
		return true, "", nil
	}

	for i, c := range children {
		if c == myNode {
			if i == 0 {
				return true, "", nil
			}
			return false, children[i-1], nil
		}
	}

	// myNode absent — ZK session likely expired.
	return false, "", fmt.Errorf("candidate %s missing from election (session expired?)", myNode)
}

// ensurePath creates each component of p as a persistent znode.
func (e *LeaderElector) ensurePath(p string) error {
	parts := strings.Split(strings.TrimPrefix(p, "/"), "/")
	cur := ""
	for _, part := range parts {
		cur += "/" + part
		_, err := e.conn.Create(cur, nil, 0, zk.WorldACL(zk.PermAll))
		if err != nil && err != zk.ErrNodeExists {
			return fmt.Errorf("election: ensure path %s: %w", cur, err)
		}
	}
	return nil
}
