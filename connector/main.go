package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

const version = "0.1.0"
const maxFileBytes int64 = 256 * 1024 * 1024

var supported = map[string]bool{".pdf": true, ".docx": true, ".pptx": true, ".xlsx": true, ".txt": true, ".md": true, ".markdown": true, ".png": true, ".jpg": true, ".jpeg": true, ".webp": true, ".tif": true, ".tiff": true, ".mp3": true, ".m4a": true, ".wav": true, ".flac": true, ".mp4": true, ".mov": true, ".mkv": true, ".webm": true}

type Source struct {
	ID   string `json:"id"`
	Name string `json:"name"`
	Root string `json:"root"`
}
type Config struct {
	BackendURL      string   `json:"backend_url"`
	VaultID         string   `json:"vault_id"`
	ProtectedToken  string   `json:"protected_token"`
	DeviceName      string   `json:"device_name"`
	Sources         []Source `json:"sources"`
	SaveDestination string   `json:"save_destination"`
	Paused          bool     `json:"paused"`
	IntervalMinutes int      `json:"interval_minutes"`
}
type FileState struct {
	DocumentID string `json:"document_id"`
	Size       int64  `json:"size"`
	MtimeNS    int64  `json:"mtime_ns"`
	SHA256     string `json:"sha256"`
	SourceID   string `json:"source_id"`
}
type State struct {
	Files map[string]FileState `json:"files"`
}
type Client struct {
	Config Config
	Token  string
	HTTP   *http.Client
}

func appDir() string {
	base := os.Getenv("LOCALAPPDATA")
	if base == "" {
		home, _ := os.UserHomeDir()
		base = filepath.Join(home, ".personal-vault")
	}
	return filepath.Join(base, "PersonalVault")
}
func configPath() string { return filepath.Join(appDir(), "config.json") }
func statePath() string  { return filepath.Join(appDir(), "state.json") }
func logsPath() string   { return filepath.Join(appDir(), "connector.log") }
func randomID(prefix string) string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("%d-%d-%s", time.Now().UnixNano(), os.Getpid(), prefix)))
	return prefix + hex.EncodeToString(sum[:16])
}

func loadConfig() (Config, string, error) {
	var cfg Config
	raw, err := os.ReadFile(configPath())
	if err != nil {
		return cfg, "", err
	}
	if err = json.Unmarshal(raw, &cfg); err != nil {
		return cfg, "", err
	}
	token, err := unprotectSecret(cfg.ProtectedToken)
	return cfg, token, err
}
func saveConfig(cfg Config, token string) error {
	if err := os.MkdirAll(appDir(), 0700); err != nil {
		return err
	}
	protected, err := protectSecret(token)
	if err != nil {
		return err
	}
	cfg.ProtectedToken = protected
	raw, _ := json.MarshalIndent(cfg, "", "  ")
	return os.WriteFile(configPath(), raw, 0600)
}
func loadState() State {
	state := State{Files: map[string]FileState{}}
	raw, err := os.ReadFile(statePath())
	if err == nil {
		_ = json.Unmarshal(raw, &state)
	}
	if state.Files == nil {
		state.Files = map[string]FileState{}
	}
	return state
}
func saveState(state State) error {
	raw, _ := json.MarshalIndent(state, "", "  ")
	return os.WriteFile(statePath(), raw, 0600)
}
func logf(format string, args ...any) {
	_ = os.MkdirAll(appDir(), 0700)
	if info, err := os.Stat(logsPath()); err == nil && info.Size() > 5*1024*1024 {
		_ = os.Remove(logsPath() + ".1")
		_ = os.Rename(logsPath(), logsPath()+".1")
	}
	f, err := os.OpenFile(logsPath(), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err == nil {
		defer f.Close()
		fmt.Fprintf(f, "%s "+format+"\n", append([]any{time.Now().Format(time.RFC3339)}, args...)...)
	}
}

func (c *Client) request(method, path string, body io.Reader, contentType string) (*http.Response, error) {
	req, err := http.NewRequest(method, strings.TrimRight(c.Config.BackendURL, "/")+path, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.Token)
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	req.Header.Set("User-Agent", "PersonalVaultConnector/"+version)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		defer resp.Body.Close()
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8192))
		return nil, fmt.Errorf("%s: %s", resp.Status, strings.TrimSpace(string(raw)))
	}
	return resp, nil
}
func (c *Client) json(method, path string, input any, output any) error {
	var body io.Reader
	if input != nil {
		raw, _ := json.Marshal(input)
		body = bytes.NewReader(raw)
	}
	resp, err := c.request(method, path, body, "application/json")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if output != nil {
		return json.NewDecoder(resp.Body).Decode(output)
	}
	return nil
}

