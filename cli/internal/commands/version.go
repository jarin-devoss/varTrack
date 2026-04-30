package commands

import (
	"fmt"
	"runtime"

	"github.com/spf13/cobra"
)

// These are set at build time via ldflags:
//   -X github.com/vartrack/vtctl/internal/commands.Version=v1.0.0
//   -X github.com/vartrack/vtctl/internal/commands.GitCommit=abc1234
var (
	Version   = "dev"
	GitCommit = "unknown"
)

func newVersionCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "version",
		Short: "Print vt version information",
		Run: func(cmd *cobra.Command, args []string) {
			fmt.Printf("vt %s (%s) %s/%s\n", Version, GitCommit, runtime.GOOS, runtime.GOARCH)
		},
	}
}
