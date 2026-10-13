"""Lets the tests' fake `claude` (a stand-in for subprocess.run) drive dev tasks, which
now run the CLI through tasks.run_agent (streamed, see agent._stream_claude).

    with patch_claude(fake):      # instead of mock.patch("subprocess.run", fake)
        registry._execute(...)

The fake keeps receiving exactly what it did before - the command, `input=` (the
prompt), `env=`, `cwd=`, `timeout=` - and its JSON stdout becomes the final result
line. git and every other command still go through it, i.e. through the real run.
"""

import contextlib
import json
from unittest import mock

from pm_studio import tasks as tasks_module


def _as_stream(fake):
    def run_agent(command, text, *, cwd, env, timeout, on_message, on_start=None):
        proc = fake(command, input=text, cwd=cwd, env=env, timeout=timeout,
                    capture_output=True, text=True, encoding="utf-8")
        stdout = (proc.stdout or "").strip()
        try:
            data = json.loads(stdout) if stdout else None
        except json.JSONDecodeError:
            return proc.returncode, None, stdout, proc.stderr or ""
        return proc.returncode, data, "", proc.stderr or ""
    return run_agent


@contextlib.contextmanager
def patch_claude(fake):
    with mock.patch("subprocess.run", fake), \
            mock.patch.object(tasks_module, "run_agent", _as_stream(fake)):
        yield
