# Implementation Roadmap

## Phase 1 — Core Pipeline (Weeks 1–3) ✅ Complete

**Goal**: A working end-to-end pipeline from idea to task breakdown, no parallel
execution, no HITL, no code generation. Proves the stage machine, artifact
routing, and memory system work.

### Week 1: Foundation
- [x] `core/registry.py` — ToolRegistry
- [x] `core/memory.py` — ArtifactBundle + ProjectMemory + streaming scrubber
- [x] `core/session.py` — PipelineDB SQLite (WAL mode, jittered-backoff retry)
- [x] `core/agent_runtime.py` — call_model_openai thin wrapper
- [x] `core/agent_base.py` — BaseSDLCAgent with run() loop + IterationBudget
- [x] Wire up tests: `pytest tests/` passes without any API calls

**Deliverable**: `pytest tests/test_pipeline_unit.py` passes with 100% of unit tests.

### Week 2: First three stages
- [x] `agents/roles.py` — DomainExpertAgent, ProductOwnerAgent, BusinessAnalystAgent
- [x] `core/pipeline.py` — Stage machine (sequential stages 0–1)
- [x] `sdlc.py` — CLI `run` command
- [x] Manual test: `python sdlc.py run "Build a todo app" --auto-approve`

**Deliverable**: Running the CLI produces a BRS artifact in `output/`.

### Week 3: Architecture + planning stages
- [x] ArchitectAgent + TechLeadAgent
- [x] Pipeline stages 2–3 (Architecture, Planning)
- [x] Checkpoint/resume: `python sdlc.py resume <run_id>`
- [x] `python sdlc.py status <run_id>` shows stage progress

**Deliverable**: Full pipeline up to task breakdown. Output directory contains:
`glossary.json`, `user_stories.json`, `brs.json`, `adrs.json`, `tasks.json`.

---

## Phase 2 — Parallel Development (Weeks 4–6) ✅ Complete

**Goal**: Developer agents actually write code to the output workspace.
Introduces the parallel fan-out + kanban board.

### Week 4: Developer agent tools
- [x] `tools/file_tools.py` — read_file, write_file, patch, search_files
- [x] `tools/terminal_tool.py` — simplified local subprocess executor
- [x] `tools/web_tools.py` — web_search (via httpx + search API)
- [x] Register all tools in the registry

### Week 5: Parallel dispatch
- [x] `core/pipeline.py` — `_run_parallel_stage()` with ThreadPoolExecutor
- [x] `core/session.py` — kanban task CRUD
- [x] `python sdlc.py kanban <run_id>` shows task board

### Week 6: Code output
- [x] DeveloperBackendAgent writes real files to `output/<run_id>/src/`
- [x] DevOpsAgent writes Dockerfile + `docker-compose.yml` + `.github/workflows/ci.yml`
- [x] `git init` in output workspace; each task creates a commit

**Deliverable**: `output/<run_id>/src/` contains generated API code; Docker
`build` succeeds.

---

## Phase 3 — HITL Governance (Weeks 7–8) ✅ Complete

**Goal**: Human can review and approve/reject/amend each stage gate.

### Week 7: CLI gate
- [x] `gateway/hitl.py` — HITLGateway with CLI mode
- [x] Wire gates into `core/pipeline.py` `_execute()`
- [x] `python sdlc.py approve <run_id> <stage>` programmatic approval
- [x] `python sdlc.py audit <run_id>` shows decisions

### Week 8: Slack + webhook gates
- [x] Slack gate: post artifact summary → poll for reaction
- [x] Webhook gate: POST to URL → wait on ApprovalQueue
- [x] Config-driven gate mode per stage
- [x] AMENDED flow: human edits JSON → pipeline re-runs downstream stages

**Deliverable**: A Slack message appears at the architecture gate; reacting
with ✅ advances the pipeline.

---

## Phase 4 — QA Loop (Weeks 9–10) ✅ Complete

**Goal**: QA agent runs the generated tests and feeds failures back.

### Week 9: QA agent
- [x] QAEngineerAgent generates pytest suite from BRS acceptance criteria
- [x] Runs `pytest` in a subprocess; parses XML/JSON output
- [x] Reports coverage via `coverage.py`

### Week 10: Fix loop
- [x] On QA failure: create a new developer task "Fix failing test: <id>"
- [x] Developer agent picks up the fix task (max 3 iterations)
- [x] If 3 fix attempts all fail: escalate to HITL gate
- [x] Coverage threshold enforced by AutoEvaluator