func chooseFolder(prompt string) (string, error) {
	script := `Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='` + strings.ReplaceAll(prompt, "'", "''") + `'; if($d.ShowDialog() -eq 'OK'){[Console]::Out.Write($d.SelectedPath)}`
	out, err := exec.Command("powershell.exe", "-NoProfile", "-STA", "-Command", script).Output()
	if err != nil {
		return "", err
	}
	path := strings.TrimSpace(string(out))
	if path == "" {
		return "", errors.New("no folder selected")
	}
	return path, nil
}
func prompt(reader *bufio.Reader, label, def string) string {
	if def != "" {
		fmt.Printf("%s [%s]: ", label, def)
	} else {
		fmt.Printf("%s: ", label)
	}
	value, _ := reader.ReadString('\n')
	value = strings.TrimSpace(value)
	if value == "" {
		return def
	}
	return value
}

func setup() error {
	reader := bufio.NewReader(os.Stdin)
	fmt.Println("Personal Vault Connector setup")
	backend := prompt(reader, "Private Vault URL (ending /vault)", "")
	pair := strings.ToUpper(prompt(reader, "Pairing code", ""))
	host, _ := os.Hostname()
	device := prompt(reader, "Device name", host)
	claimRaw, _ := json.Marshal(map[string]any{"pairing_code": pair, "device_name": device})
	resp, err := http.Post(strings.TrimRight(backend, "/")+"/api/pair/claim", "application/json", bytes.NewReader(claimRaw))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		raw, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("pairing failed: %s", raw)
	}
	var claim struct {
		VaultID     string `json:"vault_id"`
		AccessToken string `json:"access_token"`
	}
	if err = json.NewDecoder(resp.Body).Decode(&claim); err != nil {
		return err
	}
	var sources []Source
	for {
		root, err := chooseFolder("Choose a folder to index")
		if err != nil {
			if len(sources) == 0 {
				return err
			}
			break
		}
		name := prompt(reader, "Source name", filepath.Base(root))
		sources = append(sources, Source{ID: randomID("src_"), Name: name, Root: root})
		if strings.ToLower(prompt(reader, "Add another source? (y/N)", "n")) != "y" {
			break
		}
	}
	saveDest, err := chooseFolder("Choose where explicitly saved chat artifacts should be written")
	if err != nil {
		return err
	}
	cfg := Config{BackendURL: strings.TrimRight(backend, "/"), VaultID: claim.VaultID, DeviceName: device, Sources: sources, SaveDestination: saveDest, IntervalMinutes: 10}
	if err = saveConfig(cfg, claim.AccessToken); err != nil {
		return err
	}
	fmt.Printf("Paired %d source(s). Configuration: %s\n", len(sources), configPath())
	return nil
}

