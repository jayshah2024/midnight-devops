# Datadog → Squadcast / Slack (notification path)

Datadog monitors (in `monitors/`) route by severity: **critical → Squadcast** (on-call paging
/ incident management), **warning → Slack** (#node-ops-alerts). The routing is done with
`@notification` handles in each monitor's message body.

## Squadcast

1. In **Squadcast → Services → <your service> → Integrations**, add the **Datadog** integration
   (Squadcast supports a native Datadog integration / webhook). Copy the generated webhook URL.
2. In **Datadog → Integrations → Webhooks**, create a webhook named `squadcast` with that URL
   (Squadcast's Datadog integration parses Datadog's payload to open/auto-resolve incidents).
3. Reference it in a monitor as `@webhook-squadcast`. Auto-resolve works because the monitors
   set notifications on both alert and recovery transitions.


## Slack

1. **Datadog → Integrations → Slack**, authorize the workspace, and map the channel
   `#node-ops-alerts`.
2. Reference it as `@slack-node-ops-alerts` in monitor messages.

## Routing summary

| Severity | Handle in monitor | Destination |
|---|---|---|
| critical (height stalled, node down, disk critical) | `@webhook-squadcast` | Squadcast → on-call paged |
| warning (peers low, disk >85%, cpu/mem) | `@slack-node-ops-alerts` | Slack channel |
