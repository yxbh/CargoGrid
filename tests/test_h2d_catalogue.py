"""H2D-specific placement policy; generated projects remain unsliced and unverified physically."""

from collections import Counter, defaultdict
from dataclasses import asdict
from math import hypot

import pytest

from cargo_grid.accessories import Accessory
from cargo_grid.catalogue import accessory_variants, h2d_dual_safe_catalogue_job, tile_sizes
from cargo_grid.cli import main
from cargo_grid.jobs import Design
from cargo_grid.parameters import BuildVolume, Tile


def required_accessories(max_cells):
    return {
        **{
            family: [Accessory(family, nx=n) for n in range(1, max_cells + 1)]
            for family in ("edge-x", "edge-y", "support")
        },
        "corner-in": [Accessory("corner-in", variant=v) for v in (1, 2, 3, 4)],
        "corner-out": [Accessory("corner-out", variant=v) for v in (1, 2, 3, 4, 5, 6)],
        "support-end": [Accessory("support-end", variant=v) for v in (1, 2, 3, 4)],
        "support-bit": [Accessory("support-bit", length=n) for n in (20, 30, 40, 50)],
        "vertical-tile-bracket": [
            Accessory("vertical-tile-bracket", nx=1, ny=2),
            Accessory("vertical-tile-bracket", nx=2, ny=1),
            Accessory("vertical-tile-bracket", nx=2, ny=2),
            Accessory("vertical-tile-bracket", nx=1, ny=1, panel_height_cells=2),
            Accessory("vertical-tile-bracket", nx=2, ny=1, panel_height_cells=2),
        ],
        "ramp": [
            Accessory("ramp", nx=n, ramp_join=join)
            for n in range(1, max_cells + 1)
            for join in ("female", "male")
        ],
        "vertical-stop": [
            Accessory("vertical-stop", nx=x, ny=y, height=h)
            for x, y in ((1, 1), (1, 2), (2, 1), (2, 2))
            for h in (60, 120)
        ],
        "lock-45": [Accessory("lock-45", nx=n, ny=n) for n in (1, 2)],
        "plate": [Accessory("plate", nx=x, ny=y) for x, y in ((1, 1), (1, 2), (2, 2))],
    }


@pytest.mark.parametrize(
    "build,max_cells",
    [(BuildVolume(246, 246, 120), 4), (BuildVolume(350, 320, 325), 5)],
    ids=["bounded", "h2d"],
)
def test_accessory_families_finite_and_complete(build, max_cells):
    expected = required_accessories(max_cells)
    actual = defaultdict(list)
    for spec in accessory_variants(build):
        actual[spec.family].append(spec)
    assert actual.keys() == expected.keys(), "add an independent contract for each family"
    for family, specs in expected.items():
        assert len(set(specs)) == len(specs)
        assert Counter(actual[family]) == Counter(specs), family


def canonical_parameters(parameters):
    return tuple(
        sorted(
            (key, canonical_parameters(value) if isinstance(value, dict) else value)
            for key, value in parameters.items()
        )
    )


def accessory_parameters(spec):
    parameters = {"family": spec.family, **asdict(spec)}
    if isinstance(spec, Accessory):
        if spec.panel_height_cells is None:
            del parameters["panel_height_cells"]
        if spec.ramp_join == "female":
            del parameters["ramp_join"]
    return canonical_parameters(parameters)


PLATE_GROUPS = {
    "tile": "Tiles",
    "vertical-stop": "Normal stops",
    "vertical-tile-bracket": "Tile brackets - deep and shallow",
    "lock-45": "Angled stops",
    "plate": "Attachment plates",
    "edge-x": "Edges and corners",
    "edge-y": "Edges and corners",
    "corner-in": "Edges and corners",
    "corner-out": "Edges and corners",
    "support": "Rails and connectors",
    "support-bit": "Rails and connectors",
    "support-end": "Rails and connectors",
}


