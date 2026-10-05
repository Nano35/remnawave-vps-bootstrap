#!/usr/bin/env bash
# Run on a DIFFERENT Linux machine with recent Xray and curl installed.
set -Eeuo pipefail
CONFIG=${1:?Usage: bash external-test.sh external-test.json [https://example.com/]}
URL=${2:-https://example.com/}
[[ $URL == https://* ]] || { echo 'HTTPS test URL required'; exit 1; }
command -v xray >/dev/null
command -v curl >/dev/null
ss -H -ltn | awk '{print $4}' | grep -Eq ':10808$' && { echo 'Port 10808 occupied'; exit 1; }
xray run -test -config "$CONFIG"
xray run -config "$CONFIG" &
CLIENT_PID=$!
trap 'kill "$CLIENT_PID" 2>/dev/null || true; wait "$CLIENT_PID" 2>/dev/null || true' EXIT
for attempt in {1..20}; do
    kill -0 "$CLIENT_PID"
    if curl --fail --silent --show-error --proxy socks5h://127.0.0.1:10808 \
        --connect-timeout 8 --max-time 20 "$URL" -o /dev/null; then
        printf 'PASS: HTTPS request via VLESS TCP Reality with xtls-rprx-vision.\n'
        exit 0
    fi
    sleep 1
done
echo 'FAIL: VLESS/Reality/Vision request did not succeed' >&2
exit 1
