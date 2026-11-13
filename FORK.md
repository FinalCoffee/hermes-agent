# Accomy fork of Hermes Agent

This repository is a fork of [Hermes Agent](https://github.com/NousResearch/hermes-agent). It turns Hermes
into **Accomy AI Travel**, a Telegram travel-planning assistant for end users, as a demonstration: it searches
hotels, flights and event tickets, applies a travel standard, and takes orders that are recorded only in its
own database.

This document says how the fork differs from upstream and how to keep the two in step. The parts the fork
adds have their own documents:

- [`plugins/accomy_travel/README.md`](plugins/accomy_travel/README.md): the plugin, its tools, limits, order
  store and travel standard, and how to install and run the profile.
- [`deploy/accomy/README.md`](deploy/accomy/README.md): the container deployment, the order database, the demo
  payment page and the EC2 runbook.

## Branches

| Branch | What it is |
|---|---|
| `main` | Upstream Hermes Agent, unmodified. It is the base the fork is measured against |
| `develop` | The fork: `main` plus the commits below. Work is committed here and pushed to `origin` |

The numbers in this document were taken at commit `cbe3896c0b`. To take them again:

```sh
git log --oneline main..develop          # the fork's commits
git diff --stat main...develop | tail -1 # files and lines changed
git diff --numstat main...develop        # per file
```

## The fork at a glance

180 files differ from upstream, not counting this file and the Claude Code rules in `.claude/`: 27,833 lines
added and 251 removed.

| Part | Files | Lines added | Lines removed |
|---|---:|---:|---:|
| Core code: `agent/`, `gateway/`, `plugins/platforms/`, `hermes_cli/`, `locales/` | 47 | 1,162 | 239 |
| Tests of the core changes: `tests/gateway/`, `tests/agent/` | 14 | 2,288 | 3 |
| Docs of the core changes: `website/docs/` | 3 | 57 | 0 |
| Build: `pyproject.toml`, `uv.lock`, `Dockerfile`, `scripts/boundary_lint_baseline.json` | 4 | 54 | 9 |
| Plugin: `plugins/accomy_travel/` | 37 | 9,050 | 0 |
| Plugin tests: `tests/plugins/accomy_travel/` | 59 | 13,276 | 0 |
| Profile: `distributions/accomy/` | 4 | 348 | 0 |
| Deployment: `deploy/accomy/` | 12 | 1,598 | 0 |

Fifteen commits carry the code, oldest first:

| Commit | What it adds |
|---|---|
| `a6576595c9` | Core: suggested replies as Telegram inline buttons |
| `3310ba2ff5` | The Accomy AI Travel plugin and its locked-down profile |
| `450304c319` | Core: the slash policy covers skill commands; `telegram.accept_attachments` |
| `b98b848b27` | Plugin: usage limits for an open bot, and the order store |
| `8451fd3b26` | Container deployment with an outbound allowlist |
| `93a87c854d` | Core: `gateway.hide_operator_details` |
| `d54b197e14` | Plugin: demonstration orders, customers and trips, the demo payment page |
| `512542048e` | Plugin: the travel standard for hotel and flight search |
| `cbe3896c0b` | Plugin: a test that an order sends no request to any supplier |
| `266744927a` | Plugin: limits sized for about twenty people at once |
| `99c86d39fd` | Plugin: a flight search waits 60 seconds for TMC |
| `4676ec7b1a` | Deployment: the EC2 path, and how to operate the instance |
| `1ad39f328c` | Plugin: the bot reads photos, voice notes and files |
| `098db70745` | Plugin: scanned PDF pages are read |
| `391501b525` | Plugin: flights are searched as round trips and multi-city trips |

## Changes to upstream code

The fork changes core for three features. Each is a general gateway feature with nothing Accomy-specific in
it. Suggested replies and hidden operator details are off until their key is set, and attachments are refused
only when their key is set to `false`. The slash policy changes have no key of their own: they apply wherever
a slash policy is configured.

### 1. Suggested replies as Telegram buttons

A model can end a reply with one line, `[suggestions: A | B | C]`. The gateway removes that line from the text
and, on Telegram, shows the options as buttons under the reply. Tapping one sends it as the user's next
message.

- **Switch:** `telegram.suggested_replies: true`. The default is off, and only the YAML boolean `true` turns
  it on.
- **Options:** two to four, each cut to 40 characters, taken from the last footer line of the reply.
- **A tap** is authorised like a message, becomes the tapper's next message, and collapses the keyboard to a
  single "✓ option" button. In a group only the person who asked can tap. A keyboard lives in memory for at
  most 24 hours, and a new reply in the same chat or topic retires the previous one.
- **Other platforms** show no buttons. The footer line is still removed from the text.
- **Who teaches the model:** not core. A plugin or a skill has to tell the model about the line. The Accomy
  plugin does, in its `accomy.suggested_replies` prompt section.
- **Code:** `gateway/suggested_replies.py`, `plugins/platforms/telegram/telegram_suggestions.py`.
- **Docs:** "Suggested replies (tap-to-answer buttons)" in `website/docs/user-guide/messaging/telegram.md`.

### 2. The slash policy covers skill commands, and Telegram can refuse attachments

Upstream's slash policy (`allow_admin_from`, `user_allowed_commands`, and the `group_` variants) decides which
slash commands a non-admin may run. A scope is gated when its admin list is set. In the fork the policy covers
more:

- **Skill and bundle commands** follow the policy. A refused one reads like an unknown command.
- **`/status` and `/context` during a running turn** follow the policy.
- **"Always" on a slash confirm card** is an admin's choice in a gated scope.
- **Telegram update cards** are answered only through the card the running process sent, with yes or no.
- **Refusing attachments:** `telegram.accept_attachments` defaults to `true`. With `false`, photos, videos,
  video notes, voice notes, audio, files, stickers and GIFs are refused before any download or vision call,
  and the agent is told that an attachment was not read. This is for text-only bots.
- **Code:** `gateway/skill_slash_access.py`, `gateway/approval_actor.py`, `gateway/run_inbound.py`,
  `plugins/platforms/telegram/telegram_attachments.py`.
- **Docs:** "Refusing attachments (text-only bots)" and the slash policy section in
  `website/docs/user-guide/messaging/telegram.md`.

### 3. `gateway.hide_operator_details`

For a bot whose users are not the people who run it. With the key set to `true`:

- The system prompt names the assistant "a helpful assistant" in place of the Hermes identity sentence (when
  the profile has no `SOUL.md`). It leaves out the help guidance that points at the Hermes docs, the host,
  home and working directory lines, and the profile line.
- Replies leave out the error type and message, the model name on an empty reply, the reason on a partial
  reply, and the session details that `/new` and an auto-reset print. A failed turn gets one plain line.
- The plain lines are three locale keys in all 17 language packs. They are translated in `zh` and `zh-hant`
  and are English in the other packs. A profile rewords any gateway message in its own `locales/<lang>.yaml`,
  which upstream's loader already reads before the bundled pack.
- **Switch:** `gateway.hide_operator_details: true`. The default is `false`, which leaves the stock prompt and
  wording unchanged.
- **Code:** `agent/operator_details.py` is the only reader, and it reads the config on every call.
- **Docs:** "Hiding operator details from chat" in `website/docs/user-guide/configuration.md`.

### What changes without setting a key

Everything else behaves as upstream does until a key is set. These three effects need no key:

- A `[suggestions: ...]` line is removed from a reply on every platform whenever a model writes one.
- Where a slash policy is configured, it also covers skill and bundle commands, `/status` and `/context`
  during a running turn, and the "Always" answer.
- A Telegram update card is answered only through the card the running process sent, with yes or no.

### Upstream files the fork changed

| File | For | What changed |
|---|---|---|
| `gateway/suggested_replies.py` (new) | 1 | The marker: parsing, stripping, hiding a half-written line while streaming |
| `gateway/platforms/base.py` | 1 | The final reply strips the footer and hands the options to the adapter |
| `gateway/platforms/base_turn_notice.py` (new) | 1, 3 | The "turn failed" notice, moved out of `base.py`; plain wording when details are hidden |
| `gateway/run_turn.py` | 1 | Strips the footer from the edits that reconcile a streamed reply |
| `gateway/run_turn_final_reconcile.py` | 1 | Attaches the options to a reply that was streamed |
| `gateway/run_turn_teardown.py` (new) | 1 | Turn cleanup, moved out of `run_turn.py` to keep that file from growing |
| `gateway/session_ingress.py` | 1 | Passes the options to the final send |
| `gateway/stream_consumer.py` | 1 | The streaming view hides the footer |
| `plugins/platforms/telegram/telegram_suggestions.py` (new) | 1 | The keyboard and the tap |
| `plugins/platforms/telegram/telegram_slash_confirm.py` (new) | 1, 2 | The slash confirm card, moved out of the adapter; passes the chosen answer to the policy |
| `plugins/platforms/telegram/adapter.py` | 1, 2 | Hooks for the above, the update card check, the two new Telegram keys |
| `gateway/skill_slash_access.py` (new) | 2 | The policy check for skill and bundle commands |
| `gateway/approval_actor.py` | 2 | "Always" is for admins when the scope is gated |
| `gateway/run_inbound.py` | 2 | Access is checked before `/status`, `/context`, a bundle and a skill |
| `gateway/relay/prompt_resolvers.py` | 2 | Passes the chosen answer to the policy |
| `plugins/platforms/discord/adapter.py`, `slack/adapter.py`, `whatsapp_cloud/adapter.py` | 2 | Pass the chosen answer to the policy |
| `plugins/platforms/telegram/telegram_attachments.py` (new) | 2 | Refusing attachments |
| `plugins/platforms/telegram/telegram_stickers.py` (new) | 2 | Sticker description, moved out of the adapter |
| `agent/operator_details.py` (new) | 3 | Reads the switch |
| `agent/system_prompt.py`, `agent/prompt_builder.py` | 3 | What the prompt leaves out |
| `agent/turn_failure_copy.py`, `agent/turn_truncation.py` | 3 | One plain line for a failed or truncated turn, read when it is needed |
| `gateway/run.py`, `gateway/run_turn_persistence.py` | 3 | Partial and empty replies without the reason or the model name |
| `gateway/run_turn_prepare.py`, `gateway/slash_commands_session.py` | 3 | No session details after an auto-reset or `/new` |
| `hermes_cli/config_defaults.py` | 2, 3 | The defaults of `telegram.accept_attachments` and `gateway.hide_operator_details` |
| `locales/*.yaml` (17 files) | 1, 3 | Three toasts for the buttons and three plain failure lines |
| `website/docs/user-guide/messaging/telegram.md`, `configuration.md`, `profile-distributions.md` | 1, 2, 3 | The docs of the new keys |
| `scripts/boundary_lint_baseline.json` | 1, 2 | Imports that moved with the Telegram modules, and one new one |
| `tests/gateway/` (9 new files), `tests/agent/test_operator_details.py` (new) | 1, 2, 3 | The tests of the three features |
| `tests/gateway/test_prompt_opener_binding.py`, `test_skill_slash_native_boundary.py`, `test_stream_flood_frozen_preview.py`, `test_telegram_approval_buttons.py` | 1, 2 | Upstream tests adjusted to the changed hooks |
| `pyproject.toml`, `uv.lock`, `Dockerfile` | Plugin | The `postgres` extra (asyncpg) for the order store, also in the test group and in the image. `pypdf` in the test group, for the plugin's PDF reading (the image has it through the `documents` extra). `pypdfium2`, which draws scanned PDF pages for the plugin to have read: in the test group, and in the image through the `documents-pdf` extra. Upstream leaves that extra out of its image, and the comment above it in `pyproject.toml` still says so |

## What exists only in the fork

| Part | What it is |
|---|---|
| `plugins/accomy_travel/` | The plugin: 17 model tools (12 for search, 4 for orders, 1 that reads a file a user sent), flight search by round trip and multi-city trip, an inbound gate with usage limits, the prompt sections that make the assistant a travel assistant, the order store, the travel standard, and speech-to-text for voice notes through Amazon Transcribe |
| `distributions/accomy/` | The operator profile: the model gets only the plugin's tools, photos, voice notes and files are read within daily limits, operator details are hidden, and the gateway messages an end user can reach are reworded or blanked |
| `deploy/accomy/` | A container deployment in which the bot reaches only the hosts on an allowlist, the scripts that put it on an AWS EC2 instance and operate it there, and a demo payment page on AWS Lambda |
| `tests/plugins/accomy_travel/` | 28 test files, 16 fixtures trimmed from real supplier answers, and 8 small sample files (PDF, office, audio) for the attachment tests |

Three properties of the plugin matter to anyone who changes it:

- **Suppliers are only read.** The TMC client refuses every request outside a short list of read endpoints,
  so no order, payment or cancellation can reach TMC. An order is a row in the bot's own PostgreSQL database
  with the mode `SIMULATED`.
- **An order comes from a quote.** A search tool remembers what it showed each user under a quote id, and
  `place_order` accepts quote ids only. No price or supplier id comes from the model.
- **The travel standard is enforced in code.** The hotel and flight tools leave out whatever is outside it, so
  it gets no quote and cannot be ordered.
- **A file belongs to its sender.** The profile accepts photos, voice notes and files. The inbound gate notes
  each file under the user who sent it, and the tool that reads files opens only a file noted for the caller
  of the turn. Documents are parsed in a separate process, and the files of a dropped message are deleted.

### Why the plugin is not offered upstream

Upstream does not take third-party product plugins in its tree: they ship as standalone plugin repositories
(`plugins/AGENTS.md`, "No new third-party-product plugins"). The plugin is written so that it can leave. It
imports only names on the published plugin API (`hermes_cli/data/plugin_api.json`), it touches no core file,
and its directory can move unchanged to `$HERMES_HOME/plugins/accomy_travel/` or to its own repository.

The three core features are general and could be proposed upstream. None has been.

## Keeping the fork in step with upstream

This clone has one remote, `origin`, which is the fork. Its `main` is the fork's copy of upstream, so new
upstream commits arrive only after an `upstream` remote is added.

### Procedure

```sh
# Once.
git remote add upstream https://github.com/NousResearch/hermes-agent.git

# Each time. Note the old base first: it is the commit the fork's commits sit on.
old_base=$(git merge-base main develop)
git fetch upstream
git switch main && git merge --ff-only upstream/main
git switch develop && git rebase --onto main "$old_base" develop
```

The rebase rewrites the fork's commits, so the next push of `develop` needs `--force-with-lease`. Agree on
that with everyone who has the branch before you start.

### Where it will conflict

Upstream edits these files often, and the fork changed them. Expect conflicts here, most likely first:

1. `plugins/platforms/telegram/adapter.py`. The fork moved code out of it, so an upstream edit to a moved line
   conflicts as a change against a deletion. Apply the upstream edit to the `telegram_*.py` module that now
   holds the code.
2. `hermes_cli/config_defaults.py`: the `telegram` and `gateway` blocks.
3. `gateway/run.py`, `gateway/run_turn.py`, `gateway/run_inbound.py`, `gateway/platforms/base.py`.
4. `agent/system_prompt.py`, `agent/prompt_builder.py`.
5. `locales/*.yaml`. Keep upstream's text and add the fork's six keys again in the same places.

Regenerate these instead of merging them by hand:

| File | How |
|---|---|
| `uv.lock` | `hermes pm lock`, committed together with `pyproject.toml` |
| `scripts/boundary_lint_baseline.json` | `python scripts/boundary_lint.py --update`, then check that the diff holds only the Telegram lines |

### What to check after a rebase

- **Upstream callers of changed functions.** `slash_confirm_refusal` needs the keyword `choice`, and
  `build_environment_hints` takes `show_host`. A caller upstream added since the last rebase lacks them.
- **The size ratchet.** A file over 2,000 lines may only shrink. If upstream grew one that the fork also
  touches, move the fork's code to a sibling module. `gateway/run_inbound.py` is the closest to the limit.
- **No config read at import.** `agent/turn_truncation.py` must not call `site_copy` at module level.
- **New upstream wording.** `tests/plugins/accomy_travel/test_enduser_surface.py` fails when upstream added a
  gateway message that names Hermes and the Accomy profile neither rewords it nor lists it as operator-only.
  Add it to `distributions/accomy/locales/en.yaml`.
- **The plugin API.** The plugin imports `agent.secret_scope`, `agent.transcription_provider`,
  `gateway.session_context`, `gateway.config` and `tools.registry`, and it asks the host's model to read scanned
  pages through `ctx.llm`. If upstream moved one of these, the plugin fails to load or scanned pages go unread.
- **Attachments.** The plugin reads what the gateway hands it for a photo, a voice note or a file: the
  message's `media_urls` and `media_text_inlined`, the notes that name a saved path, and the transcription
  provider contract. `tests/plugins/accomy_travel/test_attachments.py`, `test_tools_attachments.py` and the
  voice test in `test_distribution.py` fail when one of these changed.

Then run the fork's tests and the lint:

```sh
scripts/run_tests.sh tests/plugins/accomy_travel tests/agent/test_operator_details.py \
    tests/gateway/test_suggested_replies.py tests/gateway/test_suggested_replies_turn_e2e.py \
    tests/gateway/test_telegram_suggestions.py tests/gateway/test_final_suggestions_delivery.py \
    tests/gateway/test_streamed_suggestions_reconcile.py tests/gateway/test_stream_consumer_suggestions.py \
    tests/gateway/test_slash_gate_skills_status_always.py tests/gateway/test_telegram_attachment_refusal.py \
    tests/gateway/test_telegram_sticker_without_vision.py tests/gateway/test_prompt_opener_binding.py \
    tests/gateway/test_skill_slash_native_boundary.py tests/gateway/test_stream_flood_frozen_preview.py \
    tests/gateway/test_telegram_approval_buttons.py \
    tests/ci/test_docs_drift.py tests/ci/test_prompt_surface.py tests/scripts/test_boundary_lint.py
python scripts/check
```

The order tests in `tests/plugins/accomy_travel/` need a local PostgreSQL container and are skipped without
it. The command that starts it is at the top of `tests/plugins/accomy_travel/test_orderstore.py`.

The bot runs on an AWS EC2 instance from an image, so it changes only after a commit, a new image and the
bootstrap run again on the instance. `deploy/accomy/README.md` has that procedure under "Operating the
instance".

## Rules for work on the fork

- Keep the difference from upstream small. New behaviour goes into the plugin or the deployment, and an
  upstream file is edited only for one of the three core features. A change that touches an upstream file adds
  the file to the table above.
- Suppliers are only read. Nothing adds an order, payment, cancel, change or refund endpoint to the TMC
  client's list, and nothing writes to the TMC tenant.
- The repository is public. No secret value, private hostname, cloud account id or personal data is committed.
  A weakness in upstream Hermes that falls under section 3.1 of `SECURITY.md` is reported privately, as that
  file asks.
- Upstream's rules apply to all code here: `AGENTS.md` at the root and in each area.

## Instructions for Claude Code

Upstream ships `AGENTS.md` files, which Claude Code reads. The fork adds its own instructions in
`.claude/rules/` and leaves upstream's files alone:

| File | Loaded | What it covers |
|---|---|---|
| `.claude/rules/fork.md` | In every session | That this is a fork, what it adds, and the rules above |
| `.claude/rules/fork-core.md` | With an upstream file the fork changed | The three core features and what to keep true in them |
| `.claude/rules/accomy-plugin.md` | With a file of the plugin or its tests | The plugin's modules and what must stay true |
| `.claude/rules/accomy-deploy.md` | With a file of the profile or the deployment | The profile's settings and the deployment's guarantees |

Do not add a `CLAUDE.md` to this repository. Claude Code reads `AGENTS.md` only while the project has no
`CLAUDE.md`, so adding one would switch off the root `AGENTS.md` and every per-area one. This is how Claude
Code's documentation describes it (<https://code.claude.com/docs/en/memory>); check it again when Claude Code
changes.
