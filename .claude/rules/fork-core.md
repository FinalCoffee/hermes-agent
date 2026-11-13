---
paths:
  - "agent/operator_details.py"
  - "agent/prompt_builder.py"
  - "agent/system_prompt.py"
  - "agent/turn_failure_copy.py"
  - "agent/turn_truncation.py"
  - "gateway/approval_actor.py"
  - "gateway/platforms/base.py"
  - "gateway/platforms/base_turn_notice.py"
  - "gateway/relay/prompt_resolvers.py"
  - "gateway/run.py"
  - "gateway/run_inbound.py"
  - "gateway/run_turn.py"
  - "gateway/run_turn_final_reconcile.py"
  - "gateway/run_turn_persistence.py"
  - "gateway/run_turn_prepare.py"
  - "gateway/run_turn_teardown.py"
  - "gateway/session_ingress.py"
  - "gateway/skill_slash_access.py"
  - "gateway/slash_commands_session.py"
  - "gateway/stream_consumer.py"
  - "gateway/suggested_replies.py"
  - "hermes_cli/config_defaults.py"
  - "locales/af.yaml"
  - "locales/ar.yaml"
  - "locales/de.yaml"
  - "locales/en.yaml"
  - "locales/es.yaml"
  - "locales/fr.yaml"
  - "locales/ga.yaml"
  - "locales/hu.yaml"
  - "locales/it.yaml"
  - "locales/ja.yaml"
  - "locales/ko.yaml"
  - "locales/pt.yaml"
  - "locales/ru.yaml"
  - "locales/tr.yaml"
  - "locales/uk.yaml"
  - "locales/zh-hant.yaml"
  - "locales/zh.yaml"
  - "plugins/platforms/telegram/adapter.py"
  - "plugins/platforms/telegram/telegram_attachments.py"
  - "plugins/platforms/telegram/telegram_slash_confirm.py"
  - "plugins/platforms/telegram/telegram_stickers.py"
  - "plugins/platforms/telegram/telegram_suggestions.py"
  - "plugins/platforms/discord/adapter.py"
  - "plugins/platforms/slack/adapter.py"
  - "plugins/platforms/whatsapp_cloud/adapter.py"
  - "scripts/boundary_lint_baseline.json"
  - "tests/agent/test_operator_details.py"
  - "tests/gateway/test_*suggest*.py"
  - "tests/gateway/test_slash_gate_skills_status_always.py"
  - "tests/gateway/test_telegram_attachment_refusal.py"
  - "tests/gateway/test_telegram_sticker_without_vision.py"
  - "tests/gateway/test_prompt_opener_binding.py"
  - "tests/gateway/test_skill_slash_native_boundary.py"
  - "tests/gateway/test_stream_flood_frozen_preview.py"
  - "tests/gateway/test_telegram_approval_buttons.py"
  - "website/docs/user-guide/configuration.md"
  - "website/docs/user-guide/messaging/telegram.md"
  - "website/docs/user-guide/profile-distributions.md"
  - "pyproject.toml"
  - "uv.lock"
  - "Dockerfile"
---

# Upstream files the fork changed

This file belongs to upstream and the fork changed it, or it is a module the fork added to core. Upstream
edits most of these often, so every extra line here is a future merge conflict. `FORK.md` has the table of
changed files and the procedure for bringing in new upstream work.

The fork made three changes to core. Each is a general gateway feature that the Accomy profile uses.

## Suggested replies as Telegram buttons

- The model ends a reply with one line, `[suggestions: A | B | C]`. `gateway/suggested_replies.py` takes the
  options from the last complete footer (two to four, each cut to 40 characters) and removes every footer line
  from the text. The removal happens on every platform: in the final text, in the streaming view
  (`hide_partial_suggestions`) and in the edits that reconcile a streamed message.
- Only Telegram shows buttons, and only when `telegram.suggested_replies` is the YAML boolean `true`. An absent
  key is off. `plugins/platforms/telegram/telegram_suggestions.py` keeps one keyboard per chat and topic, in
  memory, for at most 24 hours and 256 keyboards.
- A tap is authorised like a message before the keyboard is claimed, becomes the tapper's next message, and
  collapses the keyboard to one "✓ option" button. Outside a private chat only the person who asked can tap.
- Core does not teach the marker to the model. The Accomy plugin's prompt section does
  (`plugins/accomy_travel/policy.py`), and it keeps its own copy of the marker rule because the plugin API does
  not expose `gateway/suggested_replies.py`.
- Keep true: the streaming view and the final view go through the same `_scan`, so they cannot disagree.
  `attach_suggested_replies` is called for every confirmed final reply that is not ephemeral, even one with no
  options, because that call retires the previous keyboard. The base adapter's `attach_suggested_replies`
  returns `False`.
