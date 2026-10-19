# systemd 示例：每 5 分钟守护一轮
# 安装：
#   sudo cp examples/glm-auto-reset-card.{service,timer} /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now glm-auto-reset-card.timer
# 环境变量写在 /etc/glm-auto-reset-card.env（权限 600）：
#   GLM_API_KEY=你的key
#   GLM_AUTO_RESET_CONFIRM=YES

# ---------- glm-auto-reset-card.service ----------
[Unit]
Description=GLM Coding Plan reset-card guard (one round)

[Service]
Type=oneshot
EnvironmentFile=/etc/glm-auto-reset-card.env
ExecStart=/usr/bin/python3 /opt/glm-auto-reset-card/glm_auto_reset_card.py run --execute
# 退出码 2 = dry-run 会烧卡（不应出现，execute 已开启）；4 = 需要运行 resolve
# systemd 对 oneshot 非零退出会标记 failed，方便你接监控告警

# ---------- glm-auto-reset-card.timer ----------
[Unit]
Description=Run glm-auto-reset-card every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
