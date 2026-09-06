package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func artifactFixture(id, name string, content []byte, note *string) Artifact {
	digest := sha256.Sum256(content)
	return Artifact{
		ID: id, FileName: name, NoteText: note, HasFile: true,
		ContentSHA256: hex.EncodeToString(digest[:]), ContentSizeBytes: int64(len(content)),
	}
}

func TestWriteArtifactPreservesBinaryAnnotationAndIsIdempotent(t *testing.T) {
	content := []byte("original binary attachment")
	note := "user annotation"
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		_, _ = response.Write(content)
	}))
	defer server.Close()
	destination := t.TempDir()
	client := &Client{Config: Config{BackendURL: server.URL, SaveDestination: destination}, Token: "test", HTTP: server.Client()}
	artifact := artifactFixture("artifact_1234567890abcdef", "invoice.pdf", content, &note)
	relative, err := writeArtifact(client, artifact)
	if err != nil {
		t.Fatal(err)
	}
	if relative != "invoice.pdf" {
		t.Fatalf("unexpected target %q", relative)
	}
	if raw, _ := os.ReadFile(filepath.Join(destination, relative)); string(raw) != string(content) {
		t.Fatal("binary attachment was not preserved")
	}
	if raw, _ := os.ReadFile(filepath.Join(destination, relative+".note.md")); string(raw) != note {
		t.Fatal("annotation sidecar was not preserved")
	}
	again, err := writeArtifact(client, artifact)
	if err != nil || again != relative {
		t.Fatalf("retry was not idempotent: %q %v", again, err)
	}
}

func TestWriteArtifactCollisionNeverOverwritesOrDeletesExistingFile(t *testing.T) {
	content := []byte("new attachment")
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		_, _ = response.Write(content)
	}))
	defer server.Close()
	destination := t.TempDir()
	existing := filepath.Join(destination, "invoice.pdf")
	if err := os.WriteFile(existing, []byte("keep me"), 0600); err != nil {
		t.Fatal(err)
	}
	client := &Client{Config: Config{BackendURL: server.URL, SaveDestination: destination}, Token: "test", HTTP: server.Client()}
	artifact := artifactFixture("artifact_abcdef1234567890", "invoice.pdf", content, nil)
	relative, err := writeArtifact(client, artifact)
	if err != nil {
		t.Fatal(err)
	}
	if relative == "invoice.pdf" {
		t.Fatal("collision reused an unrelated file")
	}
	if raw, _ := os.ReadFile(existing); string(raw) != "keep me" {
		t.Fatal("existing file was overwritten or deleted")
	}
	if raw, _ := os.ReadFile(filepath.Join(destination, relative)); string(raw) != string(content) {
		t.Fatal("new collision-safe target is incorrect")
	}
}

func TestSavedDestinationCoverage(t *testing.T) {
	root := t.TempDir()
	if !pathWithinRoot(root, filepath.Join(root, "Saved")) {
		t.Fatal("nested saved destination should already be covered")
	}
	if pathWithinRoot(root, filepath.Join(filepath.Dir(root), "elsewhere")) {
		t.Fatal("unrelated saved destination must be registered separately")
	}
	sources := []Source{{ID: "src_existing", Name: "Existing", Root: root}}
	if got := ensureSavedDestinationSource(sources, filepath.Join(root, "Saved")); len(got) != 1 {
		t.Fatal("covered destination was registered twice")
	}
	if got := ensureSavedDestinationSource(sources, filepath.Join(filepath.Dir(root), "elsewhere")); len(got) != 2 || got[1].Name != "Saved Chat Artifacts" {
		t.Fatal("uncovered destination was not registered for indexing")
	}
}

func TestPauseChangeWakesDaemonCycle(t *testing.T) {
	originalInterval, originalReader := configurationPollInterval, readConfiguredPause
	defer func() {
		configurationPollInterval, readConfiguredPause = originalInterval, originalReader
	}()
	configurationPollInterval = time.Millisecond
	readConfiguredPause = func() (bool, error) { return true, nil }
	started := time.Now()
	if !waitForNextCycle(context.Background(), false, time.Minute) {
		t.Fatal("pause change should wake the daemon loop")
	}
	if time.Since(started) > time.Second {
		t.Fatal("pause change was not detected promptly")
	}
}
