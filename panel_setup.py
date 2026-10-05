#!/usr/bin/env python3
"""Panel API helper; token stays in the environment, state is root-only."""
import ipaddress
import datetime
import hashlib
import http.client
import base64
import json
import os
from pathlib import Path
import secrets
import re
import sys
import subprocess
import time
import urllib.error
import urllib.request
import urllib.parse

ROOT = Path(os.environ.get('BOOT_STATE', '/var/lib/remna-bootstrap'))
def panel_connection(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
            parsed.fragment or parsed.path not in ('', '/', '/auth/login', '/auth/login/')):
        raise RuntimeError('Use HTTPS panel root URL or its access/login link')
    try:
        parsed.port
    except ValueError:
        raise RuntimeError('Invalid panel port') from None
    cookie = ''
    if parsed.query:
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if len(pairs) != 1 or not all(re.fullmatch(r'[A-Za-z0-9_-]+', x) for x in pairs[0]):
            raise RuntimeError('Access link must contain one nonempty name=value parameter')
        cookie = pairs[0][0] + '=' + pairs[0][1]
    return 'https://' + parsed.netloc.lower(), cookie


URL, ACCESS_COOKIE = panel_connection(os.environ['PANEL_URL'])
ACCESS_COOKIE = os.environ.get('PANEL_ACCESS_COOKIE') or ACCESS_COOKIE
if ACCESS_COOKIE and not re.fullmatch(r'[A-Za-z0-9_-]+=[A-Za-z0-9_-]+', ACCESS_COOKIE):
    raise RuntimeError('Invalid access cookie format')
def normalize_token(value):
    token = value.strip()
    token = re.sub(r'^Bearer(?:[ \t]+|$)', '', token, flags=re.IGNORECASE).strip()
    if not token:
        raise RuntimeError('API token is empty; paste the token issued by the panel')
    if any(not 33 <= ord(c) <= 126 for c in token):
        raise RuntimeError('API token contains internal whitespace/control/non-ASCII characters; paste it as one line')
    if token.startswith('eyJ') and not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token):
        raise RuntimeError('JWT contains an extra character or is incomplete; copy the token without a trailing backslash, quotes or other suffix')
    return token


try:
    TOKEN = normalize_token(os.environ.get('PANEL_TOKEN', ''))
except RuntimeError as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
DOMAIN = os.environ['NODE_DOMAIN']
NAME = os.environ['NODE_NAME']


