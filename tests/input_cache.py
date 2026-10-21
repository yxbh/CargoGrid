"""Source-keyed test inputs; assertions and public constructors are never cached."""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import fields, is_dataclass
from hashlib import sha256
from importlib.metadata import version
from io import BytesIO
from math import isfinite
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from types import MethodType
from typing import TYPE_CHECKING

from filelock import FileLock

if TYPE_CHECKING:
    import numpy as np
    from build123d import Shape

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 1
MESH_FILES = {"vertices.npy", "faces.npy"}


class InputCacheError(ValueError):
    """A present input is incomplete, corrupt or incompatible with its key."""


def _canonical(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        from cargo_grid.vehicles.zeekr_7x_contours import ContourOutline

        native_fields = {"_wire", "_face"} if type(value) is ContourOutline else set()
        return {
            "type": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": {
                field.name: _canonical(getattr(value, field.name))
                for field in fields(value)
                if field.name not in native_fields
            },
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("input parameters require string mapping keys")
        return {"mapping": [[key, _canonical(value[key])] for key in sorted(value)]}
    if isinstance(value, tuple):
        return {"tuple": [_canonical(item) for item in value]}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"unsupported input parameter type: {type(value).__qualname__}")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _restore(value: object) -> object:
    if isinstance(value, list):
        return [_restore(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"tuple"}:
            return tuple(_restore(item) for item in value["tuple"])
        if set(value) == {"mapping"}:
            return {key: _restore(item) for key, item in value["mapping"]}
        raise InputCacheError("unsupported cached input metadata")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise InputCacheError("invalid cached input metadata")


def _binary(shape: Shape) -> bytes:
    from OCP.BinTools import BinTools, BinTools_FormatVersion

    stream = BytesIO()
    BinTools.Write_s(
        shape.wrapped,
        stream,
        False,
        False,
        BinTools_FormatVersion.BinTools_FormatVersion_CURRENT,
    )
    return stream.getvalue()


def _copy_input_shape(shape: Shape, memo: dict) -> Shape:
    from build123d import Shape
    from OCP.BinTools import BinTools
    from OCP.TopoDS import TopoDS_Shape

    # The ordinary kernel copy rewrites some surfaces, invalidating retained mesh bytes.
    # Binary cloning keeps test-owned geometry independent without that conversion.
    data = _binary(shape)
    marker = getattr(shape, "_test_input_mesh", None)
    unchanged = marker is not None and marker[1] == sha256(data).hexdigest()
    wrapped = TopoDS_Shape()
    BinTools.Read_s(wrapped, BytesIO(data))
    if wrapped.IsNull():
        raise InputCacheError("cannot clone an empty geometry input")
    copied = Shape.cast(wrapped)
    memo[id(shape)] = copied
    for name, value in vars(shape).items():
        if name not in {"_wrapped", "_history", "__deepcopy__", "_test_input_mesh"}:
            setattr(copied, name, deepcopy(value, memo))
    if unchanged:
        # BREP reads can normalize signed zero; only an unchanged source may retain its arrays.
        copied._test_input_mesh = (marker[0], sha256(_binary(copied)).hexdigest())
    copied.__deepcopy__ = MethodType(_copy_input_shape, copied)
    return copied


def _toolchain() -> dict[str, str]:
    return {
        "python": sys.version,
        "platform": sys.platform,
        "libc": ":".join(platform.libc_ver()),
        "macos": platform.mac_ver()[0],
        "machine": platform.machine(),
        **{
            name: version(name)
            for name in ("build123d", "cadquery-ocp-novtk", "numpy", "scipy", "shapely")
        },
    }


def _verify_geometry(shape: Shape, record: dict, entry: Path) -> None:
    import numpy as np
    from OCP.Precision import Precision

    bounds = shape.bounding_box()
    volume = shape.volume
    if (
        not shape.is_valid
        or len(shape.solids()) != record["solids"]
        or len(shape.faces()) != record["faces"]
        or len(shape.edges()) != record["edges"]
        or not isfinite(volume)
        or volume <= 0
        or abs(volume - record["volume"]) > Precision.Confusion_s()
        or not all(np.isfinite(tuple(bounds.min) + tuple(bounds.max)))
        or not np.allclose(
            [list(bounds.min), list(bounds.max)],
            record["bounds"],
            atol=Precision.Confusion_s(),
            rtol=0,
        )
    ):
        raise InputCacheError(f"invalid input cache geometry: {entry}")


class InputStore:
    def __init__(
        self,
        root: Path,
        *,
        enabled: bool = True,
        dependencies: Mapping[str, str] | None = None,
        sources_at_start: Mapping[Path, str] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.enabled = enabled
        self.dependencies = dict(_toolchain() if dependencies is None else dependencies)
        self.sources_at_start = dict(sources_at_start or {})
        self._temporary: list[TemporaryDirectory] = []

    def close(self) -> None:
        for temporary in self._temporary:
            temporary.cleanup()
        self._temporary.clear()

    def key(self, kind: str, parameters: object, sources: Sequence[Path]) -> str:
        source_hashes = {}
        for path in (*sources, Path(__file__)):
            name = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
            digest = sha256(path.read_bytes()).hexdigest()
            if path in self.sources_at_start and self.sources_at_start[path] != digest:
                raise InputCacheError(
                    f"source changed while tests were running: {path}; restart pytest"
                )
            source_hashes[name] = digest
        return sha256(
            _json_bytes(
                {
                    "schema": SCHEMA,
                    "kind": kind,
                    "parameters": _canonical(parameters),
                    "sources": source_hashes,
                    "dependencies": self.dependencies,
                }
            )
        ).hexdigest()

    def _manifest(self, entry: Path) -> dict:
        try:
            data = (entry / "entry.json").read_bytes()
            if sha256(data).hexdigest() != (entry / "entry.sha256").read_text():
                raise InputCacheError(f"input cache manifest checksum mismatch: {entry}")
            record = json.loads(data)
            _json_bytes(record)
        except (OSError, ValueError) as error:
            raise InputCacheError(f"cannot read input cache entry {entry}: {error}") from error
        if (
            entry.is_symlink()
            or not isinstance(record, dict)
            or record.get("schema") != SCHEMA
            or record.get("key") != entry.name
            or not isinstance(record.get("files"), dict)
            or not record["files"]
        ):
            raise InputCacheError(f"invalid input cache manifest: {entry}")
        for name, digest in record["files"].items():
            if not isinstance(name, str) or not isinstance(digest, str) or len(digest) != 64:
                raise InputCacheError(f"invalid input cache file record: {entry}")
            path = PurePosixPath(name)
            if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in name:
                raise InputCacheError(f"unsafe input cache member: {entry}/{name}")
            member = entry / name
            if (
                any(
                    parent.is_symlink() for parent in member.parents if parent.is_relative_to(entry)
                )
                or member.is_symlink()
                or not member.is_file()
            ):
                raise InputCacheError(f"missing input cache member: {member}")
            if sha256(member.read_bytes()).hexdigest() != digest:
                raise InputCacheError(f"input cache checksum mismatch: {member}")
        actual = {
            path.relative_to(entry).as_posix()
            for path in entry.rglob("*")
            if path.is_file() and path not in {entry / "entry.json", entry / "entry.sha256"}
        }
        if actual != set(record["files"]):
            raise InputCacheError(f"unexpected input cache members: {entry}")
        return record

    def _publish(self, key: str, build: Callable[[Path], dict]) -> tuple[Path, dict]:
        self.root.mkdir(parents=True, exist_ok=True)
        entry = self.root / key
        with FileLock(str(self.root / f"{key}.lock")):
            if entry.exists() or entry.is_symlink():
                return entry, self._manifest(entry)
            with TemporaryDirectory(prefix=".building-", dir=self.root) as temporary:
                staging = Path(temporary) / key
                staging.mkdir()
                metadata = build(staging)
                files = {
                    path.relative_to(staging).as_posix(): sha256(path.read_bytes()).hexdigest()
                    for path in sorted(staging.rglob("*"))
                    if path.is_file()
                }
                if not files or any(path.is_symlink() for path in staging.rglob("*")):
                    raise InputCacheError("input builder must produce ordinary files")
                record = {"schema": SCHEMA, "key": key, "files": files, **metadata}
                data = _json_bytes(record)
                (staging / "entry.json").write_bytes(data)
                (staging / "entry.sha256").write_text(sha256(data).hexdigest())
                staging.replace(entry)
        return entry, self._manifest(entry)

    def geometry(
        self,
        kind: str,
        parameters: object,
        sources: Sequence[Path],
        create: Callable[[], Shape],
        *,
        describe: Callable[[], dict] | None = None,
    ) -> Shape:
        if not self.enabled:
            return create()
        from build123d import Color, Shape
        from OCP.BinTools import BinTools
        from OCP.TopoDS import TopoDS_Shape

        fresh = None

        def build(staging: Path) -> dict:
            nonlocal fresh
            shape = fresh = create()
            bounds = shape.bounding_box()
            volume = shape.volume
            if not shape.is_valid or not shape.solids() or not isfinite(volume) or volume <= 0:
                raise ValueError("geometry input must contain valid positive-volume solids")
            (staging / "shape.brep").write_bytes(_binary(shape))
            if self.key(f"geometry:{kind}", parameters, sources) != key:
                raise InputCacheError("geometry inputs changed during construction")
            return {
                "type": "geometry",
                "label": shape.label,
                "color": tuple(shape.color) if shape.color is not None else None,
                "solids": len(shape.solids()),
                "faces": len(shape.faces()),
                "edges": len(shape.edges()),
                "volume": volume,
                "bounds": [list(bounds.min), list(bounds.max)],
                "data": _canonical(describe()) if describe is not None else None,
            }

        key = self.key(f"geometry:{kind}", parameters, sources)
        entry, record = self._publish(key, build)
        if record.get("type") != "geometry" or set(record["files"]) != {"shape.brep"}:
            raise InputCacheError(f"not a geometry input: {entry}")
        wrapped = TopoDS_Shape()
        BinTools.Read_s(wrapped, BytesIO((entry / "shape.brep").read_bytes()))
        if wrapped.IsNull():
            raise InputCacheError(f"empty input cache geometry: {entry}")
        shape = Shape.cast(wrapped)
        shape.label = record["label"]
        shape.color = Color(*record["color"]) if record["color"] is not None else None
        _verify_geometry(shape, record, entry)
        shape._test_input_mesh = (key, sha256(_binary(shape)).hexdigest())
        shape._test_input_data = _restore(record["data"])

        def create_mesh():
            nonlocal fresh

            from cargo_grid.meshes import checked_mesh

            # A BREP remesh can change bytes; use the original constructor's owned copy.
            original, fresh = fresh, None
            if (
                original is None
                or sha256(_binary(original)).hexdigest() != record["files"]["shape.brep"]
            ):
                original = create()
            _verify_geometry(original, record, entry)
            owned = deepcopy(original)
            owned.bounding_box()
            result = checked_mesh(owned)
            if self.key(f"geometry:{kind}", parameters, sources) != key:
                raise InputCacheError("mesh inputs changed during construction")
            return result

        shape._test_input_mesh_factory = create_mesh
        shape.__deepcopy__ = MethodType(_copy_input_shape, shape)
        return shape

    def design(self, kind, parameters, sources, create):
        from cargo_grid.jobs import Design

        if not self.enabled:
            return create()
        data = {}

        def build():
            design = create()
            data.update(
                {
                    field.name: getattr(design, field.name)
                    for field in fields(Design)
                    if field.name != "shape"
                }
            )
            return design.shape

        shape = self.geometry(f"design:{kind}", parameters, sources, build, describe=lambda: data)
        return Design(shape=shape, **shape._test_input_data)

    def checked_mesh(self, shape: Shape) -> tuple[np.ndarray, np.ndarray, dict]:
        from cargo_grid.meshes import _edge_defects, checked_mesh

        marker = getattr(shape, "_test_input_mesh", None)
        # Moving, editing or replacing public geometry must still trigger a fresh mesh.
        if not self.enabled or marker is None or marker[1] != sha256(_binary(shape)).hexdigest():
            return checked_mesh(shape)
        import numpy as np

        key = sha256(f"mesh:{marker[0]}".encode()).hexdigest()

        def build(staging):
            points, faces, report = shape._test_input_mesh_factory()
            np.save(staging / "vertices.npy", np.asarray(points, dtype="<f8"), allow_pickle=False)
            np.save(staging / "faces.npy", np.asarray(faces, dtype="<i8"), allow_pickle=False)
            return {"type": "mesh", "geometry": marker[0], "mesh": report}

        entry, record = self._publish(key, build)
        if (
            record.get("type") != "mesh"
            or record.get("geometry") != marker[0]
            or set(record["files"]) != MESH_FILES
        ):
            raise InputCacheError(f"not a checked mesh input: {entry}")
        points = np.load(entry / "vertices.npy", mmap_mode="r", allow_pickle=False)
        faces = np.load(entry / "faces.npy", mmap_mode="r", allow_pickle=False)
        if (
            points.dtype != np.dtype("<f8")
            or faces.dtype != np.dtype("<i8")
            or points.ndim != 2
            or faces.ndim != 2
            or points.shape[1] != 3
            or faces.shape[1] != 3
            or not len(points)
            or not len(faces)
            or not np.isfinite(points).all()
            or faces.min() < 0
            or faces.max() >= len(points)
            or any(_edge_defects(faces, len(points)))
        ):
            raise InputCacheError(f"invalid input cache mesh: {entry}")
        triangles = points[faces]
        volume = float(
            np.einsum(
                "ij,ij->i",
                triangles[:, 0],
                np.cross(triangles[:, 1], triangles[:, 2]),
            ).sum()
            / 6
        )
        if not isfinite(volume) or volume <= 0:
            raise InputCacheError(f"nonpositive input cache mesh volume: {entry}")
        return points, faces, record["mesh"]

    def artifacts(
        self,
        kind: str,
        parameters: object,
        sources: Sequence[Path],
        create: Callable[[Path], None],
    ) -> Path:
        if not self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = TemporaryDirectory(prefix="fresh-", dir=self.root)
            self._temporary.append(temporary)
            destination = Path(temporary.name) / "files"
            destination.mkdir()
            create(destination)
            return destination

        def build(staging: Path) -> dict:
            destination = staging / "files"
            destination.mkdir()
            create(destination)
            if self.key(f"artifacts:{kind}", parameters, sources) != key:
                raise InputCacheError("artifact inputs changed during export")
            return {"type": "artifacts"}

        key = self.key(f"artifacts:{kind}", parameters, sources)
        entry, record = self._publish(key, build)
        if record.get("type") != "artifacts":
            raise InputCacheError(f"not an artifact input: {entry}")
        return entry / "files"
