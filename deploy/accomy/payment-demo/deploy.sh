#!/bin/sh
# Deploy (or update) the demo payment page. Needs the AWS CLI with a profile that may create the stack.
#
#   AWS_PROFILE=accomy AWS_REGION=ap-southeast-1 ./deploy.sh /path/to/profile/.env
#
# The profile's .env supplies the bot token (the page sends Telegram messages as the bot). The key that
# signs payment links is created once in Parameter Store; put the same value in the bot's .env as
# ACCOMY_PAYMENT_LINK_SECRET (the last lines printed say how). No secret is printed or passed as an
# argument. To remove everything: aws cloudformation delete-stack --stack-name accomy-payment-demo,
# then delete the two parameters under /accomy/payment-demo/. If the very first run fails, delete the
# rolled-back stack the same way before running again.
set -eu

env_file="${1:?usage: deploy.sh <path to the bot profile .env>}"
stack="${ACCOMY_PAYMENT_STACK:-accomy-payment-demo}"
domain="${ACCOMY_PAYMENT_DOMAIN-dev-chatbot-payment.accomy-tech.com}"
cd "$(dirname "$0")"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
umask 077

if ! aws ssm get-parameter --name /accomy/payment-demo/link-secret > /dev/null 2>&1; then
    openssl rand -hex 32 > "$work/link-secret"
    aws ssm put-parameter --name /accomy/payment-demo/link-secret --type SecureString \
        --value "file://$work/link-secret" > /dev/null
fi
sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$env_file" > "$work/bot-token"
# Exactly one well-formed token: anything else would be stored, then end up in a URL the page cannot use.
[ "$(grep -cE '^[0-9]+:[A-Za-z0-9_-]+$' "$work/bot-token")" = 1 ] && [ "$(wc -l < "$work/bot-token")" -eq 1 ] || {
    echo "deploy: $env_file must hold exactly one TELEGRAM_BOT_TOKEN of the usual shape" >&2; exit 1; }
aws ssm put-parameter --name /accomy/payment-demo/telegram-bot-token --type SecureString --overwrite \
    --value "file://$work/bot-token" > /dev/null

# ACCOMY_PAYMENT_DOMAIN= (empty) serves the page on the API's own address, with no DNS record.
overrides=""
if [ -n "$domain" ]; then
    zone_name="${domain#*.}"
    zone="$(aws route53 list-hosted-zones-by-name --dns-name "$zone_name" \
        --query "HostedZones[?Name=='$zone_name.' && Config.PrivateZone==\`false\`] | [0].Id" --output text | sed 's|.*/||')"
    certificate="$(aws acm list-certificates --certificate-statuses ISSUED \
        --query "CertificateSummaryList[?DomainName=='*.$zone_name' || DomainName=='$domain'] | [0].CertificateArn" \
        --output text)"
    [ "$zone" != "None" ] && [ "$certificate" != "None" ] || {
        echo "deploy: no public hosted zone or issued certificate for $domain in this account and region" >&2; exit 1; }
    overrides="HostedZoneId=$zone CertificateArn=$certificate"
fi

# shellcheck disable=SC2086  # $overrides is two KEY=VALUE words or nothing
aws cloudformation deploy --stack-name "$stack" --template-file template.yaml --capabilities CAPABILITY_IAM \
    --no-fail-on-empty-changeset --parameter-overrides "DomainName=$domain" $overrides

cp handler.py "$work/index.py"
(cd "$work" && zip -q code.zip index.py)
aws lambda update-function-code --function-name "$stack" --zip-file "fileb://$work/code.zip" > /dev/null
aws lambda wait function-updated --function-name "$stack"

echo "deploy: the page is at $(aws cloudformation describe-stacks --stack-name "$stack" --query "Stacks[0].Outputs[?OutputKey=='Url'].OutputValue" --output text)/p/<token>"
echo "deploy: the bot needs the link key as ACCOMY_PAYMENT_LINK_SECRET in its .env. Append it without printing it:"
echo "  printf 'ACCOMY_PAYMENT_LINK_SECRET=%s\\n' \"\$(aws ssm get-parameter --name /accomy/payment-demo/link-secret --with-decryption --query Parameter.Value --output text)\" >> <profile .env>"
