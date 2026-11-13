#!/bin/sh
# Set up an Amazon Linux 2023 EC2 host for the bot. Run as root, as instance user data or from a
# Session Manager shell. Safe to run again with a new image. See README.md in this directory for
# the instance it expects.
#
#   ACCOMY_IMAGE=<account>.dkr.ecr.<region>.amazonaws.com/accomy-hermes:<tag> \
#   ACCOMY_SECRETS_PARAM=/accomy/bot/env ACCOMY_OPEN=1 ACCOMY_DB_HOST=<database host> sh ec2-bootstrap.sh
#
# ACCOMY_SECRETS_PARAM names an SSM Parameter Store SecureString holding the profile's .env. The values
# go from Parameter Store through a pipe into the Docker data volume and into no other file. The
# volume is on the instance's disk (/var/lib/docker, readable by root), so keep that disk encrypted.
# ACCOMY_OPEN=1 opens the bot to every Telegram account; leave it unset to answer only the IDs in
# TELEGRAM_ALLOWED_USERS.
# ACCOMY_DB_HOST is the order database's host name, the one in ACCOMY_DATABASE_URL. It starts the
# relay that is the bot's only route to it. Leave it unset to run without the order store.
# ACCOMY_MEM_LIMIT and ACCOMY_CPUS, when set, replace the bot container's limits (1024m and 1.5).
#
# Every run writes the host's settings afresh, so a later run needs the same variables as the first. The
# last run's are kept in /opt/accomy/.env, where the next run can read them back.
set -eu

: "${ACCOMY_IMAGE:?set ACCOMY_IMAGE to the ECR image URI}"
: "${ACCOMY_SECRETS_PARAM:?set ACCOMY_SECRETS_PARAM to the SSM parameter that holds the .env}"
# cloud-init runs user data without HOME, and `docker login` needs one for its config file.
export HOME="${HOME:-/root}"
registry="${ACCOMY_IMAGE%%/*}"
region="$(echo "$registry" | cut -d. -f4)"
home=/opt/accomy

COMPOSE_VERSION=v5.7.1
case "$(uname -m)" in
    aarch64) compose_arch=aarch64; compose_sha=f2f49f903240895fa764c0dca3a14548b754ad9426f6b609ccaf55ec5a3aa2a5 ;;
    x86_64) compose_arch=x86_64; compose_sha=af426722a8af5de812fcee30cc1c39f0639f5f723cbadefb25e5d4db054064cf ;;
    *) echo "ec2-bootstrap: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

dnf install -y docker
# Logs rotate. A container the Compose file does not describe gets the same cap as the ones it does, and the
# journal, which also holds the Docker daemon's own log, keeps at most 500 MB.
if [ ! -e /etc/docker/daemon.json ]; then
    mkdir -p /etc/docker
    printf '{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}\n' > /etc/docker/daemon.json
fi
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\n' > /etc/systemd/journald.conf.d/accomy.conf
systemctl restart systemd-journald
systemctl enable --now docker

plugins=/usr/local/lib/docker/cli-plugins
mkdir -p "$plugins"
curl -fsSL --retry 3 -o "$plugins/docker-compose.new" \
    "https://github.com/docker/compose/releases/download/$COMPOSE_VERSION/docker-compose-linux-$compose_arch"
echo "$compose_sha  $plugins/docker-compose.new" | sha256sum -c -
chmod 0755 "$plugins/docker-compose.new"
mv "$plugins/docker-compose.new" "$plugins/docker-compose"

aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry"
docker pull "$ACCOMY_IMAGE"
docker logout "$registry"

# The image carries its own deployment files and the profile it was tested with.
mkdir -p "$home"
cid="$(docker create "$ACCOMY_IMAGE")"
docker cp "$cid:/opt/hermes/deploy/accomy/." "$home/"
docker cp "$cid:/opt/hermes/distributions/accomy/config.yaml" "$home/config.yaml"
docker cp "$cid:/opt/hermes/distributions/accomy/SOUL.md" "$home/SOUL.md"
mkdir -p "$home/locales"
docker cp "$cid:/opt/hermes/distributions/accomy/locales/." "$home/locales/"
docker rm "$cid" > /dev/null
chown -R root:root "$home"
chmod -R go-w "$home"

# Compose variables for this host, and this run's settings for the next run to repeat. Not secrets: those
# live only in the data volume.
cat > "$home/.env" <<VARS
ACCOMY_IMAGE=$ACCOMY_IMAGE
ACCOMY_CONFIG=$home/config.yaml
ACCOMY_SOUL=$home/SOUL.md
ACCOMY_LOCALES=$home/locales
ACCOMY_SECRETS_PARAM=$ACCOMY_SECRETS_PARAM
VARS
[ -z "${ACCOMY_OPEN:-}" ] || echo "ACCOMY_OPEN=1" >> "$home/.env"
if [ -n "${ACCOMY_DB_HOST:-}" ]; then
    printf 'COMPOSE_PROFILES=db\nACCOMY_DB_HOST=%s\n' "$ACCOMY_DB_HOST" >> "$home/.env"
fi
[ -z "${ACCOMY_MEM_LIMIT:-}" ] || printf 'ACCOMY_MEM_LIMIT=%s\n' "$ACCOMY_MEM_LIMIT" >> "$home/.env"
[ -z "${ACCOMY_CPUS:-}" ] || printf 'ACCOMY_CPUS=%s\n' "$ACCOMY_CPUS" >> "$home/.env"

cd "$home"
aws ssm get-parameter --region "$region" --name "$ACCOMY_SECRETS_PARAM" --with-decryption \
    --query Parameter.Value --output text \
    | sh ./seed-secrets.sh ${ACCOMY_OPEN:+--open} -
# Recreate: the secrets live in the volume, so Compose sees no change in a run that only reseeds them.
docker compose up -d --force-recreate
docker compose ps
if [ -n "${ACCOMY_DB_HOST:-}" ]; then
    # Shows whether the order store's schema is applied. Applying it is the operator's step:
    # docker compose exec -u hermes bot hermes accomy-orders migrate --commit
    docker compose exec -T -u hermes bot hermes accomy-orders status || true
fi
