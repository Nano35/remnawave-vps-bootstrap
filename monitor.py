#!/usr/bin/env python3
"""Quiet on unchanged state; optional generic JSON webhook on failure/recovery."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import re
import sys
import subprocess
import time
import urllib.request

CONFIG = Path(os.environ.get('REMNA_MONITOR_CONFIG', '/etc/remna-bootstrap/monitor.json'))
STATUS = Path(os.environ.get('REMNA_MONITOR_STATUS', '/var/lib/remna-bootstrap/monitor-status.json'))


def command(*args):
    return subprocess.check_output(args, text=True, timeout=15).strip()


CORE_SCAN = r'''
for p in /proc/[0-9]*; do
    exe=$(readlink "$p/exe" 2>/dev/null) || continue
    exe=${exe%" (deleted)"}
    case "$exe" in */xray|*/rw-core|*/xray-custom) ;; *) continue;; esac
    args=$(tr '\000' '\n' 2>/dev/null < "$p/cmdline") || continue
    printf '%s\n' "$args" | grep -Eq '^(-test(=true)?|version)$' && continue
    printf '%s\n' "$args" | grep -Eq '^(run|-config|-c|-confdir)(=.+)?$' || continue
    digest=$(sha256sum "$p/exe" 2>/dev/null) || continue
    printf '%s %s\n' "${p##*/}" "${digest%% *}"
done
'''


def running_core_hash(expected=None, attempts=3, delay=1):
    """Inspect executable identity, excluding probes; never print process arguments."""
    if expected is not None and not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise RuntimeError('Invalid expected core SHA256')
    reason = 'No running core executable found'
    for attempt in range(attempts):
        try:
            result = subprocess.run(['docker', 'exec', 'remnanode', 'sh', '-c', CORE_SCAN],
                capture_output=True, text=True, timeout=15)
            if result.returncode:
                reason = f'Docker core scan failed (exit {result.returncode})'
            else:
                rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
                if any(len(row) != 2 or not row[0].isdigit() or
                        not re.fullmatch(r'[0-9a-f]{64}', row[1]) for row in rows):
                    reason = 'Unexpected core scan output'
                elif len(rows) != 1:
                    reason = f'Expected one active core executable, found {len(rows)}'
                    if rows:
                        reason += ' (PIDs: ' + ', '.join(row[0] for row in rows) + ')'
                elif expected is not None and rows[0][1] != expected:
                    reason = 'Running core SHA256 differs from the installed fork'
                else:
                    return rows[0][1]
        except (OSError, subprocess.TimeoutExpired):
            reason = 'Docker core scan unavailable or timed out'
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise RuntimeError(reason)


def problems(config):
    errors = []
    for container in ('remnanode', 'caddy-remnawave'):
        try:
            info = json.loads(command('docker', 'inspect', container))[0]['State']
            if not info['Running'] or info.get('Health', {}).get('Status') == 'unhealthy':
                errors.append(container + ': stopped/unhealthy')
        except Exception:
            errors.append(container + ': unavailable')
    try:
        used = shutil.disk_usage('/var/lib/docker')
        if used.free / used.total * 100 < config.get('disk_free_percent', 10):
            errors.append('Docker disk free space below threshold')
    except OSError:
        errors.append('Docker disk status unavailable')
    try:
        domain = config['domain']
        with socket.create_connection((domain, 443), timeout=10) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=domain) as tls:
                expires = ssl.cert_time_to_seconds(tls.getpeercert()['notAfter'])
        if expires - time.time() < config.get('certificate_days', 14) * 86400:
            errors.append('TLS certificate expires soon')
    except Exception:
        errors.append('Selfsteal TLS verification failed')
    try:
        expected = hashlib.sha256(Path('/opt/remnanode/xray-custom').read_bytes()).hexdigest()
        actual = running_core_hash(expected)
        if actual != expected:
            errors.append('Running core differs from installed fork')
    except Exception:
        errors.append('Running core could not be verified')
    return sorted(errors)


def update(config, errors):
    previous = json.loads(STATUS.read_text()) if STATUS.exists() else None
    changed = previous is not None and previous['problems'] != errors
    notify = changed or (previous is None and bool(errors))
    event = {'node': config['domain'], 'status': 'failed' if errors else 'healthy', 'problems': errors}
    if notify:
        print(json.dumps(event))
        if config.get('webhook'):
            data = json.dumps(event).encode()
            req = urllib.request.Request(config['webhook'], data=data,
                headers={'Content-Type': 'application/json'}, method='POST')
            # Avoid redirecting secrets embedded in webhook URLs.
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs): return None
            try:
                with urllib.request.build_opener(NoRedirect).open(req, timeout=15):
                    pass
            except Exception:
                print('Webhook delivery failed; retrying on next monitor run')
                return 1  # Retain old state so delivery retries.
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    temp = STATUS.with_suffix('.tmp')
    temp.write_text(json.dumps({'problems': errors, 'checked_at': time.time()}))
    temp.chmod(0o600)
    temp.replace(STATUS)
    return 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--core-hash':
        try:
            print(running_core_hash(sys.argv[2] if len(sys.argv) > 2 else None, attempts=10, delay=2))
        except Exception as e:
            print(f'ERROR: Core process verification: {e}', file=sys.stderr)
            raise SystemExit(1)
        raise SystemExit(0)
    try:
        config = json.loads(CONFIG.read_text())
        raise SystemExit(update(config, problems(config)))
    except Exception:
        print('Monitor failed to load/check its configuration; inspect local service')
        raise SystemExit(1)
