// Command vartrack-bundle reads a varTrack config.cue the way the gateway and the
// watcher do, for the services that are not written in Go.
//
//	vartrack-bundle export config.cue    print the bundle as JSON, or exit 1 with why it is refused
//	vartrack-bundle check  config.cue    exit 1 with why it is refused; print nothing otherwise
//
// There is one reading of config.cue (libs/go/bundle): the same CUE evaluation, the
// same shorthands and renamed options, the same strict parse against the protos. A
// config this command refuses is refused by every service, and the orchestrator is
// handed what this prints instead of reading the file with its own rules.
package main

import (
	"fmt"
	"os"

	"vartrack/libs/bundle"
)

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: vartrack-bundle export|check <config.cue>")
		os.Exit(2)
	}
	command, path := os.Args[1], os.Args[2]
	switch command {
	case "export":
		out, err := bundle.Export(path)
		if err != nil {
			fail(err)
		}
		os.Stdout.Write(out)
		os.Stdout.WriteString("\n")
	case "check":
		if _, err := bundle.Export(path); err != nil {
			fail(err)
		}
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q: export or check\n", command)
		os.Exit(2)
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
