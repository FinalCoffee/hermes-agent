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
| Network | HTTPS to the hosts listed in `squid.conf`, through the `egress` proxy. With the `db` profile, one PostgreSQL server through the `db` relay | Any other host, bare IP addresses, plain HTTP, the host machine, the LAN, the cloud metadata service |
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

Not yet run on a real instance. The commands were checked against the AWS and Docker documentation and the
flags against the AWS CLI offline; the Compose plugin install was exercised in an Amazon Linux 2023 arm64
container. Nothing else in `ec2-bootstrap.sh` (ECR, Parameter Store, systemd, Compose v5 against the AMI's
Docker engine) has run.

The instance needs no inbound access at all: use Session Manager instead of SSH. Put it in a private subnet
whose route to the internet is a NAT gateway, with no public address, in the VPC of the order database so the
database's security group can admit it. Keep the instance metadata service at hop limit 1.

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
aws ecr create-repository --repository-name accomy-hermes
aws ecr get-login-password | docker login --username AWS --password-stdin ${REPO%%/*}
ACCOMY_IMAGE=$IMAGE deploy/accomy/build-image.sh
docker push $IMAGE

# 2. Secrets: the profile's .env as one encrypted parameter (4 KB at most).
aws ssm put-parameter --name /accomy/bot/env --type SecureString \
    --value file://$HOME/path/to/profile/.env

# 3. Instance role: Session Manager, pull from ECR, read that one parameter.
aws iam create-role --role-name accomy-bot-ec2 --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name accomy-bot-ec2 \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam attach-role-policy --role-name accomy-bot-ec2 \
    --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
aws iam put-role-policy --role-name accomy-bot-ec2 --policy-name read-bot-secrets --policy-document \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"ssm:GetParameter\",\"Resource\":\"arn:aws:ssm:$AWS_REGION:$ACCOUNT:parameter/accomy/bot/env\"}]}"
aws iam create-instance-profile --instance-profile-name accomy-bot-ec2
aws iam add-role-to-instance-profile --instance-profile-name accomy-bot-ec2 --role-name accomy-bot-ec2
sleep 15    # a new instance profile takes a moment to reach EC2

# 4. Security group with no inbound rule (a new group allows all outbound).
SG=$(aws ec2 create-security-group --group-name accomy-bot --description "Accomy bot: no inbound" \
    --vpc-id $VPC --query GroupId --output text)

# 5. Launch. User data is ec2-bootstrap.sh with its settings in front; it holds no secret.
{ printf '#!/bin/sh\nexport ACCOMY_IMAGE=%s ACCOMY_SECRETS_PARAM=/accomy/bot/env ACCOMY_OPEN=1 ACCOMY_DB_HOST=%s\n' \
      "$IMAGE" "$DB_HOST"
  cat deploy/accomy/ec2-bootstrap.sh; } > accomy-user-data.sh
AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
    --query Parameter.Value --output text)
ROOT=$(aws ec2 describe-images --image-ids $AMI --query 'Images[0].RootDeviceName' --output text)
aws ec2 run-instances --image-id $AMI --instance-type t4g.small --subnet-id $SUBNET \
    --no-associate-public-ip-address --iam-instance-profile Name=accomy-bot-ec2 --security-group-ids $SG \
    --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1,HttpEndpoint=enabled \
    --block-device-mappings "DeviceName=$ROOT,Ebs={VolumeSize=30,VolumeType=gp3,Encrypted=true}" \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=accomy-bot}]' \
    --user-data fileb://accomy-user-data.sh
```

Leave out `ACCOMY_OPEN=1` to keep the bot to `TELEGRAM_ALLOWED_USERS`. If `run-instances` answers
`Invalid IAM Instance Profile name`, wait and run it again.

A `t4g.small` (2 vCPU, 2 GB) should be enough for a demo: the idle bot uses about 230 MB, the proxy about
20 MB and the relay under 5 MB, and the bot is capped at 1 GB. That sizing has not been measured under real
traffic. The image is 3.2 GB, hence the 30 GB disk.

Afterwards:

```sh
aws ssm start-session --target <instance-id>
sudo tail -f /var/log/cloud-init-output.log            # the bootstrap's progress
cd /opt/accomy && sudo docker compose logs -f bot
sudo docker compose exec -u hermes bot hermes accomy-orders migrate --commit   # once, for the order store
```

To ship a new build, push a new tag and run `ec2-bootstrap.sh` again on the instance with the new
`ACCOMY_IMAGE` (it is at `/opt/accomy/ec2-bootstrap.sh`). It pulls the image, refreshes `/opt/accomy` from
it, reseeds the secrets and recreates the containers. The data volume is kept.

## What this does not cover

- No budget in tokens or money. Set a spend cap on the model key at the provider.
- The turn and supplier limits are counted in the bot's memory. A restart resets them.
- Disk. The data volume and the bot's own log files have no size limit, so a compromised bot could fill the
  disk it shares with Docker: the Docker Desktop disk on a Mac, the instance's root disk on EC2.
- Code that already runs as the bot user can change what the volume holds: the `.env`, the conversations, the
  bundled skills. That stays inside the container and its allowed hosts.
- The allowed hosts are trusted with whatever the bot sends them.
- Docker is not a hypervisor. A kernel or Docker escape is outside this boundary; on EC2 the instance holds
  nothing else, which is the point of giving the bot its own.
