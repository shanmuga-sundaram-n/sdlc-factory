# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

SDLC Factory is an autonomous multi-agent software delivery pipeline. Given a plain-English idea, it runs a sequence of AI specialist agents (domain expert → product owner → BA → architect → tech lead → developers → QA → release manager) and produces a complete software delivery artifact at each stage. Human-in-the-loop (HITL) gates pause the pipeline for approval at key stages.

## Docs reference

Use these docs for high-level context before making structural changes:

| Doc | When to read it |
|---|---|
| `docs/PRODUCT_SPEC.md` | Understanding the vision, NFRs, user stories, risk register, and full config model. Read before changing pipeline scope or adding major features. |
| `docs/TECH_ARCHITECTURE.md` | Deep-dive on every design pattern (stage machine, parallel dispatch, 3-tier memory, HITL governance, security model, observability, provider strategy, full data model SQL). Read before touching `core/`. |
| `docs/AGENT_ROLES.md` | Exact authority statements, input/output artifact schemas, and toolset specs for all 10 roles. Read before adding or modifying any agent role — the schemas there are the authoritative contracts, not the code. |
| `docs/IMPLEMENTATION_ROADMAP.md` | What is built vs. planned. Read before assuming a feature exists. |
| `docs/TECHNICAL_GUIDE.md` | Comprehensive engineering reference: all components, data flows, security model, observability, Web UI, configuration, and extension guide. Start here for a full system understanding. |

## Commands

```bash
# Package management (uses uv)
uv sync                        # install all deps including dev extras
uv sync --extra anthropic      # also install anthropic SDK

# Run the pipeline
python sdlc.py run "Build a FastAPI todo app with user auth"
python sdlc.py run "Build a SaaS billing platform" --config sdlc_config.yaml --auto-approve
python sdlc.py resume <run_id>
python sdlc.py status <run_id>
python sdlc.py kanban <run_id>
python sdlc.py approve <run_id> <stage> --decision APPROVED --rationale "looks good"
python sdlc.py audit <run_id>
python sdlc.py list

# Tests
uv run pytest                  # all tests
uv run pytest tests/test_pipeline_unit.py::TestPipelineDB  # single class
uv run pytest -k "test_quality_gate"                        # by keyword

# Lint / format
uv run ruff check .
uv run black .
```

## Required environment variables

```bash
OPENAI_API_KEY=...             # or ANTHROPIC_API_KEY
OPENAI_BASE_URL=...            # optional — point at any OpenAI-compatible endpoint
                               # (e.g. Anthropic's API with the right base URL)
SLACK_BOT_TOKEN=...            # optional — only for Slack HITL mode
HITL_WEBHOOK_URL=...           # optional — only for webhook HITL mode
```

The SQLite database is written to `~/.sdlc-factory/pipeline.db` by default. Generated code and artifacts land in `./output/<run_id>/`.

## Architecture

### Stage machine (`core/pipeline.py`)

`SDLCPipeline.run(idea)` drives a linear sequence of `StageDefinition` objects (`SDLC_STAGES`). Each stage specifies which agent roles to invoke, whether they run in parallel, and whether a HITL gate follows. Development stage (`development`) fans out to three developer agents via `ThreadPoolExecutor`. Rejected gates pause the run (`status=paused`); the caller resumes with `pipeline.run("", resume_run_id=...)`.

### Agent model (`core/agent_base.py`)

`BaseSDLCAgent` wraps the OpenAI-compatible client. Subclasses declare `ROLE`, `OUTPUT_SCHEMA` (JSON Schema), `ENABLED_TOOLSETS`, `SKILL_MD_PATH`, and `prior_stages()`. The base class:
1. Assembles the system prompt (identity + SKILL.md + memory context).
2. Runs a tool-calling loop (`_run_loop`) respecting an `IterationBudget`.
3. Extracts the final ` ```json ``` ` artifact from the model's last message.

Agents never call each other directly — they communicate only via `ArtifactBundle`.

### All built-in roles (`agents/roles.py`)

| Role key | Class | Stage |
|---|---|---|
| `domain_expert` | `DomainExpertAgent` | discovery |
| `product_owner` | `ProductOwnerAgent` | discovery |
| `business_analyst` | `BusinessAnalystAgent` | requirements |
| `architect` | `ArchitectAgent` | architecture |
| `tech_lead` | `TechLeadAgent` | planning |
| `developer_backend` | `DeveloperBackendAgent` | development (parallel) |
| `developer_ui` | `DeveloperUIAgent` | development (parallel) |
| `developer_devops` | `DeveloperDevOpsAgent` | development (parallel) |
| `qa_engineer` | `QAEngineerAgent` | quality_gate |
| `release_manager` | `ReleaseManagerAgent` | delivery |

`AGENT_ROLE_MAP` at the bottom of that file is the authoritative mapping used by `pipeline._register_builtin_agents()`.

### Three-tier context system (`core/memory.py`)

