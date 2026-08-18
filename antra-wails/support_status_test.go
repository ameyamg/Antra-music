package main

import "testing"

// The donation link comes from a remote gist that is edited independently of
// releases, so a stale Ko-fi link must never reach the UI: Ko-fi's PayPal is
// permanently restricted and the page cannot take money.
func TestRetiredFundingLinkIsIgnored(t *testing.T) {
	base := SupportStatus{Link: "https://patreon.com/AntraVerse"}

	cases := []struct {
		name     string
		override string
		want     string
	}{
		{"stale ko-fi from gist is ignored", "https://ko-fi.com/antraverse", "https://patreon.com/AntraVerse"},
		{"ko-fi with different case is ignored", "https://KO-FI.com/AntraVerse", "https://patreon.com/AntraVerse"},
		{"empty override keeps default", "", "https://patreon.com/AntraVerse"},
		{"a new patreon url is honoured", "https://patreon.com/Somebody", "https://patreon.com/Somebody"},
		{"an unrelated platform is honoured", "https://buymeacoffee.com/x", "https://buymeacoffee.com/x"},
	}

	for _, c := range cases {
		got := mergeSupportStatus(base, supportStatusPatch{Link: c.override}).Link
		if got != c.want {
			t.Errorf("%s: got %q, want %q", c.name, got, c.want)
		}
	}
}

// The guard must not swallow the rest of the payload.
func TestNonLinkFieldsStillOverride(t *testing.T) {
	base := SupportStatus{Title: "old", Message: "old", Link: "https://patreon.com/AntraVerse"}
	got := mergeSupportStatus(base, supportStatusPatch{
		Title:   "Support Antra",
		Message: "Solo-maintained by one developer.",
		Link:    "https://ko-fi.com/antraverse",
	})
	if got.Title != "Support Antra" || got.Message != "Solo-maintained by one developer." {
		t.Errorf("title/message should still come from the gist, got %+v", got)
	}
	if got.Link != "https://patreon.com/AntraVerse" {
		t.Errorf("link should have fallen back to Patreon, got %q", got.Link)
	}
}
