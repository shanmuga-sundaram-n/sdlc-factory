#!/usr/bin/env python3
"""SDLC Factory — CLI entry point.

Usage:
    python sdlc.py run "Build a FastAPI todo app with user auth"
    python sdlc.py run "Build a SaaS billing platform" --config sdlc_config.yaml
    python sdlc.py resume <run_id>
    python sdlc.py status <run_id>
    python sdlc.py kanban <run_id>
    python sdlc.py approve <run_id> <stage> [--decision APPROVED] [--rationale "..."]
    python sdlc.py audit <run_id>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _load_config(config_path: str) -> dict:
    import yaml
    if not Path(config_path).exists():
        return {}
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Model pricing — ($/1M input tokens, $/1M output tokens)
# ---------------------------------------------------------------------------

_PRICING: dict = {
    "claude-opus-4-8":              (15.0,  75.0),
    "claude-opus-4-7":              (15.0,  75.0),
    "claude-sonnet-4-6":            ( 3.0,  15.0),
    "claude-haiku-4-5":             ( 0.25,  1.25),
    "claude-haiku-4-5-20251001":    ( 0.25,  1.25),
    "gpt-4o":                       ( 2.5,  10.0),
    "gpt-4o-mini":                  ( 0.15,  0.60),
}
_DEFAULT_PRICING = (3.0, 15.0)


def _calc_cost(tokens_in: int, tokens_out: int, model: str) -> float:
    p_in, p_out = _PRICING.get(model, _DEFAULT_PRICING)
    return (tokens_in / 1_000_000) * p_in + (tokens_out / 1_000_000) * p_out


def _print_pipeline_plan(config: dict) -> None:
    from core.pipeline import SDLC_STAGES
    gov = config.get("governance", {})
    gate_cfgs = {g["stage"]: g for g in gov.get("hitl_gates", [])}
    default_mode = gov.get("default_mode", "cli")
    default_model = config.get("pipeline", {}).get("model", "claude-sonnet-4-6")

    print("\n=== SDLC Factory — Dry Run Plan ===\n")
    print(f"  Default model  : {default_model}")
    print(f"  Default gate   : {default_mode}")
    print()
    print(f"  {'Stage':<20} {'Agents':<48} {'Gate'}")
    print("  " + "-" * 82)
    for stage in SDLC_STAGES:
        agents = ", ".join(stage.agent_roles)
        if stage.parallel:
            agents += " (parallel)"
        if stage.hitl_gate:
            gate_mode = gate_cfgs.get(stage.name, {}).get("mode", default_mode)
            gate_str = f"[HITL:{gate_mode}]"
        else:
            gate_str = "—"
        print(f"  {stage.name:<20} {agents:<48} {gate_str}")

    agent_overrides = config.get("agents", {})
    if agent_overrides:
        print("\n  Agent model overrides:")
        for role, cfg in agent_overrides.items():
            if "model" in cfg:
                print(f"    {role}: {cfg['model']}")
    print()


def cmd_run(args: argparse.Namespace) -> int:
    from core.pipeline import SDLCPipeline
    from core.session import get_default_db

    config = _load_config(args.config) if args.config else {}

    # CLI overrides
    if args.model:
        config.setdefault("pipeline", {})["model"] = args.model
    if args.workspace:
        config["workspace"] = args.workspace
    if args.auto_approve:
        config.setdefault("governance", {})["default_mode"] = "auto"
    tenant = getattr(args, "tenant", "default") or "default"
    if tenant != "default":
        config["tenant_id"] = tenant

    if args.dry_run:
        _print_pipeline_plan(config)
        return 0

    db = get_default_db(tenant_id=tenant)
    pipeline = SDLCPipeline.from_dict(config, db=db)
    print(f"\nSDLC Factory — starting pipeline run")
    print(f"Idea: {args.idea}\n")

    result = pipeline.run(args.idea)

    run_id = result.get("run_id", "")
    print(f"\n{'='*60}")
    print(f"Pipeline run completed:")
    print(f"  run_id : {run_id}")
    print(f"  status : {result.get('status')}")
    if result.get("paused_at"):
        print(f"  paused : {result.get('paused_at')}")
        print(f"  reason : {result.get('rationale')}")
        print(f"\nResume:  python sdlc.py resume {run_id}")
        print(f"Approve: python sdlc.py approve {run_id} {result.get('paused_at')}")
    if result.get("status") == "completed":
        print(f"\nCost:    python sdlc.py cost {run_id}")
        print(f"Deliver: python sdlc.py deliver {run_id}")
    print(f"{'='*60}\n")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    from core.pipeline import SDLCPipeline
    from core.session import get_default_db

    config = _load_config(args.config) if args.config else {}
    pipeline = SDLCPipeline.from_dict(config)
    result = pipeline.run("", resume_run_id=args.run_id)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from core.session import get_default_db

    db = get_default_db()
    run = db.get_run(args.run_id)
    if not run:
        print(f"Run {args.run_id} not found", file=sys.stderr)
        return 1

    print(f"\nRun: {args.run_id}")
    print(f"Idea: {run['idea'][:80]}")
    print(f"Status: {run['status']}")
    print(f"Stage: {run.get('current_stage', '—')}")
    print(f"Started: {run.get('started_at', '—')}")
    print(f"Completed: {run.get('completed_at', '—')}")
    return 0


def cmd_kanban(args: argparse.Namespace) -> int:
    from core.session import get_default_db

    db = get_default_db()
    tasks = db.list_tasks(args.run_id)
    if not tasks:
        print(f"No tasks found for run {args.run_id}")
        return 0

    print(f"\nKanban board — run {args.run_id}\n")
    print(f"{'ID':<20} {'Title':<35} {'Assignee':<20} {'Status':<12}")
    print("-" * 90)
    for t in tasks:
        print(f"{t['task_id']:<20} {t['title'][:34]:<35} {t['assignee']:<20} {t['status']:<12}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    from core.pipeline import SDLC_STAGES
    from core.session import get_default_db
    from gateway.hitl import Decision

    db = get_default_db()
    artifact = db.get_artifact(args.run_id, args.stage)
    if not artifact:
        print(f"No artifact found for stage {args.stage} in run {args.run_id}", file=sys.stderr)
        return 1

    decision = Decision(args.decision.upper())
    actor = f"cli:{os.getenv('USER', 'unknown')}"

    # AMENDED: replace artifact content and invalidate downstream stages
    if decision == Decision.AMENDED:
        if not args.amended_artifact:
            print(
                "Error: --amended-artifact JSON is required when --decision AMENDED",
                file=sys.stderr,
            )
            return 1
        try:
            new_artifact = json.loads(args.amended_artifact)
        except json.JSONDecodeError as exc:
            print(f"Error: invalid JSON in --amended-artifact: {exc}", file=sys.stderr)
            return 1
        db.update_artifact(args.run_id, args.stage, new_artifact)
        stage_order = [s.name for s in SDLC_STAGES]
        cleared = db.clear_stages_after(args.run_id, args.stage, stage_order)
        if cleared:
            print(f"Cleared {len(cleared)} downstream stage(s): {', '.join(cleared)}")
    else:
        db.approve_artifact(args.run_id, args.stage, approved_by=actor)

    db.log_decision(args.run_id, args.stage, decision.value, actor=actor, rationale=args.rationale or "")
    print(f"Stage '{args.stage}' marked as {decision.value}")

    if args.resume:
        from core.pipeline import SDLCPipeline
        config = _load_config(args.config) if getattr(args, "config", None) else {}
        pipeline = SDLCPipeline.from_dict(config, db=db)
        result = pipeline.run("", resume_run_id=args.run_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    return 0


def cmd_deliver(args: argparse.Namespace) -> int:
    import subprocess as _sp
    from core.session import get_default_db

    db = get_default_db()
    run = db.get_run(args.run_id)
    if not run:
        print(f"Run {args.run_id} not found", file=sys.stderr)
        return 1
    if run["status"] != "completed":
        print(
            f"Run {args.run_id} is not completed (status={run['status']}). "
            "Complete the pipeline first.",
            file=sys.stderr,
        )
        return 1

    config = json.loads(run.get("config_json") or "{}")
    workspace_base = Path(config.get("workspace", "./output")).expanduser()
    workspace = workspace_base / args.run_id

    delivery = db.get_artifact(args.run_id, "delivery")
    if not delivery:
        print(f"No delivery artifact for run {args.run_id}", file=sys.stderr)
        return 1
    inner = delivery.get("release_manager", delivery)

    version = inner.get("version", "unknown")
    release_tag = inner.get("release_tag") or f"sdlc-factory/{args.run_id}:latest"

    print(f"\nDelivery — run {args.run_id}")
    print(f"  Version     : {version}")
    print(f"  Release tag : {release_tag}")
    print(f"  Workspace   : {workspace}")

    checklist = inner.get("deployment_checklist", [])
    if checklist:
        print(f"\nDeployment checklist ({len(checklist)} items):")
        for item in checklist:
            print(f"  [ ] {item}")

    if not args.no_docker:
        dockerfile = workspace / "Dockerfile"
        if not dockerfile.exists():
            print(
                "\nNo Dockerfile found in workspace — skipping Docker build. "
                "Run the pipeline first to generate DevOps artifacts.",
            )
        else:
            print(f"\nBuilding Docker image {release_tag} ...")
            build_result = _sp.run(
                ["docker", "build", "-t", release_tag, "."],
                cwd=str(workspace),
                capture_output=True,
                text=True,
            )
            if build_result.returncode == 0:
                print(f"  Docker build succeeded: {release_tag}")
                if args.smoke_test:
                    print("  Running smoke test ...")
                    run_result = _sp.run(
                        ["docker", "run", "--rm", release_tag, "--version"],
                        capture_output=True, text=True, timeout=30,
                    )
                    status = "PASS" if run_result.returncode == 0 else "FAIL"
                    print(f"  Smoke test: {status}")
                    if run_result.stdout:
                        print(f"  Output: {run_result.stdout.strip()[:200]}")
            else:
                print(f"  Docker build FAILED (rc={build_result.returncode})")
                print(f"  {build_result.stderr[:500]}")
                return 1

    rollback = inner.get("rollback_procedure", "")
    if rollback:
        print(f"\nRollback procedure:\n  {rollback[:300]}")

    print()
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    from core.session import get_default_db

    db = get_default_db()
    rows = db.get_token_summary(args.run_id)
    if not rows:
        print(f"No token usage data for run {args.run_id}.")
        print("Token data is recorded when the pipeline runs with a live API key.")
        return 0

    col_w = (20, 22, 22, 11, 12, 10)
    header = (
        f"{'Stage':<{col_w[0]}} {'Role':<{col_w[1]}} {'Model':<{col_w[2]}} "
        f"{'Tokens In':>{col_w[3]}} {'Tokens Out':>{col_w[4]}} {'Cost (USD)':>{col_w[5]}}"
    )
    sep = "-" * sum(col_w)
    print(f"\nCost breakdown — run {args.run_id}\n")
    print(header)
    print(sep)

    total_in = total_out = total_cost = 0
    for row in rows:
        model = row.get("model") or ""
        t_in = int(row["tokens_in"])
        t_out = int(row["tokens_out"])
        cost = _calc_cost(t_in, t_out, model)
        total_in += t_in
        total_out += t_out
        total_cost += cost
        print(
            f"{row['stage']:<{col_w[0]}} {row['role']:<{col_w[1]}} {model:<{col_w[2]}} "
            f"{t_in:>{col_w[3]},} {t_out:>{col_w[4]},} ${cost:>{col_w[5]-1}.4f}"
        )

    print(sep)
    print(
        f"{'TOTAL':<{col_w[0]+col_w[1]+col_w[2]+1}} "
        f"{total_in:>{col_w[3]},} {total_out:>{col_w[4]},} ${total_cost:>{col_w[5]-1}.4f}"
    )
    print()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Dump all stage artifacts from the DB to output/<run_id>/artifacts/<stage>.json."""
    from core.session import get_default_db
    from core.pipeline import SDLC_STAGES

    db = get_default_db()
    run = db.get_run(args.run_id)
    if not run:
        print(f"Run {args.run_id} not found", file=sys.stderr)
        return 1

    config = json.loads(run.get("config_json") or "{}")
    workspace_base = Path(config.get("workspace", "./output")).expanduser()
    artifacts_dir = workspace_base / args.run_id / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for stage in SDLC_STAGES:
        artifact = db.get_artifact(args.run_id, stage.name)
        if artifact is None:
            continue
        dest = artifacts_dir / f"{stage.name}.json"
        dest.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append((stage.name, dest))

    if not written:
        print(f"No artifacts found in DB for run {args.run_id}")
        return 0

    print(f"\nExported {len(written)} artifact(s) to {artifacts_dir}\n")
    for stage_name, path in written:
        print(f"  {stage_name:<20} → {path.name}")
    print()
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "Web UI requires the 'ui' extra. Install it with:\n"
            "  uv sync --extra ui",
            file=sys.stderr,
        )
        return 1

    print(f"\nSDLC Factory UI → http://{args.host}:{args.port}\n")
    uvicorn.run(
        "gateway.web_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from core.session import get_default_db

    db = get_default_db()
    log = db.get_audit_log(args.run_id)
    if not log:
        print(f"No audit log entries for run {args.run_id}")
        return 0

    print(f"\nAudit log — run {args.run_id}\n")
    for entry in log:
        print(
            f"[{entry['timestamp']}] {entry['stage']:<20} {entry['decision']:<10} "
            f"by {entry['actor']} — {entry['rationale']}"
        )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from core.session import get_default_db

    tenant = getattr(args, "tenant", "default") or "default"
    db = get_default_db(tenant_id=tenant)
    runs = db.list_runs(limit=args.limit)
    if not runs:
        print("No pipeline runs found")
        return 0

    print(f"\n{'Run ID':<20} {'Status':<12} {'Stage':<20} {'Idea'}")
    print("-" * 90)
    for r in runs:
        idea = (r.get("idea") or "")[:40]
        print(f"{r['run_id']:<20} {r['status']:<12} {r.get('current_stage', '—'):<20} {idea}")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SDLC Factory — Autonomous AI software delivery pipeline"
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--tenant", default="default",
        help="Tenant ID — isolates DB records and loads ~/.sdlc-factory/tenants/<id>.env",
    )
    sub = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Start a new pipeline run")
    p_run.add_argument("idea", help="Plain-English description of what to build")
    p_run.add_argument("--config", "-c", help="Path to sdlc_config.yaml")
    p_run.add_argument("--model", help="Override the default model")
    p_run.add_argument("--workspace", "-w", help="Override the output workspace directory")
    p_run.add_argument("--auto-approve", action="store_true",
                       help="Auto-approve all HITL gates (non-interactive)")
    p_run.add_argument("--dry-run", action="store_true",
                       help="Print the pipeline plan and exit without making API calls")

    # resume
    p_resume = sub.add_parser("resume", help="Resume a paused or failed run")
    p_resume.add_argument("run_id")
    p_resume.add_argument("--config", "-c")

    # status
    p_status = sub.add_parser("status", help="Show run status")
    p_status.add_argument("run_id")

    # kanban
    p_kanban = sub.add_parser("kanban", help="Show the kanban task board for a run")
    p_kanban.add_argument("run_id")

    # approve
    p_approve = sub.add_parser("approve", help="Approve, reject, or amend a stage from the CLI")
    p_approve.add_argument("run_id")
    p_approve.add_argument("stage")
    p_approve.add_argument("--decision", default="APPROVED",
                           choices=["APPROVED", "REJECTED", "AMENDED"])
    p_approve.add_argument("--rationale", default="")
    p_approve.add_argument(
        "--amended-artifact",
        metavar="JSON",
        default=None,
        help="New artifact JSON string (required when --decision AMENDED)",
    )
    p_approve.add_argument(
        "--resume", action="store_true",
        help="Resume the pipeline immediately after approving",
    )
    p_approve.add_argument("--config", "-c", default=None,
                           help="Config file to use when --resume is set")

    # deliver
    p_deliver = sub.add_parser("deliver", help="Build Docker image and print deployment checklist")
    p_deliver.add_argument("run_id")
    p_deliver.add_argument("--no-docker", action="store_true",
                           help="Skip Docker build (print checklist only)")
    p_deliver.add_argument("--smoke-test", action="store_true",
                           help="Run a quick smoke test after the Docker build")

    # cost
    p_cost = sub.add_parser("cost", help="Show per-stage token usage and estimated cost")
    p_cost.add_argument("run_id")

    # ui
    p_ui = sub.add_parser("ui", help="Launch the web dashboard")
    p_ui.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_ui.add_argument("--port", type=int, default=7474, help="Port (default: 7474)")
    p_ui.add_argument("--reload", action="store_true", help="Enable auto-reload (dev mode)")

    # export
    p_export = sub.add_parser("export", help="Write all DB artifacts for a run to disk as JSON")
    p_export.add_argument("run_id")

    # audit
    p_audit = sub.add_parser("audit", help="Show the audit log for a run")
    p_audit.add_argument("run_id")

    # list
    p_list = sub.add_parser("list", help="List recent pipeline runs")
    p_list.add_argument("--limit", type=int, default=20)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    _setup_logging(verbose=args.verbose)

    commands = {
        "run": cmd_run,
        "resume": cmd_resume,
        "status": cmd_status,
        "kanban": cmd_kanban,
        "approve": cmd_approve,
        "deliver": cmd_deliver,
        "cost": cmd_cost,
        "ui": cmd_ui,
        "export": cmd_export,
        "audit": cmd_audit,
        "list": cmd_list,
    }

    if args.command is None:
        parser.print_help()
        return 0

    handler = commands.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
