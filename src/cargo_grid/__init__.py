"""Independent parametric cargo-mat geometry; no reference assets are bundled."""

from pathlib import Path

from cargo_grid._version import __version__
from cargo_grid.parameters import BuildVolume, Interface, JointStyle, Tile
from cargo_grid.rods import Rod, RodBrace, make_rod, make_rod_brace
from cargo_grid.tiles import make_tile
from cargo_grid.trunk_blocker import TrunkBlockerSpec


def export_trunk_blocker(
    output: Path,
    spec: TrunkBlockerSpec = TrunkBlockerSpec(),
) -> Path:
    """Export the complete experimental blocker through its native CAD pipeline."""

    from cargo_grid.trunk_blocker_export import export_trunk_blocker as export

    return export(output, spec)


__all__ = [
    "BuildVolume",
    "Interface",
    "JointStyle",
    "Rod",
    "RodBrace",
    "Tile",
    "TrunkBlockerSpec",
    "__version__",
    "export_trunk_blocker",
    "make_rod",
    "make_rod_brace",
    "make_tile",
]
