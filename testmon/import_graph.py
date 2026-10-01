"""Make a test depend on the module-level code of every module it imports.

Coverage sees a module's code outside function bodies only while the module
executes: once per process, and usually during collection, before any test
runs. So a test that only reads a module's globals or class bodies (a constant,
a pydantic model, an ORM table) never depended on that module, and a change to
it selected nothing (issue #191).

While collecting, the recorder wraps ``builtins.__import__`` and
``importlib.import_module`` and records who imports what:

- an import that runs while a project module's body executes is an edge of
  that module, shared by every test that reaches it;
- otherwise, an import belongs to the fixture being set up, else to the
  running test.

A test reaches its own module, the conftest modules on its path, the modules
defining the fixtures in its closure, and what those fixtures and the test
imported; then, transitively, every edge of a reached module. Each reached
module gets the pseudo line ``MODULE_LEVEL``, so the stored fingerprint
includes the module's code outside function bodies, and selection stays the
usual fingerprint lookup.
"""

import builtins
import importlib
import os
import sys
import sysconfig
from collections import defaultdict
from contextlib import contextmanager
from importlib.util import resolve_name

from testmon.process_code import MODULE_LEVEL


class ImportRecorder:
    def __init__(self, rootdir):
        self.rootdir = os.path.join(rootdir, "")
        self.omit = tuple(
            os.path.join(path, "")
            for key, path in sysconfig.get_paths().items()
            if key.endswith("lib")
        )
        self.edges = defaultdict(set)  # module file -> files its body imports
        self.owned = defaultdict(set)  # fixture key or test name -> files it imported
        self.owners = []  # the running test, then the fixtures being set up
        self.roots = {}  # test name -> (files, fixture keys)
        self._files = {}
        self._original_import = None
        self._original_import_module = None

    def install(self):
        self._original_import = builtins.__import__
        self._original_import_module = importlib.import_module
        builtins.__import__ = self._import
        importlib.import_module = self._import_module

    def uninstall(self):
        if self._original_import:
            builtins.__import__ = self._original_import
            importlib.import_module = self._original_import_module
            self._original_import = None

    def project_file(self, path):
        """``path`` relative to rootdir if coverage would trace it, else None."""
        if path not in self._files:
            path_ = os.path.abspath(path) if path else ""
            self._files[path] = (
                os.path.relpath(path_, self.rootdir).replace(os.sep, "/")
                if path_.endswith(".py")
                and path_.startswith(self.rootdir)
                and not path_.startswith(self.omit)
                else None
            )
        return self._files[path]

    def _import(self, name, globals=None, locals=None, fromlist=(), level=0):
        module = self._original_import(name, globals, locals, fromlist, level)
        try:
            if level:
                package = (globals or {}).get("__package__") or ""
                name = resolve_name("." * level + name, package)
            self._record(sys._getframe(1), name, fromlist)
        except Exception:  # pylint: disable=broad-except
            pass  # recording must never break an import
        return module

    def _import_module(self, name, package=None):
        module = self._original_import_module(name, package)
        try:
            self._record(sys._getframe(1), module.__name__, ())
        except Exception:  # pylint: disable=broad-except
            pass
        return module

    def _record(self, frame, name, fromlist):
        parts = name.split(".")
        names = [".".join(parts[: i + 1]) for i in range(len(parts))]
        module = sys.modules.get(name)
        for attribute in fromlist or ():
            if attribute == "*":
                names.extend(f"{name}.{a}" for a in getattr(module, "__all__", ()))
            else:
                names.append(f"{name}.{attribute}")
        files = set()
        for imported in names:
            file = self.project_file(
                getattr(sys.modules.get(imported), "__file__", None)
            )
            if file:
                files.add(file)
        if files:
            self._owner(frame).update(files)

    def _owner(self, frame):
        first_project_file = None
        while frame is not None:
            code = frame.f_code
            if code.co_name == "<module>" and self.project_file(code.co_filename):
                return self.edges[self.project_file(code.co_filename)]
            if first_project_file is None:
                first_project_file = self.project_file(code.co_filename)
            frame = frame.f_back
        if self.owners:
            return self.owned[self.owners[-1]]
        # Collection or a session hook: a project function imported it.
        return self.edges[first_project_file] if first_project_file else set()

    def fixture_key(self, fixturedef):
        code = getattr(fixturedef.func, "__code__", None)
        file = code and self.project_file(code.co_filename)
        return file or getattr(fixturedef.func, "__module__", ""), fixturedef.argname

    @contextmanager
    def owning(self, owner):
        self.owners.append(owner)
        try:
            yield
        finally:
            self.owners.pop()

    def add_fixtures(self, test_name, fixturedefs):
        if test_name not in self.roots:
            return
        files, keys = self.roots[test_name]
        for fixturedef in fixturedefs:
            key = self.fixture_key(fixturedef)
            keys.add(key)
            if key[0].endswith(".py"):  # a project file, not a plugin's module
                files.add(key[0])

    def start_test(self, item):
        files = {self.project_file(str(item.path))} - {None}
        directory = os.path.dirname(str(item.path))
        while directory.startswith(self.rootdir):
            conftest = self.project_file(os.path.join(directory, "conftest.py"))
            if conftest and os.path.exists(os.path.join(self.rootdir, conftest)):
                files.add(conftest)
            directory = os.path.dirname(directory)
        self.roots[item.nodeid] = (files, set())
        info = getattr(item, "_fixtureinfo", None)
        if info:
            for name in info.names_closure:
                self.add_fixtures(item.nodeid, info.name2fixturedefs.get(name, ()))

    def reached(self, test_names):
        """Every project file each test reaches through imports."""
        closures = {}
        result = {}
        for test_name in test_names:
            files, keys = self.roots.get(test_name, ((), ()))
            start = set(files) | self.owned.get(test_name, set())
            for key in keys:
                start |= self.owned.get(key, set())
            start = frozenset(start)
            if start not in closures:
                closures[start] = self._closure(start)
            result[test_name] = closures[start]
        return result

    def _closure(self, start):
        seen, stack = set(), list(start)
        while stack:
            file = stack.pop()
            if file in seen:
                continue
            seen.add(file)
            stack.extend(self.edges.get(file, ()))
            package = self._package(file)
            if package:
                stack.append(package)
        return seen

    def _package(self, file):
        """The ``__init__.py`` Python executes before ``file``, if any."""
        directory = os.path.dirname(file)
        if os.path.basename(file) == "__init__.py":
            directory = os.path.dirname(directory)
        init = f"{directory}/__init__.py" if directory else "__init__.py"
        key = ("package", init)
        if key not in self._files:
            self._files[key] = os.path.exists(os.path.join(self.rootdir, init))
        return init if self._files[key] and init != file else None
