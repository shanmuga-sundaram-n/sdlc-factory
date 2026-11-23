"""Web UI backend — FastAPI REST API for the SDLC Factory dashboard.

Serves the React single-page app at / and exposes read/write API endpoints
that mirror the existing CLI commands. No auth — intended for local dev use.

Start with:
    python sdlc.py ui [--port 7474] [--host 127.0.0.1]
Or directly:
    uvicorn gateway.web_server:app --port 7474
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_UI_DIR = Path(__file__).parent.parent / "ui"

# ---------------------------------------------------------------------------
# Lazy FastAPI import — keeps startup fast when ui extra is not installed
# ---------------------------------------------------------------------------

def _get_app():
    try:
        from fastapi import FastAPI, HTTPException, Body
        from fastapi.responses import HTMLResponse
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError as exc:
        raise RuntimeError(
            "Web UI requires the 'ui' extra: uv sync --extra ui"
        ) from exc

    from core.session import get_default_db
    from core.pipeline import SDLC_STAGES

    _STAGE_NAMES = [s.name for s in SDLC_STAGES]
    _STAGE_META = {
        s.name: {"parallel": s.parallel, "hitl_gate": s.hitl_gate}
        for s in SDLC_STAGES
    }

    app = FastAPI(title="SDLC Factory", version="0.1.0", docs_url="/api/docs")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _db():
        return get_default_db()

    def _calc_cost(tokens_in: int, tokens_out: int, model: str) -> float:
        pricing = {
            "claude-opus-4-8":   (15.0, 75.0),
            "claude-opus-4-7":   (15.0, 75.0),
            "claude-sonnet-4-6": (3.0,  15.0),
            "claude-haiku-4-5":  (0.25,  1.25),
            "gpt-4o":            (2.5,  10.0),
            "gpt-4o-mini":       (0.15,  0.60),
        }
        p_in, p_out = pricing.get(model, (3.0, 15.0))
        return (tokens_in / 1_000_000) * p_in + (tokens_out / 1_000_000) * p_out

    def _stage_statuses(run_id: str) -> List[Dict[str, Any]]:
        db = _db()
        conn = db._conn()
        rows = conn.execute(
            "SELECT stage, status, created_at, approved_at, approved_by "
            "FROM stage_artifacts WHERE run_id=? ORDER BY created_at",
            (run_id,),
        ).fetchall()
        completed = {r["stage"]: dict(r) for r in rows}

        run = db.get_run(run_id)
        current = run.get("current_stage") if run else None

        stages = []
        for name in _STAGE_NAMES:
            rec = completed.get(name)
            if rec:
                stage_status = rec["status"]
            elif name == current and run and run.get("status") == "running":
                stage_status = "running"
            else:
                stage_status = "pending"

            stages.append({
                "name": name,
                "status": stage_status,
                "parallel": _STAGE_META[name]["parallel"],
                "hitl_gate": _STAGE_META[name]["hitl_gate"],
                "created_at": rec["created_at"] if rec else None,
                "approved_at": rec["approved_at"] if rec else None,
                "approved_by": rec["approved_by"] if rec else None,
            })
        return stages

    # ------------------------------------------------------------------
    # Run endpoints
    # ------------------------------------------------------------------

    @app.get("/api/runs")
    def list_runs(limit: int = 50) -> List[Dict[str, Any]]:
        return _db().list_runs(limit=limit)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> Dict[str, Any]:
        run = _db().get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        run["stages"] = _stage_statuses(run_id)
        return run

    @app.get("/api/runs/{run_id}/stages")
    def get_stages(run_id: str) -> List[Dict[str, Any]]:
        if not _db().get_run(run_id):
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        return _stage_statuses(run_id)

    @app.get("/api/runs/{run_id}/artifacts/{stage}")
    def get_artifact(run_id: str, stage: str) -> Any:
        artifact = _db().get_artifact(run_id, stage)
        if artifact is None:
            raise HTTPException(status_code=404, detail=f"No artifact for stage {stage}")
        return artifact

    @app.get("/api/runs/{run_id}/kanban")
    def get_kanban(run_id: str) -> List[Dict[str, Any]]:
        if not _db().get_run(run_id):
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        return _db().list_tasks(run_id)

    @app.get("/api/runs/{run_id}/audit")
    def get_audit(run_id: str) -> List[Dict[str, Any]]:
        if not _db().get_run(run_id):
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        return _db().get_audit_log(run_id)

    @app.get("/api/runs/{run_id}/cost")
    def get_cost(run_id: str) -> Dict[str, Any]:
        rows = _db().get_token_summary(run_id)
        total_in = total_out = total_cost = 0.0
        items = []
        for r in rows:
            t_in = int(r.get("tokens_in") or 0)
            t_out = int(r.get("tokens_out") or 0)
            model = r.get("model") or ""
            cost = _calc_cost(t_in, t_out, model)
            total_in += t_in
            total_out += t_out
            total_cost += cost
            items.append({**r, "cost_usd": round(cost, 6)})
        return {
            "items": items,
            "total_tokens_in": int(total_in),
            "total_tokens_out": int(total_out),
            "total_cost_usd": round(total_cost, 6),
        }

    # ------------------------------------------------------------------
    # HITL approval endpoint
    # ------------------------------------------------------------------

    @app.post("/api/runs/{run_id}/approve")
    def approve_stage(
        run_id: str,
        payload: Dict[str, Any] = Body(...),
    ) -> Dict[str, Any]:
        from gateway.hitl import Decision
        from core.pipeline import SDLC_STAGES

        stage = payload.get("stage")
        decision_str = (payload.get("decision") or "APPROVED").upper()
        rationale = payload.get("rationale", "")
        amended_artifact = payload.get("amended_artifact")

        if not stage:
            raise HTTPException(status_code=400, detail="stage is required")

        db = _db()
        if not db.get_artifact(run_id, stage):
            raise HTTPException(status_code=404, detail=f"No artifact for stage {stage}")

        try:
            decision = Decision(decision_str)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid decision: {decision_str}")

        actor = "ui:web"

        if decision == Decision.AMENDED:
            if not amended_artifact:
                raise HTTPException(
                    status_code=400,
                    detail="amended_artifact is required when decision=AMENDED",
                )
            db.update_artifact(run_id, stage, amended_artifact)
            stage_order = [s.name for s in SDLC_STAGES]
            cleared = db.clear_stages_after(run_id, stage, stage_order)
            db.log_decision(run_id, stage, decision.value, actor, rationale)
            return {"status": "amended", "cleared_stages": cleared}

        db.approve_artifact(run_id, stage, approved_by=actor)
        db.log_decision(run_id, stage, decision.value, actor, rationale)
        return {"status": decision.value.lower()}

    # ------------------------------------------------------------------
    # Serve the React SPA
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    @app.get("/{full_path:path}", response_class=HTMLResponse)
    def spa(full_path: str = "") -> HTMLResponse:
        # API routes are handled before this catch-all
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        index = _UI_DIR / "index.html"
        if not index.exists():
            return HTMLResponse("<h1>UI not found. Run uv sync --extra ui.</h1>", status_code=500)
        return HTMLResponse(index.read_text(encoding="utf-8"))

    return app


app = _get_app()
