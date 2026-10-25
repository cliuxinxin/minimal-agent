"""Double-click entry point: prepare one local environment, then open the web app."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
import venv
import webbrowser


ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8765'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def reopen_running_app() -> bool:
    """Reuse only this workspace's server running the current on-disk code."""
    try:
        # main() checks the interpreter before importing the application package.
        from miniagent.runtime import runtime_identity

        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(URL + '/api/health', timeout=0.5) as response:
            data = json.loads(response.read(4096))
        expected = runtime_identity(ROOT)
        if (isinstance(data, dict) and data.get('app') == expected['app']
                and data.get('workspace_id') == expected['workspace_id']):
            if (data.get('version') != expected['version']
                    or data.get('source_revision') != expected['source_revision']):
                print(f'检测到尚未加载当前更新的旧服务，正在启动 Minimal Agent {expected["version"]}。'
                      '原服务和正在进行的任务会继续保留。', flush=True)
                return False
            print(f'Minimal Agent is already running: {URL}', flush=True)
            webbrowser.open(URL)
            return True
    except (OSError, URLError, ValueError):
        pass
    return False


def main() -> int:
    if sys.version_info < (3, 11):
        print('Please install Python 3.11 or newer from https://www.python.org/downloads/, then open Start again.')
        return 1
    if reopen_running_app():
        return 0

    environment = ROOT / '.venv'
    python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    try:
        if not python.is_file():
            print('Preparing Minimal Agent for the first launch...', flush=True)
            venv.EnvBuilder(with_pip=True).create(environment)
        check = subprocess.run(
            [str(python), '-c',
             'import sys, pydantic; assert sys.version_info >= (3, 11); '
             'v = tuple(map(int, pydantic.__version__.split(".")[:2])); assert (2, 7) <= v < (3, 0)'],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if check.returncode:
            print('Installing the required Python package (Pydantic)...', flush=True)
            subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')],
                           cwd=ROOT, check=True)
        return subprocess.call([str(python), '-m', 'miniagent.web'], cwd=ROOT)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f'Unable to start Minimal Agent: {error}')
        print('Check your Python installation and network connection, then open Start again.')
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
