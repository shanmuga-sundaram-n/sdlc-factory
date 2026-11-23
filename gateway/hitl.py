"""Human-in-the-Loop governance gateway.

Supports four delivery modes for approval requests:
  cli     — interactive terminal prompt (default for local dev)
  auto    — rule-based evaluation against artifact schema
  slack   — post to a Slack channel; poll for reaction or thread reply
  webhook — POST to an external URL; wait for callback

Also exposes an ApprovalQueue that background threads can post to, while
the main thread (or a webhook handler) resolves them.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums + data classes
# ---------------------------------------------------------------------------

class Decision(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    AMENDED  = "AMENDED"


@dataclass
class ApprovalRequest:
    request_id: str
    run_id: str
    stage: str
    artifact: Any
    created_at: float = field(default_factory=time.time)


@dataclass
class ApprovalResponse:
    request_id: str
    decision: Decision
    actor: str
    rationale: str
    amended_artifact: Optional[Any] = None
    resolved_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# ApprovalQueue — threadsafe, supports timeout wait
# ---------------------------------------------------------------------------

class ApprovalQueue:
    """Async approval queue: producer posts a request, consumer resolves it.

    The gate submits an ApprovalRequest and blocks on wait_for_response().
    An out-of-band resolver (webhook handler, Slack bot, CLI) calls resolve().

    Uses a per-session dictionary keyed by request ID for O(1) resolution.
    """

    def __init__(self):
        self._pending: Dict[str, ApprovalRequest] = {}
        self._responses: Dict[str, ApprovalResponse] = {}
        self._events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def submit(self, request: ApprovalRequest) -> None:
        with self._lock:
            self._pending[request.request_id] = request
            self._events[request.request_id] = threading.Event()
        logger.info(
            "ApprovalQueue: submitted request %s (run=%s stage=%s)",
            request.request_id, request.run_id, request.stage,
        )

    def resolve(self, response: ApprovalResponse) -> bool:
        with self._lock:
            if response.request_id not in self._pending:
                logger.warning("ApprovalQueue: unknown request %s", response.request_id)
                return False
            self._responses[response.request_id] = response
            event = self._events.get(response.request_id)
        if event:
            event.set()
        logger.info(
            "ApprovalQueue: resolved %s → %s by %s",
            response.request_id, response.decision, response.actor,
        )
        return True

    def wait_for_response(
        self, request_id: str, timeout: float = 3600.0
    ) -> Optional[ApprovalResponse]:
        with self._lock:
            event = self._events.get(request_id)
        if event is None:
            return None
        signalled = event.wait(timeout=timeout)
        if not signalled:
            logger.warning("ApprovalQueue: timeout waiting for %s", request_id)
            return None
        with self._lock:
            return self._responses.pop(request_id, None)

    def list_pending(self) -> List[ApprovalRequest]:
        with self._lock:
            return list(self._pending.values())

    def pop_request(self, request_id: str) -> Optional[ApprovalRequest]:
        with self._lock:
            return self._pending.pop(request_id, None)


# ---------------------------------------------------------------------------
# Rule-based auto-evaluator
# ---------------------------------------------------------------------------

class AutoEvaluator:
    """Rule-based HITL gate for the auto mode.

    Rules are functions (artifact, config) → Optional[Decision].
    The first rule that returns a non-None decision wins.
    If no rule fires, defaults to APPROVED.
    """

    def __init__(self):
        self._rules: List[Callable] = [
            self._quality_gate_rule,
            self._open_questions_rule,
        ]

    def evaluate(self, stage: str, artifact: Any, config: Dict) -> Tuple[Decision, str]:
        for rule in self._rules:
            result = rule(stage, artifact, config)
            if result is not None:
                decision, rationale = result
                return decision, rationale
        return Decision.APPROVED, "auto: no blocking rules matched"

    @staticmethod
    def _unwrap(artifact: Any) -> Any:
        """Unwrap single-role stage artifacts: {role: payload} → payload."""
        if isinstance(artifact, dict) and len(artifact) == 1:
            only_val = next(iter(artifact.values()))
            if isinstance(only_val, dict):
                return only_val
        return artifact

    def _quality_gate_rule(
        self, stage: str, artifact: Any, config: Dict
    ) -> Optional[Tuple[Decision, str]]:
        if stage != "quality_gate":
            return None
        effective = self._unwrap(artifact)
        if not isinstance(effective, dict):
            return Decision.REJECTED, "auto: quality_gate artifact is not a dict"
        threshold = config.get("coverage_threshold", 80)
        coverage = effective.get("coverage_pct", 0)
        gate_passed = effective.get("gate_passed", False)
        if gate_passed and coverage >= threshold:
            return Decision.APPROVED, f"auto: coverage {coverage}% ≥ {threshold}%"
        return Decision.REJECTED, f"auto: coverage {coverage}% < {threshold}% or gate_passed=false"

    def _open_questions_rule(
        self, stage: str, artifact: Any, config: Dict
    ) -> Optional[Tuple[Decision, str]]:
        if stage != "requirements":
            return None
        effective = self._unwrap(artifact)
        if not isinstance(effective, dict):
            return None
        questions = effective.get("open_questions", [])
        max_allowed = config.get("max_open_questions", 0)
        if len(questions) > max_allowed:
            qs = "; ".join(questions[:3])
            return (
                Decision.REJECTED,
                f"auto: {len(questions)} open questions remain (max {max_allowed}): {qs}…",
            )
        return None


# ---------------------------------------------------------------------------
# Slack delivery
# ---------------------------------------------------------------------------

def _post_to_slack(channel: str, text: str, token: str) -> Optional[str]:
    """Post a message to Slack and return the ts (message timestamp)."""
    import urllib.request
    payload = json.dumps({"channel": channel, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("ts")
    except Exception as exc:
        logger.error("Slack post failed: %s", exc)
        return None


def _poll_slack_reactions(channel: str, ts: str, token: str, timeout: float = 3600) -> Optional[str]:
    """Poll for a thumbsup/thumbsdown reaction on a Slack message."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(15)
        url = (
            f"https://slack.com/api/reactions.get?channel={channel}&timestamp={ts}"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            reactions = {
                r["name"]: r["count"]
                for r in (data.get("message", {}).get("reactions") or [])
            }
            if "+1" in reactions or "white_check_mark" in reactions:
                return "APPROVED"
            if "-1" in reactions or "x" in reactions:
                return "REJECTED"
        except Exception as exc:
            logger.warning("Slack poll error: %s", exc)
    return None


# ---------------------------------------------------------------------------
# HITLGateway — main class
# ---------------------------------------------------------------------------

class HITLGateway:
    """Flexible HITL governance gateway.

    Instantiate once per pipeline run. Call request_approval() at each gate.
    """

    def __init__(
        self,
        approval_queue: Optional[ApprovalQueue] = None,
        auto_evaluator: Optional[AutoEvaluator] = None,
    ):
        self._queue = approval_queue or ApprovalQueue()
        self._auto = auto_evaluator or AutoEvaluator()

    @property
    def approval_queue(self) -> ApprovalQueue:
        return self._queue

    def request_approval(
        self,
        stage: str,
        artifact: Any,
        run_id: str,
        *,
        mode: str = "cli",
        config: Optional[Dict] = None,
        timeout: float = 3600.0,
    ) -> Tuple[Decision, str, Any]:
        """Request human approval for a stage artifact.

        Returns (decision, rationale, artifact) — artifact may be mutated
        if decision is AMENDED.
        """
        cfg = config or {}

        if mode == "auto":
            decision, rationale = self._auto.evaluate(stage, artifact, cfg)
            return decision, rationale, artifact

        if mode == "cli":
            return self._cli_gate(stage, artifact, cfg)

        if mode == "slack":
            return self._slack_gate(stage, artifact, run_id, cfg, timeout)

        if mode == "webhook":
            return self._webhook_gate(stage, artifact, run_id, cfg, timeout)

        if mode == "queue":
            return self._queue_gate(stage, artifact, run_id, timeout)

        # Unknown mode — default to approve with warning
        logger.warning("HITLGateway: unknown mode %r; auto-approving stage %s", mode, stage)
        return Decision.APPROVED, f"unknown mode {mode!r}; auto-approved", artifact

    # ------------------------------------------------------------------
    # CLI gate
    # ------------------------------------------------------------------

    def _cli_gate(
        self, stage: str, artifact: Any, config: Dict
    ) -> Tuple[Decision, str, Any]:
        print(f"\n{'='*70}")
        print(f"  HITL APPROVAL GATE — Stage: {stage.upper()}")
        print(f"{'='*70}")
        summary = json.dumps(artifact, indent=2, ensure_ascii=False)
        print(summary[:4000])
        if len(summary) > 4000:
            print(f"  … ({len(summary) - 4000} more chars)")
        print(f"{'='*70}")

        while True:
            try:
                answer = input(
                    "\n[y] Approve  [n] Reject  [e] Amend  > "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return Decision.REJECTED, "cli: interrupted", artifact

            if answer in ("y", "yes", "approve"):
                rationale = input("Rationale (Enter to skip): ").strip()
                return Decision.APPROVED, rationale or "cli: approved", artifact

            if answer in ("n", "no", "reject"):
                rationale = input("Rejection reason: ").strip()
                return Decision.REJECTED, rationale or "cli: rejected", artifact

            if answer in ("e", "edit", "amend"):
                print("Paste amended artifact JSON (end with a blank line):")
                lines = []
                while True:
                    try:
                        line = input()
                    except EOFError:
                        break
                    if line == "":
                        break
                    lines.append(line)
                raw = "\n".join(lines)
                try:
                    amended = json.loads(raw)
                    if isinstance(artifact, dict) and isinstance(amended, dict):
                        artifact.update(amended)
                    else:
                        artifact = amended
                    return Decision.AMENDED, "cli: amended by operator", artifact
                except json.JSONDecodeError as exc:
                    print(f"Invalid JSON: {exc}. Try again.")

            print("Please enter y, n, or e.")

    # ------------------------------------------------------------------
    # Slack gate
    # ------------------------------------------------------------------

    def _slack_gate(
        self, stage: str, artifact: Any, run_id: str, config: Dict, timeout: float
    ) -> Tuple[Decision, str, Any]:
        token = os.getenv("SLACK_BOT_TOKEN", "")
        channel = config.get("slack_channel", os.getenv("SLACK_APPROVAL_CHANNEL", "#sdlc-approvals"))
        if not token:
            logger.warning("SLACK_BOT_TOKEN not set; falling back to CLI gate")
            return self._cli_gate(stage, artifact, config)

        summary = json.dumps(artifact, ensure_ascii=False)[:2000]
        text = (
            f":robot_face: *SDLC Pipeline Approval Request*\n"
            f"Run: `{run_id}` | Stage: `{stage}`\n\n"
            f"```{summary}```\n\n"
            f"React with :+1: to approve or :-1: to reject."
        )
        ts = _post_to_slack(channel, text, token)
        if not ts:
            logger.error("Slack post failed; falling back to CLI gate")
            return self._cli_gate(stage, artifact, config)

        logger.info("Slack gate posted to %s (ts=%s); polling for reactions…", channel, ts)
        reaction = _poll_slack_reactions(channel, ts, token, timeout=timeout)
        if reaction == "APPROVED":
            return Decision.APPROVED, "slack: approved via reaction", artifact
        if reaction == "REJECTED":
            return Decision.REJECTED, "slack: rejected via reaction", artifact
        return Decision.REJECTED, "slack: timeout waiting for reaction", artifact

    # ------------------------------------------------------------------
    # Webhook gate
    # ------------------------------------------------------------------

    def _webhook_gate(
        self, stage: str, artifact: Any, run_id: str, config: Dict, timeout: float
    ) -> Tuple[Decision, str, Any]:
        import urllib.request
        webhook_url = config.get("webhook_url", os.getenv("HITL_WEBHOOK_URL", ""))
        if not webhook_url:
            logger.warning("HITL_WEBHOOK_URL not set; falling back to queue gate")
            return self._queue_gate(stage, artifact, run_id, timeout)

        request_id = str(uuid.uuid4())
        payload = json.dumps({
            "request_id": request_id,
            "run_id": run_id,
            "stage": stage,
            "artifact_preview": json.dumps(artifact, ensure_ascii=False)[:3000],
        }).encode()
        req = urllib.request.Request(
            webhook_url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as exc:
            logger.error("Webhook POST failed: %s; falling back to queue gate", exc)
            return self._queue_gate(stage, artifact, run_id, timeout)

        # Register in queue so the callback can resolve it
        req_obj = ApprovalRequest(
            request_id=request_id, run_id=run_id, stage=stage, artifact=artifact
        )
        self._queue.submit(req_obj)
        resp = self._queue.wait_for_response(request_id, timeout=timeout)
        if resp is None:
            return Decision.REJECTED, "webhook: timeout", artifact
        if resp.decision == Decision.AMENDED and resp.amended_artifact is not None:
            artifact = resp.amended_artifact
        return resp.decision, resp.rationale, artifact

    # ------------------------------------------------------------------
    # Queue gate — used by API server / programmatic callers
    # ------------------------------------------------------------------

    def _queue_gate(
        self, stage: str, artifact: Any, run_id: str, timeout: float
    ) -> Tuple[Decision, str, Any]:
        request_id = str(uuid.uuid4())
        req_obj = ApprovalRequest(
            request_id=request_id, run_id=run_id, stage=stage, artifact=artifact
        )
        self._queue.submit(req_obj)
        logger.info("Queue gate: waiting for approval of request %s", request_id)
        resp = self._queue.wait_for_response(request_id, timeout=timeout)
        if resp is None:
            return Decision.REJECTED, "queue: timeout", artifact
        if resp.decision == Decision.AMENDED and resp.amended_artifact is not None:
            artifact = resp.amended_artifact
        return resp.decision, resp.rationale, artifact
