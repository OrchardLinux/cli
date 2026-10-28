"""Terminal progress, private diagnostics and non-overlapping installation timing."""
from contextlib import contextmanager
import os
import sys
import tempfile
import threading
import time


class Clock:
    def __init__(self):
        self.started = time.monotonic()
        self.excluded = {'download': 0.0, 'input': 0.0}
        self.phases = []
        self.lock = threading.Lock()
        self.active = {}

    @contextmanager
    def exclude(self, kind):
        with self.lock:
            count, began = self.active.get(kind, (0, time.monotonic()))
            self.active[kind] = (count + 1, began)
        try:
            yield
        finally:
            with self.lock:
                count, began = self.active[kind]
                if count == 1:
                    self.excluded[kind] += time.monotonic() - began
                    del self.active[kind]
                else:
                    self.active[kind] = (count - 1, began)

    def result(self):
        elapsed = time.monotonic() - self.started
        return {'install_seconds': max(0, elapsed - sum(self.excluded.values())),
                'elapsed_seconds': elapsed, 'excluded_seconds': dict(self.excluded)}


class Progress:
    def __init__(self, output=None, *, verbose=False):
        self.output = output or sys.stderr
        self.lock = threading.RLock()
        self.started = time.monotonic()
        self.last = None
        self.initial = None
        self.interactive = bool(getattr(self.output, 'isatty', lambda: False)())
        self.active_line = False
        self.verbose = verbose
        self.rows = {}
        self.rendered = 0
        self.log_path = None
        self.device_phase = None
        self.device_progress = None

    def task(self, name):
        return TaskProgress(self, name)

    def end_line(self):
        if self.active_line:
            print(file=self.output, flush=True)
            self.active_line = False
        self.rows.clear()
        self.rendered = 0

    def phase(self, message):
        with self.lock:
            self.end_line()
            print(message, file=self.output, flush=True)

    def diagnostic(self, message, *, display=True):
        with self.lock:
            if self.log_path is None:
                from .cache import cache_root
                directory = cache_root() / 'private/diagnostics'
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, self.log_path = tempfile.mkstemp(prefix='session-', suffix='.log', dir=directory)
                os.close(fd)
            with open(self.log_path, 'a') as stream:
                stream.write(str(message) + '\n')
            if self.verbose and display:
                self.phase(str(message))

    def transfer(self, sent, total):
        now = time.monotonic()
        with self.lock:
            if self.initial is None:
                self.initial = sent
            interval = 0.2 if self.interactive else 10
            if sent != total and self.last is not None and now - self.last < interval:
                return
            self.last = now
            message = transfer_text(sent, total, self.started, self.initial, now)
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
            self.started, self.last, self.initial = time.monotonic(), None, None

    def reset_device_progress(self):
        """End a device meter without inventing completion, including on RPC failure."""
        with self.lock:
            if self.device_progress is not None:
                self.end_line()
            self.device_phase = self.device_progress = None

    def cached_extraction(self, record):
        """Render validated compressed-byte consumption, not USB transfer speed."""
        if self.device_phase != 'cached-extraction':
            return False
        count, total, complete = record.get('bytes'), record.get('total'), record.get('complete')
        if (type(count) is not int or type(total) is not int or type(complete) is not bool or
                not 0 < total <= 2**63 - 1 or not 0 <= count <= total or
                (complete and count != total) or (not complete and count == total)):
            return False
        now = time.monotonic()
        with self.lock:
            state = self.device_progress
            if state is None:
                if count != 0 or complete:
                    return False
                state = self.device_progress = {'bytes': 0, 'total': total, 'started': now,
                                               'last': None, 'complete': False}
            elif (state['total'] != total or count < state['bytes'] or state['complete']):
                return False
            state['bytes'], state['complete'] = count, complete
            interval = 0.2 if self.interactive else 10
            if not complete and state['last'] is not None and now - state['last'] < interval:
                return True
            state['last'] = now
            message = 'Cached extraction  ' + transfer_text(count, total, state['started'], 0, now,
                                                          complete=complete)
            if self.interactive:
                print('\r\033[2K' + message, end='', file=self.output, flush=True)
                self.active_line = True
                if complete:
                    self.end_line()
            else:
                print(message, file=self.output, flush=True)
        return True

    def event(self, record):
        if record.get('event') == 'phase':
            self.reset_device_progress()
            self.device_phase = record.get('phase')
        if (record.get('event') == 'progress' and record.get('phase') == 'cached-extraction' and
                self.cached_extraction(record)):
            # Keep structured diagnostics without interrupting the live meter in verbose mode.
            self.diagnostic(str(record), display=False)
            return
        # Old and unknown hardware events retain their diagnostic-only behavior.
        self.diagnostic(str(record))

    def row(self, name, message, *, complete=False):
        with self.lock:
            self.rows[name] = message
            if self.interactive:
                if self.rendered:
                    self.output.write(f'\033[{self.rendered}A')
                for label, text in self.rows.items():
                    self.output.write(f'\r\033[2K  {label:<20.20} {text}\n')
                self.output.flush()
                self.rendered = len(self.rows)
            elif complete:
                print(f'  {name:<20.20} {message}', file=self.output, flush=True)


def transfer_text(sent, total, started, initial, now, *, complete=None):
    unit, divisor = ('KiB', 1024) if total < 1048576 else ('MiB', 1048576)
    rate = max(0, sent - initial) / max(now - started, 0.001)
    eta = f'{(total - sent) / rate:.0f}s' if rate and sent < total else '—'
    percent = sent * 100 / max(total, 1) if complete is None else (100 if complete else sent * 100 // total)
    return (f'{percent:3.0f}%  '
            f'{sent / divisor:5.0f} / {total / divisor:5.0f} {unit}  '
            f'{rate / 1048576:5.1f} MiB/s  {eta:>5}')


class TaskProgress(Progress):
    """A named row sharing the parent's terminal and output lock."""
    def __init__(self, parent, name):
        super().__init__(parent.output, verbose=parent.verbose)
        self.parent, self.name = parent, name
        self.started, self.last, self.initial = time.monotonic(), None, None

    def task(self, name):
        return self.parent.task(name)

    def phase(self, message):
        self.parent.row(self.name, message, complete=True)

    def reset_transfer(self):
        self.started, self.last, self.initial = time.monotonic(), None, None

    def transfer(self, sent, total):
        now = time.monotonic()
        if self.initial is None:
            self.initial = sent
        interval = 0.2 if self.parent.interactive else 10
        if sent != total and self.last is not None and now - self.last < interval:
            return
        self.last = now
        self.parent.row(self.name, transfer_text(sent, total, self.started, self.initial, now),
                        complete=sent == total or not self.parent.interactive)

    def diagnostic(self, message, *, display=True):
        self.parent.diagnostic(message, display=display)