func registerSources(c *Client) error {
	for _, source := range c.Config.Sources {
		if err := c.json("POST", "/api/sources", map[string]any{"source_id": source.ID, "name": source.Name, "platform": "windows", "root_label": source.Root}, nil); err != nil {
			return err
		}
	}
	return nil
}
func hashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err = io.Copy(h, io.LimitReader(f, maxFileBytes+1)); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
func upload(c *Client, source Source, path, rel string, info os.FileInfo, sha, docID string, generation int64) error {
	pipeR, pipeW := io.Pipe()
	writer := multipart.NewWriter(pipeW)
	go func() {
		defer pipeW.Close()
		fields := map[string]string{"source_id": source.ID, "relative_path": filepath.ToSlash(rel), "display_name": info.Name(), "content_sha256": sha, "mtime_ns": fmt.Sprint(info.ModTime().UnixNano()), "size_bytes": fmt.Sprint(info.Size()), "scan_generation": fmt.Sprint(generation), "document_id": docID}
		for k, v := range fields {
			_ = writer.WriteField(k, v)
		}
		part, err := writer.CreateFormFile("file", info.Name())
		if err == nil {
			f, e := os.Open(path)
			if e == nil {
				_, err = io.CopyN(part, f, info.Size())
				_ = f.Close()
			} else {
				err = e
			}
		}
		_ = writer.Close()
		if err != nil {
			_ = pipeW.CloseWithError(err)
		}
	}()
	resp, err := c.request("POST", "/api/ingest", pipeR, writer.FormDataContentType())
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	return nil
}
func seen(c *Client, source Source, rel string, info os.FileInfo, state FileState, generation int64) error {
	return c.json("POST", "/api/seen", map[string]any{"source_id": source.ID, "document_id": state.DocumentID, "relative_path": filepath.ToSlash(rel), "display_name": info.Name(), "mtime_ns": info.ModTime().UnixNano(), "size_bytes": info.Size(), "scan_generation": generation}, nil)
}

func scan(c *Client) error {
	if c.Config.Paused {
		return nil
	}
	if err := registerSources(c); err != nil {
		return err
	}
	state := loadState()
	next := State{Files: map[string]FileState{}}
	generation := time.Now().UnixNano()
	allComplete := true
	incompleteSources := map[string]bool{}
	for _, source := range c.Config.Sources {
		complete := true
		if _, err := os.Stat(source.Root); err != nil {
			logf("source unavailable %s: %v", source.Root, err)
			complete = false
			allComplete = false
		} else {
			err = filepath.Walk(source.Root, func(path string, info os.FileInfo, walkErr error) error {
				if walkErr != nil {
					logf("unreadable %s: %v", path, walkErr)
					complete = false
					return nil
				}
				if info.IsDir() {
					return nil
				}
				if !supported[strings.ToLower(filepath.Ext(path))] {
					return nil
				}
				if info.Size() > maxFileBytes {
					logf("unsupported oversize %s (%d bytes)", path, info.Size())
					return nil
				}
				rel, _ := filepath.Rel(source.Root, path)
				key := source.ID + "|" + strings.ToLower(filepath.Clean(rel))
				old, exists := state.Files[key]
				if exists && old.Size == info.Size() && old.MtimeNS == info.ModTime().UnixNano() {
					if err := seen(c, source, rel, info, old, generation); err != nil {
						logf("seen failed %s: %v", path, err)
						complete = false
					} else {
						next.Files[key] = old
					}
					return nil
				}
				sha, err := hashFile(path)
				if err != nil {
					logf("content unavailable (cloud placeholder, network, or permission) %s: %v", path, err)
					complete = false
					return nil
				}
				docID := ""
				if exists {
					docID = old.DocumentID
				} else {
					for oldKey, candidate := range state.Files {
						if candidate.SourceID == source.ID && candidate.SHA256 == sha && candidate.Size == info.Size() {
							if _, already := next.Files[oldKey]; !already {
								docID = candidate.DocumentID
								break
							}
						}
					}
					if docID == "" {
						docID = randomID("doc_")
					}
				}
				if err = upload(c, source, path, rel, info, sha, docID, generation); err != nil {
					logf("upload failed %s: %v", path, err)
					complete = false
					return nil
				}
				next.Files[key] = FileState{DocumentID: docID, Size: info.Size(), MtimeNS: info.ModTime().UnixNano(), SHA256: sha, SourceID: source.ID}
				return nil
			})
			if err != nil {
				complete = false
				allComplete = false
			}
		}
		if !complete {
			incompleteSources[source.ID] = true
		}
		if err := c.json("POST", "/api/reconcile", map[string]any{"source_id": source.ID, "generation": generation, "complete": complete}, nil); err != nil {
			logf("reconcile failed %s: %v", source.Name, err)
			allComplete = false
			incompleteSources[source.ID] = true
		}
	}
	for key, item := range state.Files {
		if _, present := next.Files[key]; !present && incompleteSources[item.SourceID] {
			next.Files[key] = item
		}
	}
	if err := saveState(next); err != nil {
		return err
	}
	if !allComplete {
		return errors.New("scan completed with unavailable items; deletions were not confirmed")
	}
	return nil
}

