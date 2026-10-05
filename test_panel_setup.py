"""Offline API contract simulation. Run: python test_panel_setup.py."""
import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import hashlib
import io
import json
from types import SimpleNamespace
from contextlib import redirect_stdout

os.environ.update(PANEL_URL='https://panel.example.com', PANEL_TOKEN='test',
    NODE_DOMAIN='node.example.com', NODE_NAME='Test_Node', PANEL_IP='203.0.113.2')
spec = importlib.util.spec_from_file_location('panel_setup', Path(__file__).with_name('panel_setup.py'))
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
records = {'profiles': [], 'nodes': [], 'hosts': [], 'squads': [
    {'uuid': 'squad-existing', 'name': 'Existing', 'inbounds': [{'uuid': 'old-inbound'}]}], 'users': []}
calls = []


def fake(method, path, body=None):
    calls.append((method, path))
    if method == 'GET':
        if path == 'config-profiles':
            return {'configProfiles': copy.deepcopy(records['profiles'])}
        if path in ('nodes', 'hosts'):
            return copy.deepcopy(records[path])
        if path == 'keygen':
            return {'secretKey': 'ZmFrZQ=='}
        if path == 'system/tools/x25519/generate':
            return {'keypairs': [{'privateKey': 'test-private', 'publicKey': 'test-public'}]}
        if path == 'internal-squads':
            return {'internalSquads': copy.deepcopy(records['squads'])}
    if method == 'PATCH' and path == 'internal-squads':
        r = next(s for s in records['squads'] if s['uuid'] == body['uuid'])
        r['inbounds'] = list(body['inbounds'])
        return copy.deepcopy(r)
    if method == 'DELETE':
        endpoint, ident = path.rsplit('/', 1)
        key = 'id' if endpoint == 'users' else 'uuid'
        category = {'config-profiles': 'profiles', 'internal-squads': 'squads'}.get(endpoint, endpoint)
        records[category] = [r for r in records[category] if str(r[key]) != ident]
        return None
    if method == 'POST':
        r = copy.deepcopy(body)
        r['uuid'] = path + '-uuid'
        if path == 'config-profiles':
            r['inbounds'] = [{'tag': 'VLESS_REALITY', 'uuid': 'inbound-uuid'}]
            records['profiles'].append(r)
        elif path == 'nodes':
            r['isConnected'] = True
            records['nodes'].append(r)
        elif path == 'hosts':
            records['hosts'].append(r)
        elif path == 'internal-squads':
            records['squads'].append(r)
        elif path == 'users':
            r['id'] = 123
            r['vlessUuid'] = '00000000-0000-4000-8000-000000000001'
            records['users'].append(r)
        else:
            raise AssertionError(path)
        return copy.deepcopy(r)
    raise AssertionError((method, path))


helper.api = fake
with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as td:
    helper.ROOT = Path(td)
    helper.prepare()
    backup = Path(td) / 'backups' / 'first'
    backup.mkdir(parents=True)
    (backup / 'manifest.json').write_text('{}')
    helper.panel_snapshot(backup)
    helper.register()
    helper.verify()
    first = (Path(td) / 'xray.json').read_text()
    helper.prepare()
    helper.register()
    assert sum(m == 'POST' for m, _ in calls) == 3
    assert (Path(td) / 'xray.json').read_text() == first
    os.environ['SQUAD_UUID'] = 'squad-existing'
    helper.attach_squad()
    helper.attach_squad()
    assert helper.squad_ids(records['squads'][0]) == ['old-inbound', 'inbound-uuid']
    assert sum(m == 'PATCH' for m, _ in calls) == 1
    helper.test_user()
    client = __import__('json').loads((Path(td) / 'external-test.json').read_text())
    assert client['outbounds'][0]['settings']['vnext'][0]['users'][0]['flow'] == 'xtls-rprx-vision'
    assert records['users'][0]['trafficLimitBytes'] == 100 * 1024 * 1024
    # Preserve a concurrently added unrelated inbound during rollback.
    records['squads'][0]['inbounds'].append('new-unrelated-inbound')
    helper.DOMAIN = 'different.example.com'
    try:
        helper.prepare()
    except RuntimeError:
        pass
    else:
        raise AssertionError('Changed domain accepted')
    helper.DOMAIN = 'node.example.com'
    records['hosts'].append({'uuid': 'foreign', 'remark': 'other', 'address': helper.DOMAIN})
    try:
        helper.prepare()
    except RuntimeError:
        pass
    else:
        raise AssertionError('Foreign host accepted')
    records['hosts'].pop()
    records['nodes'].append({'uuid': 'foreign-node', 'name': 'Foreign', 'address': 'other.example.com',
        'configProfile': {'activeConfigProfileUuid': 'config-profiles-uuid'}})
    try:
        helper.rollback_panel(backup)
    except RuntimeError:
        pass
    else:
        raise AssertionError('Rollback accepted profile used by unrelated node')
    records['nodes'].pop()
    helper.rollback_panel(backup)
    assert not records['profiles'] and not records['nodes'] and not records['hosts']
    assert not records['users']
    assert len(records['squads']) == 1
    assert helper.squad_ids(records['squads'][0]) == ['old-inbound', 'new-unrelated-inbound']
    assert 'node_uuid' not in helper.load()

