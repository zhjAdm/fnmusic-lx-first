import hashlib
import json

import pytest


def fixture_job(tmp_path):
    package = tmp_path / 'package.fpk'
    package.write_bytes(b'verified-fpk')
    installed = tmp_path / 'installed.py'
    installed.write_text('__version__ = "0.1.12"\n')
    (tmp_path / 'request.json').write_text(json.dumps({
        'version': '0.1.13', 'sha256': hashlib.sha256(package.read_bytes()).hexdigest(),
        'installed_file': str(installed), 'install_volume': 3, 'data_volume': 3,
    }))
    return installed


def daemon(installed, *, refuse=0, actual_version='0.1.13', task_status=2, ambiguous=False):
    from fnmusic import upgrade_worker as module

    calls = []
    def call(route, body):
        calls.append((route, body))
        if route == '/rpc/v1/update/info' and body == {}:
            raise module.UpgradeError('daemon_rejected', 10030)
        if route == '/rpc/v1/download/task':
            return {'downloadTaskId': 'download-1'}
        if route == '/rpc/v1/download/status':
            return {'status': 2, 'appName': 'fnmusic-lx-first', 'version': '0.1.13', 'packageType': 'file', 'installed': True}
        if route == '/rpc/v1/update/info':
            return {'wizardInfo': {'appName': 'fnmusic-lx-first', 'version': '0.1.13', 'installedVolumeID': 3}}
        if route == '/rpc/v1/update/task':
            if refuse:
                raise module.UpgradeError('daemon_rejected', refuse)
            if ambiguous:
                raise module.OutcomeUnknown('submission_unknown')
            installed.write_text(f'__version__ = "{actual_version}"\n')
            return {'taskId': 'upgrade-1'}
        if route == '/rpc/v1/common/status':
            return {'status': task_status}
        raise AssertionError(route)
    return call, calls


def test_rpc_upgrade_preserves_volumes_and_waits_for_actual_task(tmp_path):
    from fnmusic.upgrade_worker import run

    installed = fixture_job(tmp_path)
    call, calls = daemon(installed)
    run(tmp_path, call=call, sleep=lambda _: None)
    assert (tmp_path / 'status').read_text().strip() == 'verifying'
    assert [route for route, body in calls][-2:] == ['/rpc/v1/update/task', '/rpc/v1/common/status']
    body = next(body for route, body in calls if route == '/rpc/v1/update/task')
    assert body['appName'] == 'fnmusic-lx-first' and body['updateVersion'] == '0.1.13'
    assert body['systemParameters']['installVolumeID'] == 3
    assert body['systemParameters']['dataVolumeId'] == 3
    assert (tmp_path / 'task-id').read_text().strip() == 'upgrade-1'


def test_nas_path_package_type_is_forwarded_to_upgrade_info_and_task(tmp_path):
    from fnmusic.upgrade_worker import run

    installed = fixture_job(tmp_path)
    call, calls = daemon(installed)
    def path_package(route, body):
        if route in {'/rpc/v1/update/info', '/rpc/v1/update/task'} and body:
            assert body['packageType'] == 'path'
        result = call(route, body)
        if route == '/rpc/v1/download/status':
            result.update(packageType='path', packageSourceType='file')
        return result
    run(tmp_path, call=path_package, sleep=lambda _: None)
    assert (tmp_path / 'status').read_text().strip() == 'verifying'
    assert sum(route == '/rpc/v1/update/task' for route, body in calls) == 1


@pytest.mark.parametrize('kind', ['cloud', '', None, 1])
def test_non_local_package_type_never_submits_upgrade(tmp_path, kind):
    from fnmusic.upgrade_worker import run

    installed = fixture_job(tmp_path)
    call, calls = daemon(installed)
    def wrong(route, body):
        result = call(route, body)
        if route == '/rpc/v1/download/status':
            result['packageType'] = kind
        return result
    run(tmp_path, call=wrong, sleep=lambda _: None)
    assert (tmp_path / 'status').read_text().strip() == 'error'
    assert not any(route == '/rpc/v1/update/task' for route, body in calls)