def token_diagnostics():
    """Describe structure only; decoding does not validate the JWT signature."""
    if re.fullmatch(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', TOKEN):
        return 'UUID-shaped value; check that you copied the token, not the token record UUID'
    parts = TOKEN.split('.')
    if len(parts) != 3:
        return 'Not a three-part JWT; verify the full copied value and panel version'
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        if not isinstance(claims, dict): raise ValueError()
    except (ValueError, UnicodeError):
        return 'JWT payload is not valid JSON/base64; check for truncation or surrounding quotes'
    role = claims.get('role')
    role_label = 'API' if isinstance(role, str) and role.lower() == 'api' else (
        'ADMIN/browser' if isinstance(role, str) and role.lower() == 'admin' else 'other/unknown')
    exp = claims.get('exp')
    expiry = 'no exp claim' if exp is None else (
        'expired' if isinstance(exp, (int, float)) and exp <= time.time() else
        'not expired' if isinstance(exp, (int, float)) else 'invalid exp claim')
    return f'JWT payload decoded (signature NOT verified); role={role_label}; {expiry}'


def auth_error_details(error):
    """Read only bounded auth-error metadata, never dump response headers/cookies."""
    scheme = (error.headers.get('WWW-Authenticate', '') if error.headers else '').split(' ', 1)[0].lower()
    challenge = scheme if scheme in ('basic', 'bearer') else 'none/other'
    try:
        body = json.loads(error.read(16384))
        if not isinstance(body, dict): raise ValueError()
        message = body.get('message', '')
        if not isinstance(message, str): message = ''
        for secret in (TOKEN, ACCESS_COOKIE):
            if secret: message = message.replace(secret, '[hidden]')
        if ACCESS_COOKIE:
            message = message.replace(ACCESS_COOKIE.split('=', 1)[1], '[hidden]')
        message = re.sub(r'[\x00-\x1f\x7f]', ' ', message)[:240]
        code = body.get('errorCode')
        code_text = f'; errorCode={code}' if isinstance(code, int) else ''
        return f'JSON auth response; challenge={challenge}{code_text}' + (f'; message={message}' if message else '')
    except (ValueError, UnicodeError, OSError, http.client.HTTPException):
        return f'Non-JSON auth response; challenge={challenge}. Check external proxy/login protection'
    finally:
        error.close()


def api(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json',
        'Accept': 'application/json', 'User-Agent': 'RemnaBootstrap/1.1'}
    if ACCESS_COOKIE:
        headers['Cookie'] = ACCESS_COOKIE
    req = urllib.request.Request(URL + '/api/' + path, data=data, method=method,
        headers=headers)
    # Never follow redirects with the Authorization header.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    attempts = 3 if method == 'GET' else 1
    for attempt in range(attempts):
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as r:
                raw = r.read()
            if not raw:
                if method == 'DELETE': return None
                raise RuntimeError(f'API {method} {path}: empty response')
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                raise RuntimeError(f'API {method} {path}: expected JSON; got another response. Check reverse-proxy/login protection.') from None
            if not isinstance(result, dict) or 'response' not in result:
                raise RuntimeError(f'API {method} {path}: unexpected JSON envelope; check panel API version')
            return result['response']
        except urllib.error.HTTPError as e:
            if method == 'DELETE' and e.code == 404:
                return None
            if e.code in (429, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(attempt + 1)
                continue
            if e.code == 401:
                detail = auth_error_details(e)
                raise RuntimeError(f'API {method} {path}: HTTP 401. {detail}. '
                    'Authentication was rejected; verify full token, target panel and Authorization forwarding. '
                    'Cookie access and API-token authentication are separate checks.') from None
            raise RuntimeError(f'API {method} {path}: HTTP {e.code}; check token permissions/API access. Redirects are blocked.') from None
        except (urllib.error.URLError, http.client.RemoteDisconnected, http.client.IncompleteRead,
                ConnectionError, TimeoutError, OSError):
            if attempt + 1 < attempts:
                time.sleep(attempt + 1)
                continue
            hint = ('Check access-cookie validity and reverse-proxy configuration.' if ACCESS_COOKIE else
                'If the panel opens only through a secret ?name=value link, enter that full link; the proxy may abort requests without its access cookie.')
            raise RuntimeError(f'API {method} {path} at {URL}: connection closed/unavailable after {attempts} attempt(s). {hint}') from None


def check_api():
    print('Panel origin:', URL)
    print('Access cookie:', 'configured (value hidden)' if ACCESS_COOKIE else 'not configured')
    print('Token check:', token_diagnostics())
    for endpoint in ('config-profiles', 'nodes', 'hosts', 'internal-squads'):
        api('GET', endpoint)
        print('API GET ' + endpoint + ': OK')


def save(name, value):
    p = ROOT / name
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.chmod(0o600)
    tmp.replace(p)


def load():
    state = json.loads((ROOT / 'state.json').read_text())
    desired = {'domain': DOMAIN, 'name': NAME, 'panel': URL, 'panel_ip': os.environ['PANEL_IP']}
    if any(state.get(k) != v for k, v in desired.items()):
        raise RuntimeError('Saved setup parameters differ. Use the original parameters.')
    return state


def prepare():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    desired = {'domain': DOMAIN, 'name': NAME, 'panel': URL, 'panel_ip': os.environ['PANEL_IP']}
    ipaddress.IPv4Address(desired['panel_ip'])
    profiles = api('GET', 'config-profiles')['configProfiles']
    nodes = api('GET', 'nodes')
    hosts = api('GET', 'hosts')
    if (ROOT / 'state.json').exists():
        state = load()
        if any(state.get(k) != v for k, v in desired.items()):
            raise RuntimeError('Saved setup parameters differ. Use the original parameters.')
    else:
        state = desired
        save('state.json', state)
    for records, key, conflict in [
        (profiles, 'profile_uuid', lambda x: x['name'] == NAME),
        (nodes, 'node_uuid', lambda x: x['name'] == NAME or x['address'] == DOMAIN),
        (hosts, 'host_uuid', lambda x: x['remark'] == NAME or x['address'] == DOMAIN),
    ]:
        if state.get(key) and not any(x['uuid'] == state[key] for x in records):
            raise RuntimeError(f'Saved {key} no longer exists; inspect panel/state before resuming')
        if any(conflict(x) and x['uuid'] != state.get(key) for x in records):
            raise RuntimeError('Panel already contains this name/address outside saved state; no overwrite')
    secret = api('GET', 'keygen')['secretKey']
    if not secret or any(c.isspace() for c in secret):
        raise RuntimeError('Invalid node SECRET_KEY response')
    (ROOT / 'node-secret').write_text(secret + '\n\n')
    (ROOT / 'node-secret').chmod(0o600)
    if not (ROOT / 'xray.json').exists():
        pair = api('GET', 'system/tools/x25519/generate')['keypairs'][0]
        private = pair['privateKey']
        if pair.get('publicKey'):
            save('reality-public.json', {'publicKey': pair['publicKey']})
        config = {
            'log': {'loglevel': 'warning'},
            'inbounds': [{
                'tag': 'VLESS_REALITY', 'listen': '0.0.0.0', 'port': 443, 'protocol': 'vless',
                'settings': {'clients': [], 'decryption': 'none'},
                'sniffing': {'enabled': True, 'destOverride': ['http', 'tls', 'quic'], 'routeOnly': True},
                'streamSettings': {'network': 'tcp', 'security': 'reality', 'realitySettings': {
                    'show': False, 'xver': 1, 'dest': '/dev/shm/nginx.sock',
                    'privateKey': private, 'serverNames': [DOMAIN], 'shortIds': [secrets.token_hex(8)]
                }}
            }],
            'outbounds': [{'tag': 'DIRECT', 'protocol': 'freedom'}, {'tag': 'BLOCK', 'protocol': 'blackhole'}],
            'routing': {'rules': [{'type': 'field', 'ip': [
                '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
                '172.16.0.0/12', '192.168.0.0/16', '224.0.0.0/4', '240.0.0.0/4',
                '::/128', '::1/128', 'fc00::/7', 'fe80::/10', 'ff00::/8'
            ], 'outboundTag': 'BLOCK'}]}
        }
        save('xray.json', config)
    print('Panel API and saved parameters checked.')


def register():
    state = load()
    config = json.loads((ROOT / 'xray.json').read_text())
    if not state.get('profile_uuid'):
        r = api('POST', 'config-profiles', {'name': NAME, 'config': config})
        state['profile_uuid'] = r['uuid']
        state['inbound_uuid'] = next(x['uuid'] for x in r['inbounds'] if x['tag'] == 'VLESS_REALITY')
        save('state.json', state)
    if not state.get('node_uuid'):
        r = api('POST', 'nodes', {'name': NAME, 'address': DOMAIN, 'port': 2222,
            'configProfile': {'activeConfigProfileUuid': state['profile_uuid'],
                'activeInbounds': [state['inbound_uuid']]}, 'countryCode': 'XX'})
        state['node_uuid'] = r['uuid']
        save('state.json', state)
    if not state.get('host_uuid'):
        r = api('POST', 'hosts', {'remark': NAME, 'address': DOMAIN, 'port': 443,
            'sni': DOMAIN, 'fingerprint': 'chrome', 'isDisabled': False,
            'inbound': {'configProfileUuid': state['profile_uuid'],
                'configProfileInboundUuid': state['inbound_uuid']}})
        state['host_uuid'] = r['uuid']
        save('state.json', state)
    print('Created/reused profile, node and subscription host. State:', ROOT / 'state.json')


def verify():
    state = load()
    for _ in range(30):
        node = next((n for n in api('GET', 'nodes') if n['uuid'] == state['node_uuid']), None)
        if node and node.get('isConnected') and not node.get('isXrayError', False):
            print('Panel reports connected node without Xray error.')
            return
        time.sleep(4)
    raise RuntimeError('Node connection/Xray status not healthy after 120 seconds')


def squad_ids(squad):
    return [x['uuid'] if isinstance(x, dict) else x for x in squad['inbounds']]


def attach_squad():
    state = load()
    squads = api('GET', 'internal-squads')['internalSquads']
    for i, squad in enumerate(squads, 1):
        print(f'{i}: {squad["name"]} ({squad["uuid"]})')
    selected = os.environ.get('SQUAD_UUID')
    if selected is None:
        answer = input('Internal Squad: номер или Enter — не добавлять: ').strip()
        if not answer:
            print('Skipped squad assignment; add VLESS_REALITY manually.')
            return
        if not answer.isdigit() or not 1 <= int(answer) <= len(squads):
            raise RuntimeError('Invalid squad selection')
        selected = squads[int(answer) - 1]['uuid']
    squad = next((s for s in squads if s['uuid'] == selected), None)
    if not squad:
        raise RuntimeError('Selected squad does not exist')
    before = squad_ids(squad)
    inbound = state['inbound_uuid']
    if inbound not in before:
        # Journal intent BEFORE PATCH: a lost response can be safely reconciled/rolled back.
        additions = state.setdefault('squad_additions', [])
        entry = {'squad_uuid': selected, 'inbound_uuid': inbound}
        if entry not in additions:
            additions.append(entry)
            save('state.json', state)
        api('PATCH', 'internal-squads', {'uuid': selected, 'inbounds': before + [inbound]})
    confirmed = next(s for s in api('GET', 'internal-squads')['internalSquads'] if s['uuid'] == selected)
    if inbound not in squad_ids(confirmed):
        raise RuntimeError('Squad assignment not confirmed')
    print('New inbound added; existing squad inbounds preserved.')


def panel_snapshot(backup):
    p = Path(backup).resolve()
    if not p.is_relative_to((ROOT / 'backups').resolve()):
        raise RuntimeError('Invalid backup directory')
    state = load()
    data = {'state': state, 'profiles': api('GET', 'config-profiles'),
        'nodes': api('GET', 'nodes'), 'hosts': api('GET', 'hosts'),
        'squads': api('GET', 'internal-squads')}
    # Backups contain config secrets; root-only and local only.
    target = p / 'panel.json'
    target.write_text(json.dumps(data, indent=2))
    target.chmod(0o600)
    manifest_path = p / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['panel_sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2))


def wait_deleted(path, listing, key, value):
    api('DELETE', path)
    for _ in range(30):
        records = api('GET', listing)
        if listing == 'config-profiles':
            records = records['configProfiles']
        if not any(x[key] == value for x in records):
            return
        time.sleep(2)
    raise RuntimeError(f'Deletion not confirmed: {path}')


def rollback_panel(backup):
    p = Path(backup).resolve()
    if not p.is_relative_to((ROOT / 'backups').resolve()):
        raise RuntimeError('Invalid backup directory')
    manifest = json.loads((p / 'manifest.json').read_text())
    panel_bytes = (p / 'panel.json').read_bytes()
    if hashlib.sha256(panel_bytes).hexdigest() != manifest.get('panel_sha256'):
        raise RuntimeError('Invalid panel backup checksum')
    original = json.loads(panel_bytes)['state']
    state = load()
    # Never rewind objects that existed at snapshot time. Only owned new objects are removed.
    profiles = api('GET', 'config-profiles')['configProfiles']
    nodes = api('GET', 'nodes')
    hosts = api('GET', 'hosts')
    new_profile = state.get('profile_uuid') if not original.get('profile_uuid') else None
    if new_profile:
        if any(n.get('configProfile', {}).get('activeConfigProfileUuid') == new_profile and
               n['uuid'] != state.get('node_uuid') for n in nodes):
            raise RuntimeError('New profile is now used by another node; rollback refused')
        if any(h.get('inbound', {}).get('configProfileUuid') == new_profile and
               h['uuid'] != state.get('host_uuid') for h in hosts):
            raise RuntimeError('New profile is now used by another host; rollback refused')
        owned_squads = {x['squad_uuid'] for x in state.get('squad_additions', [])}
        if state.get('test_squad_uuid'):
            owned_squads.add(state['test_squad_uuid'])
        if any(state.get('inbound_uuid') in squad_ids(s) and s['uuid'] not in owned_squads
                for s in api('GET', 'internal-squads')['internalSquads']):
            raise RuntimeError('New inbound is now used by an unrelated squad; rollback refused')
    for records, key, check in [
        (profiles, 'profile_uuid', lambda x: x['name'] == NAME),
        (nodes, 'node_uuid', lambda x: x['name'] == NAME and x['address'] == DOMAIN),
        (hosts, 'host_uuid', lambda x: x['remark'] == NAME and x['address'] == DOMAIN),
    ]:
        found = next((x for x in records if x['uuid'] == state.get(key)), None)
        if found and not original.get(key) and not check(found):
            raise RuntimeError('Object identity changed; rollback refused')
    if state.get('test_user_id') != original.get('test_user_id') or state.get('test_squad_uuid') != original.get('test_squad_uuid'):
        cleanup_test()
    for entry in state.get('squad_additions', []):
        if entry in original.get('squad_additions', []):
            continue
        squads = api('GET', 'internal-squads')['internalSquads']
        squad = next((s for s in squads if s['uuid'] == entry['squad_uuid']), None)
        if squad:
            api('PATCH', 'internal-squads', {'uuid': squad['uuid'], 'inbounds': [
                x for x in squad_ids(squad) if x != entry['inbound_uuid']]})
    for key, endpoint, records in [('node_uuid', 'nodes', nodes), ('host_uuid', 'hosts', hosts),
            ('profile_uuid', 'config-profiles', profiles)]:
        value = state.get(key)
        if value and not original.get(key) and any(x['uuid'] == value for x in records):
            wait_deleted(endpoint + '/' + value, endpoint, 'uuid', value)
    save('state.json', original)
    print('Panel rollback completed; unrelated/current squad inbounds preserved.')


def public_key():
    if (ROOT / 'reality-public.json').exists():
        return json.loads((ROOT / 'reality-public.json').read_text())['publicKey']
    # Upgrade from the first script version: derive public key inside the container.
    private = json.loads((ROOT / 'xray.json').read_text())['inbounds'][0]['streamSettings']['realitySettings']['privateKey']
    text = subprocess.check_output(['docker', 'exec', 'remnanode', 'xray', 'x25519', '-i', private],
        text=True, timeout=10)
    for line in text.splitlines():
        if line.lower().startswith(('public', 'password')):
            return line.split(':', 1)[1].strip()
    raise RuntimeError('Could not derive Reality public key')


def test_user():
    state = load()
    if state.get('test_user_id') or state.get('test_squad_uuid'):
        raise RuntimeError('Clean up the previous test user/squad first')
    suffix = secrets.token_hex(4)
    squad = api('POST', 'internal-squads', {'name': 'BootstrapTest_' + suffix,
        'inbounds': [state['inbound_uuid']]})
    state['test_squad_uuid'] = squad['uuid']
    save('state.json', state)
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    user = api('POST', 'users', {'username': 'bootstrap_test_' + suffix, 'status': 'ACTIVE',
        'expireAt': expires.isoformat(), 'trafficLimitBytes': 100 * 1024 * 1024,
        'activeInternalSquads': [squad['uuid']]})
    state['test_user_id'] = user['id']
    save('state.json', state)
    reality = json.loads((ROOT / 'xray.json').read_text())['inbounds'][0]['streamSettings']['realitySettings']
    config = {'log': {'loglevel': 'warning'}, 'inbounds': [{'tag': 'TEST_SOCKS', 'listen': '127.0.0.1',
        'port': 10808, 'protocol': 'socks', 'settings': {'udp': False}}], 'outbounds': [{
        'protocol': 'vless', 'settings': {'vnext': [{'address': DOMAIN, 'port': 443,
            'users': [{'id': user['vlessUuid'], 'encryption': 'none', 'flow': 'xtls-rprx-vision'}]}]},
        'streamSettings': {'network': 'tcp', 'security': 'reality', 'realitySettings': {
            'serverName': DOMAIN, 'fingerprint': 'chrome', 'password': public_key(),
            'shortId': reality['shortIds'][0]}}}]}
    save('external-test.json', config)
    print('External client config:', ROOT / 'external-test.json')
    print('Test account expires in 1 hour; 100 MiB limit. Run cleanup-test after checking.')


def cleanup_test():
    state = load()
    if state.get('test_squad_uuid'):
        squads = api('GET', 'internal-squads')['internalSquads']
        squad = next((s for s in squads if s['uuid'] == state['test_squad_uuid']), None)
        if squad and squad.get('info', {}).get('membersCount', 0) > 1:
            raise RuntimeError('Test squad has additional users; cleanup refused')
    if state.get('test_user_id'):
        api('DELETE', 'users/' + str(state['test_user_id']))
        state.pop('test_user_id')
        save('state.json', state)
    if state.get('test_squad_uuid'):
        api('DELETE', 'internal-squads/' + state['test_squad_uuid'])
        state.pop('test_squad_uuid')
        save('state.json', state)
    (ROOT / 'external-test.json').unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        command = sys.argv[1]
        if command == 'connection': print(URL); print(ACCESS_COOKIE)
        elif command == 'check-api': check_api()
        elif command == 'snapshot': panel_snapshot(sys.argv[2])
        elif command == 'rollback': rollback_panel(sys.argv[2])
        else: {'prepare': prepare, 'register': register, 'verify': verify,
            'squad': attach_squad, 'test-user': test_user, 'cleanup-test': cleanup_test}[command]()
    except Exception as e:
        print(f'ERROR: {e}', file=sys.stderr)
        sys.exit(1)
