#!/usr/bin/env bash
# ============================================================================ #
# Sync Authenticated X (Twitter) Session from Mac to EC2
# ============================================================================ #
set -euo pipefail

PROFILE="${AWS_PROFILE:-agent-profile}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
INSTANCE_ID="${BOTSENSAI_INSTANCE_ID:-i-0bb5f0e7d264a2937}"
BUCKET="${BOTSENSAI_BACKUP_BUCKET:-botsensai-backups-538471157365}"
LOCAL_PROFILE_DIR="${HOME}/botsensai-x-profile"
ARCHIVE_PATH="/tmp/botsensai-x-profile.tar.gz"

echo "============================================================"
echo " Botsensai X Session Sync (Mac -> EC2)"
echo "============================================================"

if [ ! -d "$LOCAL_PROFILE_DIR" ]; then
    echo "[-] Error: Local profile directory '$LOCAL_PROFILE_DIR' not found."
    echo "    Please run 'python3 scripts/x_login.py' first on your Mac to log into X."
    exit 1
fi

echo "[+] Packaging local browser profile from $LOCAL_PROFILE_DIR..."
tar -czf "$ARCHIVE_PATH" -C "$LOCAL_PROFILE_DIR" .
ARCHIVE_SIZE=$(du -h "$ARCHIVE_PATH" | cut -f1)
echo "[+] Package created: $ARCHIVE_PATH ($ARCHIVE_SIZE)"

echo "[+] Uploading profile bundle to Amazon S3..."
aws s3 cp "$ARCHIVE_PATH" "s3://${BUCKET}/profiles/botsensai-x-profile.tar.gz" \
    --profile "$PROFILE" --region "$REGION"

echo "[+] Instructing EC2 instance ($INSTANCE_ID) to deploy profile..."
SSM_COMMANDS=$(cat <<'SSM_EOF'
set -e
export PATH="/usr/local/bin:$PATH"
echo "==> Downloading browser profile from S3..."
aws s3 cp s3://botsensai-backups-538471157365/profiles/botsensai-x-profile.tar.gz /tmp/botsensai-x-profile.tar.gz

echo "==> Unpacking to /home/ubuntu/botsensai-browser-profile..."
mkdir -p /home/ubuntu/botsensai-browser-profile
tar -xzf /tmp/botsensai-x-profile.tar.gz -C /home/ubuntu/botsensai-browser-profile/
chown -R ubuntu:ubuntu /home/ubuntu/botsensai-browser-profile

echo "==> Enabling x_session in config/botsensai.yaml..."
python3 -c "
path = '/home/ubuntu/Botsensai/config/botsensai.yaml'
with open(path) as f:
    text = f.read()
text = text.replace('x_session:\n  enabled: false\n  acknowledged_burner: false', 'x_session:\n  enabled: true\n  acknowledged_burner: true')
text = text.replace('x_session:\n  enabled: false', 'x_session:\n  enabled: true')
with open(path, 'w') as f:
    f.write(text)
"

echo "==> Restarting background daemon..."
systemctl restart botsensai-serve.service

echo "==> Running doctor to verify session..."
sudo -u ubuntu -i bash -c "cd /home/ubuntu/Botsensai && .venv/bin/botsensai doctor --config config/botsensai.yaml"
SSM_EOF
)

CMD_ID=$(aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --document-name "AWS-RunShellScript" \
    --parameters "{\"commands\":[$(echo "$SSM_COMMANDS" | jq -R -s .)]}" \
    --query "Command.CommandId" \
    --output text)

echo "[+] Remote command dispatched (ID: $CMD_ID). Waiting for verification..."
sleep 6
aws ssm get-command-invocation \
    --command-id "$CMD_ID" \
    --instance-id "$INSTANCE_ID" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --query "StandardOutputContent" \
    --output text

echo "============================================================"
echo "[+] X session successfully synced and activated on EC2!"
echo "============================================================"
