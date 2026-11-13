# Accomy bot in Docker

Runs the Accomy AI Travel bot (`plugins/accomy_travel/`, profile `distributions/accomy/`) with the whole
Hermes process tree inside a container. This is the posture `SECURITY.md` §2.2 asks for when strangers can
message the bot: the operating system, not the agent, is the boundary.

This directory exists only in the Accomy fork. `FORK.md` at the repository root says how the fork differs from
upstream Hermes.

## What the bot container can reach

| | Allowed | Blocked |
|---|---|---|
| Files | Its image (read-only), the `data` volume, and in-memory scratch space | Every host file except the read-only profile mounts below. No host directory and no Docker socket are mounted |
| Profile | Reads `config.yaml`, `SOUL.md` and the `locales/` wording | Cannot change them: all three are read-only mounts. The plugin and hook directories of the volume are empty and read-only, so only the image's code is loaded |
| Network | HTTPS to the hosts listed in `squid.conf`, and one port of Amazon Transcribe for speech-to-text, through the `egress` proxy. With the `db` profile, one PostgreSQL server through the `db` relay | Any other host, bare IP addresses, plain HTTP, the host machine, the LAN, the cloud metadata service |
| Inbound | Nothing. Telegram is long-polled outbound | No port is published |
| Privilege | The gateway runs as uid 10000 with no capabilities and cannot gain any (`no-new-privileges`). The supervisor above it stays root with six capabilities | Memory, CPU and process limits |

A model that has been talked round, or a bug in the agent, can still read what the bot itself needs: the keys
in the volume's `.env` and the conversations in the volume. It can send them only to the allowed hosts, and
one of those is Telegram, so "only" includes the chat of whoever is talking to the bot. Keep a spend cap on the
model key and use supplier keys that carry nothing but search access.

## Run it

Docker with Compose v2 and git are the requirements. From the repository root:

```sh
deploy/accomy/build-image.sh                                    # builds accomy-hermes:local
deploy/accomy/seed-secrets.sh --open ~/path/to/profile/.env     # once; see below
docker compose -f deploy/accomy/docker-compose.yml up -d
docker compose -f deploy/accomy/docker-compose.yml logs -f bot
```

`seed-secrets.sh` copies the profile's `.env` (the variables listed in `distributions/accomy/distribution.yaml`)
into the data volume. `--open` lets every Telegram account talk to the bot; without it only the IDs in
`TELEGRAM_ALLOWED_USERS` are answered. Read "Running it open to everyone" in `plugins/accomy_travel/README.md`
before using `--open`.

The container runs `distributions/accomy/config.yaml` as it is in this checkout. To run another profile file,
set `ACCOMY_CONFIG`, `ACCOMY_SOUL` and `ACCOMY_LOCALES` to host paths. `locales/` holds the end-user
wording (`en.yaml`): the bot reads it from `/opt/data/locales`, and the bootstrap copies it out of the image next to `config.yaml`.

| Task | Command (add `-f deploy/accomy/docker-compose.yml`, or run from this directory) |
|---|---|
| Watch conversations live | `./watch.sh` (messages, tool calls, tool results and replies) |
| Follow the bot's log | `docker compose exec -u hermes bot hermes logs -f` (add `errors` for errors only) |
| Stop the bot | `docker compose stop bot` |
| Stop everything | `docker compose down` (the data volume is kept) |
| Apply a profile edit | `docker compose restart bot` |
| Replace the secrets | `./seed-secrets.sh [--open] <file>` then `docker compose restart bot` |
| Update the code | `./build-image.sh` then `docker compose up -d` |
| What the proxy refused | `docker compose logs egress \| grep DENIED` |
| A shell as the bot user | `docker compose exec -u hermes bot bash` |
| Delete all bot state | `docker compose down -v` |

Only one process may poll a Telegram bot token. Stop this stack before starting the same bot anywhere else,
including `hermes gateway` or `hermes chat` on the host with the same `.env`.

Expected noise in the logs: at start-up Hermes tries `hermes-agent.nousresearch.com`, `raw.githubusercontent.com`
and `openrouter.ai` for catalogs the bot does not need, and the proxy refuses them (`DENIED`). The bot also
prints `install out of sync (pnpm ...)`, which is about build tools the run-time image does not carry.

