#!/usr/bin/env bash
# ============================================================================ #
# Query EC2 system performance and resource usage via AWS SSM
# ============================================================================ #
set -euo pipefail

INSTANCE_ID="${1:-${BOTSENSAI_INSTANCE_ID:-i-0bb5f0e7d264a2937}}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
PROFILE="${AWS_PROFILE:-agent-profile}"

PROFILE_FLAG=""
if [ -n "$PROFILE" ]; then
    PROFILE_FLAG="--profile $PROFILE"
fi

SHELL_SCRIPT='
echo "=== UPTIME & LOAD ==="
uptime
echo ""
echo "=== MEMORY (MB) ==="
free -m
echo ""
echo "=== DISK USAGE ==="
df -h /
echo ""
echo "=== TOP PROCESSES BY CPU & MEM ==="
ps aux --sort=-%cpu | head -n 6
'

PARAMS=$(python3 -c "import json, sys; print(json.dumps({'commands': [sys.argv[1]]}))" "$SHELL_SCRIPT")

CMD_ID=$(aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --region "$REGION" $PROFILE_FLAG \
    --document-name "AWS-RunShellScript" \
    --comment "Botsensai server stats" \
    --parameters "$PARAMS" \
    --query "Command.CommandId" \
    --output text)

aws ssm wait command-executed \
    --command-id "$CMD_ID" \
    --instance-id "$INSTANCE_ID" \
    --region "$REGION" $PROFILE_FLAG 2>/dev/null || true

aws ssm get-command-invocation \
    --command-id "$CMD_ID" \
    --instance-id "$INSTANCE_ID" \
    --region "$REGION" $PROFILE_FLAG \
    --query "StandardOutputContent" \
    --output text
