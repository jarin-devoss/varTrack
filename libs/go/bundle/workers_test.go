package bundle

import (
	"strings"
	"testing"
)

// workersBundle is a whole config with a workers block: the gateway, the watcher and the
// orchestrator's vartrack-bundle all read it through the same strict loader.
func workersBundle(workers string) string {
	return wholeBundle(`token: "t"`, mongoOK, ``) + "\n" + `bundle: workers: ` + workers + "\n"
}

func TestAWorkersBlockLoadsAndKeepsItsValues(t *testing.T) {
	b, err := Load(writeCUE(t, workersBundle(`{concurrency: 8, max_tasks_per_child: 500}`)), Options{Strict: true})
	if err != nil {
		t.Fatalf("a valid workers block was refused: %v", err)
	}
	if got := b.GetWorkers().GetConcurrency(); got != 8 {
		t.Errorf("concurrency = %d, want 8", got)
	}
	if got := b.GetWorkers().GetMaxTasksPerChild(); got != 500 {
		t.Errorf("max_tasks_per_child = %d, want 500", got)
	}
}

func TestAWorkersBlockMayNameOnlyOneOfItsKeys(t *testing.T) {
	b, err := Load(writeCUE(t, workersBundle(`{concurrency: 2}`)), Options{Strict: true})
	if err != nil {
		t.Fatalf("a workers block with one key was refused: %v", err)
	}
	if b.GetWorkers().MaxTasksPerChild != nil {
		t.Errorf("max_tasks_per_child was set to %d, want it unset (the default is the worker's)",
			b.GetWorkers().GetMaxTasksPerChild())
	}
}

func TestAMaxTasksPerChildOfZeroMeansNeverReplace(t *testing.T) {
	b, err := Load(writeCUE(t, workersBundle(`{max_tasks_per_child: 0}`)), Options{Strict: true})
	if err != nil {
		t.Fatalf("max_tasks_per_child: 0 was refused: %v", err)
	}
	if b.GetWorkers().MaxTasksPerChild == nil {
		t.Error("max_tasks_per_child: 0 was read as unset, so the default would replace it")
	}
}

func TestAConcurrencyOutsideOneToSixtyFourIsRefusedWithItsPath(t *testing.T) {
	for _, value := range []string{"0", "65", "1000"} {
		err := loadWhole(t, workersBundle(`{concurrency: `+value+`}`))
		if err == nil || !strings.Contains(err.Error(), "workers.concurrency") {
			t.Errorf("concurrency %s was not refused with its path: %v", value, err)
		}
	}
}

func TestAConcurrencyThatIsNotANumberIsRefused(t *testing.T) {
	if err := loadWhole(t, workersBundle(`{concurrency: "four"}`)); err == nil {
		t.Fatal(`concurrency: "four" loaded`)
	}
}

func TestAnUnknownWorkersKeyIsRefused(t *testing.T) {
	// A misspelt key must not be ignored: the operator would believe it was set.
	if err := loadWhole(t, workersBundle(`{concurency: 8}`)); err == nil {
		t.Fatal("a misspelt workers key loaded")
	}
}