type Artifact struct {
	ID       string  `json:"id"`
	FileName string  `json:"file_name"`
	NoteText *string `json:"note_text"`
	HasFile  bool    `json:"has_file"`
}

func safeName(name string) string {
	name = filepath.Base(name)
	name = strings.Map(func(r rune) rune {
		if strings.ContainsRune(`<>:"/\\|?*`, r) || r < 32 {
			return '_'
		}
		return r
	}, name)
	if name == "" {
		return "chat-artifact"
	}
	return name
}
func deliverArtifacts(c *Client) error {
	var listing struct {
		Artifacts []Artifact `json:"artifacts"`
	}
	if err := c.json("GET", "/api/connector/artifacts", nil, &listing); err != nil {
		return err
	}
	for _, a := range listing.Artifacts {
		name := safeName(a.FileName)
		target := filepath.Join(c.Config.SaveDestination, name)
		if _, err := os.Stat(target); err == nil {
			ext := filepath.Ext(name)
			target = filepath.Join(c.Config.SaveDestination, strings.TrimSuffix(name, ext)+"-"+time.Now().Format("20060102-150405")+ext)
		}
		var err error
		if a.NoteText != nil {
			err = os.WriteFile(target, []byte(*a.NoteText), 0600)
		} else {
			resp, e := c.request("GET", "/api/connector/artifacts/"+url.PathEscape(a.ID)+"/content", nil, "")
			if e != nil {
				err = e
			} else {
				f, e2 := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
				if e2 == nil {
					var written int64
					written, e2 = io.Copy(f, io.LimitReader(resp.Body, maxFileBytes+1))
					if written > maxFileBytes {
						e2 = errors.New("artifact exceeds byte limit")
					}
					_ = f.Close()
				}
				_ = resp.Body.Close()
				err = e2
			}
		}
		payload := map[string]any{"success": err == nil, "saved_relative_path": target}
		if err != nil {
			_ = os.Remove(target)
			payload["error"] = err.Error()
			payload["saved_relative_path"] = nil
		}
		_ = c.json("POST", "/api/connector/artifacts/"+url.PathEscape(a.ID)+"/complete", payload, nil)
	}
	return nil
}