@pytest.mark.parametrize('mode', ['no_change', 'refused', 'unknown', 'task_failed'])
def test_failed_or_unobserved_upgrade_never_falls_back_or_reports_done(tmp_path, mode):
    from fnmusic.upgrade_worker import run

    installed = fixture_job(tmp_path)
    call, calls = daemon(installed, refuse=10237 if mode == 'refused' else 0,
                         actual_version='0.1.12' if mode == 'no_change' else '0.1.13',
                         ambiguous=mode == 'unknown', task_status=3 if mode == 'task_failed' else 2)
    run(tmp_path, call=call, sleep=lambda _: None)
    assert (tmp_path / 'status').read_text().strip() == ('unknown' if mode == 'unknown' else 'error')
    assert sum(route == '/rpc/v1/update/task' for route, body in calls) == 1
    assert all('uninstall' not in route and '/install/' not in route for route, body in calls)


def test_mismatched_staged_package_cannot_submit_upgrade(tmp_path):
    from fnmusic.upgrade_worker import run

    installed = fixture_job(tmp_path)
    call, calls = daemon(installed)
    def wrong(route, body):
        result = call(route, body)
        if route == '/rpc/v1/download/status':
            result['appName'] = 'another-app'
        return result
    run(tmp_path, call=wrong, sleep=lambda _: None)
    assert (tmp_path / 'status').read_text().strip() == 'error'
    assert not any(route == '/rpc/v1/update/task' for route, body in calls)


def test_socket_client_rejects_http_200_business_error_without_printing_raw_message(monkeypatch):
    from fnmusic.upgrade_worker import AppCenter, UpgradeError

    class Response:
        status = 200
        def read(self, maximum):
            return b'{"code":10237,"msg":"token=private","data":{}}'
    class Connection:
        def connect(self):
            pass
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            pass
    client = AppCenter()
    monkeypatch.setattr(client, 'connection', lambda: Connection())
    with pytest.raises(UpgradeError) as error:
        client.call('/rpc/v1/update/info', {})
    assert error.value.code == 10237
    assert 'private' not in str(error.value)


def test_missing_task_id_after_submission_is_unknown_and_never_retried(tmp_path):
    from fnmusic.upgrade_worker import run

    installed = fixture_job(tmp_path)
    call, calls = daemon(installed)
    def missing(route, body):
        result = call(route, body)
        return {} if route == '/rpc/v1/update/task' else result
    run(tmp_path, call=missing, sleep=lambda _: None)
    assert (tmp_path / 'status').read_text().strip() == 'unknown'
    assert sum(route == '/rpc/v1/update/task' for route, body in calls) == 1


def test_local_write_failure_after_task_accepted_keeps_outcome_unknown(tmp_path, monkeypatch):
    from fnmusic import upgrade_worker as module

    installed = fixture_job(tmp_path)
    call, calls = daemon(installed)
    original = module.write
    def denied(job, name, value):
        if name == 'task-id':
            raise OSError('disk full')
        return original(job, name, value)
    monkeypatch.setattr(module, 'write', denied)
    module.run(tmp_path, call=call, sleep=lambda _: None)
    assert (tmp_path / 'submitted').exists()
    assert (tmp_path / 'status').read_text().strip() == 'unknown'
    assert sum(route == '/rpc/v1/update/task' for route, body in calls) == 1


def test_data_volume_is_preserved_independently_of_install_volume(tmp_path):
    from fnmusic.upgrade_worker import run

    installed = fixture_job(tmp_path)
    request = json.loads((tmp_path / 'request.json').read_text())
    request['data_volume'] = 5
    (tmp_path / 'request.json').write_text(json.dumps(request))
    call, calls = daemon(installed)
    run(tmp_path, call=call, sleep=lambda _: None)
    parameters = next(body['systemParameters'] for route, body in calls if route == '/rpc/v1/update/task')
    assert parameters['installVolumeID'] == 3 and parameters['dataVolumeId'] == 5


def test_real_unix_http_client_uses_local_daemon_protocol(tmp_path):
    import http.server
    import socketserver
    import threading
    from fnmusic.upgrade_worker import AppCenter, probe

    seen = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            body = b'{"code":10030,"msg":""}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    # Keep the socket path below the AF_UNIX path-length limit.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='fnmusic-rpc-', dir='/tmp') as folder:
        server = socketserver.UnixStreamServer(folder + '/rpc.sock', Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            probe(AppCenter(folder + '/rpc.sock').call)
            assert seen == [('/rpc/v1/update/info', {})]
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
