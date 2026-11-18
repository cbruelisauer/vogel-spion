#!/bin/bash
# Run every minute by wifi-watchdog.timer. If the router stops answering,
# restart Wi-Fi every 3 minutes; reboot after 15 minutes without connection.
STATE=/run/wifi-watchdog-failures
gw=$(ip -4 route show default | awk '{print $3; exit}')
if [ -n "$gw" ] && ping -c 3 -W 3 "$gw" >/dev/null 2>&1; then
  rm -f "$STATE"
  exit 0
fi
fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"
logger -t wifi-watchdog "router ${gw:-(no route)} unreachable, failure $fails"
if [ "$fails" -ge 15 ]; then
  logger -t wifi-watchdog "no network for 15 min, rebooting"
  systemctl reboot
elif [ $(( fails % 3 )) -eq 0 ]; then
  logger -t wifi-watchdog "restarting Wi-Fi"
  nmcli radio wifi off; sleep 5; nmcli radio wifi on
fi
