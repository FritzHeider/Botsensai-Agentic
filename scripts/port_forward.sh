#!/usr/bin/env bash
# ============================================================================ #
# Securely forward port 8001 from EC2 to localhost via AWS SSM
# No open inbound firewall ports or public IP exposure required.
# ============================================================================ #
set -euo pipefail

INSTANCE_ID="${1:-${BOTSENSAI_INSTANCE_ID:-i-0bb5f0e7d264a2937}}"
LOCAL_PORT="${2:-8001}"
REMOTE_PORT="${3:-8001}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
PROFILE="${AWS_PROFILE:-agent-profile}"

PROFILE_FLAG=""
if [ -n "$PROFILE" ]; then
    PROFILE_FLAG="--profile $PROFILE"
fi

echo "======================================================================"
echo "  Botsensai AWS SSM Port Forwarding Tunnel"
echo "======================================================================"
echo "  Target Instance : $INSTANCE_ID ($REGION)"
echo "  Local Port      : http://localhost:$LOCAL_PORT"
echo "  Remote Port     : $REMOTE_PORT (Botsensai API Server)"
echo "  Documentation   : http://localhost:$LOCAL_PORT/docs"
echo "  OpenAPI JSON    : http://localhost:$LOCAL_PORT/openapi.json"
echo "======================================================================"
echo ""
echo "Starting encrypted SSM tunnel. Press Ctrl+C to stop forwarding."
echo ""

exec aws ssm start-session \
    --target "$INSTANCE_ID" \
    --region "$REGION" $PROFILE_FLAG \
    --document-name AWS-StartPortForwardingSession \
    --parameters "{\"portNumber\":[\"$REMOTE_PORT\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}"
