"""A very small pytest stand-in, so every suite runs on the documented interpreter.

Two suites in this package (`test_meetings_ui_views`, `test_meetings_ui_live`)
were written in pytest style. `drift/.venv` has no pytest and `drift/` is
read-only, so those two could not be run the way `adjourn/DEMO.md` tells you to
run everything else — which meant nobody could honestly say the suite was green.

This module supplies exactly the pytest surface those two files use and nothing
else: `fixture`, `mark.parametrize`, `mark.skipif`, `raises`, `skip`, and a
collector. It is a shim, not a test framework. If real pytest is installed it
wins — the test modules import it first and only fall back to this.

It also supplies the two builtin fixtures those suites ask for — `tmp_path` and
`monkeypatch` — because both are load-bearing safety fixtures here: `tmp_path`
is what keeps the disk-fallback tests off the real recordings directory, and
`monkeypatch` is what puts `config.RECORDINGS_DIR` back afterwards. A shim that
silently skipped them would turn two real tests into two lies.

What it deliberately does NOT do: fixture scopes beyond per-function,
conftest.py, plugins, assertion rewriting, `-k` selection, xfail. If a future
test needs any of those, install pytest rather than growing this file.
"""

from __future__ import annotations

import inspect
import os
import shutil
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

__all__ = ["fixture", "mark", "raises", "skip", "Skipped", "main", "run_module"]


class Skipped(Exception):
    """Raised by `skip()` and by a false `skipif` condition."""


def skip(reason: str = "") -> None:
    raise Skipped(reason or "skipped")


# --- fixtures ---------------------------------------------------------------


def fixture(func):
    """Mark a function as a fixture. Bare `@fixture` only — no scopes, no args.

    A generator function is a teardown fixture: everything after its single
    `yield` runs when the test finishes, pass or fail.
    """
    func.__minipytest_fixture__ = True
    return func


def _builtin_request():
    """A stand-in for pytest's `request`.

    Only its existence is load-bearing — `client_with(request)` accepts it and
    never reads it. It is a namespace rather than None so that a test which
    starts touching attributes fails loudly instead of on `NoneType`.
    """
    return SimpleNamespace(node=None, param=None, config=None)


