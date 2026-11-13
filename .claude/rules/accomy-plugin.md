---
paths:
  - "plugins/accomy_travel/**"
  - "tests/plugins/accomy_travel/**"
---

# Accomy AI Travel plugin

`plugins/accomy_travel/README.md` is the design and operator document: read it before changing behaviour, and
update it in the same change. This file is the short list of what must stay true.

The plugin exists only in this fork. Upstream does not take third-party product plugins in its tree
(`plugins/AGENTS.md`), so nothing here is written to be merged upstream. It is written to move out unchanged
to `$HERMES_HOME/plugins/accomy_travel/` or to its own repository.

## Map

| Module | What it holds |
|---|---|
| `__init__.py`, `plugin.yaml` | Registration: the tools, three hooks, six prompt sections, the `accomy-orders` CLI command |
| `_common.py` | The tool contract, secrets, `request_json` (the plugin's only HTTP function), caller identity, rate limits, supplier budgets |
| `tmc_client.py` | Signed TMC client and its read-endpoint allowlist |
| `tools_hotels.py`, `tools_flights.py` | Hotel and flight search, room rates, fare verification |
| `standard.py` | The travel standard those two tools enforce |
| `seatunique_client.py`, `tools_events.py` | Event and ticket search (GET only) |
| `tools_places.py`, `geo.py` | Places, weather and geocoding (Google) |
| `tools_research.py`, `scope.py` | Public-web research (Brave) and the scope screen |
| `fx.py` | Conversion to US dollars from a fixed table |
| `quotes.py` | What each caller was shown, under short quote ids |
| `tools_orders.py`, `orderbridge.py`, `orderstore/` | Demonstration orders in PostgreSQL: connection, migrations, store, operator CLI |
| `payment.py` | Signed links to the demo payment page, and the void and status requests to it |
| `policy.py` | The prompt sections, the out-of-scope reminder, the suggested-replies footer |
| `ingress.py`, `welcome.py` | The inbound gate with its limits, and the `/start` and `/help` replies |

## What must stay true

- **No supplier write, ever.** `tmc_client._send` refuses every method and path outside `_READ_ENDPOINTS`,
  apart from `GET /hotel/<digits>`, the hotel detail read. Never add an order, payment, cancel, change or
  refund endpoint, and never send a TMC request from another module. Seat Unique is GET only. Two tests pin this and must not be weakened:
  `test_tmc_client.py::test_order_payment_and_after_sale_endpoints_never_reach_the_supplier` and
  `test_tools_orders.py::test_placing_reading_and_cancelling_an_order_sends_no_request_to_any_supplier`.
- **An order is built from quotes.** The items of `place_order` are quote ids, with a quantity for event
  tickets. The price and the facts of an order come from what a search tool showed that caller (`quotes.py`),
  never from a number the model types. The model supplies only the travellers, the contact and the trip. The
  buyer is the session actor the gateway bound, never model output.
- **Prices are in US dollars.** Tools return prices through `fx.py`. A price in a currency the table lacks is
  marked `unconverted` and cannot be ordered.
- **The travel standard has one source.** Its numbers live in `standard.py`. The tools leave out what is
  outside it, and `policy.py` renders the same constants into the prompt. Change a limit there and nowhere else.
- **The prompt sections share a budget of 8,000 rendered characters**, enforced by core
  (`hermes_cli/plugins_dispatch.py`). Core walks the sections in id order and skips each one that would take
  the total past the budget, with only a warning in the log.
  `test_policy.py::test_rendered_prompt_carries_every_section_within_the_budget` catches it. The budget is
  nearly used: trim before you add.
- **A hook never rewrites the system prompt.** The out-of-scope reminder goes into the current user turn, which
  keeps the prompt cache valid for the whole conversation.
- **Only the published plugin API.** The plugin's core imports are `agent.secret_scope`,
  `gateway.session_context`, `gateway.config.Platform` and `tools.registry`, all listed in
  `hermes_cli/data/plugin_api.json`. Do not import another core module. Read a secret through
  `_common.secret`, never from `os.environ`.
- **Applied migrations are frozen.** `orderstore/migrate.py` compares checksums and refuses a changed file.
  Numbers run without gaps, and a migration file holds no transaction statement. A non-loopback database is
  refused unless its connection string verifies TLS (`orderstore/db.py`).
- **Limits are named constants** in `ingress.py`, `_common.py`, `tools_orders.py`, `tools_research.py` and
  `quotes.py`. The "Limits" section of the README states the same numbers: change both together.
- **Secrets are named, never stored.** `distributions/accomy/distribution.yaml` lists the variable names. No
  fixture, test or document carries a value.

## Tests

- Run `scripts/run_tests.sh tests/plugins/accomy_travel`, never bare `pytest`.
- Fixtures under `tests/plugins/accomy_travel/fixtures/` are trimmed real supplier answers, as the test
  modules say. Tests replace the transport (`_common.request_json`) or the client functions
  (`tmc_client.flight_search` and its siblings). The suite refuses a lookup of any external host name
  (`tests/conftest.py`), so a test that reaches a real supplier fails unless it is marked `live` or
  `allow_network`.
- The order tests need a local PostgreSQL container. The command that starts it is at the top of
  `tests/plugins/accomy_travel/test_orderstore.py`. Without it those tests are skipped, and a run with skipped
  order tests proves nothing about orders.
- A test names the behaviour it protects. When you fix a bug, show that the new test fails without the fix.
