#!/usr/bin/env python3
"""Host-side backups, reproducible images and mandatory core checksum verification."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = Path(os.environ.get('BOOT_STATE', '/var/lib/remna-bootstrap'))
FILES = [
    '/opt/remnanode/docker-compose.yml', '/opt/remnanode/Caddyfile',
    '/opt/remnanode/xray-custom', '/opt/remnanode/.xray-custom.state',
    '/var/www/html/index.html', '/etc/ufw/user.rules', '/etc/ufw/user6.rules',
    '/etc/ufw/ufw.conf', '/etc/default/ufw',
    '/etc/sysctl.d/90-remna-bootstrap.conf',
    '/etc/unbound/unbound.conf.d/remna-bootstrap.conf',
    '/etc/systemd/resolved.conf.d/90-remna-bootstrap.conf',
    '/etc/systemd/system/remna-monitor.service', '/etc/systemd/system/remna-monitor.timer',
    '/etc/remna-bootstrap/monitor.json', '/usr/local/lib/remna-bootstrap/monitor.py',
    str(ROOT / 'images.json'), str(ROOT / 'state.json'), str(ROOT / 'stack-owned'),
    str(ROOT / 'xray.json'), str(ROOT / 'reality-public.json'),
]


def run(*args):
    return subprocess.check_output(args, text=True, timeout=120).strip()


def snapshot():
    backup = ROOT / 'backups' / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + str(time.time_ns()))
    backup.mkdir(parents=True, mode=0o700)
    manifest = {'files': {}, 'ufw_active': 'Status: active' in run('ufw', 'status'),
        'stack_existed': Path('/opt/remnanode/docker-compose.yml').exists(),
        'services': {}, 'sysctl': {k: run('sysctl', '-n', k) for k in
            ('net.core.default_qdisc', 'net.ipv4.tcp_congestion_control')}}
    for service in ('unbound', 'remna-monitor.timer'):
        p = subprocess.run(['systemctl', 'is-active', service], capture_output=True, text=True)
        enabled = subprocess.run(['systemctl', 'is-enabled', service], capture_output=True, text=True)
        manifest['services'][service] = {'active': p.returncode == 0, 'enabled': enabled.returncode == 0}
    for i, name in enumerate(FILES):
        p = Path(name)
        if p.is_symlink():
            raise RuntimeError(f'Refusing snapshot of symlink: {name}')
        if p.exists():
            target = backup / str(i)
            shutil.copy2(p, target)
            target.chmod(0o600)
            manifest['files'][name] = {'blob': str(i), 'mode': p.stat().st_mode & 0o777,
                'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
        else:
            manifest['files'][name] = None
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    (ROOT / 'latest-backup').write_text(str(backup))
    print(backup)


def validate_backup(backup):
    backup = Path(backup).resolve()
    if not backup.is_relative_to((ROOT / 'backups').resolve()):
        raise RuntimeError('Backup outside allowed directory')
    manifest = json.loads((backup / 'manifest.json').read_text())
    if set(manifest['files']) != set(FILES):
        raise RuntimeError('Backup file allowlist differs; review before restoring')
    if set(manifest.get('sysctl', {})) != {'net.core.default_qdisc', 'net.ipv4.tcp_congestion_control'}:
        raise RuntimeError('Backup sysctl allowlist differs')
    # Validate every blob before making any change.
    for name, entry in manifest['files'].items():
        if Path(name).is_symlink():
            raise RuntimeError(f'Refusing restore over symlink: {name}')
        if entry:
            blob = (backup / entry['blob']).resolve()
            if blob.parent != backup or hashlib.sha256(blob.read_bytes()).hexdigest() != entry['sha256']:
                raise RuntimeError('Invalid backup checksum/path')
    if 'panel_sha256' in manifest and hashlib.sha256((backup / 'panel.json').read_bytes()).hexdigest() != manifest['panel_sha256']:
        raise RuntimeError('Invalid panel backup checksum')
    return backup, manifest


def restore(backup):
    backup, manifest = validate_backup(backup)
    if Path('/opt/remnanode/docker-compose.yml').exists():
        subprocess.run(['docker', 'compose', '-f', '/opt/remnanode/docker-compose.yml',
            'stop', 'remnanode', 'caddy'], check=True, timeout=120)
    # Stop/disable newly added services while their unit files still exist.
    for service, status in manifest['services'].items():
        if not status['active']:
            subprocess.run(['systemctl', 'stop', service], check=False)
        if not status['enabled']:
            subprocess.run(['systemctl', 'disable', service], check=False)
    for name, entry in manifest['files'].items():
        p = Path(name)
        if entry:
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(backup / entry['blob'], p)
            p.chmod(entry['mode'])
        else:
            p.unlink(missing_ok=True)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['ufw', '--force', 'enable' if manifest['ufw_active'] else 'disable'], check=True)
    subprocess.run(['sysctl', '--system'], check=True, stdout=subprocess.DEVNULL)
    for key, value in manifest['sysctl'].items():
        subprocess.run(['sysctl', '-w', f'{key}={value}'], check=True, stdout=subprocess.DEVNULL)
    for service, status in manifest['services'].items():
        # A newly installed package can remain, but its enabled/active state is reverted below.
        if status['active']:
            subprocess.run(['systemctl', 'restart', service], check=True)
        if status['enabled']:
            subprocess.run(['systemctl', 'enable', service], check=True)
    subprocess.run(['systemctl', 'restart', 'systemd-resolved'], check=True)
    if manifest['stack_existed']:
        subprocess.run(['docker', 'compose', '-f', '/opt/remnanode/docker-compose.yml',
            'up', '-d', '--force-recreate'], check=True, timeout=180)
    print('Local files, runtime sysctl and firewall restored; packages/certificates are retained.')


def pin_images(compose_path='/opt/remnanode/docker-compose.yml'):
    compose = Path(compose_path)
    config = json.loads(run('docker', 'compose', '-f', str(compose), 'config', '--format', 'json'))
    pins = json.loads((ROOT / 'images.json').read_text()) if (ROOT / 'images.json').exists() else {}
    for service in ('remnanode', 'caddy'):
        image = config['services'][service]['image']
        if service not in pins:
            subprocess.run(['docker', 'pull', image], check=True, timeout=300)
            info = json.loads(run('docker', 'image', 'inspect', image))[0]
            repo = image.split('@')[0]
            repo = repo.rsplit(':', 1)[0] if ':' in repo.rsplit('/', 1)[-1] else repo
            if '/' not in repo:
                repo = 'library/' + repo
            digests = info.get('RepoDigests', [])
            pin = next((d for d in digests if d.split('@')[0].removeprefix('docker.io/') in
                (repo, repo.removeprefix('library/'))), None)
            if not pin or not re.search(r'@sha256:[a-f0-9]{64}$', pin):
                raise RuntimeError(f'No verified RepoDigest for {image}')
            pins[service] = pin
        else:
            subprocess.run(['docker', 'pull', pins[service]], check=True, timeout=300)
    # Update only service image lines using Compose's parsed service/image association.
    lines = compose.read_text().splitlines()
    current = None
    service_indent = None
    in_services = False
    for i, line in enumerate(lines):
        if line == 'services:':
            in_services = True
            continue
        if in_services and line and not line[0].isspace():
            in_services = False
        if not in_services:
            continue
        match = re.match(r'^(\s+)([a-zA-Z0-9_-]+):\s*$', line)
        if match and (service_indent is None or len(match[1]) == service_indent):
            service_indent = len(match[1])
            current = match[2]
        if current in pins and re.match(r'^\s+image:', line):
            lines[i] = re.sub(r'image:.*$', 'image: ' + pins[current], line)
    compose.write_text('\n'.join(lines) + '\n')
    subprocess.run(['docker', 'compose', '-f', str(compose), 'config', '-q'], check=True)
    (ROOT / 'images.json').write_text(json.dumps(pins, indent=2))
    print('Node and Caddy images pinned to RepoDigests.')


def certificate_storage():
    info = json.loads(run('docker', 'inspect', 'caddy-remnawave'))[0]
    if not any(m['Destination'] == '/data' and m['Type'] == 'volume' for m in info['Mounts']):
        raise RuntimeError('Caddy certificate storage is not a persistent Docker volume')
    print('Caddy ACME certificate storage uses a persistent volume.')


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'remna-bootstrap'}), timeout=60) as r:
        return r.read()


def verify_checksum(data, sidecar):
    match = re.search(r'^SHA2-256=\s*([a-fA-F0-9]{64})\s*$', sidecar, re.MULTILINE)
    if not match or hashlib.sha256(data).hexdigest() != match[1].lower():
        raise RuntimeError('Mandatory SHA256 verification failed/missing')


def fetch_core(tag, asset):
    if not re.fullmatch(r'v[0-9.]+-[0-9]+', tag) or not re.fullmatch(r'Xray-[A-Za-z0-9_-]+\.zip', asset):
        raise RuntimeError('Invalid release tag/asset')
    base = f'https://github.com/Jolymmiles/Xray-core/releases/download/{tag}/'
    data = fetch(base + asset)
    sidecar = fetch(base + asset + '.dgst').decode()
    verify_checksum(data, sidecar)
    dest = ROOT / 'verified-core'
    dest.mkdir(mode=0o700, exist_ok=True)
    archive = dest / 'core.zip'
    archive.write_bytes(data)
    with zipfile.ZipFile(archive) as z:
        binary = z.read('xray')
    if not binary.startswith(b'\x7fELF'):
        raise RuntimeError('Downloaded binary is not ELF')
    (dest / 'xray').write_bytes(binary)
    (dest / 'xray').chmod(0o755)
    (dest / 'core.dgst').write_text(sidecar)
    (dest / 'release.json').write_text(json.dumps({'tag': tag, 'asset': asset,
        'archive_sha256': hashlib.sha256(data).hexdigest(),
        'binary_sha256': hashlib.sha256(binary).hexdigest()}))
    print('Fork archive SHA256 verified; upstream will install this local verified binary.')


if __name__ == '__main__':
    try:
        command = sys.argv[1]
        if command == 'snapshot': snapshot()
        elif command == 'restore': restore(sys.argv[2])
        elif command == 'validate-backup': validate_backup(sys.argv[2]); print('Backup paths/checksums verified.')
        elif command == 'pin-images': pin_images()
        elif command == 'certificate-storage': certificate_storage()
        elif command == 'fetch-core': fetch_core(sys.argv[2], sys.argv[3])
        else: raise RuntimeError('Unknown operation')
    except Exception as e:
        print(f'ERROR: {e}', file=sys.stderr)
        sys.exit(1)
