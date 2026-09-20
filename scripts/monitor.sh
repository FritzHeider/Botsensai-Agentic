#!/usr/bin/env bash
# ============================================================================ #
# Botsensai 2.0 Real-Time Production Monitor
# Fetches hot wallet balance, service statuses, execution activity & system load.
# ============================================================================ #
set -euo pipefail

INSTANCE_ID="${1:-${BOTSENSAI_INSTANCE_ID:-i-0bb5f0e7d264a2937}}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
PROFILE="${AWS_PROFILE:-agent-toolkit}"

PROFILE_FLAG=""
if [ -n "$PROFILE" ]; then
    PROFILE_FLAG="--profile $PROFILE"
fi

SHELL_SCRIPT='
echo "========================================================================"
echo "               BOTSENSAI 2.0 LIVE PRODUCTION STATUS                     "
echo "========================================================================"

echo ""
echo "--- [1] HOT WALLET ON-CHAIN CUSTODY ---"
sudo -u ubuntu /home/ubuntu/Botsensai/.venv/bin/python -c "
import requests, json
from pathlib import Path
from solders.keypair import Keypair

kp_path = Path(\"/home/ubuntu/.config/solana/id.json\")
if kp_path.exists():
    kp = Keypair.from_bytes(bytes(json.loads(kp_path.read_text())))
    pubkey = str(kp.pubkey())
    try:
        r = requests.post(\"https://api.mainnet-beta.solana.com\", json={\"jsonrpc\": \"2.0\", \"id\": 1, \"method\": \"getBalance\", \"params\": [pubkey]}, timeout=5)
        lamports = r.json().get(\"result\", {}).get(\"value\", 0)
        sol = lamports / 1e9
        print(f\"Wallet Address : {pubkey}\")
        print(f\"SOL Balance    : {sol:.4f} SOL ({lamports:,} lamports)\")
        print(f\"Solscan URL    : https://solscan.io/account/{pubkey}\")
    except Exception as e:
        print(f\"Wallet Address : {pubkey} (RPC balance error: {e})\")
else:
    print(\"No keypair found at ~/.config/solana/id.json\")
"

echo ""
echo "--- [2] SYSTEMD SERVICES & SCHEDULED TIMERS ---"
for unit in botsensai-daemon botsensai-executor botsensai-serve botsensai-telemetry-publisher botsensai-wal-checkpoint.timer botsensai-optimizer.timer; do
    state=$(systemctl is-active "$unit" 2>/dev/null || echo "inactive")
    if [ "$state" = "active" ]; then
        printf "  %-35s : \033[32mACTIVE\033[0m\n" "$unit"
    else
        printf "  %-35s : \033[31m%s\033[0m\n" "$unit" "$state"
    fi
done

echo ""
echo "--- [3] LIVE JITO EXECUTOR STATUS (Last 5 Logs) ---"
if [ -f /home/ubuntu/Botsensai/data/executor.log ]; then
    tail -n 5 /home/ubuntu/Botsensai/data/executor.log
else
    echo "No executor.log found"
fi

echo ""
echo "--- [4] SUPERVISOR & ON-CHAIN SWEEPS (Last 5 Logs) ---"
if [ -f /home/ubuntu/Botsensai/data/daemon.log ]; then
    tail -n 5 /home/ubuntu/Botsensai/data/daemon.log
else
    echo "No daemon.log found"
fi

echo ""
echo "--- [5] HOST RESOURCE LOAD & STORAGE ---"
echo -n "Uptime & Load  : "
uptime | awk -F"load average:" "{ print \$2 }"
echo -n "Memory Usage   : "
free -h | awk "/^Mem:/ { print \$3 \" used / \" \$2 \" total (\" \$7 \" available)\" }"
echo -n "Root Disk Free : "
df -h / | awk "NR==2 { print \$4 \" available / \" \$2 \" total (\" \$5 \" used)\" }"
echo ""
echo "========================================================================"
'

PARAMS=$(python3 -c "import json, sys; print(json.dumps({'commands': [sys.argv[1]]}))" "$SHELL_SCRIPT")

CMD_ID=$(aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --region "$REGION" $PROFILE_FLAG \
    --document-name "AWS-RunShellScript" \
    --comment "Botsensai production status monitor" \
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
