# Event-Driven Architecture Pattern Fragment

## Core concepts
- **Event**: an immutable record of something that happened (`OrderPlaced`, `PaymentProcessed`)
- **Command**: an intent to change state (`PlaceOrder`, `CancelSubscription`)
- **Query**: a read request that never changes state

## Event schema (CloudEvents-compatible)
```json
{
  "specversion": "1.0",
  "id": "<uuid>",
  "source": "/services/orders",
  "type": "com.example.orders.placed.v1",
  "datacontenttype": "application/json",
  "time": "2025-01-01T12:00:00Z",
  "data": { "order_id": "...", "total": 99.99 }
}
```

## Naming conventions
- Event types: `<domain>.<aggregate>.<past-tense-verb>.<version>` — e.g. `billing.invoice.created.v1`
- Topics: `<domain>.<aggregate>.<version>` — e.g. `billing.invoices.v1`
- Consumer groups: `<service>.<purpose>` — e.g. `notifications.send-confirmation`

## Idempotency
- Every consumer MUST be idempotent — track processed event IDs in a deduplication store
- Use event `id` + consumer group as the deduplication key
- At-least-once delivery is the baseline; exactly-once requires transactional outbox

## Transactional outbox pattern
1. Write domain event to `outbox` table in the **same** DB transaction as the state change
2. A poller / CDC (Debezium) publishes events from `outbox` to the broker
3. Mark events as `published` after successful broker acknowledgement

## Schema evolution
- Additive changes (new optional fields) are backward-compatible — bump minor version in `type`
- Removing or renaming fields requires a new major version with a migration period
- Consumers must ignore unknown fields (`additionalProperties: true` in JSON Schema)

## Error handling
- Implement dead-letter queues (DLQ) for messages that fail after N retries
- Log the full event + exception before sending to DLQ
- Alert on DLQ depth > 0

## Testing
- Unit test: aggregate logic in isolation (no broker)
- Integration test: producer → in-memory broker → consumer
- Contract test: schema registry or Pact to verify producer/consumer schema agreement
