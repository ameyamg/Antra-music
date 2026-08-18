package main

// Live end-to-end check of the credential separation against the REAL mirrors.
//
// Skipped unless ANTRA_TEST_DEVICE_TOKEN is set, so it never runs in a normal
// `go test`. Tokens are minted on the VPS for a throwaway user — the point is to
// exercise the actual HTTP path (/api/device/status) and the real config
// read/write, which no pure unit test can cover.
//
// LOCALAPPDATA is redirected to a temp dir so the developer's own config.json is
// never read or written.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func writeTestConfig(t *testing.T, cfg map[string]any) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("LOCALAPPDATA", dir)
	if err := os.MkdirAll(filepath.Join(dir, "Antra"), 0o755); err != nil {
		t.Fatal(err)
	}
	cfg["download_path"] = dir
	cfg["first_run_complete"] = true
	b, _ := json.Marshal(cfg)
	if err := os.WriteFile(filepath.Join(dir, "Antra", "config.json"), b, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestLiveDeviceLoginSupporterFlow(t *testing.T) {
	supTok := os.Getenv("ANTRA_TEST_DEVICE_TOKEN")
	if supTok == "" {
		t.Skip("set ANTRA_TEST_DEVICE_TOKEN (supporter/premium tier) to run")
	}
	freeTok := os.Getenv("ANTRA_TEST_DEVICE_TOKEN_FREE")
	realKey := os.Getenv("ANTRA_TEST_SUPPORTER_KEY")

	app := &App{}

	t.Run("signed in with supporter token, no pasted key", func(t *testing.T) {
		writeTestConfig(t, map[string]any{"antra_device_token": supTok})
		invalidateDeviceStatus()

		st := app.GetDeviceAccountStatus()
		t.Logf("status: signed_in=%v valid=%v reachable=%v supporter=%v tier=%q user=%q",
			st.SignedIn, st.Valid, st.Reachable, st.IsSupporter, st.Tier, st.Username)
		if !st.SignedIn || !st.Reachable || !st.Valid {
			t.Fatalf("expected a live valid session, got %+v", st)
		}
		if !st.IsSupporter {
			t.Fatalf("token tier %q did not read as supporter", st.Tier)
		}

		sup, key := app.supporterContext()
		if !sup {
			t.Error("supporterContext: expected supporter — this is the reported bug")
		}
		if key != supTok {
			t.Errorf("mirror key: got %q, want the device token (per-user attribution)", key)
		}
	})

	t.Run("legacy install: token still in antra_api_key", func(t *testing.T) {
		// The pre-fix shape. GetConfig migrates it on read, and the supporter
		// status must survive the migration.
		writeTestConfig(t, map[string]any{"antra_api_key": supTok})
		invalidateDeviceStatus()

		cfg := app.GetConfig()
		if cfg.AntraDeviceToken != supTok {
			t.Errorf("migration did not move the token: device=%q api=%q", cfg.AntraDeviceToken, cfg.AntraApiKey)
		}
		if cfg.AntraApiKey != "" {
			t.Errorf("migration left the token in the supporter-key field: %q", cfg.AntraApiKey)
		}
		if sup, _ := app.supporterContext(); !sup {
			t.Error("migrated install lost supporter status")
		}
	})

	if freeTok != "" && realKey != "" {
		t.Run("free account must not cost a pasted supporter key its tier", func(t *testing.T) {
			writeTestConfig(t, map[string]any{
				"antra_device_token": freeTok,
				"antra_api_key":      realKey,
			})
			invalidateDeviceStatus()

			sup, key := app.supporterContext()
			if !sup {
				t.Error("supporter key was ignored because the account is free tier")
			}
			if key != realKey {
				t.Errorf("mirror key: got %q, want the supporter key so FEAT-7 prices the tier", key)
			}
		})
	}

	t.Run("signed out", func(t *testing.T) {
		writeTestConfig(t, map[string]any{"antra_device_token": supTok})
		invalidateDeviceStatus()
		if err := app.SignOutDevice(); err != "" {
			t.Fatal(err)
		}
		cfg := app.GetConfig()
		if storedDeviceToken(cfg) != "" {
			t.Error("sign-out left a token behind")
		}
		st := app.GetDeviceAccountStatus()
		if st.SignedIn {
			t.Error("still reports signed in after sign-out")
		}
	})
}
