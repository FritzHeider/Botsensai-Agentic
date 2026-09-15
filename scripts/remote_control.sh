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
        run_ssm "SVC=\$(systemctl is-active botsensai-serve.service >/dev/null 2>&1 && echo 'botsensai-serve.service' || echo 'botsensai.service'); sudo systemctl status \$SVC --no-pager"
        ;;
    doctor)
        run_ssm "su - ubuntu -c 'cd /home/ubuntu/Botsensai 2>/dev/null || cd /home/ubuntu/botsensai; source .venv/bin/activate 2>/dev/null || true; python3 -m botsensai.cli doctor --config config/botsensai.yaml'"
        ;;
    logs)
        LINES="${1:-50}"
        run_ssm "SVC=\$(systemctl is-active botsensai-serve.service >/dev/null 2>&1 && echo 'botsensai-serve.service' || echo 'botsensai.service'); sudo journalctl -u \$SVC -n $LINES --no-pager"
        ;;
    sweep)
        run_ssm "su - ubuntu -c 'cd /home/ubuntu/Botsensai 2>/dev/null || cd /home/ubuntu/botsensai; source .venv/bin/activate; python3 -m botsensai.cli sweep --limit 40 --candidates 10'"
        ;;
    db-stats)
        run_ssm "su - ubuntu -c 'cd /home/ubuntu/Botsensai && .venv/bin/python3 -c \"from botsensai.store.db import Database; from botsensai.config import get_settings; print(Database(get_settings().path(get_settings().db_path)).counts())\"'"
        ;;
    restart)
        run_ssm "SVC=\$(systemctl is-active botsensai-serve.service >/dev/null 2>&1 && echo 'botsensai-serve.service' || echo 'botsensai.service'); sudo systemctl restart \$SVC && sudo systemctl status \$SVC --no-pager"
        ;;
    stop)
        run_ssm "SVC=\$(systemctl is-active botsensai-serve.service >/dev/null 2>&1 && echo 'botsensai-serve.service' || echo 'botsensai.service'); sudo systemctl stop \$SVC && echo \"Service \$SVC stopped.\""
        ;;
    start)
        run_ssm "SVC=\$(systemctl is-enabled botsensai-serve.service >/dev/null 2>&1 && echo 'botsensai-serve.service' || echo 'botsensai.service'); sudo systemctl start \$SVC && sudo systemctl status \$SVC --no-pager"
        ;;
    update)
        run_ssm "su - ubuntu -c 'cd /home/ubuntu/Botsensai 2>/dev/null || cd /home/ubuntu/botsensai; git pull origin main; source .venv/bin/activate; pip install -e .'; SVC=\$(systemctl is-active botsensai-serve.service >/dev/null 2>&1 && echo 'botsensai-serve.service' || echo 'botsensai.service'); sudo systemctl restart \$SVC && sudo systemctl status \$SVC --no-pager"
        ;;
    *)
        echo "Unknown command: $COMMAND"
        show_help
        exit 1
        ;;
esac