- **Tier 1 — `ProjectMemory`**: shared `PROJECT_MEMORY.md` in the workspace; injected into every agent's system prompt.
- **Tier 2 — `ArtifactBundle`**: accumulated stage artifacts (JSON); each agent receives only artifacts from `prior_stages()`.
- **Tier 3 — Kanban blackboard**: ephemeral task comments in SQLite, used within the parallel development stage.

Context is injected inside `<stage-context>…</stage-context>` fence tags. `StreamingContextScrubber` strips these from model output to prevent leakage.

### State store (`core/session.py`)

`PipelineDB` is a thread-safe SQLite wrapper (WAL mode, jittered-backoff retry). Tables: `pipeline_runs`, `stage_artifacts` (with FTS5 virtual table), `audit_log`, `kanban_tasks`, `kanban_events`. The singleton is accessed via `get_default_db()`.

### Model call (`core/agent_runtime.py`)

`call_model_openai()` is the only place that calls the LLM. It uses the `openai` SDK with a lazily initialised client (saves ~240ms cold start). The `base_url` parameter makes it compatible with any OpenAI-compatible provider. `call_model_with_retry()` wraps it with exponential backoff.

### HITL gateway (`gateway/hitl.py`)

`HITLGateway.request_approval()` supports five modes: `cli` (interactive terminal), `auto` (rule-based via `AutoEvaluator`), `slack` (reaction polling), `webhook` (POST + callback), `queue` (programmatic, blocks on `ApprovalQueue`). The `auto` mode has two built-in rules: quality gate coverage threshold, and max open questions on requirements.

Gate decisions: `APPROVED` advances the pipeline, `REJECTED` pauses it, `AMENDED` mutates the artifact in place and re-persists it before advancing — downstream stages then receive the amended version. Every decision is written to `audit_log`.

### Tool registry (`core/registry.py`)

`ToolRegistry` is a thread-safe singleton (`registry`). Tool modules in `tools/` call `registry.register()` at import time. `discover_tools()` imports them all. Each `ToolEntry` has a `check_fn` (TTL-cached, 30s) that can gate availability at runtime.

## What is not yet implemented

The `tools/` directory contains only `__init__.py`. All tool modules referenced by agents (`file_tools.py`, `terminal_tool.py`, `web_tools.py`, `kanban_tools.py`, etc.) are Phase 2 work per `docs/IMPLEMENTATION_ROADMAP.md`. The pipeline scaffold and tests run without them, but agents will have empty toolsets until these are written.

Similarly, `skills/<role-name>/SKILL.md` files (e.g. `skills/architect/SKILL.md`) are referenced by every role via `SKILL_MD_PATH` but do not exist yet. `BaseSDLCAgent.skill_md()` silently returns `""` if the file is missing, so the pipeline still runs.

## Model recommendations

From `docs/TECH_ARCHITECTURE.md` — use these defaults when configuring `agents:` overrides:

| Role(s) | Recommended model | Reason |
|---|---|---|
| `architect` | `claude-opus-4-7` | Complex reasoning, ADR quality |
| All others | `claude-sonnet-4-6` | Code/text generation, cost/quality balance |
| Compression auxiliary | `claude-haiku-4-5` | Fast, cheap context summarisation |

## Security constraints

Agents are confined to the `output/<run_id>/` workspace for all file writes. Developer agents have `clarify`, `send_message`, and `delegate_task` blocked unconditionally (`AGENT_BLOCKED_TOOLS_DEFAULT` in `core/agent_base.py`). Prompt injection scanning of the idea string is planned for Phase 2 (see `docs/TECH_ARCHITECTURE.md` §Security Model).

## Adding a new agent role

Read `docs/AGENT_ROLES.md` for the authority statement, input/output contract, and toolset for each existing role before adding a new one.

1. Subclass `BaseSDLCAgent` in `agents/roles.py`.
2. Set `ROLE`, `ROLE_DISPLAY`, `OUTPUT_SCHEMA`, `ENABLED_TOOLSETS`, and override `prior_stages()`.
3. Create `skills/<role-name>/SKILL.md` with the role's instructions.
4. Add the role to `AGENT_ROLE_MAP`.
5. Reference the role string in a `StageDefinition` in `SDLC_STAGES` (`core/pipeline.py`).

## Adding a new tool

Create `tools/<toolname>.py`. At module level, call:
```python
from core.registry import registry
registry.register(name="my_tool", handler=my_fn, schema={...}, toolset="my_toolset")
```
`discover_tools()` picks it up automatically. Import errors are logged as warnings and do not crash startup.

## Configuration

`config/pipeline_config.yaml` is the reference configuration. Copy it to `sdlc_config.yaml` and pass `--config sdlc_config.yaml`. Per-role model overrides, toolset whitelists, HITL gate modes, parallel concurrency, context compression settings, and observability options are all configured there. See `docs/PRODUCT_SPEC.md` §Configuration Model for the full annotated schema.
