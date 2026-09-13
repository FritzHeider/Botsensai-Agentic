#!/bin/bash
# =============================================================================
# Botsensai EC2 Provisioning & Bootstrap Script
# =============================================================================

exec > >(tee /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

echo "Starting Botsensai setup..."

# Update and install system dependencies
apt-get update -y
apt-get install -y python3-pip python3-venv git tmux jq curl

# Switch to ubuntu user to clone repository and install environment
su - ubuntu -c '
cd /home/ubuntu

REPO_URL="${REPOSITORY_URL:-https://github.com/FritzHeider/Botsensai-Agentic.git}"

if [ ! -d "Botsensai" ]; then
    echo "Cloning ${REPO_URL}..."
    git clone "${REPO_URL}" Botsensai
fi

cd Botsensai

# Create python venv
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

source .venv/bin/activate

# Install botsensai with browser and ml extras
pip install --upgrade pip
pip install -e ".[browser,ml]"

# Install Playwright dependencies
playwright install chromium --with-deps

# Create directories for data if they don't exist
mkdir -p data/content docs

# Setup Crontab for daily maintenance
(crontab -l 2>/dev/null; echo "0 2 * * * cd /home/ubuntu/Botsensai && /home/ubuntu/Botsensai/.venv/bin/botsensai label --min-age-hours 24 >> data/cron.log 2>&1") | crontab -
(crontab -l 2>/dev/null; echo "0 3 * * * cd /home/ubuntu/Botsensai && /home/ubuntu/Botsensai/.venv/bin/botsensai fit --min-samples 200 >> data/cron.log 2>&1") | crontab -
(crontab -l 2>/dev/null; echo "0 4 * * * cd /home/ubuntu/Botsensai && /home/ubuntu/Botsensai/.venv/bin/botsensai track-record --out docs/TRACK_RECORD.md >> data/cron.log 2>&1") | crontab -

echo "Botsensai setup complete!"
'
