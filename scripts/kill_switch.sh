#!/usr/bin/env bash
# ============================================================================ #
# Botsensai Remote Emergency Kill Switch
#
# Modes:
#   soft-kill  - Halts the botsensai background service immediately via SSM
#   hard-kill  - Stops the entire EC2 instance via AWS EC2 API
#   status     - Checks both EC2 instance state and daemon state
#   resume     - Starts the EC2 instance / restarts the service
#
# Usage:
#   ./scripts/kill_switch.sh <mode> <INSTANCE_ID>
# ============================================================================ #
set -euo pipefail

MODE="${1:-help}"
INSTANCE_ID="${2:-${BOTSENSAI_INSTANCE_ID:-i-0bb5f0e7d264a2937}}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
PROFILE="${AWS_PROFILE:-agent-profile}"

PROFILE_FLAG=""
if [ -n "$PROFILE" ]; then
    PROFILE_FLAG="--profile $PROFILE"
fi

show_help() {
    cat <<EOF
Botsensai Kill Switch

Usage:
  $0 soft-kill <INSTANCE_ID>  - Halt background daemon via SSM (keeps instance running)
  $0 hard-kill <INSTANCE_ID>  - Stop the EC2 instance immediately
  $0 resume    <INSTANCE_ID>  - Start instance and restart service
  $0 status    <INSTANCE_ID>  - Inspect instance and service state

Environment Variables:
  BOTSENSAI_INSTANCE_ID
  AWS_REGION
  AWS_PROFILE
EOF
}

if [ "$MODE" = "help" ] || [ -z "$MODE" ]; then
    show_help
    exit 0
fi

if [ -z "$INSTANCE_ID" ]; then
    echo "Error: INSTANCE_ID is required."
    echo ""
    show_help
    exit 1
fi

case "$MODE" in
    soft-kill)
        echo "==> Triggering SOFT KILL on $INSTANCE_ID: stopping botsensai.service..."
        ./scripts/remote_control.sh stop "$INSTANCE_ID"
        echo "==> Soft kill dispatched. Service stopped."
        ;;
    hard-kill)
        echo "==> Triggering HARD KILL on $INSTANCE_ID: stopping EC2 instance..."
        aws ec2 stop-instances --instance-ids "$INSTANCE_ID" --region "$REGION" $PROFILE_FLAG
        echo "==> EC2 stop signal sent. Waiting for stopped state..."
        aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID" --region "$REGION" $PROFILE_FLAG || true
        echo "==> Instance $INSTANCE_ID is now STOPPED."
        ;;
    resume)
        echo "==> Resuming EC2 instance $INSTANCE_ID..."
        aws ec2 start-instances --instance-ids "$INSTANCE_ID" --region "$REGION" $PROFILE_FLAG
        echo "==> Waiting for instance to enter running state..."
        aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION" $PROFILE_FLAG || true
        echo "==> Instance is RUNNING. Waiting for SSM agent to respond (30s)..."
        sleep 30
        ./scripts/remote_control.sh restart "$INSTANCE_ID" || true
        ;;
    status)
        echo "==> EC2 Instance Status:"
        aws ec2 describe-instances \
            --instance-ids "$INSTANCE_ID" \
            --region "$REGION" $PROFILE_FLAG \
            --query "Reservations[0].Instances[0].{InstanceId:InstanceId,State:State.Name,PublicIp:PublicIpAddress}" \
            --output table
        echo ""
        echo "==> Daemon Service Status:"
        ./scripts/remote_control.sh status "$INSTANCE_ID" || true
        ;;
    *)
        echo "Unknown mode: $MODE"
        show_help
        exit 1
        ;;
esac
