service = """[Unit]
Description=Botsensai Continuous Model Optimizer
After=network.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu/Botsensai
Nice=19
IOSchedulingClass=idle
MemoryMax=800M
ExecStart=/home/ubuntu/Botsensai/.venv/bin/python3 /home/ubuntu/Botsensai/scripts/auto_optimizer.py --min-samples 100
StandardOutput=append:/home/ubuntu/Botsensai/data/optimizer.log
StandardError=append:/home/ubuntu/Botsensai/data/optimizer.log

[Install]
WantedBy=multi-user.target
"""

timer = """[Unit]
Description=Trigger Botsensai Model Optimizer periodically

[Timer]
OnBootSec=15min
OnUnitActiveSec=4h
Persistent=true

[Install]
WantedBy=timers.target
"""

with open('/etc/systemd/system/botsensai-optimizer.service', 'w') as f:
    f.write(service.strip() + '\n')
with open('/etc/systemd/system/botsensai-optimizer.timer', 'w') as f:
    f.write(timer.strip() + '\n')
print('SUCCESS_WRITTEN')
