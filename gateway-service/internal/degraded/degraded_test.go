package degraded

import "testing"

func TestAModeExistsOnlyWhenNamed(t *testing.T) {
	t.Setenv("VARTRACK_ALLOW_DEGRADED", "")
	if ok, err := Allowed("nonce_store", false); ok || err != nil {
		t.Fatalf("nothing named: got %v, %v", ok, err)
	}
	t.Setenv("VARTRACK_ALLOW_DEGRADED", " nonce_store , config")
	for _, mode := range []string{"nonce_store", "config"} {
		if ok, err := Allowed(mode, false); !ok || err != nil {
			t.Fatalf("%s named: got %v, %v", mode, ok, err)
		}
	}
	if ok, _ := Allowed("tls", false); ok {
		t.Fatal("tls was not named")
	}
}

func TestSomeModesAreRefusedInProductionWhateverIsNamed(t *testing.T) {
	t.Setenv("VARTRACK_ALLOW_DEGRADED", "nonce_store,tls,config")
	for mode, want := range map[string]bool{"nonce_store": false, "tls": false, "config": true} {
		if ok, _ := Allowed(mode, true); ok != want {
			t.Fatalf("%s in production = %v, want %v", mode, ok, want)
		}
	}
}

func TestATypoIsAnErrorNotAnEmptyAllowList(t *testing.T) {
	t.Setenv("VARTRACK_ALLOW_DEGRADED", "nonce-store")
	if _, err := Allowed("nonce_store", false); err == nil {
		t.Fatal("a misspelled mode must be an error")
	}
}

func TestAModeInUseIsListedWithItsReason(t *testing.T) {
	Reset()
	On("nonce_store", "redis refused the password")
	if got := Active()["nonce_store"]; got != "redis refused the password" {
		t.Fatalf("Active() = %v", Active())
	}
	Reset()
	if len(Active()) != 0 {
		t.Fatal("Reset left modes behind")
	}
}
