#!/usr/bin/env python3
"""Run a built/installed CLI against its runtime in disposable POSIX state.

Python standard library only; no model, account, gateway, mesh or service setup.
Synthetic metadata is clearly distinguished from real inference measurements.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import termios
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', required=True, type=Path, help='Installed bin/potluck or built dist/index.js')
    parser.add_argument('--runtime', type=Path, help='Optional explicit runtime; omit to test package discovery')
    parser.add_argument('--report', required=True, type=Path, help='New output report file (never contains tokens)')
    args = parser.parse_args()
    if os.name != 'posix':
        parser.error('This terminal smoke requires macOS or Linux')
    cli = args.cli.resolve(strict=True)
    if args.report.exists():
        parser.error('Choose a new report file; existing evidence is never overwritten')
    command = [str(cli)] if cli.suffix != '.js' else [shutil.which('node') or 'node', str(cli)]
    checks = []
    result = {'schemaVersion': 1, 'passed': False, 'telemetry': 'synthetic validation metadata, not inference throughput',
              'checks': checks, 'platform': os.uname().sysname, 'runtimeDiscovery': 'explicit' if args.runtime else 'bundled'}
    with tempfile.TemporaryDirectory(prefix='potluck-usage-smoke-') as directory:
        state = Path(directory)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        env = {k: v for k, v in os.environ.items() if not k.startswith('POTLUCK_') and k not in ('NO_COLOR', 'PYTHONPATH', 'PYTHONHOME')}
        env.update(POTLUCK_DATA_DIR=directory, POTLUCK_MEMORY_DB_PATH=str(state / 'memory.db'),
                   POTLUCK_BIND_HOST='127.0.0.1', POTLUCK_SIDECAR_URL=f'http://127.0.0.1:{port}',
                   POTLUCK_LOCAL_API_SOCKET=str(state / 'absent.sock'), POTLUCK_CLOUD_API_URL='http://127.0.0.1:1',
                   TERM='xterm-256color', COLORFGBG='15;0')
        if args.runtime:
            env['POTLUCK_RUNTIME_BIN'] = str(args.runtime.resolve(strict=True))
        record = state / 'run/runtime.json'
        runtime_pid = None

        def run(*arguments, expected=0, json_output=True):
            proc = subprocess.run(command + list(arguments) + (['--json'] if json_output else []), env=env,
                                  cwd=directory, capture_output=True, text=True, timeout=45)
            # Never echo errors/logs which may contain control credentials.
            assert proc.returncode == expected, f'CLI {arguments[0]} returned {proc.returncode}'
            return json.loads(proc.stdout) if json_output else proc.stdout

        def request(path, data=None, authenticated=True):
            info = json.loads(record.read_text())
            headers = {'Authorization': 'Bearer ' + info['token']} if authenticated else {}
            if data is not None:
                headers['Content-Type'] = 'application/json'
            req = urllib.request.Request(info['url'] + path, headers=headers,
                                         data=None if data is None else json.dumps(data).encode())
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.load(response)

        def alive(pid):
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False

        try:
            offline = run('usage', expected=1)
            assert offline['error'] == 'usage_unavailable'
            result['version'] = run('--version', json_output=False).strip()
            assert 'potluck usage' in run('--help', json_output=False)
            up = run('up', '--timeout', '30000')
            runtime_pid = up['pid']
            assert up['started'] is True and up['owner'] == 'cli'
            assert request('/usage/dashboard')['totals']['requests'] == 0
            try:
                request('/usage/dashboard', authenticated=False)
                raise AssertionError('Unauthenticated usage access was accepted')
            except urllib.error.HTTPError as error:
                assert error.code == 401
            checks.append('runtime startup, empty state, offline JSON, local authentication')
            for output, duration, ok in [(20, 2000, True), (80, 3000, True), (5, 100, False)]:
                request('/usage/record', {'model_slug': 'validation-only', 'direction': 'consumed', 'scope': 'local',
                    'input_tokens': 50, 'output_tokens': output, 'duration_ms': duration, 'ttft_ms': 100, 'ok': ok})
            request('/usage/record', {'model_slug': 'validation-served', 'direction': 'served', 'scope': 'pool', 'output_tokens': 7})
            data = run('usage', '--window', '1')
            assert data['totals']['requests'] == 3 and data['totals']['failures'] == 1
            assert data['totals']['outputTokens'] == 105 and data['totals']['endToEndTokensPerSecond'] == 20
            assert data['performance']['local']['model'] is None and data['split']['state'] == 'idle'
            served = run('usage', '--direction', 'served')
            assert served['totals']['requests'] == 1 and served['totals']['endToEndTokensPerSecond'] is None
            assert 'validation-only' in run('usage', 'requests', '--once', json_output=False)
            checks.append('recorded metadata, rates, unknown samples, direction filtering and request snapshot')

            for ending in ('q', 'SIGTERM'):
                master, slave = pty.openpty()
                original = termios.tcgetattr(slave)
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 38, 130, 0, 0))
                child = subprocess.Popen(command + ['usage'], env=env, cwd=directory,
                    # Keep the harness's session. A session-leading /bin/sh
                    # launcher can acquire and revoke this PTY on exit on macOS,
                    # making post-exit termios inspection fail with ENOTTY.
                    stdin=slave, stdout=slave, stderr=slave)
                collected = bytearray()

                def drain(seconds):
                    until = time.monotonic() + seconds
                    while time.monotonic() < until:
                        if select.select([master], [], [], .05)[0]:
                            try:
                                collected.extend(os.read(master, 65536))
                            except OSError:
                                break

                def await_text(value, timeout=10):
                    until = time.monotonic() + timeout
                    while value not in collected and time.monotonic() < until:
                        drain(.1)
                    assert value in collected, 'Expected terminal view was not rendered'

                try:
                    await_text(b'validation-only')
                    assert b'?1049h' in collected and not termios.tcgetattr(slave)[3] & termios.ICANON
                    collected.clear(); os.write(master, b'4'); await_text(b'Your other machines')
                    collected.clear(); os.write(master, b'd'); await_text(b'CONTRIBUTION')
                    collected.clear(); os.write(master, b'w'); await_text(b'30D')
                    collected.clear(); os.write(master, b't'); await_text(b'48;2;246;243;237m')
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
                    os.kill(child.pid, signal.SIGWINCH); drain(.2)
                    if ending == 'q':
                        os.write(master, b'q')
                    else:
                        os.kill(child.pid, signal.SIGTERM)
                    drain(.5)
                    assert child.wait(timeout=5) == 0
                    assert b'?25h\x1b[?1049l' in collected
                    assert termios.tcgetattr(slave) == original, 'Keyboard mode was not restored'
                    checks.append(f'PTY views/filters/theme/resize and exact keyboard/cursor restoration after {ending}')
                finally:
                    if child.poll() is None:
                        child.kill(); child.wait(timeout=5)
                    os.close(master); os.close(slave)
            run('down', '--timeout', '30000')
            assert not record.exists() and not alive(runtime_pid), 'Runtime did not exit after shutdown'
            runtime_pid = None
            checks.append('shutdown removes discovery AND exits the runtime process')
            result['passed'] = True
        finally:
            # A startup timeout can still leave our private child running.
            if runtime_pid is None and record.exists():
                info = json.loads(record.read_text())
                if info.get('owner') == 'cli':
                    runtime_pid = info['pid']
            if runtime_pid is not None:
                try:
                    run('down', '--timeout', '30000')
                except Exception:
                    pass
                if alive(runtime_pid):
                    os.kill(runtime_pid, signal.SIGTERM)
                    until = time.monotonic() + 10
                    while alive(runtime_pid) and time.monotonic() < until:
                        time.sleep(.1)
                    if alive(runtime_pid):
                        os.kill(runtime_pid, signal.SIGKILL)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open('x') as destination:
                json.dump(result, destination, indent=2)
                destination.write('\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
