---
paths:
  - "deploy/accomy/**"
  - "distributions/accomy/**"
---

# Accomy profile and container deployment

`deploy/accomy/README.md` is the operator document for the container deployment, the order database, the demo
payment page and the EC2 runbook. The profile is described in `plugins/accomy_travel/README.md`. Read the one
you are changing first, and update it in the same change.

## The profile: `distributions/accomy/`

| File | What it decides |
|---|---|
| `config.yaml` | The model endpoint, the tools the model gets, the gateway and Telegram settings |
| `SOUL.md` | The assistant's identity |
| `locales/en.yaml` | The wording of every gateway message an end user can reach |
| `distribution.yaml` | The profile's description and the names of the secrets it needs |

The profile is for end users who are not the operator. These settings carry that, and
`tests/plugins/accomy_travel/test_distribution.py` and `test_enduser_surface.py` check them:

- The model gets the `accomy_travel` toolset and nothing else; `no_mcp` beside it keeps MCP servers out.
  Memory, skills, cron, the terminal, files and the web toolset are off, so nothing carries between users and
  the model has no general way to reach a network host.
- `gateway.hide_operator_details: true` and `telegram.accept_attachments: false`.
- `telegram.allow_admin_from` and `telegram.group_allow_admin_from` are `["0"]`. The placeholder turns the
  slash policy on and matches nobody. Never leave either list empty: upstream turns gating off for a scope
  with no admin list, and every user can then run every command.
- `locales/en.yaml` rewords or blanks gateway messages; an empty string removes a message.
  `test_enduser_surface.py` fails when a bundled gateway message that names the agent is neither reworded
  there nor listed as operator-only. Expect that test to flag new upstream messages after a merge from
  upstream.
- `distribution.yaml` names variables. It never holds a value.

## The deployment: `deploy/accomy/`

| File | What it does |
|---|---|
| `docker-compose.yml` | Three services: `bot`, `egress` (the Squid proxy) and `db` (a relay to the order database, in the `db` profile) |
| `squid.conf` | The hosts the bot may reach. Everything else is refused |
| `build-image.sh` | Builds the image from a snapshot of the checkout |
| `seed-secrets.sh` | Streams a `.env` into the data volume without printing it |
| `watch.sh` | Follows the bot's conversations, read-only |
| `ec2-bootstrap.sh` | Sets up an EC2 host. It has not run on a real instance yet |
| `payment-demo/` | The demo payment page: one AWS Lambda, an HTTP API and a DynamoDB table |

Keep these true:

- The bot container publishes no port and has a read-only root. It drops every capability and adds back the
  six its process supervisor needs to hand the volume to the bot user and drop to it; the gateway itself runs
  with none. It mounts no Docker socket and no writable host path: `config.yaml`, `SOUL.md`, `locales/` and
  the database CA certificate are read-only mounts. Secrets exist only in the data volume.
- The proxy allows HTTPS to the hosts in `squid.conf` and nothing else. It decides by host, so it cannot tell a
  search request from an order request to the same supplier. The supplier boundary is code in the plugin, not
  this file. A new supplier host is added here and to `SUPPLIER_BUDGETS` in `plugins/accomy_travel/_common.py`.
- `build-image.sh` puts every file git does not ignore into the image, untracked ones too. A stray file in the
  checkout ships. The script stops on an untracked file whose name looks like a secret; do not work around
  that. It does not check tracked files.
- One process polls a Telegram bot token. Never start a second stack, `hermes gateway` or `hermes chat` with
  the same token while the bot runs. Use another Compose project name and a dummy `.env` for experiments.
- The running bot is the image, not the checkout. A change to plugin code reaches it after
  `./build-image.sh` and `docker compose up -d bot`. A change to the `.env` reaches it after
  `./seed-secrets.sh` and a restart.
- `payment-demo/deploy.sh` creates cloud resources. So does anything that touches DNS, a database or an EC2
  host. Ask the user before running any of them.
- The payment page charges nothing. It accepts one public test card and sends the buyer one Telegram message.
  Apart from the order database, it is the only service the order tools contact: to void the link of a
  cancelled order and to ask whether an order was paid. The bot and the page share the key named
  `ACCOMY_PAYMENT_LINK_SECRET`.
