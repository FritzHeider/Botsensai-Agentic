#!/usr/bin/env bash
set -euo pipefail

INSTANCE_ID="i-0bb5f0e7d264a2937"
PROFILE="agent-toolkit"
REGION="us-east-1"

if [ $# -eq 0 ]; then
  RAW_SCRIPT=$(cat)
else
  RAW_SCRIPT="$1"
fi

B64_SCRIPT=$(echo "$RAW_SCRIPT" | base64 | tr -d '\r\n')

CMD_ID=$(aws ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name "AWS-RunShellScript" \
  --parameters "commands=[\"echo $B64_SCRIPT | base64 -d | bash\"]" \
  --profile "$PROFILE" \
  --region "$REGION" \
  --output text \
  --query "Command.CommandId")

echo "SSM Command sent: $CMD_ID" >&2

for i in $(seq 1 30); do
  sleep 1.5
  STATUS=$(aws ssm get-command-invocation \
    --command-id "$CMD_ID" \
    --instance-id "$INSTANCE_ID" \
    --profile "$PROFILE" \
    --region "$REGION" \
    --output text \
    --query "Status")
  
  if [ "$STATUS" = "Success" ] || [ "$STATUS" = "Failed" ] || [ "$STATUS" = "TimedOut" ]; then
    aws ssm get-command-invocation \
      --command-id "$CMD_ID" \
      --instance-id "$INSTANCE_ID" \
      --profile "$PROFILE" \
      --region "$REGION" \
      --output text \
      --query "StandardOutputContent"
    
    STDERR=$(aws ssm get-command-invocation \
      --command-id "$CMD_ID" \
      --instance-id "$INSTANCE_ID" \
      --profile "$PROFILE" \
      --region "$REGION" \
      --output text \
      --query "StandardErrorContent")
    if [ -n "$STDERR" ]; then
      echo "--- STDERR ---" >&2
      echo "$STDERR" >&2
    fi
    if [ "$STATUS" = "Failed" ]; then exit 1; fi
    exit 0
  fi
done

echo "SSM Command timed out." >&2
exit 1
