package commands

import (
	"context"
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"
)

func newTaskCmd() *cobra.Command {
	task := &cobra.Command{
		Use:   "task",
		Short: "Inspect sync task status",
	}
	task.AddCommand(newTaskGetCmd(), newTaskListCmd(), newTaskWatchCmd())
	return task
}

func newTaskGetCmd() *cobra.Command {
	var jsonOut bool

	cmd := &cobra.Command{
		Use:   "get <task-id>",
		Short: "Get the status of a sync task",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			gw, err := buildClient()
			if err != nil {
				return err
			}

			task, err := gw.GetTask(context.Background(), args[0])
			if err != nil {
				return err
			}

			if jsonOut {
				return printJSON(task)
			}

			fmt.Printf("Task:       %s\n", task.TaskID)
			fmt.Printf("State:      %s\n", task.State)
			fmt.Printf("Datasource: %s\n", task.Datasource)
			fmt.Printf("Env:        %s\n", task.Env)
			fmt.Printf("File:       %s\n", task.FilePath)
			fmt.Printf("Tenant:     %s\n", task.TenantID)
			fmt.Printf("Written:    %d\n", task.Written)
			fmt.Printf("Pruned:     %d\n", task.Pruned)
			if task.Error != "" {
				fmt.Printf("Error:      %s\n", task.Error)
			}
			return nil
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false, "Output as JSON")
	return cmd
}

func newTaskListCmd() *cobra.Command {
	var (
		tenantID string
		limit    int
		jsonOut  bool
	)

	cmd := &cobra.Command{
		Use:   "list",
		Short: "List recent sync tasks",
		RunE: func(cmd *cobra.Command, args []string) error {
			gw, err := buildClient()
			if err != nil {
				return err
			}

			resp, err := gw.ListTasks(context.Background(), tenantID, limit)
			if err != nil {
				return err
			}

			if jsonOut {
				return printJSON(resp)
			}

			if len(resp.Tasks) == 0 {
				fmt.Println("No tasks found.")
				return nil
			}

			fmt.Printf("%-36s  %-12s  %-20s  %-12s  %s\n",
				"TASK ID", "STATE", "DATASOURCE", "ENV", "FILE")
			fmt.Println(strings.Repeat("─", 100))
			for _, t := range resp.Tasks {
				fmt.Printf("%-36s  %-12s  %-20s  %-12s  %s\n",
					t.TaskID, t.State, t.Datasource, t.Env, t.FilePath)
			}
			return nil
		},
	}

	cmd.Flags().StringVar(&tenantID, "tenant", os.Getenv("VARTRACK_TENANT"), "Tenant ID")
	cmd.Flags().IntVar(&limit, "limit", 20, "Maximum number of tasks to return")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "Output as JSON")
	return cmd
}

func newTaskWatchCmd() *cobra.Command {
	var jsonOut bool

	cmd := &cobra.Command{
		Use:   "watch <task-id>",
		Short: "Stream task status until completion",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			gw, err := buildClient()
			if err != nil {
				return err
			}
			return pollTask(cmd.Context(), gw, args[0], jsonOut)
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false, "Output final result as JSON")
	return cmd
}
