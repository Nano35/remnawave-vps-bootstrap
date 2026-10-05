#!/usr/bin/env bash
# Sourced by bootstrap.sh after its initial backup.
configure_bbr() {
    modprobe tcp_bbr
    sysctl -n net.ipv4.tcp_available_congestion_control | grep -qw bbr || die 'Kernel does not offer BBR'
    cat > /etc/sysctl.d/90-remna-bootstrap.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
EOF
    sysctl -p /etc/sysctl.d/90-remna-bootstrap.conf
    [[ $(sysctl -n net.ipv4.tcp_congestion_control) == bbr ]] || die 'BBR was not enabled'
}

configure_dns() {
    apt-get install -y unbound
    install -d /etc/unbound/unbound.conf.d /etc/systemd/resolved.conf.d
    cat > /etc/unbound/unbound.conf.d/remna-bootstrap.conf <<'EOF'
server:
    interface: 127.0.0.1@5335
    access-control: 127.0.0.0/8 allow
    do-ip6: no
    tls-cert-bundle: /etc/ssl/certs/ca-certificates.crt
    cache-min-ttl: 0
    cache-max-ttl: 86400
    prefetch: yes
    hide-identity: yes
    hide-version: yes
forward-zone:
    name: "."
    forward-tls-upstream: yes
    forward-addr: 9.9.9.9@853#dns.quad9.net
    forward-addr: 149.112.112.112@853#dns.quad9.net
    forward-addr: 1.1.1.1@853#cloudflare-dns.com
    forward-addr: 1.0.0.1@853#cloudflare-dns.com
EOF
    unbound-checkconf
    # The package may have failed its initial start before our IPv4 config existed.
    systemctl reset-failed unbound
    systemctl enable unbound
    systemctl restart unbound
    dig @127.0.0.1 -p 5335 +time=5 +tries=2 +short example.com A | grep -Eq '^[0-9.]+$' || die 'Local DoT resolver failed'
    cat > /etc/systemd/resolved.conf.d/90-remna-bootstrap.conf <<'EOF'
[Resolve]
DNS=127.0.0.1:5335
FallbackDNS=9.9.9.9 1.1.1.1
Domains=~.
EOF
    systemctl restart systemd-resolved
    resolvectl query example.com >/dev/null
}

install_monitor() {
    install -d -m 700 /etc/remna-bootstrap
    install -d -m 755 /usr/local/lib/remna-bootstrap
    install -m 755 "$HERE/monitor.py" /usr/local/lib/remna-bootstrap/monitor.py
    export MONITOR_WEBHOOK
    python3 - <<'PY'
import json, os, pathlib
p = pathlib.Path('/etc/remna-bootstrap/monitor.json')
p.write_text(json.dumps({'domain': os.environ['NODE_DOMAIN'], 'webhook': os.environ['MONITOR_WEBHOOK'],
    'certificate_days': 14, 'disk_free_percent': 10}, indent=2))
p.chmod(0o600)
PY
    cat > /etc/systemd/system/remna-monitor.service <<'EOF'
[Unit]
Description=Remnawave node local health monitor
After=docker.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/remna-bootstrap/monitor.py
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/remna-bootstrap
EOF
    cat > /etc/systemd/system/remna-monitor.timer <<'EOF'
[Unit]
Description=Check Remnawave node every five minutes
[Timer]
OnCalendar=*:0/5
RandomizedDelaySec=20
Persistent=true
[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now remna-monitor.timer
    systemctl start remna-monitor.service
    unset MONITOR_WEBHOOK
}
