"""Child-process execution with a separate result channel, not an OS sandbox."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading


OUTPUT_LIMIT = 64 * 1024


def stop_process_tree():
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/PID', str(os.getpid()), '/T', '/F'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif os.getpgrp() == os.getpid():
        os.killpg(os.getpid(), signal.SIGKILL)
    os._exit(1)


def watch_host(control):
    try:
        while control.read(1):
            pass
    finally:
        stop_process_tree()


def main():
    # User and subprocess output is ordinary stdout/stderr, never a protocol.
    result_path = Path(sys.argv[1])
    sys.argv = ['<generated-python>']
    # Keep the lifetime pipe private; scripts and their children get EOF stdin.
    control = os.fdopen(os.dup(sys.stdin.fileno()), 'rb', buffering=0)
    sys.stdout.reconfigure(encoding='utf-8', errors='replace', write_through=True)
    sys.stderr.reconfigure(encoding='utf-8', errors='replace', write_through=True)
    watchdog = None
    try:
        job = json.loads(control.readline().decode('utf-8'))
        with open(os.devnull, 'rb') as empty_stdin:
            os.dup2(empty_stdin.fileno(), 0)
        sys.stdin = open(0, 'r', encoding='utf-8', closefd=False)
        watchdog = threading.Thread(target=watch_host, args=(control,), daemon=True)
        watchdog.start()
        namespace = {'__name__': '__main__' if job['mode'] == 'script' else '__agent_tool__',
                     '__file__': '<generated-python>'}
        exec(compile(job['code'], '<generated-python>', 'exec'), namespace)
        if job['mode'] == 'tool':
            entry = namespace.get('run')
            if not callable(entry):
                raise ValueError('Saved tools must define def run(arguments).')
            result = entry(job['arguments'])
        else:
            result = namespace.get('result')
        encoded = json.dumps({'ok': True, 'result': result}, ensure_ascii=False,
                             allow_nan=False).encode('utf-8')
        if len(encoded) > OUTPUT_LIMIT:
            raise ValueError('Python output exceeded 64 KiB.')
    except BaseException as error:
        encoded = json.dumps({'ok': False, 'error': f'{type(error).__name__}: {error}'[:2000]},
                             ensure_ascii=False).encode('utf-8', errors='replace')
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    temporary = result_path.with_suffix('.tmp')
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, result_path)
    # Stay alive so the host can terminate the complete Windows process tree.
    if watchdog is not None:
        watchdog.join()


if __name__ == '__main__':
    main()