On a Mac the stack stops while the machine sleeps, and Docker Desktop must be running.

## Adding a supplier or changing the model endpoint

Add the new host to `squid.conf` and run `docker compose restart egress`. A host that is not listed fails
with a network error in the bot and a `DENIED` line in the proxy log.

## Voice notes and files

The profile accepts photos, voice notes and files (`telegram.accept_attachments: true`; the plugin README,
section "Attachments", says what is read and how). Two things here serve that:

- **The media cache is in the data volume**, under `cache/`. The gateway removes a file after a day, and the
  plugin deletes the files of a message it drops, and limits files to 1,500 a day across all senders. At
  Telegram's 20 MB per file that is at most 30 GB: give the volume's disk that much room.
- **Speech-to-text is Amazon Transcribe.** The bot streams a voice note's audio to
  `transcribestreaming.<region>.amazonaws.com` on port 8443, the only port Transcribe serves a WebSocket on.
  `squid.conf` allows that one host on that one port. The region is `transcribe_region` in the profile's
  `config.yaml`: change the host in `squid.conf` with it.

The bot cannot reach the instance metadata service, so it cannot borrow an instance role. It signs its requests
with a key pair of its own, `ACCOMY_TRANSCRIBE_ACCESS_KEY_ID` and `ACCOMY_TRANSCRIBE_SECRET_ACCESS_KEY` in
the profile's `.env`. Give that key pair to an IAM user that may do nothing else:

```sh
cat > transcribe-policy.json <<'POLICY'
{"Version": "2012-10-17", "Statement": [{
  "Effect": "Allow", "Action": "transcribe:StartStreamTranscriptionWebSocket", "Resource": "*",
  "Condition": {"StringEquals": {"aws:RequestedRegion": "ap-southeast-1"}}}]}
POLICY
aws iam create-user --user-name dev-accomy-bot-transcribe
aws iam put-user-policy --user-name dev-accomy-bot-transcribe --policy-name transcribe-streaming \
    --policy-document file://transcribe-policy.json
aws iam create-access-key --user-name dev-accomy-bot-transcribe    # prints the key pair once: put it in the .env
```

Then seed the `.env` again (`./seed-secrets.sh`, or "Changing a secret" below on EC2) and restart the bot.
Without the key pair the bot still runs: a voice note then reaches the assistant as a recording it could not
hear, and it asks the user to type. To check speech-to-text on a running bot, send it a voice note and look for
the proxy's line: `docker compose logs egress | grep transcribestreaming` shows `TCP_TUNNEL/200` for each
recording. Transcribe bills by the second of audio; the plugin's limit of six hours a day is the ceiling.

## The order database

The order store (`plugins/accomy_travel/README.md`, Order store) is PostgreSQL, which the HTTPS proxy cannot
carry. The `db` service is a TCP relay to one server and port, and the bot's only route to it. Inside the bot
container the database's own host name resolves to the relay, so `ACCOMY_DATABASE_URL` is the same string as
everywhere else and TLS still verifies the database's certificate (`sslmode=verify-full`). The relay sees
ciphertext.

