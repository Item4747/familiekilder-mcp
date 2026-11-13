"""Run each unittest file in its own process and report one combined result.

Standard library only, so a fresh virtual environment can run it. A file that
crashes, hangs or runs no tests counts as failed: a green result must mean that
every listed file really finished.
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

RAN = re.compile(r'^Ran (\d+) tests? in ', re.MULTILINE)
SKIPPED = re.compile(r'skipped=(\d+)')
FILE_TIMEOUT_SECONDS = 15 * 60
SLOWEST_SHOWN = 5


def default_jobs():
    # Half the cores: several agents may run tests on the same machine at once.
    return max(1, min(8, (os.cpu_count() or 2) // 2))


class Run:
    """The processes still running, so an interrupt can stop all of them."""

    def __init__(self):
        self.lock = threading.Lock()
        self.processes = set()
        self.stopping = False

    def start(self, command, **options):
        with self.lock:
            if self.stopping:
                return None
            process = subprocess.Popen(command, start_new_session=True, **options)
            self.processes.add(process)
            return process

    def finish(self, process):
        with self.lock:
            self.processes.discard(process)

    def stop(self):
        with self.lock:
            self.stopping = True
            running = list(self.processes)
        for process in running:
            kill_group(process)


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def project_of(path):
    """The nearest folder above a test file that has a pyproject.toml.

    A file runs from its own project, so the tests of a project nested inside
    another repository import the same way as in a checkout of that project alone.
    """
    for folder in Path(path).resolve().parents:
        if (folder / 'pyproject.toml').is_file():
            return folder
    return Path.cwd()


def run_file(path, python, run, timeout):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='fi-') as temporary, tempfile.TemporaryFile() as output:
        environment = {**os.environ, 'TMPDIR': temporary}
        process = run.start([python, '-m', 'unittest', str(Path(path).resolve())], stdout=output,
                            stderr=subprocess.STDOUT, env=environment, cwd=project_of(path))
        if process is None:
            return result(path, started, 'stoppet', '')
        try:
            process.wait(timeout=timeout)
            status = None
        except subprocess.TimeoutExpired:
            status = f'stoppet etter {timeout} sekunder'
        # Children of a test may outlive it; none may survive into the next file.
        kill_group(process)
        process.wait()
        run.finish(process)
        output.seek(0)
        text = output.read().decode('utf8', 'replace')
    if status is None and process.returncode != 0:
        status = f'avsluttet med kode {process.returncode}'
    return result(path, started, status, text)


def result(path, started, status, text):
    ran = RAN.search(text)
    tests = int(ran.group(1)) if ran else 0
    summary = text[ran.end():] if ran else ''
    if status is None and not tests:
        status = 'kjørte ingen tester'
    return {
        'path': path,
        'seconds': time.monotonic() - started,
        'tests': tests,
        'skipped': sum(int(count) for count in SKIPPED.findall(summary)),
        'failure': status,
        'output': text,
    }


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--jobs', type=int, default=default_jobs())
    parser.add_argument('--timeout', type=int, default=FILE_TIMEOUT_SECONDS,
                        help='Sekunder per fil')
    parser.add_argument('--python', default=sys.executable)
    options = parser.parse_args(arguments)
    if options.jobs < 1:
        parser.error('--jobs må være minst 1')

    # Largest first: long files should not start last and stretch the run.
    files = sorted(options.files, key=lambda path: path.stat().st_size, reverse=True)
    started = time.monotonic()
    run = Run()
    results = []
    # Test processes have their own sessions, so a stopped runner must stop them itself.
    for stop_signal in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(stop_signal, interrupted)
    pool = ThreadPoolExecutor(max_workers=options.jobs)
    completed = False
    try:
        futures = [pool.submit(run_file, path, options.python, run, options.timeout)
                   for path in files]
        for future in as_completed(futures):
            results.append(future.result())
            print('.' if results[-1]['failure'] is None else 'F', end='', flush=True)
        completed = True
    except KeyboardInterrupt:
        print('\nAvbrutt', file=sys.stderr)
        return 130
    finally:
        if not completed:
            for stop_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(stop_signal, signal.SIG_IGN)
            run.stop()
        pool.shutdown(wait=True, cancel_futures=True)
    print()

    failed = sorted((item for item in results if item['failure']),
                    key=lambda item: item['path'].name)
    for item in failed:
        print(f"\n===== {item['path'].name}: {item['failure']}\n{item['output']}")
    for item in sorted(results, key=lambda item: item['seconds'], reverse=True)[:SLOWEST_SHOWN]:
        print(f"{item['seconds']:7.1f}s  {item['path'].name}")
    tests = sum(item['tests'] for item in results)
    skipped = sum(item['skipped'] for item in results)
    print(f'{tests} tester i {len(results)} filer på {time.monotonic() - started:.1f} sekunder, '
          f'{skipped} hoppet over, {options.jobs} samtidige')
    if failed:
        print('FEILET: ' + ', '.join(item['path'].name for item in failed))
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
