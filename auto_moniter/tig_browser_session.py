"""Keep approved TIG browser contexts alive between checks for one task."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time

from auto_moniter.control import read_control
from auto_moniter.directions import Directions

TERMINAL = {'done', 'stopped', 'cancelled', 'failed', 'invalid', 'max_rounds', 'stalled'}


def finished(run):
    state = Directions(run).read('state.json', 32 * 1024 * 1024)
    return (not state or state.get('status') in TERMINAL
            or read_control(run / 'control.json').get('action') == 'stop')


def address(run, request, settings_digest):
    from auto_moniter.candidate_checks import digest

    # Abstract Unix sockets avoid pathname limits and leave no socket files.
    return '\0auto-moniter-tig-' + digest([str(run), request['url'], request['expected_version'],
                                         request['binding']['tree'],
                                         settings_digest])[:48]


def own_user(connection):
    _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return uid == os.getuid()


def execute(run, job, request, settings_path, settings_digest):
    endpoint = address(run, request, settings_digest)
    with (run / 'tig-browser-session.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        connection = socket.socket(socket.AF_UNIX)
        try:
            connection.connect(endpoint)
        except ConnectionRefusedError:
            child = subprocess.Popen([sys.executable, '-m', __name__, str(run), str(settings_path),
                                      settings_digest, endpoint[1:]],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            for _ in range(100):
                if child.poll() is not None:
                    connection.close()
                    raise RuntimeError('无法启动任务浏览器会话。')
                try:
                    connection.connect(endpoint)
                    break
                except ConnectionRefusedError:
                    time.sleep(.1)
            else:
                child.terminate()
                child.wait()
                connection.close()
                raise RuntimeError('启动任务浏览器会话超时。')
    with connection:
        if not own_user(connection):
            raise ValueError('浏览器会话进程身份不匹配。')
        connection.settimeout(900)
        connection.sendall((json.dumps({'job': job.name, 'request': request}) + '\n').encode())
        connection.shutdown(socket.SHUT_WR)
        with connection.makefile('rb') as stream:
            return json.loads(stream.readline(128 * 1024))


def serve(run, settings_path, settings_digest, endpoint):
    from playwright.sync_api import sync_playwright

    from auto_moniter.tig_browser_case import BrowserSession, chrome_args, run_request

    info = settings_path.lstat()
    if not settings_path.is_file() or settings_path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        return
    content = settings_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != settings_digest:
        return
    settings = json.loads(content)
    with socket.socket(socket.AF_UNIX) as server, sync_playwright() as playwright:
        server.bind('\0' + endpoint)
        server.listen()
        server.settimeout(2)
        browser = playwright.chromium.launch(executable_path=settings.get('chrome'), headless=True,
                                             args=chrome_args(settings))
        try:
            session = BrowserSession(browser)
            while browser.is_connected() and not finished(run):
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    if not own_user(connection):
                        continue
                    connection.settimeout(5)
                    try:
                        with connection.makefile('rb') as stream:
                            message = json.loads(stream.readline(64 * 1024))
                        job_name = message['job']
                        if len(job_name) != 64 or any(ch not in '0123456789abcdef' for ch in job_name):
                            continue
                        job = run / 'browser-executions' / job_name
                        launch = Directions(job).read('launch.json')
                        request = message['request']
                        if (launch.get('request') != request
                                or launch.get('session_settings_digest') != settings_digest
                                or address(run, request, settings_digest)[1:] != endpoint
                                or finished(run)):
                            continue
                        os.chdir(job)
                        try:
                            code, output = run_request(request, settings, session=session)
                        finally:
                            os.chdir(run)
                        connection.sendall((json.dumps([code, output], ensure_ascii=False) + '\n').encode())
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
        finally:
            browser.close()


if __name__ == '__main__':
    serve(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4])
