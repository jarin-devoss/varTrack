package commands

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"github.com/vartrack/vtctl/internal/client"
)

func newSyncCmd() *cobra.Command {
	var (
		filePath   string
		datasource string
		env        string
		tenantID   string
		format     string
		dryRun     bool
		wait       bool
		label      string
		revision   string
		platform   string
		timeout    time.Duration
		jsonOut    bool
	)

	cmd := &cobra.Command{
		Use:   "sync",
		Short: "Push a local config file to a datasource",
		Long: `Parse a local config file and write it to the specified datasource sink,
running the full ETL pipeline: parse → schema-validate → write.

The datasource must be defined in the CUE bundle deployed at the gateway.
Authentication uses the active vtctl context (or VARTRACK_TOKEN env var).

Examples:
  # Push app.yaml to the mongo-primary datasource in production
  vt sync --file configs/app.yaml --datasource mongo-primary --env production

  # Dry-run — see what would be written without touching the sink
  vt sync --file configs/app.yaml --datasource mongo --env staging --dry-run

  # CI/CD pipeline (token from env, wait for completion)
  vt sync --file settings.json --datasource redis --env pr-$PR_NUMBER \
             --label "pr-$PR_NUMBER" --wait --timeout 3m

  # Roll production back to the rule's file as it is at tag v1.4.0
  vt sync --revision v1.4.0 --datasource redis --env production`,
		RunE: func(cmd *cobra.Command, args []string) error {
			if (filePath == "") == (revision == "") {
				return fmt.Errorf("give --file or --revision")
			}
			if datasource == "" {
				return fmt.Errorf("--datasource is required")
			}
			if env == "" {
				return fmt.Errorf("--env is required")
			}

			var content []byte
			if filePath != "" {
				var err error
				if content, err = os.ReadFile(filePath); err != nil {
					return fmt.Errorf("read file %s: %w", filePath, err)
				}
				if format == "" {
					format = detectFormat(filePath)
				}
			}

			gw, err := buildClient()
			if err != nil {
				return err
			}

			ctx, cancel := context.WithTimeout(context.Background(), timeout)
			defer cancel()

			req := client.SyncRequest{
				Datasource: datasource,
				Env:        env,
				FilePath:   filepath.ToSlash(filePath),
				Content:    string(content),
				Format:     format,
				TenantID:   tenantID,
				DryRun:     dryRun,
				Label:      label,
				Revision:   revision,
				Platform:   platform,
			}

			resp, err := gw.Sync(ctx, req)
			if err != nil {
				return fmt.Errorf("sync: %w", err)
			}

			if !wait {
				if jsonOut {
					return printJSON(resp)
				}
				prefix := ""
				if resp.DryRun {
					prefix = "[dry-run] "
				}
				fmt.Printf("%sSync queued — task_id=%s\n", prefix, resp.TaskID)
				fmt.Println("Poll status: vt task get", resp.TaskID)
				return nil
			}

			// Poll until the task reaches a terminal state.
			return pollTask(ctx, gw, resp.TaskID, jsonOut)
		},
	}

	cmd.Flags().StringVar(&filePath, "file", "", "Path to the config file")
	cmd.Flags().StringVar(&datasource, "datasource", "", "Target datasource name from the CUE bundle")
	cmd.Flags().StringVar(&platform, "platform", "", "Platform of the rule (e.g. github, gitea-internal) — needed only when rules on several platforms write to the datasource")
	cmd.Flags().StringVar(&env, "env", "", "Target environment (e.g. production, staging, pr-42)")
	cmd.Flags().StringVar(&tenantID, "tenant", os.Getenv("VARTRACK_TENANT"), "Tenant ID")
	cmd.Flags().StringVar(&format, "format", "", "File format (yaml/json/toml/env/ini/hcl — auto-detected)")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false, "Parse and validate without writing to the sink")
	cmd.Flags().BoolVar(&wait, "wait", false, "Wait for the task to complete before exiting")
	cmd.Flags().StringVar(&label, "label", "", "Optional label attached to this push (e.g. git SHA)")
	cmd.Flags().StringVar(&revision, "revision", "", "Sync the rule's file as it is in Git at this branch or tag, instead of --file")
	cmd.Flags().DurationVar(&timeout, "timeout", 5*time.Minute, "Maximum time to wait")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "Output results as JSON")

	return cmd
}

// pollTask polls GET /v1/cli/tasks/{id} until the task reaches a terminal state.
func pollTask(ctx context.Context, gw *client.Client, taskID string, jsonOut bool) error {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()

	fmt.Printf("Waiting for task %s", taskID)

	for {
		select {
		case <-ctx.Done():
			fmt.Println()
			return fmt.Errorf("timed out waiting for task %s", taskID)
		case <-ticker.C:
			task, err := gw.GetTask(ctx, taskID)
			if err != nil {
				fmt.Print(".")
				continue
			}

			switch strings.ToUpper(task.State) {
			case "SUCCESS", "SUCCEEDED":
				fmt.Println()
				if jsonOut {
					return printJSON(task)
				}
				fmt.Printf("✓ Task %s completed\n", taskID)
				if task.DryRun && task.Message != "" {
					fmt.Printf("  %s\n", task.Message)
					return nil
				}
				fmt.Printf("  written=%d  pruned=%d\n", task.Written, task.Pruned)
				return nil

			case "FAILURE", "FAILED":
				fmt.Println()
				if jsonOut {
					return printJSON(task)
				}
				return fmt.Errorf("task %s failed: %s", taskID, task.Error)

			default:
				fmt.Print(".")
			}
		}
	}
}

func detectFormat(path string) string {
	ext := strings.ToLower(strings.TrimPrefix(filepath.Ext(path), "."))
	switch ext {
	case "yaml", "yml":
		return "yaml"
	case "json":
		return "json"
	case "toml":
		return "toml"
	case "env":
		return "env"
	case "ini":
		return "ini"
	case "hcl":
		return "hcl"
	case "properties":
		return "properties"
	default:
		return "yaml"
	}
}

func printJSON(v interface{}) error {
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}
