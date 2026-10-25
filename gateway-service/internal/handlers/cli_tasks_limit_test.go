package handlers_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"gateway-service/internal/auth"
	"gateway-service/internal/handlers"
	"gateway-service/internal/routes"
	pb "vartrack/libs/gen/vartrack/v1/services"

	"google.golang.org/grpc"
)

// taskLister records the limit it is asked for.
type taskLister struct {
	pb.CLIServiceClient
	asked bool
	limit int32
}

func (l *taskLister) ListTasks(_ context.Context, in *pb.ListCliTasksRequest, _ ...grpc.CallOption) (*pb.ListCliTasksResponse, error) {
	l.asked, l.limit = true, in.GetLimit()
	return &pb.ListCliTasksResponse{}, nil
}

func TestListTasksLimit(t *testing.T) {
	cases := []struct {
		query     string
		status    int
		asked     bool
		wantLimit int32
	}{
		{"", http.StatusOK, true, 0},
		{"?limit=", http.StatusOK, true, 0},
		{"?limit=50", http.StatusOK, true, 50},
		{"?limit=2147483647", http.StatusOK, true, 2147483647},
		{"?limit=2147483648", http.StatusBadRequest, false, 0},
		{"?limit=99999999999", http.StatusBadRequest, false, 0},
		{"?limit=-1", http.StatusBadRequest, false, 0},
		{"?limit=abc", http.StatusBadRequest, false, 0},
	}
	for _, c := range cases {
		t.Run(c.query, func(t *testing.T) {
			orch := &taskLister{}
			h := routes.CLIRoutes(handlers.NewCLIHandler(orch, auth.NewGitAuthorizer(noRules{}, nil), nil))
			req := httptest.NewRequest(http.MethodGet, "/tasks"+c.query, nil)
			req.Header.Set("Authorization", "Bearer good")
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, req)

			if rec.Code != c.status {
				t.Fatalf("status = %d, want %d: %s", rec.Code, c.status, rec.Body.String())
			}
			if orch.asked != c.asked || orch.limit != c.wantLimit {
				t.Fatalf("orchestrator asked=%v limit=%d, want asked=%v limit=%d", orch.asked, orch.limit, c.asked, c.wantLimit)
			}
		})
	}
}
