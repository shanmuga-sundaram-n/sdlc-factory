# SDLC Factory — Technical Architecture

## Layer Map

```mermaid
flowchart TB
    subgraph Entry["Entry Points"]
        CLI["CLI\nsdlc.py"]
        API["Web API\ngateway/web_server.py"]
        WH["Webhook\nHITL responses"]
        QUE["Programmatic\nApprovalQueue"]
    end

    subgraph Orch["Pipeline Orchestrator  core/pipeline.py"]
        SM["Stage machine → artifact routing\ngate evaluation → checkpoint/resume\nasync disk writer"]
    end

    subgraph AgentL["Agent Layer  agents/"]
        BASE["BaseSDLCAgent subclasses per role\nSKILL.md system prompt  ·  scoped toolset\nartifact producer"]
    end

    subgraph Runtime["Agent Runtime  core/agent_base.py + agent_runtime.py"]
        LOOP["Model call loop  ·  tool dispatch\ncontext compression  ·  IterationBudget  ·  OTel spans"]
    end

    subgraph Tools["Tool Pipeline  tools/"]
        TR["Self-registering modules\nTTL-cached availability checks"]
    end

    subgraph Mem["Shared Memory  core/memory.py"]
        PM["ProjectMemory Tier 1\nArtifactBundle Tier 2\nKanban blackboard Tier 3\nStreamingContextScrubber\nPrompt injection scanner"]
    end

    subgraph DB["Persistence  core/session.py — SQLite WAL"]
        TABLES["pipeline_runs  ·  stage_artifacts  ·  audit_log\nkanban_tasks  ·  kanban_events  ·  artifact_fts FTS5"]
    end

    subgraph GW["HITL Gateway  gateway/hitl.py"]
        MODES["cli  ·  auto  ·  slack  ·  webhook  ·  queue\nApprovalQueue  ·  audit record"]
    end

    subgraph Tel["Telemetry  core/telemetry.py"]
        OT["OpenTelemetry spans\nno-op fallback"]
    end

    Entry --> Orch
    Orch  --> AgentL
    AgentL --> Runtime
    Runtime --> Tools
    Runtime --> Mem
    Mem --> DB
    Orch --> GW
    Runtime --> Tel
```

---

## Core Design Patterns

### 1. Agent-as-Role: the BaseSDLCAgent

Each specialist role is a thin subclass of `BaseSDLCAgent`. The base class adds:

- **Role identity** — a structured system prompt built from `SKILL.md` plus the
  role's authority statement and output contract.
- **Artifact contract** — a Pydantic model for what the agent must produce. The
  runtime validates the structured output before passing it to the next stage.
- **Scoped toolset** — read-only roles (Architect, BA) cannot call `terminal`;
  Developer roles have write access. Enforced at schema level.
- **Iteration budget** — each role has a configurable `max_iterations` cap via
  `IterationBudget`. Developer agents default 30; Architect defaults 50 (needs
  more reasoning turns).

```mermaid
classDiagram
    class BaseSDLCAgent {
        +ROLE str
        +ROLE_DISPLAY str
        +OUTPUT_SCHEMA dict
        +ENABLED_TOOLSETS list
        +SKILL_MD_PATH str
        +run(idea, bundle, run_id, stage, config)
        +prior_stages() list
        -_run_loop(system_prompt, messages, tools, budget)
        -skill_md() str
    }

    class DomainExpertAgent { toolset: web_search, read_file, memory }
    class ProductOwnerAgent { toolset: read_file, memory, write_file }
    class BusinessAnalystAgent { toolset: read_file, memory, write_file }
    class ArchitectAgent { toolset: read_file, web_search, write_file, memory }
    class TechLeadAgent { toolset: read_file, write_file, memory, kanban_create }
    class DeveloperBackendAgent { toolset: full_stack }
    class DeveloperUIAgent { toolset: full_stack + browser }
    class DeveloperDevOpsAgent { toolset: full_stack + terminal }
    class QAEngineerAgent { toolset: read_file, terminal, execute_code, write_file }
    class ReleaseManagerAgent { toolset: read_file, write_file, terminal }

    BaseSDLCAgent <|-- DomainExpertAgent
    BaseSDLCAgent <|-- ProductOwnerAgent
    BaseSDLCAgent <|-- BusinessAnalystAgent
    BaseSDLCAgent <|-- ArchitectAgent
    BaseSDLCAgent <|-- TechLeadAgent
    BaseSDLCAgent <|-- DeveloperBackendAgent
    BaseSDLCAgent <|-- DeveloperUIAgent
    BaseSDLCAgent <|-- DeveloperDevOpsAgent
    BaseSDLCAgent <|-- QAEngineerAgent
    BaseSDLCAgent <|-- ReleaseManagerAgent
```

