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
- `gateway.hide_operator_details: true`.
- `telegram.accept_attachments: true`, with what makes that safe for an open bot: the plugin's daily file and
  speech allowances, `stt.provider: accomy-transcribe` (the plugin's provider, so no built-in speech service
  is contacted), and `supports_vision: true` on the model, without which every photo costs a second model call.
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
| `ec2-bootstrap.sh` | Sets up an EC2 host: Docker, the image, the secrets, the stack. Run once for real; a second run, for a new build, has not been exercised |
| `ec2-run.sh` | Runs one command on the EC2 host through Systems Manager and prints its output |
| `payment-demo/` | The demo payment page: one AWS Lambda, an HTTP API and a DynamoDB table |

## The deployment that is running

The bot runs on one AWS EC2 instance set up by this directory, not on a developer's machine. Start here when
a task touches the running bot:

- **Find it.** The instance carries the Name tag `dev-accomy-bot`. It is in a private subnet with no public
  address and no inbound rule, so Systems Manager is the only way in. `deploy/accomy/ec2-run.sh '<command>'`
  finds it by the tag and runs the command as root. It needs `AWS_PROFILE` and `AWS_REGION`. The region is
  the one in the README's commands. The profile name, the account and the instance id are not recorded in
  this public repository: ask the user, or read them from your private notes about this project.
- **Look before you act.** `ec2-run.sh 'cd /opt/accomy && docker compose ps'` shows the containers (the bot,
  the proxy, and the database relay when the order store is set up), and
  `ec2-run.sh 'cd /opt/accomy && docker compose logs --since 15m bot | tail -n 80'` shows what the bot is
  doing. The README section "Operating the instance" has the rest: what to check when the bot does not
  answer, resource use, the order store, a restart, shipping a build, changing a secret, removing everything.
- **Where things are.** The image is in the ECR repository `accomy-hermes` under an immutable tag per commit
  (`poc-<commit>`). The profile's `.env` is the encrypted parameter `/accomy/bot/env`. On the instance the
  stack is the Compose project `accomy` in `/opt/accomy`. `/opt/accomy/.env` holds that host's settings and
  no secret: the image, the database host, the limits, the name of the secrets parameter and whether the bot
  is open to everyone. The secrets, the conversations and the bot's logs are in the Docker volume
  `accomy_data`. The bot reads its secrets from that volume, so a changed parameter does nothing until it is
  seeded and the bot restarted.
- **The checkout is not the bot.** A change to code, the profile or this directory reaches users only after a
  commit, a new image pushed under that commit's tag, and the bootstrap run again on the instance.
- **No other poller.** Any Compose stack of this bot on a developer's machine stays stopped: it holds the same
  Telegram token, and two pollers split the messages between them. Never start one to test something. A real
  conversation for a test needs another bot's token, in a throwaway Compose project.
- **Ask before you change it.** A restart, a new build, a changed secret, a resized or stopped instance and
  any new AWS resource all change a service people are using, in an account that is not yours. Read-only
  commands need no permission; everything else needs the user's word first.
- **AWS keeps every command.** Systems Manager stores the command text and its output. Never send a secret in
  a command, and never run one that prints a secret, such as a `cat` of the `.env` or a dump of a container's
  environment, or a customer's details. List variable names with `cut -d= -f1` when you need to know what is
  set, and compare a value by its hash, never by printing it.
- **zsh and colons.** In zsh, `$ACCOUNT:repository` is the variable with a modifier applied, not the variable
  followed by text. Write `${ACCOUNT}:repository` in any command that builds an ARN.

Keep these true:

- The bot container publishes no port and has a read-only root. It drops every capability and adds back the
  six its process supervisor needs to hand the volume to the bot user and drop to it; the gateway itself runs
  with none. It mounts no Docker socket and no writable host path: `config.yaml`, `SOUL.md`, `locales/` and
  the database CA certificate are read-only mounts. Secrets exist only in the data volume.
- The proxy allows HTTPS to the hosts in `squid.conf`, port 8443 of the one Amazon Transcribe host for
  speech-to-text, and nothing else. It decides by host, so it cannot tell a search request from an order
  request to the same supplier. The supplier boundary is code in the plugin, not this file. A new supplier
  host is added here and to `SUPPLIER_BUDGETS` in `plugins/accomy_travel/_common.py`. The Transcribe host
  carries the region of `transcribe_region` in the profile: change both together.
- Speech-to-text uses the key pair of the IAM user `dev-accomy-bot-transcribe`, which may only open a
  Transcribe stream. The pair lives in the profile's `.env` like every other secret. The bot cannot use the
  instance role: it cannot reach the metadata service, and that role can read the secrets parameter.
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
