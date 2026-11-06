"""Standalone, standard-library-only client for fnOS's upgrade task channel.

This file and a private Python runtime are staged before the old app is stopped.
Protocol references and measured device evidence are in docs/self-update-design.md.
"""

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import time

DAEMON_SOCKET = '/var/run/com.trim.app.center.sock'
APP = 'fnmusic-lx-first'
MAX_RESPONSE = 1024 * 1024
ROUTES = {'/rpc/v1/' + name for name in (
    'download/task', 'download/status', 'update/info', 'update/task', 'common/status',
)}


class UpgradeError(Exception):
    def __init__(self, reason, code=0):
        self.reason, self.code = reason, code
        super().__init__(f'{reason} (code={code})')


class OutcomeUnknown(UpgradeError):
    pass


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=30)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class AppCenter:
    def __init__(self, path=DAEMON_SOCKET):
        self.path = path

    def connection(self):
        return UnixConnection(self.path)

    def call(self, route, body):
        if route not in ROUTES:
            raise UpgradeError('invalid_route')
        connection = self.connection()
        sent = False
        try:
            connection.connect()
            sent = True
            connection.request('POST', route, json.dumps(body).encode(), {'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
            if response.status != 200 or len(raw) > MAX_RESPONSE:
                raise ValueError('Invalid response')
            envelope = json.loads(raw)
            if not isinstance(envelope, dict) or type(envelope.get('code')) is not int:
                raise ValueError('Invalid envelope')
            if envelope['code']:
                raise UpgradeError('daemon_rejected', envelope['code'])
            result = envelope.get('data', {})
            if not isinstance(result, dict):
                raise ValueError('Invalid data')
            return result
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if sent and route == '/rpc/v1/update/task':
                raise OutcomeUnknown('submission_unknown') from exc
            raise UpgradeError('daemon_unavailable') from exc
        finally:
            connection.close()


def write(job, name, value):
    temporary = job / (name + '.next')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(str(value) + '\n')
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(job / name)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
        raise UpgradeError('missing_task_id')
    return value


def installed_version(path):
    try:
        with Path(path).open() as source:
            match = re.search(r'^__version__\s*=\s*[\'"]([0-9]+\.[0-9]+\.[0-9]+)[\'"]', source.read(16384), re.M)
        return match[1] if match else ''
    except OSError:
        return ''


def probe(call):
    try:
        call('/rpc/v1/update/info', {})
    except UpgradeError as error:
        if error.code == 10030:
            return
        raise
    raise UpgradeError('upgrade_route_unverified')


def poll(call, route, key, task, *, seconds, sleep, submitted=False):
    deadline, failures = time.monotonic() + seconds, 0
    while time.monotonic() < deadline:
        try:
            result = call(route, {key: task})
        except UpgradeError as error:
            # Only status reads are retried. Submitting an update is never retried.
            failures += 1
            if failures >= 5 or (not submitted and error.code not in (0, 10050)):
                if submitted:
                    raise OutcomeUnknown('task_unobserved', error.code) from error
                raise
            sleep(2)
            continue
        failures = 0
        phase = result.get('status')
        if type(phase) is not int:
            raise OutcomeUnknown('task_unobserved') if submitted else UpgradeError('invalid_status')
        if phase == 2:
            return result
        if submitted and phase == 5:
            raise OutcomeUnknown('task_unobserved')
        if phase != 1:
            raise UpgradeError('task_failed', phase)
        sleep(2)
    raise OutcomeUnknown('task_timeout') if submitted else UpgradeError('staging_timeout')


def execute(job, request, call, sleep):
    target = request['version']
    if not isinstance(target, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', target):
        raise UpgradeError('invalid_request')
    for key in ('install_volume', 'data_volume'):
        if type(request.get(key)) is not int or request[key] <= 0:
            raise UpgradeError('volume_unverified')
    sha = hashlib.sha256()
    with (job / 'package.fpk').open('rb') as source:
        while block := source.read(1024 * 1024):
            sha.update(block)
    if sha.hexdigest() != request['sha256']:
        raise UpgradeError('package_changed')
    probe(call)
    staged_task = call('/rpc/v1/download/task', {'packageSourceType': 'file', 'path': str(job / 'package.fpk')})
    staged = poll(call, '/rpc/v1/download/status', 'downloadTaskId', identifier(staged_task.get('downloadTaskId')),
                  seconds=180, sleep=sleep)
    if (staged.get('appName') != APP or staged.get('version') != target
            or staged.get('packageType') != 'file' or staged.get('installed') is not True):
        raise UpgradeError('staged_package_mismatch')
    identity = {'appName': APP, 'updateVersion': target, 'packageType': 'file', 'language': 'zh-CN'}
    info = call('/rpc/v1/update/info', identity).get('wizardInfo', {})
    if (not isinstance(info, dict) or type(info.get('installedVolumeID')) is not int
            or info['installedVolumeID'] != request['install_volume']):
        raise UpgradeError('volume_unverified')
    for key, expected in (('appName', APP), ('version', target)):
        if key in info and info[key] != expected:
            raise UpgradeError('staged_package_mismatch')
    parameters = {'agreedToProtocol': True, 'installVolumeID': request['install_volume'],
                  'dataVolumeId': request['data_volume'], 'immediateStart': False}
    write(job, 'submitted', '1')
    task = call('/rpc/v1/update/task', {**identity, 'systemParameters': parameters, 'customParameters': []})
    try:
        task_id = identifier(task.get('taskId'))
    except UpgradeError as error:
        raise OutcomeUnknown('submission_unknown') from error
    write(job, 'task-id', task_id)
    poll(call, '/rpc/v1/common/status', 'taskId', task_id, seconds=900, sleep=sleep, submitted=True)
    actual = installed_version(request['installed_file'])
    if not actual:
        raise OutcomeUnknown('version_unobserved')
    if actual != target:
        raise UpgradeError('version_not_updated')
    write(job, 'status', 'verifying')


def run(job, *, call=None, sleep=time.sleep):
    job = Path(job)
    write(job, 'status', 'installing')
    try:
        request = json.loads((job / 'request.json').read_text())
        execute(job, request, call or AppCenter().call, sleep)
    except UpgradeError as error:
        write(job, 'reason', error.reason)
        write(job, 'daemon-code', error.code)
        write(job, 'status', 'unknown' if isinstance(error, OutcomeUnknown) else 'error')
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        write(job, 'reason', 'worker_failed')
        write(job, 'status', 'unknown' if (job / 'submitted').exists() else 'error')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(run(Path(__file__).resolve().parent))