func run() error {
	cfg, token, err := loadConfig()
	if err != nil {
		return fmt.Errorf("run setup first: %w", err)
	}
	client := &Client{Config: cfg, Token: token, HTTP: &http.Client{Timeout: 30 * time.Minute}}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	for {
		if err = deliverArtifacts(client); err != nil {
			logf("artifact delivery: %v", err)
		}
		if err = scan(client); err != nil {
			logf("scan: %v", err)
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(time.Duration(max(1, cfg.IntervalMinutes)) * time.Minute):
		}
	}
}

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      any             `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
}

func rpcResult(id any, result any) map[string]any {
	return map[string]any{"jsonrpc": "2.0", "id": id, "result": result}
}
func mcpStdio() error {
	cfg, token, err := loadConfig()
	if err != nil {
		return err
	}
	client := &Client{Config: cfg, Token: token, HTTP: &http.Client{Timeout: 2 * time.Minute}}
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 64*1024), 2*1024*1024)
	enc := json.NewEncoder(os.Stdout)
	for scanner.Scan() {
		var req rpcRequest
		if json.Unmarshal(scanner.Bytes(), &req) != nil {
			continue
		}
		if req.Method == "notifications/initialized" {
			continue
		}
		switch req.Method {
		case "initialize":
			_ = enc.Encode(rpcResult(req.ID, map[string]any{"protocolVersion": "2025-06-18", "capabilities": map[string]any{"tools": map[string]any{}}, "serverInfo": map[string]any{"name": "Personal Vault Connector", "version": version}, "instructions": "Search before collection-specific claims. Cite stable document/version IDs and locators. Saving notes is explicit."}))
		case "tools/list":
			tools := []any{
				tool("search_vault", "Hybrid-search the personal vault. Pass a resolved self-contained question.", map[string]any{"question": strSchema(), "limit": map[string]any{"type": "integer", "minimum": 1, "maximum": 20}}, []string{"question"}),
				tool("fetch_evidence", "Expand a result with adjacent passages.", map[string]any{"chunk_id": strSchema(), "adjacent": map[string]any{"type": "integer", "minimum": 0, "maximum": 5}}, []string{"chunk_id"}),
				tool("inspect_visual", "Return one retained image, document-page, or video-frame preview.", map[string]any{"document_id": strSchema(), "preview_index": map[string]any{"type": "integer", "minimum": 0, "maximum": 119}}, []string{"document_id"}),
				tool("calculate_spreadsheet", "Calculate over persisted spreadsheet cells with a cited range.", map[string]any{"document_id": strSchema(), "sheet_name": strSchema(), "cell_range": strSchema(), "operation": map[string]any{"type": "string", "enum": []string{"sum", "average", "min", "max", "count"}}}, []string{"document_id", "sheet_name", "cell_range"}),
				tool("collection_status", "Show source and index readiness.", map[string]any{}, nil),
				tool("save_chat_note", "Write a user-authored note to the configured Saved Artifacts folder.", map[string]any{"title": strSchema(), "text": strSchema()}, []string{"title", "text"}),
			}
			_ = enc.Encode(rpcResult(req.ID, map[string]any{"tools": tools}))
		case "tools/call":
			var call struct {
				Name      string         `json:"name"`
				Arguments map[string]any `json:"arguments"`
			}
			_ = json.Unmarshal(req.Params, &call)
			var content []any
			var e error
			if call.Name == "inspect_visual" {
				content, e = localVisual(client, call.Arguments)
			} else {
				var result []byte
				result, e = localTool(client, call.Name, call.Arguments)
				content = []any{map[string]any{"type": "text", "text": string(result)}}
			}
			response := map[string]any{"content": content, "isError": e != nil}
			if e != nil {
				response["content"] = []any{map[string]any{"type": "text", "text": e.Error()}}
			}
			_ = enc.Encode(rpcResult(req.ID, response))
		default:
			_ = enc.Encode(map[string]any{"jsonrpc": "2.0", "id": req.ID, "error": map[string]any{"code": -32601, "message": "Method not found"}})
		}
	}
	return scanner.Err()
}
func localVisual(c *Client, args map[string]any) ([]any, error) {
	index := 0
	if n, ok := args["preview_index"].(float64); ok {
		index = int(n)
	}
	path := "/api/documents/" + url.PathEscape(fmt.Sprint(args["document_id"])) + "/previews/" + fmt.Sprint(index)
	response, err := c.request("GET", path, nil, "")
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, 10*1024*1024+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > 10*1024*1024 {
		return nil, errors.New("visual preview exceeds the 10 MiB MCP limit")
	}
	mimeType := response.Header.Get("Content-Type")
	if !strings.HasPrefix(mimeType, "image/") {
		return nil, errors.New("preview is not an image")
	}
	return []any{map[string]any{"type": "image", "data": base64.StdEncoding.EncodeToString(raw), "mimeType": mimeType}}, nil
}
func strSchema() map[string]any { return map[string]any{"type": "string"} }
func tool(name, description string, properties map[string]any, required []string) map[string]any {
	schema := map[string]any{"type": "object", "properties": properties, "additionalProperties": false}
	if required != nil {
		schema["required"] = required
	}
	return map[string]any{"name": name, "description": description, "inputSchema": schema}
}
func localTool(c *Client, name string, args map[string]any) ([]byte, error) {
	var output any
	switch name {
	case "search_vault":
		limit := 8
		if n, ok := args["limit"].(float64); ok {
			limit = int(n)
		}
		err := c.json("POST", "/api/search", map[string]any{"question": args["question"], "limit": limit}, &output)
		raw, _ := json.Marshal(output)
		return raw, err
	case "fetch_evidence":
		adjacent := 2
		if n, ok := args["adjacent"].(float64); ok {
			adjacent = int(n)
		}
		path := "/api/chunks/" + url.PathEscape(fmt.Sprint(args["chunk_id"])) + "?adjacent=" + fmt.Sprint(adjacent)
		err := c.json("GET", path, nil, &output)
		raw, _ := json.Marshal(output)
		return raw, err
	case "collection_status":
		err := c.json("GET", "/api/status", nil, &output)
		raw, _ := json.Marshal(output)
		return raw, err
	case "calculate_spreadsheet":
		operation := "sum"
		if value, ok := args["operation"].(string); ok && value != "" {
			operation = value
		}
		err := c.json("POST", "/api/table/calculate", map[string]any{"document_id": args["document_id"], "sheet_name": args["sheet_name"], "cell_range": args["cell_range"], "operation": operation}, &output)
		raw, _ := json.Marshal(output)
		return raw, err
	case "save_chat_note":
		name := strings.TrimSuffix(safeName(fmt.Sprint(args["title"])), ".md") + ".md"
		target := filepath.Join(c.Config.SaveDestination, name)
		if _, statErr := os.Stat(target); statErr == nil {
			target = filepath.Join(c.Config.SaveDestination, strings.TrimSuffix(name, ".md")+"-"+time.Now().Format("20060102-150405")+".md")
		}
		file, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err == nil {
			_, err = fmt.Fprintf(file, "---\nauthor: user via Claude Desktop\ncreated_at: %s\nstatement_type: user_note\n---\n\n%s\n", time.Now().Format(time.RFC3339), fmt.Sprint(args["text"]))
			closeErr := file.Close()
			if err == nil {
				err = closeErr
			}
		}
		raw, _ := json.Marshal(map[string]any{"saved": err == nil, "path": target, "state": "saved_to_source; background indexing pending"})
		return raw, err
	default:
		return nil, errors.New("unknown tool")
	}
}

func main() {
	command := "run"
	if len(os.Args) > 1 {
		command = os.Args[1]
	}
	var err error
	switch command {
	case "setup":
		err = setup()
	case "run":
		err = run()
	case "scan":
		cfg, token, e := loadConfig()
		if e != nil {
			err = e
		} else {
			err = scan(&Client{Config: cfg, Token: token, HTTP: &http.Client{Timeout: 30 * time.Minute}})
		}
	case "pause":
		cfg, token, e := loadConfig()
		if e == nil {
			cfg.Paused = true
			err = saveConfig(cfg, token)
		} else {
			err = e
		}
	case "resume":
		cfg, token, e := loadConfig()
		if e == nil {
			cfg.Paused = false
			err = saveConfig(cfg, token)
		} else {
			err = e
		}
	case "status":
		cfg, _, e := loadConfig()
		if e == nil {
			raw, _ := json.MarshalIndent(cfg, "", "  ")
			fmt.Println(string(raw))
		} else {
			err = e
		}
	case "mcp-stdio":
		err = mcpStdio()
	case "version":
		fmt.Println(version)
	default:
		err = fmt.Errorf("unknown command %q", command)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "Error:", err)
		os.Exit(1)
	}
}