def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

ops = module('ops')
digest = hashlib.sha256(b'archive').hexdigest()
ops.verify_checksum(b'archive', 'SHA2-256= ' + digest + '\n')
for data, checksum in [(b'altered', 'SHA2-256= ' + digest), (b'archive', 'missing')]:
    try: ops.verify_checksum(data, checksum)
    except RuntimeError: pass
    else: raise AssertionError('Bad/missing checksum accepted')

# Run backup/restore against temporary allowlisted files and stub system services.
with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as td:
    root = Path(td)
    ops.ROOT = root
    existing, absent = root / 'original.conf', root / 'new.conf'
    existing.write_text('before')
    ops.FILES = [str(existing), str(absent)]
    ops.run = lambda *args: 'Status: inactive'
    service_calls = []
    def service_stub(*args, **kwargs):
        service_calls.append(args[0])
        return SimpleNamespace(returncode=1)
    ops.subprocess = SimpleNamespace(run=service_stub, DEVNULL=-3)
    out = io.StringIO()
    with redirect_stdout(out): ops.snapshot()
    backup = Path(out.getvalue().strip())
    snapshot_manifest = json.loads((backup / 'manifest.json').read_text())
    snapshot_manifest['services']['unbound'] = {'active': True, 'enabled': True}
    (backup / 'manifest.json').write_text(json.dumps(snapshot_manifest))
    existing.write_text('after')
    absent.write_text('created')
    with redirect_stdout(io.StringIO()): ops.restore(backup)
    assert existing.read_text() == 'before' and not absent.exists()
    assert ['systemctl', 'restart', 'unbound'] in service_calls
    manifest = json.loads((backup / 'manifest.json').read_text())
    (backup / manifest['files'][str(existing)]['blob']).write_text('corrupted')
    existing.write_text('must stay')
    try: ops.restore(backup)
    except RuntimeError: pass
    else: raise AssertionError('Corrupt backup accepted')
    assert existing.read_text() == 'must stay'
    compose = root / 'compose.yml'
    compose.write_text('services:\n    caddy:\n      image: caddy:2.11.2\n      volumes:\n        - data:/data\n    remnanode:\n      image: remnawave/node:latest\nvolumes:\n  data:\n')
    images = {'caddy': 'caddy@sha256:' + 'a' * 64, 'remnanode': 'remnawave/node@sha256:' + 'b' * 64}
    def docker_run(*args):
        if 'config' in args:
            return json.dumps({'services': {'caddy': {'image': 'caddy:2.11.2'},
                'remnanode': {'image': 'remnawave/node:latest'}}})
        if args[:3] == ('docker', 'image', 'inspect'):
            return json.dumps([{'RepoDigests': [images['caddy' if args[3].startswith('caddy') else 'remnanode']]}])
        raise AssertionError(args)
    ops.run = docker_run
    with redirect_stdout(io.StringIO()): ops.pin_images(compose)
    assert all(pin in compose.read_text() for pin in images.values())
    ops.run = lambda *args: docker_run(*args) if 'config' in args else (_ for _ in ()).throw(AssertionError('Digest re-resolved'))
    with redirect_stdout(io.StringIO()): ops.pin_images(compose)
    assert json.loads((root / 'images.json').read_text()) == images

monitor = module('monitor')
with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as td:
    monitor.STATUS = Path(td) / 'status.json'
    out = io.StringIO()
    with redirect_stdout(out):
        monitor.update({'domain': 'test.example.com'}, [])
        monitor.update({'domain': 'test.example.com'}, [])
    assert out.getvalue() == ''
    with redirect_stdout(out):
        monitor.update({'domain': 'test.example.com'}, ['disk low'])
    first_event = out.getvalue()
    with redirect_stdout(out):
        monitor.update({'domain': 'test.example.com'}, ['disk low'])
    assert out.getvalue() == first_event
    with redirect_stdout(out):
        monitor.update({'domain': 'test.example.com'}, [])
    assert 'healthy' in out.getvalue()
print('PASS: API resume/conflicts, additive squads, limited Vision test user, scoped rollback, strict checksum, backup restore/corruption rejection, fixed image digests, quiet monitor/recovery')