### 2. Pipeline Orchestrator: Stage Machine

The pipeline is a **deterministic stage machine** with checkpoint semantics:

```mermaid
flowchart TD
    I([💡 Idea]) --> D0

    subgraph D0["discovery\ndomain_expert + product_owner"]
    end
    subgraph D1["requirements\nbusiness_analyst"]
    end
    subgraph D2["architecture\narchitect"]
    end
    subgraph D3["planning\ntech_lead"]
    end
    subgraph D4["development  ⟨parallel=true⟩\ndeveloper_backend · developer_ui · developer_devops"]
    end
    subgraph D5["quality_gate\nqa_engineer"]
    end
    subgraph D6["delivery\nrelease_manager"]
    end

    D0 --> D1
    D1 --> G1{{"⏸ HITL gate"}}
    G1 -->|APPROVED| D2
    D2 --> G2{{"⏸ HITL gate"}}
    G2 -->|APPROVED| D3
    D3 --> D4
    D4 --> D5
    D5 --> G3{{"⏸ HITL gate"}}
    G3 -->|APPROVED| D6
    D6 --> G4{{"⏸ HITL gate"}}
    G4 -->|APPROVED| DONE([✅ Completed])
```

Each stage:
1. Receives the **accumulated artifact bundle** from prior stages (not raw
   conversation history — preserves context budget).
2. Runs its agent(s). Parallel stages use `ThreadPoolExecutor` with
   `max_concurrent_developers` workers.
3. Validates the structured output against the stage's artifact schema.
4. Evaluates the HITL gate (see below).
5. Persists the artifact + gate outcome to SQLite.
6. Advances state machine or halts for human input.

**Checkpoint resume**: on restart, the pipeline reads the last completed stage
from the DB and resumes from the next stage with the persisted artifact bundle.

### 3. Parallel Developer Dispatch: Kanban Swarm Topology

The development stage uses a swarm topology:

```mermaid
flowchart TD
    TL["TechLeadAgent\nPlanning stage"] -->|creates task cards| KB[(Kanban Board)]

    KB --> POOL["ThreadPoolExecutor\nmax_workers = max_concurrent_developers"]

    POOL --> B["DeveloperBackendAgent\nAPI task cards  ⟨Thread 1⟩"]
    POOL --> U["DeveloperUIAgent\nUI task cards  ⟨Thread 2⟩"]
    POOL --> D["DeveloperDevOpsAgent\nIaC task card  ⟨Thread 3⟩"]

    B -->|kanban_complete| KB
    U -->|kanban_complete| KB
    D -->|kanban_complete| KB

    B & U & D --> JOIN["All threads joined\nDevelopment artifact merged"]

    JOIN --> QA["QAEngineerAgent\n⟨blocked until all done⟩"]
    QA --> RM["ReleaseManagerAgent\n⟨blocked until QA done⟩"]
```

Each developer agent:
- Gets a **fresh message history** (no sibling context leak).
- Has its **own terminal session** (task_id scoped).
- Writes code to `output/src/<task_id>/`.
- Signals completion via `kanban_complete` with a metadata blob
  `{files_written: [...], tests_passing: bool, coverage: float}`.
- Parent dispatcher polls the board and triggers the verifier when all ready
  tasks reach `done` status.

### 4. Shared Memory: Three-Tier Context

```mermaid
graph LR
    subgraph T1["Tier 1 — Project Glossary"]
        PM["PROJECT_MEMORY.md\nWritten by Domain Expert\nRead by ALL agents\nInjected into system prompt"]
    end

    subgraph T2["Tier 2 — Stage Artifacts"]
        AB["SQLite stage_artifacts\nPer-stage structured JSON\nEach agent receives only\nprior_stages() artifacts\nInjected as &lt;stage-context&gt;"]
        SC["StreamingContextScrubber\nStrips fence tags from output\nnever shown to the user"]
        AB --> SC
    end

    subgraph T3["Tier 3 — Kanban Blackboard"]
        KB["kanban_tasks + kanban_events\nEphemeral per run\nDeveloper agents post facts\nVerifier builds integration picture\nDiscarded after run completes"]
    end

    T1 -->|system prompt| AG([Agents])
    T2 -->|context block| AG
    T3 -->|task metadata| AG
```

### 5. HITL Governance

