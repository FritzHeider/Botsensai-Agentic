#!/usr/bin/env bash
# ============================================================================ #
# Botsensai Remote Control CLI (runs locally using AWS SSM Run Command)
#
# Commands:
#   status    - Check status of botsensai systemd service
#   doctor    - Run 'botsensai doctor' surface health check
#   logs      - Tail latest systemd journal logs
#   sweep     - Trigger a single manual paper sweep
#   db-stats  - Query database record counts
#   restart   - Restart botsensai.service
#   stop      - Stop botsensai.service
#   start     - Start botsensai.service
#   update    - Pull latest git commit, update deps, and restart service
#
# Usage:
#   ./scripts/remote_control.sh <command> [INSTANCE_ID] [ARGS...]
# ============================================================================ #
set -euo pipefail

COMMAND="${1:-help}"
shift || true

INSTANCE_ID="${1:-${BOTSENSAI_INSTANCE_ID:-i-0bb5f0e7d264a2937}}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
PROFILE="${AWS_PROFILE:-agent-profile}"

PROFILE_FLAG=""
if [ -n "$PROFILE" ]; then
    PROFILE_FLAG="--profile $PROFILE"
fi

show_help() {
    cat <<EOF
Botsensai Remote Control (AWS SSM)

Usage:
  $0 <command> <INSTANCE_ID> [options]

Commands:
  status   <ID>        Check systemctl status of botsensai
  doctor   <ID>        Run surface diagnostics ('botsensai doctor')
  logs     <ID> [N]    View last N lines of service logs (default: 50)
  sweep    <ID>        Execute a single discovery & scoring sweep
  db-stats <ID>        Print record counts in data/botsensai.db
  restart  <ID>        Restart the botsensai background daemon
  stop     <ID>        Pause/stop the background daemon
  start    <ID>        Start the background daemon
  update   <ID>        Git pull, install editable package, restart daemon

Environment Variables:
  BOTSENSAI_INSTANCE_ID  Default target EC2 Instance ID
  AWS_REGION             AWS Region (default: us-west-2)
  AWS_PROFILE            AWS CLI Profile (optional)
EOF
}

if [ "$COMMAND" = "help" ] || [ -z "$COMMAND" ]; then
    show_help
    exit 0
fi

if [ -z "$INSTANCE_ID" ]; then
    echo "Error: INSTANCE_ID is required (or set BOTSENSAI_INSTANCE_ID)."
    echo ""
    show_help
    exit 1
fi
shift || true

run_ssm() {
    local shell_cmd="$1"
    echo "==> Sending SSM command to $INSTANCE_ID ($REGION)..."

    # Robustly JSON-encode the command array for AWS CLI
    local params
    params=$(python3 -c "import json, sys; print(json.dumps({'commands': [sys.argv[1]]}))" "$shell_cmd")

    # Send command
    local cmd_id
    cmd_id=$(aws ssm send-command \
        --instance-ids "$INSTANCE_ID" \
        --region "$REGION" $PROFILE_FLAG \
        --document-name "AWS-RunShellScript" \
        --comment "Botsensai remote control: $COMMAND" \
        --parameters "$params" \
        --query "Command.CommandId" \
        --output text)

    echo "==> Command dispatched (ID: $cmd_id). Awaiting execution..."

    # Wait for execution to finish
    aws ssm wait command-executed \
        --command-id "$cmd_id" \
        --instance-id "$INSTANCE_ID" \
        --region "$REGION" $PROFILE_FLAG 2>/dev/null || true

    # Fetch output
    aws ssm get-command-invocation \
        --command-id "$cmd_id" \
        --instance-id "$INSTANCE_ID" \
        --region "$REGION" $PROFILE_FLAG \
        --query "{Status:Status,StandardOutputContent:StandardOutputContent,StandardErrorContent:StandardErrorContent}" \
        --output json | python3 -c "
import sys, json
data = json.load(sys.stdin)
status = data.get('Status')
stdout = data.get('StandardOutputContent', '').strip()
stderr = data.get('StandardErrorContent', '').strip()

print(f'\n--- Status: {status} ---')
if stdout:
    print(stdout)
if stderr:
    print('\n[Stderr]:\n' + stderr)
"
}

case "$COMMAND" in
    status)
        run_ssm "su - ubuntu -c 'cd /home/ubuntu/Botsensai && .venv/bin/python3 scripts/live_status.py'"
        ;;
    doctor)
        run_ssm "su - ubuntu -c 'cd /home/ubuntu/Botsensai && .venv/bin/python3 -m botsensai.cli doctor --config config/botsensai.yaml'"
        ;;
    logs)
        LINES="${1:-30}"
        TARGET="${2:-all}"
        if [ "$TARGET" = "daemon" ]; then
            run_ssm "tail -n $LINES /home/ubuntu/Botsensai/data/daemon.log"
        elif [ "$TARGET" = "executor" ]; then
            run_ssm "tail -n $LINES /home/ubuntu/Botsensai/data/executor.log"
        else
            run_ssm "echo '=== DAEMON LOGS ===' && tail -n $LINES /home/ubuntu/Botsensai/data/daemon.log && echo '' && echo '=== EXECUTOR LOGS ===' && tail -n $LINES /home/ubuntu/Botsensai/data/executor.log"
        fi
        ;;
    sweep)
        run_ssm "su - ubuntu -c 'cd /home/ubuntu/Botsensai && .venv/bin/python3 -m botsensai.cli sweep --limit 40 --candidates 10'"
        ;;
    db-stats)
        run_ssm "su - ubuntu -c 'cd /home/ubuntu/Botsensai && .venv/bin/python3 -c \"from botsensai.store.db import Database; from botsensai.config import get_settings; print(Database(get_settings().path(get_settings().db_path)).counts())\"'"
        ;;
    restart)
        run_ssm "sudo systemctl restart botsensai-daemon botsensai-executor botsensai-serve && echo 'Services restarted successfully.' && sudo systemctl is-active botsensai-daemon botsensai-executor botsensai-serve"
        ;;
    stop)
        run_ssm "sudo systemctl stop botsensai-daemon botsensai-executor && echo 'Trading daemons stopped.'"
        ;;
    start)
        run_ssm "sudo systemctl start botsensai-daemon botsensai-executor && echo 'Trading daemons started.' && sudo systemctl is-active botsensai-daemon botsensai-executor"
        ;;
    update)
        run_ssm "su - ubuntu -c 'cd /home/ubuntu/Botsensai && git pull origin main && .venv/bin/pip install -e . --no-deps 2>/dev/null || true'; sudo systemctl restart botsensai-daemon botsensai-executor && echo 'Updated and restarted live trading services.'"
        ;;
    *)
        echo "Unknown command: $COMMAND"
        show_help
        exit 1
        ;;
esac
