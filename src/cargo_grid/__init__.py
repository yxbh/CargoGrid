"""Independent parametric cargo-mat geometry; no reference assets are bundled."""

from pathlib import Path

from cargo_grid._version import __version__
from cargo_grid.adjustable_stop import AdjustableStopSpec
from cargo_grid.parameters import BuildVolume, Interface, JointStyle, Tile
from cargo_grid.rods import Rod, RodBrace, make_rod, make_rod_brace
from cargo_grid.tiles import make_tile


def export_adjustable_stop(
    output: Path,
    spec: AdjustableStopSpec = AdjustableStopSpec(),
) -> Path:
    """Export the complete adjustable stop through its native CAD pipeline."""

    from cargo_grid.adjustable_stop_export import export_adjustable_stop as export

    return export(output, spec)


__all__ = [
    "BuildVolume",
    "Interface",
    "JointStyle",
    "Rod",
    "RodBrace",
    "Tile",
    "AdjustableStopSpec",
    "__version__",
    "export_adjustable_stop",
    "make_rod",
    "make_rod_brace",
    "make_tile",
]
