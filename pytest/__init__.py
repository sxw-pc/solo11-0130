"""Minimal pytest-compatible runner for standard-library-only environments.

If a real pytest distribution is installed, importing this package re-exports
it verbatim. Otherwise it provides just enough of the pytest API used by this
repository's tests (``raises``, ``mark.fixture`` and the ``tmp_path``
fixture) and a ``python -m pytest`` entry point that discovers and runs every
``test_*.py`` file under the tests directory.
"""
import importlib.util
import sys
import tempfile
import types
from pathlib import Path

_real = importlib.util.find_spec('pytest')
if _real is not None and _real.origin and 'site-packages' in str(_real.origin):
    # Defer entirely to the genuine distribution.
    from pytest import *  # noqa: F401,F403
    import pytest as _real_pytest  # noqa: F401
    sys.modules[__name__] = _real_pytest
else:
    class _Raises:
        def __init__(self, expected, match=None):
            self.expected = expected
            self.match = match

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                raise AssertionError(
                    f'Did not raise {getattr(self.expected, "__name__", self.expected)}')
            if not issubclass(exc_type, self.expected):
                return False
            if self.match is not None and self.match not in str(exc):
                raise AssertionError(
                    f'Pattern {self.match!r} not found in {str(exc)!r}')
            return True

    class _Mark:
        @staticmethod
        def fixture(fn=None, **_kw):
            def _tag(f):
                f._pytest_fixture = True
                return f
            if fn is None:
                return _tag
            return _tag(fn)

    raises = staticmethod(lambda expected, match=None: _Raises(expected, match))
    mark = _Mark()
    fixture = staticmethod(_Mark.fixture)

    def _invoke(fn, tmp_path, fixtures, seen):
        kwargs = {}
        for param in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
            if param == 'tmp_path':
                kwargs[param] = tmp_path
            elif param in fixtures:
                if param in seen:
                    raise RuntimeError('fixture cycle')
                seen.add(param)
                kwargs[param] = _invoke(fixtures[param], tmp_path, fixtures, seen)
            else:
                raise RuntimeError(f'unknown fixture {param!r}')
        return fn(**kwargs)

    def main(args=None):
        # Locate tests: accept explicit file or directory paths, otherwise
        # fall back to the repository's ./tests folder.
        explicit = [Path(a) for a in (args or []) if not a.startswith('-')]
        test_files = []
        for path in explicit:
            if path.is_dir():
                test_files.extend(sorted(path.glob('test_*.py')))
            elif path.is_file():
                test_files.append(path)
        if not test_files:
            root = Path.cwd() / 'tests'
            test_files = sorted(root.glob('test_*.py'))
        failures, passed = [], 0
        for tf in test_files:
            spec = importlib.util.spec_from_file_location(tf.stem, tf)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            fixtures = {name: obj for name, obj in vars(module).items()
                        if callable(obj) and getattr(obj, '_pytest_fixture', False)}
            for name in sorted(dir(module)):
                if not name.startswith('test_'):
                    continue
                fn = getattr(module, name)
                if not callable(fn):
                    continue
                with tempfile.TemporaryDirectory() as tmp:
                    try:
                        _invoke(fn, Path(tmp), fixtures, set())
                        passed += 1
                        print('.', end='', flush=True)
                    except Exception as exc:  # noqa: BLE001
                        failures.append((f'{tf.name}::{name}', exc))
                        print('F', end='', flush=True)
        print()
        for label, exc in failures:
            print(f'FAILED {label}: {type(exc).__name__}: {exc}')
        print(f'{passed} passed, {len(failures)} failed')
        return 1 if failures else 0