def _builtin_tmp_path():
    """A private directory per test, removed afterwards."""
    path = Path(tempfile.mkdtemp(prefix="adjourn-minipytest-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class _MonkeyPatch:
    """setattr / setenv / delenv, each of them undone in reverse on teardown.

    The undo is the point. These suites repoint `config.RECORDINGS_DIR` at a
    temporary folder; a shim that set it and walked away would leave every
    later test in the process reading an empty directory that no longer exists.
    """

    _MISSING = object()

    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        previous = getattr(target, name, self._MISSING)
        self._undo.append(("attr", target, name, previous))
        setattr(target, name, value)

    def delattr(self, target, name, raising=True):
        if not hasattr(target, name):
            if raising:
                raise AttributeError(name)
            return
        self._undo.append(("attr", target, name, getattr(target, name)))
        delattr(target, name)

    def setenv(self, name, value):
        self._undo.append(("env", None, name, os.environ.get(name, self._MISSING)))
        os.environ[name] = str(value)

    def delenv(self, name, raising=True):
        if name not in os.environ:
            if raising:
                raise KeyError(name)
            return
        self._undo.append(("env", None, name, os.environ[name]))
        del os.environ[name]

    def undo(self):
        for kind, target, name, previous in reversed(self._undo):
            if kind == "attr":
                if previous is self._MISSING:
                    if hasattr(target, name):
                        delattr(target, name)
                else:
                    setattr(target, name, previous)
            elif previous is self._MISSING:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        self._undo.clear()


def _builtin_monkeypatch():
    patch = _MonkeyPatch()
    try:
        yield patch
    finally:
        patch.undo()


# --- marks ------------------------------------------------------------------


def _parametrize(argnames, argvalues):
    names = [n.strip() for n in argnames.split(",")] if isinstance(argnames, str) else list(argnames)

    def decorate(func):
        cases = []
        for value in argvalues:
            if len(names) == 1:
                cases.append({names[0]: value})
            else:
                cases.append(dict(zip(names, value)))
        # Stacked parametrize composes as a cartesian product, newest outermost,
        # which is what pytest does too.
        existing = getattr(func, "__minipytest_params__", None)
        if existing is None:
            func.__minipytest_params__ = cases
        else:
            func.__minipytest_params__ = [
                {**old, **new} for new in cases for old in existing
            ]
        return func

    return decorate


def _skipif(condition, reason=""):
    def decorate(func):
        if condition:
            func.__minipytest_skip__ = reason or "skipped"
        return func

    return decorate


mark = SimpleNamespace(parametrize=_parametrize, skipif=_skipif)


# --- raises -----------------------------------------------------------------


class _Raises:
    def __init__(self, expected):
        self.expected = expected
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            name = getattr(self.expected, "__name__", str(self.expected))
            raise AssertionError(f"DID NOT RAISE {name}")
        if issubclass(exc_type, self.expected):
            self.value = exc
            return True
        return False


def raises(expected):
    return _Raises(expected)


# --- the collector ----------------------------------------------------------


def _source_line(func) -> int:
    try:
        return inspect.getsourcelines(func)[1]
    except (OSError, TypeError):
        return 0


def _case_label(name: str, kwargs: dict) -> str:
    if not kwargs:
        return name
    inside = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
    return f"{name}[{inside}]"


def run_module(namespace: dict, title: str = "") -> tuple[int, int, int]:
    """Run every `test_*` callable in `namespace`. Returns (passed, failed, skipped)."""
    fixtures = {
        name: obj
        for name, obj in namespace.items()
        if callable(obj) and getattr(obj, "__minipytest_fixture__", False)
    }
    for name, builtin in (("request", _builtin_request),
                          ("tmp_path", _builtin_tmp_path),
                          ("monkeypatch", _builtin_monkeypatch)):
        fixtures.setdefault(name, builtin)

    tests = [
        (name, obj)
        for name, obj in namespace.items()
        if name.startswith("test_") and inspect.isfunction(obj)
    ]
    tests.sort(key=lambda pair: _source_line(pair[1]))

    if title:
        print(f"\n== {title} ==")

    passed = failed = skipped = 0
    for name, func in tests:
        reason = getattr(func, "__minipytest_skip__", None)
        cases = getattr(func, "__minipytest_params__", None) or [{}]
        for kwargs in cases:
            label = _case_label(name, kwargs)
            if reason:
                print(f"  skip {label} — {reason}")
                skipped += 1
                continue
            teardowns: list = []
            try:
                wanted = inspect.signature(func).parameters
                call = dict(kwargs)
                for argname in wanted:
                    if argname in call:
                        continue
                    if argname not in fixtures:
                        raise AssertionError(f"no fixture named {argname!r}")
                    call[argname] = _resolve(fixtures[argname], fixtures, teardowns)
                func(**call)
            except Skipped as exc:
                print(f"  skip {label} — {exc}")
                skipped += 1
            except Exception:  # noqa: BLE001 — a test failure is any exception
                print(f"  FAIL {label}")
                print("       " + traceback.format_exc().replace("\n", "\n       ").rstrip())
                failed += 1
            else:
                print(f"  ok   {label}")
                passed += 1
            finally:
                _unwind(teardowns)
    return passed, failed, skipped


def _resolve(fixture_func, fixtures: dict, teardowns: list):
    """Build one fixture value, resolving the fixtures it asks for in turn.

    No caching: every test gets a fresh value, which is pytest's `function`
    scope and the only scope these suites use.
    """
    wanted = inspect.signature(fixture_func).parameters
    kwargs = {}
    for argname in wanted:
        if argname not in fixtures:
            raise AssertionError(f"fixture {fixture_func.__name__!r} wants unknown {argname!r}")
        kwargs[argname] = _resolve(fixtures[argname], fixtures, teardowns)
    if inspect.isgeneratorfunction(fixture_func):
        generator = fixture_func(**kwargs)
        value = next(generator)
        teardowns.append(generator)
        return value
    return fixture_func(**kwargs)


def _unwind(teardowns: list) -> None:
    """Finish every teardown fixture, newest first, and never let one hide another."""
    for generator in reversed(teardowns):
        try:
            next(generator)
        except StopIteration:
            pass
        except Exception:  # noqa: BLE001 - a broken teardown is reported, not raised
            print("       teardown failed: " + traceback.format_exc().strip().splitlines()[-1])
    teardowns.clear()


def main(namespace: dict, title: str = "") -> int:
    passed, failed, skipped = run_module(namespace, title)
    bar = "=" * 72
    print(f"\n{bar}")
    tail = f", {skipped} skipped" if skipped else ""
    print(f"{passed} passed, {failed} failed{tail}")
    print(bar)
    return 1 if failed else 0
