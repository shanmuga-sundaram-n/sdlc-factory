# Microservices Pattern Fragment

## Service boundary principles
- A service owns its domain data — no cross-service DB joins
- API contract is the only integration surface; internal implementation is private
- Size: a team of 5–8 can own and deploy it independently

## Communication patterns
| Need | Pattern |
|---|---|
| Synchronous request/response | REST or gRPC |
| Async fire-and-forget | Events (Kafka / SQS) |
| Async request/response | Events with correlation ID + reply topic |
| Saga (distributed transaction) | Choreography (events) or Orchestration (saga orchestrator) |

## Service mesh / gateway
- All external traffic enters through an API gateway (rate limiting, auth, routing)
- Service-to-service calls use mTLS (Istio / Linkerd) for zero-trust networking
- Expose only the gateway externally; services are cluster-internal

## Resilience
- **Circuit breaker**: open after N consecutive failures; half-open after cooldown
- **Bulkhead**: isolate thread pools per downstream dependency
- **Timeout + retry with jitter**: never retry without a timeout
- **Graceful degradation**: return stale data or a safe default when a dependency fails

## Observability (three pillars)
- **Logs**: structured JSON, include `trace_id`, `span_id`, `service`, `version`
- **Metrics**: RED (Rate, Errors, Duration) per service; expose Prometheus `/metrics`
- **Traces**: OpenTelemetry — propagate W3C `traceparent` header across all calls

## Deployment
- Each service has its own CI/CD pipeline and can be deployed independently
- Use feature flags to decouple deploy from release
- Canary deployments: route 5% → 25% → 100% with automated rollback on SLO breach

## Data management
- Each service has its own database schema/instance (Database per Service pattern)
- Cross-service reads: either CQRS read model or API call (never shared DB)
- Schema migrations are backward-compatible for at least one deploy cycle

## Health checks
- `/healthz/live` — liveness: can the process handle requests?
- `/healthz/ready` — readiness: are all dependencies (DB, cache) reachable?
- `/healthz/startup` — startup: has initialisation completed? (K8s startupProbe)
