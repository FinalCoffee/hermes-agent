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
| `__init__.py`, `plugin.yaml` | Registration: the tools, three hooks, six prompt sections, the speech-to-text provider, the `accomy-orders` CLI command |
| `_common.py` | The tool contract, secrets, `request_json` (the plugin's only HTTP function), caller identity, rate limits, supplier budgets |
| `tmc_client.py` | Signed TMC client and its read-endpoint allowlist |
| `tools_hotels.py`, `tools_flights.py` | Hotel and flight search (one way, round trip and multi-city, a leg at a time), room rates, fare verification |
| `standard.py` | The travel standard those two tools enforce |
| `seatunique_client.py`, `tools_events.py` | Event and ticket search (GET only) |
| `tools_places.py`, `geo.py` | Places, weather and geocoding (Google) |
| `tools_research.py`, `scope.py` | Public-web research (Brave) and the scope screen |
| `fx.py` | Conversion to US dollars from a fixed table |
| `quotes.py` | What each caller was shown, under short quote ids, and the flights picked so far in a trip |
| `tools_orders.py`, `orderbridge.py`, `orderstore/` | Demonstration orders in PostgreSQL: connection, migrations, store, operator CLI |
| `payment.py` | Signed links to the demo payment page, and the void and status requests to it |
| `policy.py` | The prompt sections, the out-of-scope reminder, the suggested-replies footer |
| `ingress.py`, `welcome.py` | The inbound gate with its limits, and the `/start` and `/help` replies |
| `attachments.py` | The files senders attach: who sent each, the daily allowance, what an extension is read as |
| `tools_attachments.py`, `documents.py` | `read_attachment`, and the text of a document |
| `parsers.py`, `_document_worker.py` | The separate process that parses a PDF or an office file and draws PDF pages, and the function that runs it |
| `ocr.py` | Scanned PDF pages: drawn as pictures and read by the profile's own model through `ctx.llm` |
| `speech.py` | Speech-to-text through Amazon Transcribe: the gateway's provider for voice notes, and audio files and videos for `read_attachment` |

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
- **A journey out and back is one trip.** `search_flights` searches a round trip (`return_date`) or a
  multi-city trip (`more_legs`) as one supplier search, answered a leg at a time: an earlier leg's flight gets an
  `option_id` (`quotes.FLIGHT_CHOICE`), which only `continue_from` accepts, and the last leg's flight gets the
  quote of the whole trip. Keep the two ids apart: an `option_id` is never orderable. The live price of a trip
  goes through `tmc_client.flight_fares` and then verify, because verify refuses the search's own key.
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
  `agent.transcription_provider`, `gateway.session_context`, `gateway.config.Platform` and `tools.registry`,
  all listed in `hermes_cli/data/plugin_api.json`. Do not import another core module. Read a secret through
  `_common.secret`, never from `os.environ`.
- **A file belongs to its sender.** `ingress.py` notes the files of an admitted message under the user who sent
  it and deletes the files of a message it drops. `read_attachment` opens only a file noted for the caller of
  the turn (`attachments.find`): never open a path the model supplied by any other route. Two tests pin this:
  `test_tools_attachments.py::test_another_users_file_is_unknown_to_this_caller` and
  `::test_a_made_up_path_reads_nothing`.
- **Files from strangers are parsed apart.** A PDF or an office file is parsed by `_document_worker.py` in a
  process of its own with a time and a memory limit, and that script imports nothing from the plugin. Audio
  goes through ffmpeg with the fixed format and protocol lists in `speech.py`. Keep both, and keep the reading
  path free of network calls.
- **Three ways out of the plugin.** `_common.request_json` is the only HTTP function, `speech._stream` opens
  the only WebSocket, to Amazon Transcribe (the proxy allows that one host and port,
  `deploy/accomy/squid.conf`), and `ocr._ask` is the only model call the plugin makes itself: through
  `ctx.llm`, with no override, so the host chooses the model and holds the key.
- **File content is data.** It reaches the model only as a `read_attachment` result, which carries the note
  that says so: `ingress.py` takes the text the adapter put into a message out again
  (`attachments.own_words`), so the length limit and the out-of-scope screen see what the sender typed. An
  order's prices and facts still come only from quotes.
- **Applied migrations are frozen.** `orderstore/migrate.py` compares checksums and refuses a changed file.
  Numbers run without gaps, and a migration file holds no transaction statement. A non-loopback database is
  refused unless its connection string verifies TLS (`orderstore/db.py`).
- **Limits are named constants** in `ingress.py`, `attachments.py`, `documents.py`, `parsers.py`,
  `_document_worker.py`, `ocr.py`, `speech.py`, `_common.py`, `tools_flights.py`, `tmc_client.py`, `tools_orders.py`, `tools_research.py` and `quotes.py`. The "Limits" section of the README states the same numbers: change both together.
- **Secrets are named, never stored.** `distributions/accomy/distribution.yaml` lists the variable names. No
  fixture, test or document carries a value.

## Tests

- Run `scripts/run_tests.sh tests/plugins/accomy_travel`, never bare `pytest`.
- Fixtures under `tests/plugins/accomy_travel/fixtures/` are trimmed real supplier answers, as the test
  modules say. `fixtures/attachments/` holds small real files of each format the bot reads, written with the
  libraries named in `test_documents.py`. No test reaches Amazon Transcribe or a model: the socket and the
  model interface are faked. Tests replace the transport (`_common.request_json`) or the client functions
  (`tmc_client.flight_search` and its siblings). The suite refuses a lookup of any external host name
  (`tests/conftest.py`), so a test that reaches a real supplier fails unless it is marked `live` or
  `allow_network`.
- The order tests need a local PostgreSQL container. The command that starts it is at the top of
  `tests/plugins/accomy_travel/test_orderstore.py`. Without it those tests are skipped, and a run with skipped
  order tests proves nothing about orders.
- A test names the behaviour it protects. When you fix a bug, show that the new test fails without the fix.