**Deliverable**: Full QA loop; `pytest` passes on the generated code; coverage ≥ 80%.

---

## Phase 5 — Delivery (Weeks 11–12) ✅ Complete

**Goal**: Production-ready package: Docker image, release notes, CI passing.

### Week 11: Release pipeline
- [x] ReleaseManagerAgent produces CHANGELOG.md + deployment checklist
- [x] DevOps agent builds Docker image in subprocess; verifies it runs
- [x] `sdlc deliver <run_id>` command

### Week 12: Polish
- [x] OpenTelemetry spans for each stage (`core/telemetry.py` — pipeline / stage / agent spans with graceful no-op fallback)
- [x] Cost tracking: `sdlc cost <run_id>` shows token/$ per stage
- [x] `sdlc.py` `--dry-run` flag: prints plan, no API calls
- [x] Prompt injection scanning: `scan_for_injection()` wired into `pipeline.run()` before any DB state is created
- [x] Async artifact disk writes: fire-and-forget `ThreadPoolExecutor` writer keeps pipeline latency unaffected

**Deliverable**: Running `python sdlc.py run "Build a FastAPI todo app"` produces
a Docker image that passes its own test suite.

---

## Phase 6 — Enterprise Features ✅ Mostly Complete

### Completed
- [x] **Multi-tenant isolation** — per-tenant run isolation in SQLite + per-tenant credentials (`core/tenant.py`)
- [x] **Web UI** — React + FastAPI dashboard for run monitoring and HITL approvals (`gateway/web_server.py`, `ui/index.html`)
  - Run list with live status badges, summary stat cards, auto-refresh
  - 7-stage pipeline visualisation with gate indicators
  - Stage detail panel: Artifact (JSON viewer), Kanban board, Audit timeline, Cost breakdown
  - In-browser HITL Approve / Reject / Amend modal with inline JSON editor
  - Served via `python sdlc.py ui [--port 7474]`
- [x] **Skill library** — pre-built SKILL.md files for REST API, event-driven, microservices patterns
- [x] **Custom agent roles** — YAML-defined roles loaded at startup (`agents/custom_loader.py`)
- [x] **Model provider failover** — `classify_api_error` + `jittered_backoff` + credential pool rotation
- [x] **Context compression** — full context compressor for long-running developer agents (`core/context_compressor.py`)
- [x] **GitHub integration** — `gh_create_pr`, `gh_merge_pr`, `gh_request_review`, `gh_list_prs` via `github` toolset
- [x] **Export command** — `sdlc export <run_id>` writes all DB artifacts to disk

### Remaining
- [ ] **Cloud execution backends** — Modal / AWS Lambda for parallel developer sandboxes

---

## Key Files

```
sdlc-factory/
├── docs/
│   ├── PRODUCT_SPEC.md            ← What we're building and why
│   ├── TECH_ARCHITECTURE.md       ← How the system works (patterns, data model, observability)
│   ├── AGENT_ROLES.md             ← Per-role authority statements, input/output contracts
│   └── IMPLEMENTATION_ROADMAP.md  ← This file
├── core/
│   ├── registry.py                ← Tool self-registration (ToolRegistry singleton)
│   ├── memory.py                  ← 3-tier context + streaming scrubber + injection scanner
│   ├── session.py                 ← SQLite WAL persistence (PipelineDB)
│   ├── agent_base.py              ← BaseSDLCAgent + IterationBudget
│   ├── agent_runtime.py           ← Thin OpenAI-compatible model call + credential pool
│   ├── pipeline.py                ← Stage machine + parallel dispatch + async disk writer
│   └── telemetry.py               ← OpenTelemetry spans with no-op fallback
├── agents/
│   ├── roles.py                   ← All 10 built-in agent role classes
│   └── custom_loader.py           ← YAML custom role loader
├── gateway/
│   ├── hitl.py                    ← HITL gateway (cli / auto / slack / webhook / queue)
│   └── web_server.py              ← FastAPI REST API backend for the Web UI
├── ui/
│   └── index.html                 ← React SPA dashboard (CDN-based, no build step)
├── config/
│   └── pipeline_config.yaml       ← Annotated config template
├── tests/
│   └── test_pipeline_unit.py      ← Unit tests, no API calls needed
├── sdlc.py                        ← CLI entry point
└── pyproject.toml                 ← Package config + optional extras (ui, otel, anthropic, dev)
```