```mermaid
flowchart TD
    P[Pipeline] --> GATE["PipelineGate.evaluate\nstage, artifact"]

    GATE --> M{Gate mode}
    M -->|auto|    AE["AutoEvaluator\nCheck rule set\ncoverage threshold\nopen questions"]
    M -->|cli|     CL["Print artifact summary\nRead stdin y/n/edit"]
    M -->|slack|   SL["POST to channel\nPoll for reaction / reply"]
    M -->|webhook| WH["POST to URL\nPoll for response"]
    M -->|queue|   QU["Block on ApprovalQueue\nprogrammatic API"]

    AE & CL & SL & WH & QU --> DEC{Decision}

    DEC -->|APPROVED| ADV["Advance pipeline"]
    DEC -->|REJECTED| HALT["Halt run\nstatus = paused\nSurface to operator"]
    DEC -->|AMENDED|  AMD["Apply human edits to artifact\nClear downstream stages\nPartial re-run from this point"]

    ADV --> LOG[("audit_log\ntimestamp · actor · rationale")]
    HALT --> LOG
    AMD --> LOG
```

Outcomes are written to the `audit_log` table in SQLite:
`(pipeline_run_id, stage, decision, actor, timestamp, rationale)`.

### 6. Tool Registry: Self-Registration

Each tool module self-registers at import time:

```python
from core.registry import registry

registry.register(
    name="generate_requirements_doc",
    handler=generate_requirements_doc,
    schema={...},               # JSON Schema
    toolset="business_analyst",
    check_fn=lambda: True,
    emoji="📋",
)
```

`PipelineOrchestrator` calls `registry.get_definitions(enabled_toolsets=[...])`
to build the tool schema for each agent's role. Availability checks are
Availability checks are TTL-cached (30s) to avoid blocking the hot path.

### 7. Context Compression

Each agent's `ContextCompressor`:

- Triggers when token estimate > 80% of model context limit.
- Runs a cheap auxiliary model call to summarise middle turns.
- Protects the most recent 2,000 tokens (tail).
- Injects a structured `[STAGE COMPACTION]` prefix.
- Structured template tracks: `Resolved Requirements`, `Open Questions`,
  `Code Written`, `Remaining Work`, `Active Task`.

### 8. Security Model

| Layer | Mechanism |
|---|---|
| Input scanning | `scan_for_injection()` regex set applied before injecting idea/requirements |
| Path traversal | `validate_within_dir()` on all file writes; agents confined to `output/` workspace |
| Credential isolation | All API keys loaded from env; never written to artifact files |
| Subagent isolation | Developer agents cannot call `send_message`, `memory` (write), `delegate_task` (recursive) |
| Audit log | Every HITL decision, every tool call above risk threshold, logged with timestamp + actor |
| PII in logs | sender/user IDs SHA-256 hashed before log emission |
| Prompt injection | Idea text scanned via `scan_for_injection()` before first injection |

---

## Data Model (SQLite)

```sql
-- Pipeline runs
CREATE TABLE pipeline_runs (
    run_id       TEXT PRIMARY KEY,
    idea         TEXT NOT NULL,
    config_json  TEXT,
    status       TEXT DEFAULT 'running',  -- running | completed | failed | paused
    started_at   TEXT,
    completed_at TEXT,
    SCHEMA_VERSION INTEGER DEFAULT 1
);

-- Stage artifacts — one row per stage per run
CREATE TABLE stage_artifacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT REFERENCES pipeline_runs(run_id),
    stage        TEXT NOT NULL,           -- discovery | requirements | ...
    artifact_json TEXT NOT NULL,          -- structured output from the stage agents
    status       TEXT DEFAULT 'pending',  -- pending | approved | rejected | amended
    created_at   TEXT,
    approved_at  TEXT,
    approved_by  TEXT
);

-- HITL audit log
CREATE TABLE audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT,
    stage        TEXT,
    decision     TEXT,         -- APPROVED | REJECTED | AMENDED
    actor        TEXT,         -- cli:<user> | slack:<user> | auto
    rationale    TEXT,
    timestamp    TEXT
);

-- Kanban tasks for the development stage
CREATE TABLE kanban_tasks (
    task_id      TEXT PRIMARY KEY,
    run_id       TEXT,
    title        TEXT,
    assignee     TEXT,          -- developer_ui | developer_backend | developer_devops
    status       TEXT,          -- ready | in_progress | done | blocked
    metadata_json TEXT,
    created_at   TEXT,
    completed_at TEXT
);

-- Full-text search on artifacts
CREATE VIRTUAL TABLE artifact_fts USING fts5(
    run_id,
    stage,
    content,
    content='stage_artifacts',
    content_rowid='id'
);
```

---

## Message Protocol Between Agents

Agents do not call each other directly. All communication is mediated by the
pipeline's **artifact bundle** — a JSON dict accumulated across stages:

