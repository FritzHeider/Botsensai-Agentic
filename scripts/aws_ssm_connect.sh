#!/usr/bin/env bash
# ============================================================================ #
# Connect to the Botsensai Ubuntu EC2 instance via AWS SSM Session Manager
# Usage:
#   ./scripts/aws_ssm_connect.sh [INSTANCE_ID] [REGION] [PROFILE]
# ============================================================================ #
set -euo pipefail

INSTANCE_ID="${1:-${BOTSENSAI_INSTANCE_ID:-i-0bb5f0e7d264a2937}}"
REGION="${2:-${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}}"
PROFILE="${3:-${AWS_PROFILE:-agent-profile}}"

PROFILE_FLAG=""
if [ -n "$PROFILE" ]; then
    PROFILE_FLAG="--profile $PROFILE"
fi

if [ -z "$INSTANCE_ID" ]; then
    echo "==> No INSTANCE_ID provided. Attempting to auto-discover running Botsensai instance in $REGION..."
    DISCOVERED=$(aws ec2 describe-instances \
        --region "$REGION" $PROFILE_FLAG \
        --filters "Name=tag:Name,Values=*botsensai*" "Name=instance-state-name,Values=running" \
        --query "Reservations[0].Instances[0].InstanceId" \
        --output text 2>/dev/null || true)

    if [ -z "$DISCOVERED" ] || [ "$DISCOVERED" = "None" ]; then
        echo "Error: Could not auto-discover a running instance tagged 'Name=*botsensai*'."
        echo "Usage: $0 <INSTANCE_ID> [REGION] [AWS_PROFILE]"
        exit 1
    fi
    INSTANCE_ID="$DISCOVERED"
    echo "==> Discovered instance: $INSTANCE_ID"
fi

echo "==> Starting AWS SSM Session with $INSTANCE_ID ($REGION)..."
echo "==> Once connected, switch to ubuntu user: sudo su - ubuntu && cd ~/botsensai"
exec aws ssm start-session --target "$INSTANCE_ID" --region "$REGION" $PROFILE_FLAG