def test_h2d_dual_safe_plan_keeps_full_family_inventory_and_hardware_zones(monkeypatch):
    original_size = Design.bambu_size
    measured = Counter()

    def counted_size(design):
        measured[id(design)] += 1
        return original_size.fget(design)

    monkeypatch.setattr(Design, "bambu_size", property(counted_size))
    job = h2d_dual_safe_catalogue_job(hole_diameter=10, hole_scope="full")
    assert measured == Counter(id(design) for design in job.designs)
    required_tiles = {(x, y) for x in range(1, 6) for y in range(1, 6)}
    assert Counter(tile_sizes(BuildVolume(350, 320, 325))) == Counter(required_tiles)
    expected = Counter(
        canonical_parameters(asdict(Tile(x, y, hole_diameter=10, hole_scope="full")))
        for x, y in required_tiles
    )
    expected.update(
        accessory_parameters(spec) for specs in required_accessories(5).values() for spec in specs
    )
    actual = Counter(canonical_parameters(design.parameters) for design in job.designs)
    assert actual == expected, {"missing": expected - actual, "unexpected": actual - expected}
    assert set(actual.values()) == {1}
    assert len({design.name for design in job.designs}) == len(job.designs)
    assert len(job.designs) == len(job.print_placements)
    assert all(design.quantity == 1 for design in job.designs)
    plate_count = max(placement.plate for placement in job.print_placements) + 1
    assert (
        set(job.plate_names) == {p.plate for p in job.print_placements} == set(range(plate_count))
    )
    assert job.part_gap == 10
    assert job.omitted == []
    assert job.placement_policy["common_reach_mm"] == {
        "min_x": 25,
        "max_x": 325,
        "min_y": 0,
        "max_y": 320,
        "max_z": 320,
    }
    exception_plate = job.print_placements[-1].plate
    assert job.plate_names[exception_plate] == "5x5 TILE - SINGLE NOZZLE ONLY - LEFT"
    assert exception_plate == plate_count - 1
    assert job.plate_settings == {
        exception_plate: {
            "filament_map_mode": "Manual",
            "filament_maps": "1",
            "filament_volume_maps": "0",
        }
    }
    exception = job.designs[-1]
    assert "family" not in exception.parameters
    assert (exception.parameters["nx"], exception.parameters["ny"]) == (5, 5)
    assert exception.parameters["hole_diameter"] == 10
    assert exception.parameters["hole_scope"] == "full"
    by_plate = {}
    ramp_joins = set()
    plate_groups = defaultdict(set)
    group_plates = defaultdict(set)
    ramp_members = defaultdict(set)
    for design, placement in zip(job.designs, job.print_placements):
        family = design.parameters.get("family", "tile")
        if family == "ramp":
            join = design.parameters.get("ramp_join", "female")
            group = f"{join.title()} ramps"
            ramp_joins.add((design.parameters["nx"], join))
            ramp_members[join].add((design.parameters["nx"], design.quantity))
        else:
            group = PLATE_GROUPS[family]
        if placement.plate != exception_plate:
            plate_groups[placement.plate].add(group)
            group_plates[group].add(placement.plate)
        assert design.shape.is_valid and len(design.shape.solids()) == 1
        assert design.shape.volume > 0
        # Measure the real posed solid independently of the size used by packing.
        width, depth, height = design.bambu_shape.bounding_box().size
        if placement.rotation == 90:
            width, depth = depth, width
        bounds = (placement.x, placement.x + width, placement.y, placement.y + depth, height)
        assert bounds[4] <= 320 + 1e-6
        by_plate.setdefault(placement.plate, []).append(bounds)
        if placement.plate == exception_plate:
            assert bounds[0] >= 5 - 1e-6 and bounds[1] <= 320 + 1e-6
            assert bounds[2] >= 5 - 1e-6 and bounds[3] <= 315 + 1e-6
        else:
            assert bounds[0] >= 30 - 1e-6 and bounds[1] <= 320 + 1e-6
            assert bounds[2] >= 5 - 1e-6 and bounds[3] <= 315 + 1e-6
    assert ramp_joins == {(width, join) for width in range(1, 6) for join in ("female", "male")}
    assert ramp_members == {
        join: {(width, 1) for width in range(1, 6)} for join in ("female", "male")
    }
    assert set(group_plates) == {*PLATE_GROUPS.values(), "Female ramps", "Male ramps"}
    assert all(len(groups) == 1 for groups in plate_groups.values())
    for group, plates in group_plates.items():
        assert {job.plate_names[index] for index in plates} == (
            {group} if len(plates) == 1 else {f"{group} {n}" for n in range(1, len(plates) + 1)}
        )
    for join in ("female", "male"):
        assert len(group_plates[f"{join.title()} ramps"]) == 1
    assert len(by_plate[exception_plate]) == 1
    for rectangles in by_plate.values():
        for index, first in enumerate(rectangles):
            for second in rectangles[index + 1 :]:
                dx = max(0, first[0] - second[1], second[0] - first[1])
                dy = max(0, first[2] - second[3], second[2] - first[3])
                assert hypot(dx, dy) >= 10 - 1e-5
    assert all(
        design.bambu_object_settings["support_type"] == "normal(auto)"
        for design in job.designs
        if (
            design.parameters.get("family") == "ramp"
            and design.parameters.get("ramp_join", "female") == "female"
        )
        or design.parameters.get("family") == "vertical-stop"
        or (
            design.parameters.get("family") == "vertical-tile-bracket"
            and design.parameters.get("panel_height_cells") is not None
        )
    )
    assert all(
        not design.bambu_object_settings
        for design in job.designs
        if design.parameters.get("family") == "ramp"
        and design.parameters.get("ramp_join") == "male"
    )


@pytest.mark.parametrize(
    "extra,message",
    [
        ([], "requires --bambu"),
        (
            [
                "--bambu",
                "--material",
                "Bambu PETG Basic @BBL H2D 0.8 nozzle",
                "PETG",
                "#637b70",
                "--nozzle-diameter-mm",
                ".8",
                "--layer-height-mm",
                ".32",
                "--build-width-mm",
                "349",
            ],
            "unmodified H2D build envelope",
        ),
    ],
)
def test_h2d_dual_safe_cli_rejects_incomplete_hardware_requests(extra, message, tmp_path, capsys):
    command = [
        "catalogue",
        "--h2d-dual-safe",
        "--build-width-mm",
        "350",
        "--build-depth-mm",
        "320",
        "--build-height-mm",
        "325",
        "--output",
        str(tmp_path / "catalogue"),
    ]
    command.extend(extra)
    with pytest.raises(SystemExit) as error:
        main(command)
    assert error.value.code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "catalogue").exists()
