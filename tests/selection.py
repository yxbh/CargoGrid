"""Opt-in dependency recording, complete collection and planned test deselection."""

from __future__ import annotations

import builtins
import functools
import io
import json
import os
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
from test_selection import FORMAT_VERSION, environment, function_regions, git, write_json

NODE = pytest.StashKey[str]()


def pytest_addoption(parser):
    parser.addoption("--record-test-map", type=Path, help="write dependency parts for this run")
    parser.addoption("--select-tests", type=Path, help="read a selection after complete collection")
    parser.addoption("--test-shard", type=int, help="zero-based shard in the selection plan")
    parser.addoption(
        "--collect-test-plan", type=Path, help="write all collected node ids and tiers"
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        item.stash[NODE] = item.nodeid
    result = yield
    collection = [
        {"nodeid": item.stash[NODE], "slow": item.get_closest_marker("slow") is not None}
        for item in items
    ]
    output = config.getoption("--collect-test-plan")
    if output:
        write_json(output, collection)
    path = config.getoption("--select-tests")
    if path:
        plan = json.loads(path.read_text(encoding="utf-8"))
        selected = set(plan["selected"])
        shard = config.getoption("--test-shard")
        if shard is not None:
            if not 0 <= shard < len(plan["shards"]):
                raise pytest.UsageError("--test-shard is outside the plan")
            selected = set(plan["shards"][shard])
        available = {item.stash[NODE] for item in items}
        if not selected <= available:
            raise pytest.UsageError("the test plan does not match complete collection")
        kept, rejected = [], []
        for item in items:
            (kept if item.stash[NODE] in selected else rejected).append(item)
        config.hook.pytest_deselected(items=rejected)
        items[:] = kept
    recorder = getattr(config, "_dependency_recorder", None)
    if recorder:
        recorder.nodeids = {item.nodeid: item.stash[NODE] for item in items}
    return result


def pytest_configure(config):
    output = config.getoption("--record-test-map")
    if not output:
        return
    if not hasattr(config, "workerinput") and config.getoption("numprocesses", default=0):
        return
    recorder = Recorder(config, output)
    config._dependency_recorder = recorder
    config.pluginmanager.register(recorder)


class Recorder:
    def __init__(self, config, output):
        import coverage

        self.root = config.rootpath.resolve()
        self.output = output
        self.context = ""
        self.item = None
        self.nodeids = {}
        self.tests = {}
        self.fixture_keys = defaultdict(set)
        self.data = defaultdict(set)
        self.processes = set()
        self.regions = {}
        self.originals = []
        self.worker = (
            f"{config.getoption('--test-shard')}-"
            f"{getattr(config, 'workerinput', {}).get('workerid', 'main')}"
        )
        self.coverage = coverage.Coverage(
            data_file=None,
            config_file=False,
            source=[
                str(self.root / name)
                for name in ("src", "tests", "tools")
                if (self.root / name).is_dir()
            ],
        )
        # SysMonitor can omit repeated calls after a context switch on Python 3.14.
        self.coverage.set_option("run:core", "ctrace")
        self.coverage.start()
        self.patch(builtins, "open", self.open_wrapper)
        self.patch(io, "open", self.open_wrapper)
        self.patch(subprocess.Popen, "__init__", self.process_wrapper)
        self.patch(ProcessPoolExecutor, "__init__", self.process_wrapper)

    def patch(self, owner, name, wrapper):
        original = getattr(owner, name)
        self.originals.append((owner, name, original))
        setattr(owner, name, wrapper(original))

    def project_path(self, path):
        try:
            relative = Path(path).resolve().relative_to(self.root).as_posix()
        except (TypeError, ValueError, OSError):
            return None
        return relative if relative.split("/", 1)[0] not in {".git", ".local", ".venv"} else None

    def open_wrapper(self, original):
        @functools.wraps(original)
        def opened(file, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            path = self.project_path(file)
            if (
                path
                and not path.endswith((".py", ".pyc"))
                and isinstance(mode, str)
                and "r" in mode
            ):
                self.data[self.context].add(path)
            return original(file, *args, **kwargs)

        return opened

    def process_wrapper(self, original):
        @functools.wraps(original)
        def started(*args, **kwargs):
            if self.context or self.project_caller():
                self.processes.add(self.context)
            return original(*args, **kwargs)

        return started

    def project_caller(self):
        frame = sys._getframe(2)
        while frame:
            path = self.project_path(frame.f_code.co_filename)
            if (
                path
                and path.startswith(("src/", "tests/", "tools/"))
                and Path(frame.f_code.co_filename).resolve() != Path(__file__).resolve()
            ):
                return True
            frame = frame.f_back
        return False

    def switch(self, context):
        self.context = context
        self.coverage.switch_context(context)

    def fixture_key(self, definition, item):
        if definition.scope == "function":
            return None
        path = self.project_path(definition.func.__code__.co_filename)
        if not path or not path.startswith(("src/", "tests/", "tools/")):
            return None
        scope = (
            item.path.relative_to(self.root).as_posix()
            if definition.scope in {"module", "class"}
            else definition.baseid
        )
        return f"fixture|{path}|{definition.argname}|{scope}"

    def fixture_dependencies(self, item):
        for definition in item._request._fixture_defs.values():
            key = self.fixture_key(definition, item)
            if key:
                self.fixture_keys[item.stash[NODE]].add(key)

    def clear_caches(self):
        seen = set()
        for module in tuple(sys.modules.values()):
            path = self.project_path(getattr(module, "__file__", None))
            if not path or not path.startswith(("src/", "tests/", "tools/")):
                continue
            namespaces = [vars(module)]
            namespaces.extend(
                vars(value)
                for value in tuple(vars(module).values())
                if isinstance(value, type) and value.__module__ == module.__name__
            )
            for namespace in namespaces:
                for value in tuple(namespace.values()):
                    if isinstance(value, (staticmethod, classmethod)):
                        value = value.__func__
                    if isinstance(value, functools._lru_cache_wrapper) and id(value) not in seen:
                        value.cache_clear()
                        seen.add(id(value))

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.clear_caches()
        self.item = item
        node = item.stash[NODE]
        self.tests[node] = {"duration": 0.0, "outcome": "skipped"}
        self.switch(node)
        try:
            return (yield)
        finally:
            self.switch("")
            self.item = None

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        key = self.fixture_key(fixturedef, self.item) if self.item else None
        previous = self.context
        if key:
            self.fixture_keys[self.item.stash[NODE]].add(key)
            self.switch(key)
        try:
            return (yield)
        finally:
            self.switch(previous)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_teardown(self, item, nextitem):
        self.fixture_dependencies(item)

    def pytest_runtest_logreport(self, report):
        node = self.nodeids.get(report.nodeid, report.nodeid)
        if node not in self.tests:
            return
        self.tests[node]["duration"] += report.duration
        if report.failed or report.when == "call":
            self.tests[node]["outcome"] = report.outcome

    def owner(self, path, line):
        if path not in self.regions:
            self.regions[path] = function_regions(Path(path).read_bytes())
        names = [name for start, end, name in self.regions[path] if start <= line <= end]
        return names[-1] if names else "<module>"

    def pytest_sessionfinish(self, session):
        self.coverage.stop()
        for owner, name, original in reversed(self.originals):
            setattr(owner, name, original)
        contexts = defaultdict(lambda: defaultdict(set))
        data = self.coverage.get_data()
        for path in data.measured_files():
            relative = self.project_path(path)
            if not relative or not relative.startswith(("src/", "tests/", "tools/")):
                continue
            for line, names in data.contexts_by_lineno(path).items():
                owner = self.owner(path, line)
                for name in names:
                    contexts[name][relative].add(owner)

        def record(context):
            return {
                "functions": {path: sorted(names) for path, names in contexts[context].items()},
                "data": sorted(self.data[context] | self.data[""]),
                "process": context in self.processes or "" in self.processes,
            }

        tests = {
            node: {
                **record(node),
                **result,
                "fixtures": sorted(self.fixture_keys[node]),
            }
            for node, result in self.tests.items()
        }
        fixtures = {key: record(key) for keys in self.fixture_keys.values() for key in keys}
        write_json(
            self.output / f"part-{self.worker}-{os.getpid()}.json",
            {
                "version": FORMAT_VERSION,
                "commit": git(self.root, "rev-parse", "HEAD").decode().strip(),
                "environment": environment(self.root),
                "tests": tests,
                "fixtures": fixtures,
            },
        )
