"""Opaque phase RPC over an authenticated transport; no device shell recipes."""
import base64
import json
import subprocess
import threading

from .state import EndpointUnavailable

PROTOCOL = 1


class DeviceError(RuntimeError):
    def __init__(self, message, *, reported=False):
        super().__init__(message)
        self.reported = reported


class Client:
    def __init__(self, connection, progress):
        self.connection = connection
        self.ssh, self.progress = connection.argv, progress

    def call(self, operation, request=None, *, source=None, size=0, timeout=120):
        if operation not in ('status', 'prepare', 'targets', 'destination', 'install', 'handoff', 'diagnostics', 'abort', 'bootstrap', 'revoke-ssh', 'format-target', 'reset-target', 'installations', 'launch'):
            raise ValueError('Unsupported phase operation')
        try:
            self.connection.check()
        except EndpointUnavailable as error:
            raise DeviceError(str(error)) from error
        payload = base64.urlsafe_b64encode(json.dumps({'protocol': PROTOCOL, **(request or {})}).encode()).decode()
        argv = self.ssh + ['/usr/bin/orchard-device ' + operation + ' ' + payload]
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if source else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        records, errors, stderr = [], [], []
        timed_out = threading.Event()

        def expire():
            timed_out.set()
            process.kill()

        timer = threading.Timer(timeout, expire)
        timer.start()

        def collect():
            for line in process.stdout:
                try:
                    record = json.loads(line)
                    if record.get('protocol') != PROTOCOL:
                        raise ValueError('Incompatible protocol response')
                    if record['event'] == 'result':
                        records.append(record)
                    elif record['event'] == 'error':
                        errors.append(record['message'])
                    else:
                        self.progress.event(record)
                except (ValueError, KeyError) as error:
                    errors.append('Invalid device response: ' + str(error))

        def collect_errors():
            while True:
                block = process.stderr.read(4096)
                if not block:
                    return
                if sum(map(len, stderr)) < 65536:
                    stderr.append(block)

        readers = [threading.Thread(target=collect), threading.Thread(target=collect_errors)]
        for thread in readers:
            thread.start()
        try:
            if source:
                if operation != 'prepare':
                    self.progress.reset_transfer()
                sent = 0
                try:
                    for block in iter(lambda: source.read(1048576), b''):
                        process.stdin.write(block)
                        sent += len(block)
                        if operation != 'prepare':
                            self.progress.transfer(sent, size)
                    process.stdin.close()
                except BrokenPipeError:
                    pass  # Read the device's actual failure; never retry the stream.
            code = process.wait()
            for thread in readers:
                thread.join()
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
        if (timed_out.is_set() or code or errors or len(records) != 1 or
                (records and records[0].get('operation') != operation)):
            detail = '; '.join(errors) or b''.join(stderr).decode(errors='replace')[-2000:]
            if operation == 'reset-target':
                detail += '\nReset outcome is unknown; do not retry reset. Inspect storage and the saved formatting report.'
            elif operation in ('prepare', 'install', 'handoff', 'abort', 'format-target') and 'orchard install' not in detail:
                detail += '\nA fresh recovery boot is required; then run: orchard install'
            raise DeviceError(f'{operation} failed. {detail}', reported=bool(errors))
        return records[0]['result']
