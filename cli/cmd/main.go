// vtctl — VarTrack command-line interface.
package main

import (
	"fmt"
	"os"

	"github.com/vartrack/vtctl/internal/commands"
)

func main() {
	if err := commands.Root().Execute(); err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
}
