# REST API Pattern Fragment

## Resource design
- Use nouns for resource names (`/orders`, `/users/{id}/addresses`)
- HTTP verbs carry the action: GET retrieves, POST creates, PUT replaces, PATCH updates, DELETE removes
- Return 201 + Location header on resource creation; 204 on successful DELETE with no body
- Use `?filter[field]=value` for filtering; `?sort=field,-other` for sorting; `?page[number]=1&page[size]=20` for pagination

## Status codes
| Situation | Code |
|---|---|
| Success with body | 200 |
| Created | 201 |
| No content | 204 |
| Bad request / validation | 400 |
| Unauthenticated | 401 |
| Forbidden | 403 |
| Not found | 404 |
| Conflict (duplicate) | 409 |
| Unprocessable entity | 422 |
| Internal error | 500 |

## Error envelope
```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "Human-readable message",
    "details": [{"field": "email", "message": "must be a valid email"}]
  }
}
```

## Versioning
- URL prefix: `/api/v1/`, `/api/v2/`
- Breaking changes always increment the major version
- Deprecate old versions with `Sunset` and `Deprecation` response headers

## Auth
- Bearer token in `Authorization: Bearer <token>` header
- Refresh tokens returned in `HttpOnly` cookie, access tokens in JSON body
- All mutation endpoints require CSRF protection when cookies are involved

## OpenAPI
- Every endpoint MUST have an `operationId`, summary, parameter descriptions, and response schemas
- Document all error responses (400, 401, 403, 404, 422, 500)
- Mark sensitive fields with `x-sensitive: true`

## Rate limiting
- Return `Retry-After` and `X-RateLimit-Remaining` headers
- Use `429 Too Many Requests` status