- Tests: `tests/gateway/test_suggested_replies.py`, `test_suggested_replies_turn_e2e.py`,
  `test_telegram_suggestions.py`, `test_final_suggestions_delivery.py`,
  `test_streamed_suggestions_reconcile.py`, `test_stream_consumer_suggestions.py`.

## Slash policy over skill commands, and refusing attachments

- The slash policy is `allow_admin_from` and `user_allowed_commands` for private chats and the `group_`
  variants for groups (`gateway/slash_access.py`). A scope is gated when its admin list is set.
- The policy also decides skill and bundle commands (`gateway/skill_slash_access.py`: a refusal reads like an
  unknown command, and a policy read that raises refuses), `/status` and `/context` during a running turn
  (`gateway/run_inbound.py` checks access first), and the "Always" answer on a slash confirm card.
- `gateway/approval_actor.py: slash_confirm_refusal` takes a required keyword argument `choice`. Every adapter
  passes it. A caller that upstream adds without it raises `TypeError`.
- A Telegram update card accepts only `y` or `n`, and only for the card id this process sent.
- `telegram.accept_attachments` defaults to `true`, and any value other than the YAML boolean `true` refuses.
  `plugins/platforms/telegram/telegram_attachments.py` then refuses photos, videos, video notes, audio, voice
  notes, documents, stickers and animations before any download, cache write or vision call. The agent
  receives the caption, if any, and a note that an attachment was not read. An album is one turn.
- Sticker handling lives in `plugins/platforms/telegram/telegram_stickers.py`. It reaches the fetch and cache
  helpers through the adapter module, so tests patch them on `plugins.platforms.telegram.adapter`.
- Tests: `tests/gateway/test_slash_gate_skills_status_always.py`, `test_telegram_attachment_refusal.py`,
  `test_telegram_sticker_without_vision.py`.

## gateway.hide_operator_details

- Default `false`. `agent/operator_details.py: operator_details_hidden()` is the only reader and reads the
  config on every call. Never read it at import: `agent/turn_truncation.py` calls `site_copy("truncated")` at
  the point of use for that reason.
- When on, the system prompt replaces the Hermes identity sentence with "You are a helpful assistant."
  (unless a `SOUL.md` is loaded; the behaviour rules after the sentence stay) and drops the help guidance,
  the local host lines of the environment hints and the profile line. Replies drop the error type and text,
  the model name on an empty reply, the reason on a partial reply, and the session details that `/new` and an
  auto-reset print.
- The failure copy in `agent/turn_failure_copy.py` goes through `site_copy`, which returns one generic line
  when the switch is on. The turn error notice, the empty reply and the partial reply each check
  `operator_details_hidden()` themselves. A new failure path that does neither shows operator text to end
  users.
- The generic lines are three locale keys: `gateway.notify.turn_error_generic`,
  `gateway.errors.empty_reply_generic` and `gateway.errors.failed_reply_generic`. All 17 `locales/<lang>.yaml`
  packs carry them. A profile rewords them in its own `locales/<lang>.yaml`, which the upstream loader reads
  before the bundled pack (`agent/i18n_layers.py`).
- Tests: `tests/agent/test_operator_details.py`, and `tests/plugins/accomy_travel/test_enduser_surface.py` for
  the Accomy profile's wording.

## When you edit one of these files

- **Size ratchet.** A file over 2,000 lines may only shrink. The Telegram, Discord and Slack `adapter.py`
  files, `gateway/run.py`, `gateway/platforms/base.py`, `hermes_cli/config_defaults.py` and
  `gateway/run_turn.py` are over it, and `gateway/run_inbound.py` is just under. New code goes in a sibling module, which is why the fork
  created `gateway/platforms/base_turn_notice.py`, `gateway/run_turn_teardown.py` and the
  `plugins/platforms/telegram/telegram_*.py` modules.
- **The stock prompt stays the same.** With every key at its default, the system prompt, the tool list and
  the gateway's wording are upstream's. A change to the stock system prompt or tool list fails
  `tests/ci/test_prompt_surface.py`. One thing happens with no key set: a `[suggestions: ...]` line is removed
  from a reply.
- **A new config key** goes in `DEFAULT_CONFIG` and ships with its docs page in the same change
  (`tests/ci/test_docs_drift.py`).
- **A new locale key** is added to all 17 packs.
- **An import of core code moved between Telegram modules** changes `scripts/boundary_lint_baseline.json`,
  which counts each core import per importing file and imported name. Run
  `python scripts/boundary_lint.py --update` and check that its diff holds only the lines you meant.
- **`pyproject.toml`, `uv.lock`, `Dockerfile`.** The fork adds the `postgres` extra (asyncpg) for the Accomy
  order store, the same package in the test group, and `--extra postgres` in the image build. After editing
  `pyproject.toml`, run `hermes pm lock` and commit `uv.lock` with it.
- Run the tests of the feature you touched with `scripts/run_tests.sh <paths>`, then `python scripts/check`.
