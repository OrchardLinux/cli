"""Human-readable progress and non-overlapping installation timing."""
from contextlib import contextmanager
import sys
import threading
import time


class Clock:
    def __init__(self):
        self.started = time.monotonic()
        self.excluded = {'download': 0.0, 'input': 0.0}
        self.phases = []

    @contextmanager
    def exclude(self, kind):
        began = time.monotonic()
        try:
            yield
        finally:
            self.excluded[kind] += time.monotonic() - began

    def result(self):
        elapsed = time.monotonic() - self.started
        return {'install_seconds': max(0, elapsed - sum(self.excluded.values())),
                'elapsed_seconds': elapsed, 'excluded_seconds': dict(self.excluded)}


class Progress:
    def __init__(self, output=None):
        self.output = output or sys.stderr
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.last = None
        self.interactive = bool(getattr(self.output, 'isatty', lambda: False)())
        self.active_line = False

    def end_line(self):
        if self.active_line:
            print(file=self.output, flush=True)
            self.active_line = False

    def phase(self, message):
        with self.lock:
            self.end_line()
            print(message, file=self.output, flush=True)

    def transfer(self, sent, total):
        now = time.monotonic()
        with self.lock:
            interval = 0.5 if self.interactive else 10
            if sent != total and self.last is not None and now - self.last < interval:
                return
            self.last = now
            rate = sent / max(now - self.started, 0.001)
            eta = (total - sent) / max(rate, 1)
            message = (f'  {sent / 1048576:.1f}/{total / 1048576:.1f} MiB '
                       f'({sent * 100 / total:.0f}%) · {rate / 1048576:.1f} MiB/s · ETA {eta:.0f}s')
            if self.interactive:
                print('\r\033[2K' + message, end='', file=self.output, flush=True)
                self.active_line = True
                if sent == total:
                    self.end_line()
            else:
                print(message, file=self.output, flush=True)

    def reset_transfer(self):
        with self.lock:
            self.end_line()
            self.started, self.last = time.monotonic(), None

    def event(self, record):
        if record['event'] == 'phase':
            self.phase('Device: ' + record['phase'])
        elif record['event'] == 'progress':
            self.phase('  ' + record['message'])
