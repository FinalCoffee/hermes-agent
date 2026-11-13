#!/bin/sh
# Run one shell command as root on the bot's EC2 host through Systems Manager, and print what it printed.
# The host needs no inbound access for this: the command travels through the Systems Manager service.
#
#   ./ec2-run.sh 'cd /opt/accomy && docker compose ps'
#   ./ec2-run.sh 'cd /opt/accomy && docker compose logs --since 15m bot | tail -n 80' 60
#
# The second argument is the time limit in seconds (default 120). The host is the one running instance whose
# Name tag is $ACCOMY_INSTANCE_NAME (default dev-accomy-bot); set ACCOMY_INSTANCE to an instance id to skip
# the lookup. Needs python3 and the AWS CLI, with AWS_PROFILE and AWS_REGION set for the account that holds
# the instance. Systems Manager returns at most 24,000 characters of output: cut long output on the host.
#
# AWS keeps the command text and its output in the Systems Manager command history. Never put a secret in
# the command, and never run one that prints a secret, such as a cat of the .env or a dump of a container's
# environment.
set -eu

command="${1:?usage: ec2-run.sh '<command>' [seconds]}"
limit="${2:-120}"
export AWS_PAGER=""

instance="${ACCOMY_INSTANCE:-}"
if [ -z "$instance" ]; then
    instance="$(aws ec2 describe-instances \
        --filters "Name=tag:Name,Values=${ACCOMY_INSTANCE_NAME:-dev-accomy-bot}" \
                  Name=instance-state-name,Values=running \
        --query 'Reservations[].Instances[].InstanceId' --output text)"
fi
# shellcheck disable=SC2086  # split on purpose: more than one word means more than one instance
set -- $instance
[ "$#" -eq 1 ] || { echo "ec2-run: expected one running instance, found $#: $instance" >&2; exit 1; }

parameters="$(python3 -c 'import json, sys
print(json.dumps({"commands": [sys.argv[1]], "executionTimeout": [sys.argv[2]]}))' "$command" "$limit")"
id="$(aws ssm send-command --instance-ids "$instance" --document-name AWS-RunShellScript \
    --parameters "$parameters" --query Command.CommandId --output text)"

status=Pending
tries=$((limit / 2 + 15))
while [ "$tries" -gt 0 ]; do
    status="$(aws ssm get-command-invocation --command-id "$id" --instance-id "$instance" \
        --query Status --output text 2> /dev/null || echo Pending)"
    case "$status" in Success | Failed | Cancelled | TimedOut) break ;; esac
    tries=$((tries - 1))
    sleep 2
done

aws ssm get-command-invocation --command-id "$id" --instance-id "$instance" \
    --query StandardOutputContent --output text
errors="$(aws ssm get-command-invocation --command-id "$id" --instance-id "$instance" \
    --query StandardErrorContent --output text)"
[ -z "$errors" ] || printf '%s\n' "$errors" >&2
case "$status" in
    Success) ;;
    Failed | Cancelled | TimedOut) echo "ec2-run: the command ended as $status" >&2; exit 1 ;;
    *) echo "ec2-run: no result after ${limit}s (status $status); the command may still be running" >&2; exit 1 ;;
esac
