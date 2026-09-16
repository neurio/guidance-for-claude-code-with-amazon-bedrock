# SNS Filter Policy Attributes

## Rule
A `String` SNS message attribute MUST NOT hold a JSON object. Publish
structured payloads as `DataType: Binary`, and keep String attributes to flat
scalars (the values a `FilterPolicy` actually matches on).

## Why
When SNS evaluates a subscription's `FilterPolicy`, it inspects every message
attribute — not just the ones the policy names. A `String` attribute whose
value parses as a JSON **object** makes SNS classify the whole attribute set as
invalid, abandon evaluation, and drop the message for that subscription.

Nothing reports this:

- `sns_client.publish` returns success with a MessageId.
- The publisher logs its normal "sent" line.
- The subscriber is never invoked, so its logs are empty rather than wrong.
- `NumberOfNotificationsFailed` stays at 0 — this is not a delivery failure.

The only trace is the SNS metric
`NumberOfNotificationsFilteredOut-InvalidAttributes`.

This shipped in `quota_monitor._alert_message_attributes()`: an `alert_payload`
String attribute carrying `json.dumps(alert)` sat next to the `alert_type`
attribute the Slack subscription filters on. Every per-user spend alert was
published and then silently discarded — no Slack DM ever reached a real user,
while DynamoDB dedup rows and "Sent N alerts" log lines accumulated as if it
were working.

## Verified behaviour

Measured against a live topic whose subscription filters
`{"alert_type": ["monthly_cost", "daily_cost"]}`:

| Extra attribute alongside a correct `alert_type` | Result |
|---|---|
| `String` = `"just a plain string"` | delivered |
| `String` = `'{"a": 1}'` (JSON object) | **dropped** |
| `String` = `"[1,2,3]"` (JSON array) | delivered — read as `String.Array` |
| `Binary` = same JSON bytes | delivered |
| `String` = JSON object under a name the policy never mentions | **dropped** |

Two consequences worth internalising: renaming the offending attribute does not
help, and an attribute the filter policy never references can still sink the
message.

## Anti-Pattern
```python
# ❌ Wrong — drops the entire message mid-filter, silently
{
    "alert_type": {"DataType": "String", "StringValue": "monthly_cost"},
    "alert_payload": {"DataType": "String", "StringValue": json.dumps(alert)},
}

# ✅ Correct — Binary is opaque to the JSON sniffing
{
    "alert_type": {"DataType": "String", "StringValue": "monthly_cost"},
    "alert_payload": {
        "DataType": "Binary",
        "BinaryValue": json.dumps(alert, default=str).encode("utf-8"),
    },
}
```

Binary attributes reach a Lambda subscriber base64-encoded, as
`{"Type": "Binary", "Value": "<base64>"}`. Consumers must `base64.b64decode`
before `json.loads`, and should keep accepting the `String` shape so messages
in flight across a stack update are not lost.

## Also remember
An attribute the message does not carry at all is a plain non-match, which is a
*deliberate* routing mechanism here: `_publish_operational_alert` and
`sidecar_monitor` publish no attributes precisely so end users are never DM'd
about operator problems. Do not "fix" that by adding attributes to them.

## Testing
- Assert no `String` attribute value starts with `{` or `[`
  (`test_no_string_attribute_holds_json` in `test_quota_monitor_lambda.py`).
- Assert the consumer decodes the Binary envelope shape AND still parses a
  legacy `String` payload.
- A unit test cannot catch this class of bug on its own — filter evaluation is
  server-side. When changing attributes, also confirm
  `NumberOfNotificationsFilteredOut-InvalidAttributes` stays flat after deploy.

## Related
- `.claude/rules/quota-usage-source.md` — the consumer contract these alerts feed
- `.claude/rules/quota-requires-oidc.md` — which auth types produce alertable identities
- `assets/docs/QUOTA_MONITORING.md` — the zero-subscription warning, same silent-loss family
