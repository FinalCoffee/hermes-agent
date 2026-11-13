# This checkout is a fork: Accomy AI Travel

Branch `main` is upstream Hermes Agent, unmodified. Branch `develop` is the fork: upstream plus the changes
that `FORK.md` lists file by file.

The root `AGENTS.md` and each area's `AGENTS.md` are upstream's files. They stay the rules for all code here.
The files in `.claude/rules/` add what the fork needs on top and never replace them.

## Never add a CLAUDE.md to this repository

Claude Code reads `AGENTS.md` only while the project has no `CLAUDE.md`, `.claude/CLAUDE.md` or
`CLAUDE.local.md` (its documentation: <https://code.claude.com/docs/en/memory>). Creating any of the three
switches off the root `AGENTS.md` and every per-area one, without a warning. Fork instructions go in
`.claude/rules/*.md`. A rule file with `paths:` frontmatter loads when a matching file is read or edited; one
without it, like this file, loads in every session.

## What the fork adds

| Part | Where | Read first |
|---|---|---|
| Suggested replies as Telegram buttons | `gateway/suggested_replies.py`, `plugins/platforms/telegram/telegram_suggestions.py` | `.claude/rules/fork-core.md` |
| Slash policy over skill commands, and `telegram.accept_attachments` | `gateway/skill_slash_access.py`, `plugins/platforms/telegram/telegram_attachments.py` | `.claude/rules/fork-core.md` |
| `gateway.hide_operator_details` | `agent/operator_details.py` | `.claude/rules/fork-core.md` |
| Accomy AI Travel plugin and its tests | `plugins/accomy_travel/`, `tests/plugins/accomy_travel/` | `plugins/accomy_travel/README.md` |
| Accomy operator profile | `distributions/accomy/` | `plugins/accomy_travel/README.md` |
| Container deployment and demo payment page | `deploy/accomy/` | `deploy/accomy/README.md` |

`FORK.md` is the full account: every upstream file the fork changed, why, and how to bring in new upstream work.

## Where the bot runs

The bot is live on one AWS EC2 instance (Name tag `dev-accomy-bot`), set up by `deploy/accomy/`. The checkout
is not the running bot: a change reaches users only after a commit, a new image and the bootstrap run again
on the instance. `deploy/accomy/ec2-run.sh '<command>'` runs a command there through Systems Manager, and
`.claude/rules/accomy-deploy.md` says how to look at the deployment and what needs the user's word first.
The AWS profile name, the account id, the instance id and its addresses are kept out of this public
repository: ask the user for the profile.

## Rules for fork work

- **Keep the difference from upstream small.** New behaviour belongs in `plugins/accomy_travel/` or
  `deploy/accomy/`. Edit an upstream file only for one of the three core features above, or for what the
  plugin needs from the build (`pyproject.toml`, `uv.lock`, `Dockerfile`), and put new code in a sibling
  module instead of growing an upstream one. Do not edit the root `AGENTS.md`, `README.md`, `CONTRIBUTING.md`
  or `.gitignore` for the fork. When a change does touch an upstream file, add the file to the table in
  `FORK.md`.
- **Suppliers are read only.** Hotel and flight data comes from TMC through `plugins/accomy_travel/tmc_client.py`.
  Its `_READ_ENDPOINTS` list, with one pattern for the hotel detail read, is the only thing between the bot
  and TMC's order, payment, cancel, change and refund endpoints, because the network proxy allows the whole
  TMC host. Never add such an endpoint to the list, never call `_common.request_json` with a TMC address from
  another module, and never write to the TMC tenant (travel policies, employees). Orders are `SIMULATED` rows
  in the bot's own database.
- **The repository is public.** No secret value, private hostname, cloud account id or personal data goes into a
  commit, a test fixture or a rule file. Secrets live in the profile's `.env` only, and a value from it is
  never printed. A weakness in upstream Hermes that falls under `SECURITY.md` section 3.1 is reported
  privately and never described in a commit, an issue or a pull request.
- **One poller per bot token.** While a bot is running, never start `hermes gateway` or `hermes chat` against a
  profile whose `.env` holds the same Telegram token. Experiment in a throwaway Compose project with a dummy
  `.env`.
- **Applied order-store migrations are frozen.** The migration runner refuses a file that changed after it was
  applied. A correction is a new numbered file.
- **Check before a commit, and commit or push only when asked.** Run
  `scripts/run_tests.sh tests/plugins/accomy_travel` (never bare `pytest`), then `python scripts/check`.
