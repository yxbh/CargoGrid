"""Real-family byte checks, separate from the default synthetic cache contracts."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from geometry_inputs import FAMILIES
from input_cache import ROOT, InputStore

from cargo_grid import BuildVolume, Rod, Tile, make_rod, make_tile
from cargo_grid.catalogue import accessory_design
from cargo_grid.export import BambuSettings, Material, write_3mf
from cargo_grid.jobs import Design, Job
from cargo_grid.vehicles.zeekr_7x_contours import ContourParameters, _side_design


@pytest.mark.parametrize("family", ["tile", "contour", "contour-sequence", "rod"])
def test_project_bytes_are_identical_fresh_cold_and_warm(tmp_path, family):
    store = InputStore(tmp_path / "cache")
    if family == "tile":
        spec = Tile(2, 1)

        def create():
            return make_tile(spec)
    elif family.startswith("contour"):
        spec = ContourParameters()

        def create():
            return _side_design(
                "west" if family == "contour-sequence" else "east", "north", spec
            ).shape

    else:
        spec = Rod(120)

        def create():
            return make_rod(spec)

    sources = [
        *(
            ROOT / "src/cargo_grid" / f"{name}.py"
            for name in FAMILIES["contour" if family.startswith("contour") else family]
        ),
        Path(__file__),
    ]
    design = accessory_design(spec) if family == "rod" else Design("source", create(), {})
    fresh = design.shape
    shapes = [
        fresh,
        store.geometry(family, spec, sources, create),
        store.geometry(family, spec, sources, create),
    ]
    if family == "contour-sequence":
        for side, segment in (("west", "south"), ("east", "north"), ("east", "south")):
            _side_design(side, segment, spec)
    for kind, bambu in (
        ("core", None),
        ("bambu", BambuSettings((Material("PETG", "PETG", "#637B70"),), 0.8, 0.32)),
    ):
        outputs = []
        for index, shape in enumerate(shapes):
            path = tmp_path / f"{index}-{kind}.3mf"
            job = Job([replace(design, shape=shape)], BuildVolume(350, 320, 325), "part")
            if index == 0:
                write_3mf(job, path, bambu=bambu)
            else:
                with patch("cargo_grid.prepared.checked_mesh", side_effect=store.checked_mesh):
                    write_3mf(job, path, bambu=bambu)
            outputs.append(path.read_bytes())
        assert outputs[0] == outputs[1] == outputs[2], kind
