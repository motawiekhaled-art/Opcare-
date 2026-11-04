# Edge agent

Runs at each site (Architecture OSS-DAQ-ARC-001 section 6). It watches the retained configuration topics
`opcare/v1/{site}/config/+` and keeps exactly one connector running per configured source:

- new configuration → start a connector of that type (if one is installed);
- new revision → nothing to do, the running connector applies it itself (spec section 5.2);
- empty configuration (source deleted) → stop the connector and clear its retained status.

It also answers `test_connection` for configurations that are **not saved yet** on the admin topic of
component `edge-agent` — this is the "Test connection" button of the configuration screen. The agent validates
the configuration (schema + connector checks) and lets the connector open a short-lived connection.

`LocalAgent` runs connectors in-process: development, tests, the demo, and small single-box sites. The
production agent for multi-connector edge nodes applies the same rules but starts one container per source
(`opcare/connector-{type}`, environment from spec section 3); only `_start` and `_stop` change.

Tests: `python -m pytest edge -q`
