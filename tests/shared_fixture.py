"""Run-local snapshots of trusted test fixtures shared by xdist workers."""

import copyreg
import pickle
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from build123d.topology.history import ShapeHistory
from filelock import FileLock

T = TypeVar("T")


def _geometry_only_history(history):
    # BREP geometry is serializable; OCCT's construction-history handle is not.
    return ShapeHistory, ()


def shared_fixture(path: Path, create: Callable[[], T]) -> T:
    with FileLock(str(path.with_suffix(".lock"))):
        if not path.exists():
            temporary = path.with_suffix(".tmp")
            try:
                value = create()
                with temporary.open("wb") as output:
                    writer = pickle.Pickler(output, protocol=pickle.HIGHEST_PROTOCOL)
                    writer.dispatch_table = {
                        **copyreg.dispatch_table,
                        ShapeHistory: _geometry_only_history,
                    }
                    writer.dump(value)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            return value
        with path.open("rb") as source:
            return pickle.load(source)