Put two lines in `deploy/accomy/.env` (Compose's own variables, not secrets), then `docker compose up -d`:

```sh
COMPOSE_PROFILES=db
ACCOMY_DB_HOST=<the host name in ACCOMY_DATABASE_URL>
```

```sh
docker compose exec -u hermes bot hermes accomy-orders status             # which migrations are applied
docker compose exec -u hermes bot hermes accomy-orders migrate            # dry run
docker compose exec -u hermes bot hermes accomy-orders migrate --commit   # apply
```

`rds-ca-ap-southeast-1.crt` is Amazon's public certificate bundle for RDS in Singapore, the trust anchor for
that connection. For a database elsewhere, set `ACCOMY_DB_CA` to its CA file.

## The demo payment page

`payment-demo/` is a separate, tiny deployment: one AWS Lambda behind an HTTP API, and one DynamoDB table. A
confirmed order carries a signed link to it, valid for 24 hours from the moment the order was placed.

- **What the visitor sees.** The order item by item (each line opens to show its dates, flights, room or tickets,
  and the supplier's own price when the amount was converted), the total in US dollars, and a banner saying this is
  a demonstration that charges nothing. The language follows the visitor's browser (Traditional and Simplified
  Chinese, English, Japanese, Korean; English for any other) and a selector on the page changes it.
- **Paying.** The page accepts only Airwallex's public test card `4012 0003 0000 0062`, checks it in the browser and
  never receives a card number. Pressing Pay makes the Lambda send the user a real Telegram message, as the bot, in
  the page's language. An order is paid once: the table remembers it, and a second press sends nothing.
- **Cancelling.** When the bot cancels an order it tells the page, and the link then says the order was cancelled
  and takes no payment. `get_order` asks the page whether an order was paid.
- **What the link holds.** The order code, the trip name, the items and amounts, and the chat to notify. It is
  signed, not encrypted, and names no traveller. Anyone who is given the link can see that order and pay it.

```sh
AWS_PROFILE=<profile> AWS_REGION=ap-southeast-1 deploy/accomy/payment-demo/deploy.sh ~/path/to/profile/.env
```

The script puts the bot token and a link-signing key in Parameter Store, creates the stack `accomy-payment-demo`,
and prints how to add the key to the bot's `.env` as `ACCOMY_PAYMENT_LINK_SECRET`. Put the page's address in the
profile as `plugins.entries.accomy_travel.settings.payment_url`, add its host to `squid.conf` (the bot calls the page
to void and to ask), reseed the secrets and restart the bot.

By default the page gets its own name, `dev-chatbot-payment.accomy-tech.com` (`ACCOMY_PAYMENT_DOMAIN`), which needs
a free record in the hosted zone and an issued certificate for it. With `ACCOMY_PAYMENT_DOMAIN=` (empty) the page is
served on the API's own `execute-api` address instead. Remove everything with
`aws cloudformation delete-stack --stack-name accomy-payment-demo` and by deleting the two parameters under
`/accomy/payment-demo/`.

## Deploying to AWS EC2

This path has run once on a real instance: Amazon Linux 2023 arm64 on a `t4g.large` in a private subnet behind
a NAT gateway, with Docker 25.0 and Compose 5.7.1. The bot was connected to Telegram about two and a half
minutes after launch. Changing a secret, as described under "Operating the instance", has also run once.
Running the bootstrap a second time, for a new build, has not been exercised, and neither have the two lines
added to it since that record the secrets parameter and the open setting in `/opt/accomy/.env`.

The instance needs no inbound access at all: use Session Manager instead of SSH. Put it in a private subnet
whose route to the internet is a NAT gateway, with no public address, in the VPC of the order database. The
database's security group has to admit the instance on port 5432; a group that admits the whole VPC range
already does. Keep the instance metadata service at hop limit 1.

**One poller per bot token.** The bootstrap starts the bot as soon as the instance boots. Stop every other
stack that uses the same Telegram token before step 5 (`docker compose stop` in this directory), and leave it
stopped.

On your machine you need the AWS CLI, Docker and the
[Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
(`session-manager-plugin --version`).

```sh
export AWS_PROFILE=<profile> AWS_REGION=ap-southeast-1
SUBNET=<private subnet id>          # routes 0.0.0.0/0 to a NAT gateway
DB_HOST=<database host name>        # the host in ACCOMY_DATABASE_URL; leave empty to run without the order store

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
VPC=$(aws ec2 describe-subnets --subnet-ids $SUBNET --query 'Subnets[0].VpcId' --output text)
REPO=$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/accomy-hermes
IMAGE=$REPO:poc-$(git rev-parse --short HEAD)

# 1. Image: private registry. A Mac builds arm64, which matches Graviton (t4g) instances.
#    Commit first: the tag names a commit, and build-image.sh also packs uncommitted changes.
aws ecr create-repository --repository-name accomy-hermes --image-tag-mutability IMMUTABLE
aws ecr get-login-password | docker login --username AWS --password-stdin ${REPO%%/*}
ACCOMY_IMAGE=$IMAGE deploy/accomy/build-image.sh
docker push $IMAGE

# 2. Secrets: the profile's .env as one encrypted parameter (4 KB at most; check with wc -c).
aws ssm put-parameter --name /accomy/bot/env --type SecureString \
    --value file://$HOME/path/to/profile/.env

# 3. Instance role: Session Manager, pull this one image, read that one parameter.
#    The braces matter: zsh reads $ACCOUNT:parameter and $ACCOUNT:repository as a modifier on the variable.
aws iam create-role --role-name accomy-bot-ec2 --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name accomy-bot-ec2 \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam put-role-policy --role-name accomy-bot-ec2 --policy-name pull-bot-image --policy-document \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"ecr:GetAuthorizationToken\",\"Resource\":\"*\"},{\"Effect\":\"Allow\",\"Action\":[\"ecr:BatchGetImage\",\"ecr:GetDownloadUrlForLayer\",\"ecr:BatchCheckLayerAvailability\"],\"Resource\":\"arn:aws:ecr:${AWS_REGION}:${ACCOUNT}:repository/accomy-hermes\"}]}"
aws iam put-role-policy --role-name accomy-bot-ec2 --policy-name read-bot-secrets --policy-document \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"ssm:GetParameter\",\"Resource\":\"arn:aws:ssm:${AWS_REGION}:${ACCOUNT}:parameter/accomy/bot/env\"}]}"
aws iam create-instance-profile --instance-profile-name accomy-bot-ec2
aws iam add-role-to-instance-profile --instance-profile-name accomy-bot-ec2 --role-name accomy-bot-ec2
sleep 15    # a new instance profile takes a moment to reach EC2

# 4. Security group with no inbound rule (a new group allows all outbound).
SG=$(aws ec2 create-security-group --group-name accomy-bot --description "Accomy bot: no inbound" \
    --vpc-id $VPC --query GroupId --output text)

# 5. Launch. User data is ec2-bootstrap.sh with its settings in front; it holds no secret.
#    ACCOMY_OPEN=1 opens the bot to every Telegram account: delete it from the line to keep the allowlist.
{ printf '#!/bin/sh\nexport ACCOMY_IMAGE=%s ACCOMY_SECRETS_PARAM=/accomy/bot/env ACCOMY_OPEN=1 ACCOMY_DB_HOST=%s\n' \
      "$IMAGE" "$DB_HOST"
  cat deploy/accomy/ec2-bootstrap.sh; } > accomy-user-data.sh
AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
    --query Parameter.Value --output text)
ROOT=$(aws ec2 describe-images --image-ids $AMI --query 'Images[0].RootDeviceName' --output text)
aws ec2 run-instances --image-id $AMI --instance-type t4g.large --subnet-id $SUBNET \
    --no-associate-public-ip-address --iam-instance-profile Name=accomy-bot-ec2 --security-group-ids $SG \
    --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled \
    --block-device-mappings "DeviceName=$ROOT,Ebs={VolumeSize=100,VolumeType=gp3,Encrypted=true}" \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=dev-accomy-bot}]' \
    --user-data fileb://accomy-user-data.sh
```

If `run-instances` answers `Invalid IAM Instance Profile name`, wait and run it again.

**Sizing.** The command above is the instance that ran: a `t4g.large` (2 vCPU, 8 GB) with a 100 GB disk, sized
for about twenty people at once. The bot container has limits of its own, 1 GB of memory and 1.5 CPUs whatever
the instance has: add `ACCOMY_MEM_LIMIT=2048m` (and `ACCOMY_CPUS`, if wanted) to the export line to raise them,
as that run did. Measured on the bot: idle it uses about 230 MB, the proxy about 20 MB and the relay under
5 MB; each further conversation held in memory costs about 0.1 MB; one flight search peaks at about 12 MB
and a fifth of a CPU second. A `t4g.small` (2 vCPU, 2 GB) with a 30 GB disk should do for a demo with a few
users, since the image is 3.2 GB, but it has not been run, and neither has a load of twenty people.

**Logs rotate.** Each container's log is capped at three files of 10 MB, the bootstrap gives the Docker daemon
the same default and caps the system journal at 500 MB, and the bot rotates its main log files in the data
volume at 5 MB each.

### What the launch leaves behind

| Where | What |
|---|---|
| ECR repository `accomy-hermes` | The bot image, one immutable tag per commit: `poc-<commit>` |
| Parameter Store, `/accomy/bot/env` | The profile's `.env`, encrypted. The only copy of the secrets outside the instance |
| IAM role and instance profile `accomy-bot-ec2` | Session Manager, pulling that one image, reading that one parameter |
| IAM user `dev-accomy-bot-transcribe` | Speech-to-text only: one access key, kept in the profile's `.env` (see "Voice notes and files") |
| Security group `accomy-bot` | No inbound rule. Outbound is open: the proxy in the Compose file is what limits the bot |
| The instance, Name tag `dev-accomy-bot` | Docker, the Compose plugin, and the stack below |
| `/opt/accomy` on the instance | This directory as the image carried it, plus `config.yaml`, `SOUL.md` and `locales/` |
| `/opt/accomy/.env` on the instance | The Compose settings of this host: the image, the database host, the limits. No secret |
| Docker volume `accomy_data` | The bot's home: the secrets file, the conversations, its logs |

The Compose project is called `accomy`, so the containers are `accomy-bot-1` and `accomy-egress-1`, and
`accomy-db-1` when the order store is set up. They restart with the instance.

### Operating the instance

`ec2-run.sh` runs one command as root on the instance through Systems Manager and prints its output. It finds
the instance by its Name tag (`dev-accomy-bot`; another name goes in `ACCOMY_INSTANCE_NAME`). Set `AWS_PROFILE`
and `AWS_REGION` first. AWS keeps the text and the output of every command: never put a secret in one, and
never run one that prints a secret or a customer's details (`hermes accomy-orders customer` prints names and
contacts).

```sh
run=deploy/accomy/ec2-run.sh

# Is it up, and is it connected? A healthy start logs "Connected to Telegram (polling mode)" once.
$run 'cd /opt/accomy && docker compose ps'
$run 'cd /opt/accomy && docker compose logs bot | grep -E "Connected to Telegram|Conflict|ERROR" | tail -n 5'

# What is it doing?
$run 'cd /opt/accomy && docker compose logs --since 15m bot | tail -n 80'
$run 'docker stats --no-stream; df -h /'
$run 'cd /opt/accomy && docker compose exec -T -u hermes bot hermes accomy-orders status'

# Restart the bot only. The proxy and the relay keep running.
$run 'cd /opt/accomy && docker compose restart bot'
```

`watch.sh` follows the conversations and needs a terminal, so open a session for it:
`aws ssm start-session --target <instance id>`, then `cd /opt/accomy && sudo sh watch.sh`.

**When the bot does not answer.** Look, in this order, before changing anything:

| What you see | What it means | What to do |
|---|---|---|
| `docker compose ps` shows `bot` restarting or absent | The bot crashes at start | Read `docker compose logs --tail 80 bot` for the reason |
| The log has `Conflict` after `Connected to Telegram` | Another process polls the same token | Find and stop the other one. Restarting this one does not help |
| The log has no `Connected to Telegram` since the last start | It cannot reach Telegram | Check the `egress` container and its log for refused hosts |
| The bot answers but a search fails as "busy" or "too long" | A supplier limit or a slow supplier | Read the bot log around the time; see "Limits" in the plugin README |
| Orders fail, searches work | The order database is unreachable | Run the `accomy-orders status` command above |

**Shipping a new build.** Commit first, because the tag names the commit and the registry refuses to overwrite
a tag. Build and push, then run the bootstrap of your checkout on the instance:

```sh
REPO=$(aws sts get-caller-identity --query Account --output text).dkr.ecr.$AWS_REGION.amazonaws.com/accomy-hermes
IMAGE=$REPO:poc-$(git rev-parse --short HEAD)
aws ecr get-login-password | docker login --username AWS --password-stdin ${REPO%%/*}
ACCOMY_IMAGE=$IMAGE deploy/accomy/build-image.sh && docker push $IMAGE

$run "echo $(base64 < deploy/accomy/ec2-bootstrap.sh | tr -d '\n') | base64 -d > /root/ec2-bootstrap.sh"
$run ". /opt/accomy/.env && ACCOMY_IMAGE=$IMAGE ACCOMY_SECRETS_PARAM=\$ACCOMY_SECRETS_PARAM \
    ACCOMY_OPEN=\$ACCOMY_OPEN ACCOMY_DB_HOST=\$ACCOMY_DB_HOST ACCOMY_MEM_LIMIT=\$ACCOMY_MEM_LIMIT \
    ACCOMY_CPUS=\$ACCOMY_CPUS sh /root/ec2-bootstrap.sh" 900
```

The first `$run` carries the script over, since the copy in `/opt/accomy` belongs to the build that is
running. The second repeats the last run's settings, which the bootstrap keeps in `/opt/accomy/.env`, with the
new image: whether the bot is open to everyone, the database host and the limits stay as they were. To change
one of them, give it a value in place of the `\$` variable. The bootstrap pulls the image, refreshes
`/opt/accomy` from it, reseeds the secrets and recreates the containers. The data volume is kept, so
conversations survive; the limits and the quotes the bot holds in memory do not. This second run has not been
exercised yet. Carrying the script over and reading the host's settings have.

**Changing a secret.** A change to the parameter alone does nothing: the bot reads its secrets from the volume.
Edit the profile's `.env`, store it again, then seed it into the volume and restart the bot. The seeding step
prints a one-line confirmation and no value:

```sh
aws ssm put-parameter --name /accomy/bot/env --type SecureString --overwrite \
    --value file://$HOME/path/to/profile/.env
$run ". /opt/accomy/.env && cd /opt/accomy && aws ssm get-parameter --region $AWS_REGION \
    --name \$ACCOMY_SECRETS_PARAM --with-decryption --query Parameter.Value --output text \
    | sh ./seed-secrets.sh \${ACCOMY_OPEN:+--open} - && docker compose restart bot"
```

Then check that the bot connected again, with the first commands of this section.

**Trying something in a real conversation.** Do not start a second copy of this bot. Make another bot with
BotFather, put its token in a throwaway `.env`, and run the stack on your machine under another project name
(`COMPOSE_PROJECT_NAME=accomy-test`).

**Stopping and removing.** `aws ec2 stop-instances` stops the bot and keeps the disk; the bot starts again with
the instance. Removing everything loses the conversations, which live only in the instance's volume:

```sh
aws ec2 terminate-instances --instance-ids <instance id>      # its disk goes with it
aws ec2 wait instance-terminated --instance-ids <instance id>
aws ec2 delete-security-group --group-name accomy-bot          # add --group-id outside the default VPC
aws iam remove-role-from-instance-profile --instance-profile-name accomy-bot-ec2 --role-name accomy-bot-ec2
aws iam delete-instance-profile --instance-profile-name accomy-bot-ec2
aws iam delete-role-policy --role-name accomy-bot-ec2 --policy-name pull-bot-image
aws iam delete-role-policy --role-name accomy-bot-ec2 --policy-name read-bot-secrets
aws iam detach-role-policy --role-name accomy-bot-ec2 \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam delete-role --role-name accomy-bot-ec2
aws iam delete-user-policy --user-name dev-accomy-bot-transcribe --policy-name transcribe-streaming
aws iam delete-access-key --user-name dev-accomy-bot-transcribe --access-key-id <its key id>
aws iam delete-user --user-name dev-accomy-bot-transcribe
aws ssm delete-parameter --name /accomy/bot/env
aws ecr delete-repository --repository-name accomy-hermes --force
```

The demo payment page is a separate stack, removed as its own section says.

**Running the bot somewhere else instead.** Stop it here first (`docker compose stop` through `ec2-run.sh`, or
stop the instance). One process polls a Telegram token.

## What this does not cover

- No budget in tokens or money. Set a spend cap on the model key at the provider.
- The turn and supplier limits are counted in the bot's memory. A restart resets them.
- Disk. The data volume has no size quota. The bot's log files rotate in normal use and the plugin bounds the
  media cache (see "Voice notes and files"), but a compromised bot could fill the disk it shares with Docker:
  the Docker Desktop disk on a Mac, the instance's root disk on EC2.
- Files from strangers. The Telegram adapter downloads an attachment, up to 20 MB, and holds it in memory
  before the plugin can refuse the message. Documents are parsed in a separate process with a time and a
  memory limit, and audio is decoded by ffmpeg, both inside the bot container: a flaw in a parser is contained
  by the container, not by the plugin.
- What users send stays for a while: a file for a day in the media cache, and the text the model read in the
  conversation, both in the data volume.
- Code that already runs as the bot user can change what the volume holds: the `.env`, the conversations, the
  bundled skills. That stays inside the container and its allowed hosts.
- The allowed hosts are trusted with whatever the bot sends them.
- Docker is not a hypervisor. A kernel or Docker escape is outside this boundary; on EC2 the instance holds
  nothing else, which is the point of giving the bot its own.
