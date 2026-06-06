"""OpenTelemetry instrumentation for the SDLC pipeline.

Gracefully degrades to no-ops when the opentelemetry-sdk package is not
installed, so the pipeline never fails due to missing observability deps.

Configuration (all via env vars, standard OTel convention):
  OTEL_EXPORTER_OTLP_ENDPOINT  — OTLP gRPC/HTTP endpoint (e.g. http://localhost:4317)
  OTEL_TRACES_EXPORTER         — "otlp" (default when endpoint set) | "console" | "none"
  OTEL_SERVICE_NAME            — service name tag (default: "sdlc-factory")
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Generator, Optional

logger = logging.getLogger(__name__)

_TRACER: Any = None  # real opentelemetry.trace.Tracer or _NoOpTracer
_SETUP_DONE = False


# ---------------------------------------------------------------------------
# No-op fallback — used when opentelemetry-sdk is not installed
# ---------------------------------------------------------------------------

class _NoOpSpan:
    def set_attribute(self, key: str, value: Any) -> None: ...
    def record_exception(self, exc: Exception) -> None: ...
    def set_status(self, *args: Any, **kwargs: Any) -> None: ...
    def __enter__(self) -> "_NoOpSpan": return self
    def __exit__(self, *args: Any) -> None: ...


class _NoOpTracer:
    @contextmanager
    def start_as_current_span(self, name: str, **kwargs: Any) -> Generator[_NoOpSpan, None, None]:
        yield _NoOpSpan()


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def _setup_tracer() -> Any:
    """Initialise the OTel tracer once. Returns a real tracer or _NoOpTracer."""
    exporter_env = os.getenv("OTEL_TRACES_EXPORTER", "").lower()
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    service_name = os.getenv("OTEL_SERVICE_NAME", "sdlc-factory")

    # Explicit opt-out
    if exporter_env == "none":
        return _NoOpTracer()

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except ImportError:
        logger.debug("opentelemetry-sdk not installed — telemetry disabled")
        return _NoOpTracer()

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if exporter_env == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        logger.info("OTel: console exporter enabled")
    elif endpoint or exporter_env in ("otlp", ""):
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
                logger.info("OTel: OTLP exporter → %s", endpoint)
            except ImportError:
                logger.debug("opentelemetry-exporter-otlp not installed — falling back to console")
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        else:
            # No endpoint and no explicit exporter — stay silent (no exporter added)
            logger.debug("OTel: SDK present but no exporter configured — spans will be dropped")

    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)


def get_tracer() -> Any:
    """Return the singleton tracer (real or no-op)."""
    global _TRACER, _SETUP_DONE
    if not _SETUP_DONE:
        _TRACER = _setup_tracer()
        _SETUP_DONE = True
    return _TRACER


# ---------------------------------------------------------------------------
# Convenience span helpers
# ---------------------------------------------------------------------------

@contextmanager
def pipeline_span(run_id: str, idea: str) -> Generator[Any, None, None]:
    """Root span covering the entire pipeline run."""
    tracer = get_tracer()
    with tracer.start_as_current_span("pipeline.run") as span:
        span.set_attribute("run_id", run_id)
        span.set_attribute("idea.length", len(idea))
        yield span


@contextmanager
def stage_span(run_id: str, stage_name: str, *, parallel: bool = False) -> Generator[Any, None, None]:
    """Child span for a single pipeline stage."""
    tracer = get_tracer()
    with tracer.start_as_current_span(f"stage.{stage_name}") as span:
        span.set_attribute("run_id", run_id)
        span.set_attribute("stage", stage_name)
        span.set_attribute("parallel", parallel)
        yield span


@contextmanager
def agent_span(run_id: str, stage: str, role: str, agent_id: str) -> Generator[Any, None, None]:
    """Child span for a single agent invocation."""
    tracer = get_tracer()
    with tracer.start_as_current_span(f"agent.{role}") as span:
        span.set_attribute("run_id", run_id)
        span.set_attribute("stage", stage)
        span.set_attribute("role", role)
        span.set_attribute("agent_id", agent_id)
        yield span
