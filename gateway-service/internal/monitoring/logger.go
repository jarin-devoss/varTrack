// Package monitoring provides structured logging, metrics, and tracing for
// the gateway service.
//
// Logger setup runs in init() so every package that imports monitoring gets
// JSON-structured output automatically.  The log level is driven by the
// LOG_LEVEL environment variable (DEBUG | INFO | WARN | ERROR; default INFO).
package monitoring

import (
	"log/slog"
	"os"
	"strings"
)

func init() {
	level := getLogLevel()

	opts := &slog.HandlerOptions{
		Level: level,
		// AddSource emits "source":{"function":…,"file":…,"line":…} at DEBUG level
		// so local development gets full stack context without flooding INFO logs.
		AddSource: level == slog.LevelDebug,
	}

	handler := slog.NewJSONHandler(os.Stdout, opts)
	slog.SetDefault(slog.New(handler))
}

func getLogLevel() slog.Level {
	switch strings.ToUpper(os.Getenv("LOG_LEVEL")) {
	case "DEBUG":
		return slog.LevelDebug
	case "INFO":
		return slog.LevelInfo
	case "WARN":
		return slog.LevelWarn
	case "ERROR":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
