#!/bin/bash
# setup-unattended-survival.sh — make the WiFi-only host survive weeks unattended.
#
# The MT7925 WiFi driver spontaneously deadlocks NetworkManager into an
# unkillable D-state on routine disconnects (mt7925_mac_sta_remove), leaving the
# box half-alive but unreachable until a hard power-cycle. With no one home to
# reset it, the only reliable recovery is an automatic reboot. This installs:
#
#   1. net-reboot-watchdog — force-reboots (sysrq) after a sustained network
#      outage. PRIMARY recovery for the mt7925 wedge.
#   2. systemd hardware watchdog (sp5100_tco) — auto-resets a FULL kernel hang
#      (e.g. a GPU lockup) that the script above couldn't run through. Best-effort.
#   3. (--away only) Disables the nightly sync + autodeploy — removes the
#      heaviest GPU load and unattended service restarts while nobody is home.
#      Without --away, sync and autodeploy are left as-is: the reboot watchdog
#      is a permanent fixture, not just travel mode.
#
# Idempotent. Run with: sudo bash scripts/setup-unattended-survival.sh [--away]
# Reverse with:        sudo bash scripts/setup-unattended-survival.sh --undo
set -euo pipefail

[ "$(id -u)" = "0" ] || { echo "Run as root: sudo bash $0"; exit 1; }

UNDO="${1:-}"

if [ "$UNDO" = "--undo" ]; then
    echo "== Reverting unattended-survival setup =="
    systemctl disable --now net-reboot-watchdog.timer 2>/dev/null || true
    rm -f /etc/systemd/system/net-reboot-watchdog.timer \
          /etc/systemd/system/net-reboot-watchdog.service \
          /usr/local/sbin/net-reboot-watchdog.sh \
          /etc/systemd/system.conf.d/10-watchdog.conf \
          /etc/modules-load.d/sp5100_tco.conf
    systemctl daemon-reload
    echo "Re-enable the nightly sync + autodeploy manually if you want them back:"
    echo "  sudo systemctl enable --now lifeos-sync.timer lifeos-autodeploy.timer"
    echo "Done. (systemd hardware-watchdog setting removed; reboot to fully clear.)"
    exit 0
fi

echo "== 1/4  Installing net-reboot-watchdog script =="
install -d /usr/local/sbin /var/lib
cat > /usr/local/sbin/net-reboot-watchdog.sh <<'WATCHDOG_EOF'
#!/bin/bash
# Force-reboot the host after a sustained network outage. The MT7925 driver can
# deadlock NetworkManager into an unkillable D-state; only a reboot recovers it,
# and no human is present. sysrq reboot bypasses the wedged NM / D-state tasks.
set -uo pipefail
STATE="${STATE:-/var/lib/net-reboot-watchdog.count}"
LOG="${LOG:-/var/log/net-reboot-watchdog.log}"
TARGETS=(${TARGETS:-1.1.1.1 8.8.8.8 9.9.9.9})
THRESHOLD="${THRESHOLD:-5}"   # consecutive fails x 3-min timer = ~15 min outage
DRY_RUN="${DRY_RUN:-0}"
ts(){ date '+%F %T'; }
log(){ echo "$(ts) $*" >> "$LOG" 2>/dev/null; }
online=0
for t in "${TARGETS[@]}"; do
    ping -c1 -W3 "$t" >/dev/null 2>&1 && { online=1; break; }
done
if [ "$online" = "1" ]; then
    prev=$(cat "$STATE" 2>/dev/null || echo 0)
    [ "$prev" != "0" ] && log "online again — resetting counter (was $prev)"
    echo 0 > "$STATE" 2>/dev/null
    exit 0
fi
n=$(cat "$STATE" 2>/dev/null || echo 0); [[ "$n" =~ ^[0-9]+$ ]] || n=0
n=$((n+1)); echo "$n" > "$STATE" 2>/dev/null
log "OFFLINE (consecutive=$n/$THRESHOLD)"
if [ "$n" -ge "$THRESHOLD" ]; then
    log "threshold reached — FORCING REBOOT (network wedged, no human present)"
    sync; sync
    [ "$DRY_RUN" = "1" ] && { log "[dry-run] would sysrq-reboot now"; exit 0; }
    echo 0 > "$STATE" 2>/dev/null
    sleep 2
    echo b > /proc/sysrq-trigger
    sleep 5
    systemctl reboot -ff
fi
exit 0
WATCHDOG_EOF
chmod 755 /usr/local/sbin/net-reboot-watchdog.sh

echo "== 2/4  Installing systemd service + timer (checks every 3 min) =="
cat > /etc/systemd/system/net-reboot-watchdog.service <<'EOF'
[Unit]
Description=Force-reboot if the network is wedged too long (MT7925 deadlock recovery)
After=network.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/net-reboot-watchdog.sh
EOF
cat > /etc/systemd/system/net-reboot-watchdog.timer <<'EOF'
[Unit]
Description=Run net-reboot-watchdog every 3 minutes
[Timer]
OnBootSec=5min
OnUnitActiveSec=3min
AccuracySec=30s
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now net-reboot-watchdog.timer

echo "== 3/4  Hardware watchdog (auto-reset a full kernel hang) — best effort =="
modprobe sp5100_tco 2>/dev/null || true
if [ -e /dev/watchdog ]; then
    echo "sp5100_tco" > /etc/modules-load.d/sp5100_tco.conf
    install -d /etc/systemd/system.conf.d
    cat > /etc/systemd/system.conf.d/10-watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=60
RebootWatchdogSec=5min
EOF
    systemctl daemon-reexec 2>/dev/null || true
    echo "  hardware watchdog ENABLED (/dev/watchdog present, RuntimeWatchdogSec=60)"
else
    echo "  /dev/watchdog not available on this box — skipping hardware watchdog"
    echo "  (the net-reboot-watchdog above is the primary safety net regardless)"
fi

if [ "$UNDO" = "--away" ]; then
    echo "== 4/4  --away: disabling the nightly sync + autodeploy (reduce load/variables) =="
    systemctl disable --now lifeos-sync.timer 2>/dev/null && echo "  lifeos-sync.timer disabled" || echo "  (lifeos-sync.timer already off)"
    systemctl disable --now lifeos-autodeploy.timer 2>/dev/null && echo "  lifeos-autodeploy.timer disabled" || echo "  (lifeos-autodeploy.timer already off)"
else
    echo "== 4/4  Leaving nightly sync + autodeploy as-is (pass --away to disable while traveling) =="
fi

echo
echo "== DONE — verification =="
echo "reboot-watchdog timer : $(systemctl is-active net-reboot-watchdog.timer) / $(systemctl is-enabled net-reboot-watchdog.timer)"
echo "next check            : $(systemctl list-timers net-reboot-watchdog.timer --no-pager 2>/dev/null | awk 'NR==2{print $1, $2}')"
echo "hardware watchdog     : $([ -e /dev/watchdog ] && echo present || echo absent)"
echo "nightly sync          : $(systemctl is-enabled lifeos-sync.timer 2>/dev/null)"
echo "autodeploy            : $(systemctl is-enabled lifeos-autodeploy.timer 2>/dev/null)"
echo "gentle net-watchdog   : $(systemctl is-active lifeos-network-watchdog.timer 2>/dev/null) (kept — recovers brief blips)"
echo
echo "Recovery log while you're away: /var/log/net-reboot-watchdog.log"
echo "If it ever force-reboots, services auto-start on boot and the box comes back."