```json
{
  "run_id": "abc123",
  "idea": "Build a SaaS todo app with team sharing",
  "discovery": {
    "glossary": {"todo": "...", "team": "..."},
    "bounded_contexts": [...],
    "user_stories": [...],
    "mvp_scope": "..."
  },
  "requirements": {
    "brs_markdown": "...",
    "data_dictionary": [...],
    "process_flows": [...]
  },
  "architecture": {
    "adrs": [...],
    "c4_context": "...",
    "c4_container": "...",
    "tech_stack": {"backend": "FastAPI", "db": "PostgreSQL", ...}
  },
  "planning": {
    "tasks": [
      {"task_id": "t1", "title": "User auth API", "assignee": "developer_backend", ...},
      {"task_id": "t2", "title": "Login UI component", "assignee": "developer_ui", ...}
    ],
    "coding_standards": "..."
  },
  "development": {
    "t1": {"files": ["src/api/auth.py"], "tests_passing": true, "coverage": 87},
    "t2": {"files": ["src/ui/Login.tsx"], "tests_passing": true, "coverage": 92}
  },
  "quality_gate": {
    "test_plan": "...",
    "coverage_pct": 88,
    "bugs": [],
    "gate_passed": true
  }
}
```

Each agent receives this bundle injected as a `<stage-context>` block in its
system prompt, stripped from display by the streaming scrubber.

---

## Provider Strategy

| Role | Recommended model | Reason |
|---|---|---|
| DomainExpert, PO, BA | claude-sonnet-4-6 | Fast, good at structured text |
| Architect | claude-opus-4-7 | Complex reasoning, ADR quality |
| TechLead | claude-sonnet-4-6 | Good at task decomposition |
| Developer agents | claude-sonnet-4-6 | Code generation quality/cost |
| QA | claude-sonnet-4-6 | Test generation |
| Auxiliary (compressor) | claude-haiku-4-5 | Fast, cheap, context summarisation |

All models accessed via OpenAI-compatible interface (`base_url` + API key).
Provider failover uses `classify_api_error` + `jittered_backoff` retry logic.

---

## Observability

Every agent turn emits a structured JSON log line to `stdout` (see §14 of the Technical Guide for the full schema).

**OpenTelemetry span hierarchy** (optional, graceful no-op fallback):

```mermaid
graph TD
    PR["sdlc.pipeline.run\nrun_id · idea · outcome"]

    PR --> SD["sdlc.stage.discovery\nstage · parallel · outcome"]
    PR --> SR["sdlc.stage.requirements"]
    PR --> SA["sdlc.stage.architecture"]
    PR --> SP["sdlc.stage.planning"]
    PR --> SV["sdlc.stage.development\nparallel = true"]
    PR --> SQ["sdlc.stage.quality_gate"]
    PR --> SL["sdlc.stage.delivery"]

    SD --> ADE["sdlc.agent.domain_expert\nrole · agent_id"]
    SD --> APO["sdlc.agent.product_owner"]
    SR --> ABA["sdlc.agent.business_analyst"]
    SA --> AAR["sdlc.agent.architect"]
    SP --> ATL["sdlc.agent.tech_lead"]
    SV --> ABE["sdlc.agent.developer_backend"]
    SV --> AUI["sdlc.agent.developer_ui"]
    SV --> ADO["sdlc.agent.developer_devops"]
    SQ --> AQA["sdlc.agent.qa_engineer"]
    SL --> ARM["sdlc.agent.release_manager"]
```

---

## Extensibility: Adding a New Agent Role

1. Create `agents/security_reviewer.py` with a `SecurityReviewerAgent(BaseSDLCAgent)` subclass.
2. Create `skills/security-reviewer/SKILL.md` with role instructions.
3. Add a `Stage("security_review", agents=["security_reviewer"])` to `STAGES`.
4. Register any new tools in `tools/security_tool.py` via `registry.register(...)`.
5. Zero changes to core pipeline, registry, or memory system.

---

## Tech Stack

| Component | Choice | Reason |
|---|---|---|
| Language | Python 3.11+ | Rich async + threading; broad library support |
| LLM client | openai SDK (compatibility layer) | Provider-agnostic; works with any OpenAI-compatible endpoint |
| Persistence | SQLite WAL | Zero-dependency; FTS5 for artifact search |
| Concurrency | threading + ThreadPoolExecutor | Simpler than asyncio for CPU-bound tools |
| Config | YAML via PyYAML | Human-editable |
| Package | uv + pyproject.toml | Fast installs; reproducible environments |
| Testing | pytest + pytest-asyncio | Standard |
| CI | GitHub Actions | Standard workflow templates |
