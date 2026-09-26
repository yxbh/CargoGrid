"""H2D-specific placement policy; generated projects remain unsliced and unverified physically."""

from collections import Counter, defaultdict
from dataclasses import asdict
from math import hypot
from types import SimpleNamespace

import pytest

from cargo_grid.accessories import Accessory, accessory_datums
from cargo_grid.catalogue import (
    H2D_DEFAULT_PART_CLEARANCE_MM,
    accessory_variants,
    h2d_dual_safe_catalogue_job,
    tile_sizes,
)
from cargo_grid.cli import main
from cargo_grid.export import BambuSettings, Material, _PreparedProject
from cargo_grid.footprints import minimum_projected_clearance
from cargo_grid.jobs import Design
from cargo_grid.packing import PrintPlacement
from cargo_grid.parameters import BuildVolume, Exclusion, Tile
from cargo_grid.rods import Rod, RodBrace
from cargo_grid.tiles import hole_placements


def required_accessories(max_cells, *, hole_diameter=10, hole_scope="full"):
    def perimeter(
        family,
        *,
        nx=1,
        variant=1,
        outward,
    ):
        span = nx if family in {"edge-x", "edge-y"} else 1
        boundary_site = (
            hole_diameter is not None
            and hole_scope == "full"
            and any(
                site.accepted and abs(site.y) < 1e-8
                for site in hole_placements(
                    Tile(
                        span,
                        1,
                        hole_diameter=hole_diameter,
                        hole_scope=hole_scope,
                    )
                )
            )
        )
        complete = boundary_site
        return Accessory(
            family,
            nx=nx,
            variant=variant,
            edge_outward=outward,
            complete_edge_holes=complete,
            edge_hole_diameter=hole_diameter if complete else None,
        )

    return {
        "edge-x": [
            perimeter(
                "edge-x",
                nx=n,
                outward=outward,
            )
            for n in range(1, max_cells + 1)
            for outward in (10, 20, 30)
        ],
        "edge-y": [
            perimeter(
                "edge-y",
                nx=n,
                outward=outward,
            )
            for n in range(1, max_cells + 1)
            for outward in (10, 20, 30)
        ],
        "corner-in": [
            perimeter(
                "corner-in",
                variant=variant,
                outward=outward,
            )
            for variant in (1, 2, 3, 4)
            for outward in (10, 20, 30)
        ],
        "corner-out": [
            perimeter(
                "corner-out",
                variant=variant,
                outward=outward,
            )
            for variant in (1, 2, 3, 4, 5, 6)
            for outward in (10, 20, 30)
        ],
        "support": [Accessory("support", nx=n) for n in range(1, max_cells + 1)],
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
        "rod": [
            Rod(above_mat_height_mm=120, peg_diameter_mm=10, tile_thickness_mm=13),
            Rod(above_mat_height_mm=240, peg_diameter_mm=10, tile_thickness_mm=13),
        ],
        "rod-brace": [
            RodBrace(center_spacing_mm=60, bore_diameter_mm=10),
            RodBrace(center_spacing_mm=120, bore_diameter_mm=10),
        ],
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


@pytest.mark.parametrize(
    "hole_diameter,hole_scope,expected_traits",
    [
        (10, "full", {(10, True, 10), (20, True, 10), (30, True, 10)}),
        (None, "full", {(10, False, None), (20, False, None), (30, False, None)}),
        (10, "interior", {(10, False, None), (20, False, None), (30, False, None)}),
        (8, "full", {(10, True, 8), (20, True, 8), (30, True, 8)}),
        (25, "full", {(10, False, None), (20, False, None), (30, False, None)}),
    ],
)
def test_perimeter_variants_match_effective_tile_hole_pattern(
    hole_diameter,
    hole_scope,
    expected_traits,
):
    specs = accessory_variants(
        BuildVolume(350, 320, 325),
        hole_diameter=hole_diameter,
        hole_scope=hole_scope,
    )
    perimeter = [
        spec for spec in specs if spec.family in {"edge-x", "edge-y", "corner-in", "corner-out"}
    ]
    assert len(perimeter) == 60
    assert {
        (spec.edge_outward, spec.complete_edge_holes, spec.edge_hole_diameter) for spec in perimeter
    } == expected_traits
    expected = required_accessories(
        5,
        hole_diameter=hole_diameter,
        hole_scope=hole_scope,
    )
    assert Counter(perimeter) == Counter(
        spec
        for family in ("edge-x", "edge-y", "corner-in", "corner-out")
        for spec in expected[family]
    )


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
        if spec.edge_outward == 10:
            del parameters["edge_outward"]
        if not spec.complete_edge_holes:
            del parameters["complete_edge_holes"]
        if parameters["edge_hole_diameter"] in (None, 10):
            del parameters["edge_hole_diameter"]
    return canonical_parameters(parameters)


PLATE_GROUPS = {
    "tile": "Tiles",
    "vertical-stop": "Normal stops",
    "vertical-tile-bracket": "Tile brackets - deep and shallow",
    "lock-45": "Angled stops",
    "plate": "Attachment plates",
    "support": "Rails and connectors",
    "support-bit": "Rails and connectors",
    "support-end": "Rails and connectors",
    "rod": "Rods and upper braces",
    "rod-brace": "Rods and upper braces",
}


PERIMETER_FAMILIES = {"edge-x", "edge-y", "corner-in", "corner-out"}
EXCEPTION_PLATE_NAME = "5x5 TILE - SINGLE NOZZLE ONLY - LEFT"
EXPECTED_GROUP_ORDER = [
    "Tiles",
    "Female ramps",
    "Male ramps",
    "Normal stops",
    "Tile brackets - deep and shallow",
    "Angled stops",
    "Attachment plates",
    "10mm edges and corners - complete holes",
    "20mm edges and corners - complete holes",
    "30mm edges and corners - complete holes",
    "Rails and connectors",
    "Rods and upper braces",
]
NESTED_GROUP = "20mm edges and corners - complete holes"


@pytest.fixture(scope="module")
def h2d_plan():
    """Build the unpatched whole-catalogue plan once per worker; the tests only read it."""
    original_size = Design.bambu_size
    measured = Counter()

    def counted_size(design):
        measured[id(design)] += 1
        return original_size.fget(design)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Design, "bambu_size", property(counted_size))
        job = h2d_dual_safe_catalogue_job(hole_diameter=10, hole_scope="full")
    return SimpleNamespace(job=job, measured=measured, layout=_plan_layout(job))


def _design_group(design):
    """Return a planned design's expected plate group and perimeter connector kind."""
    family = design.parameters.get("family", "tile")
    if family == "ramp":
        return f"{design.parameters.get('ramp_join', 'female').title()} ramps", None
    if family not in PERIMETER_FAMILIES:
        return PLATE_GROUPS[family], None
    outward = design.parameters.get("edge_outward", 10.0)
    complete = design.parameters.get("complete_edge_holes", False)
    spec = Accessory(
        family,
        nx=design.parameters["nx"],
        variant=design.parameters["variant"],
        edge_outward=outward,
        complete_edge_holes=complete,
        edge_hole_diameter=design.parameters.get("edge_hole_diameter"),
    )
    sexes = sorted(join["sex"] for join in accessory_datums(spec)["joins"])
    if family in {"edge-x", "edge-y"}:
        assert len(set(sexes)) == 1
        connector = sexes[0]
    else:
        connector = (
            sexes[0]
            if len(sexes) == 1
            else f"all-{sexes[0]}"
            if sexes[0] == sexes[1]
            else "male-female"
        )
    mode = "complete holes" if complete else "plain"
    return f"{outward:g}mm edges and corners - {mode}", connector


def _plan_layout(job):
    exception_plate = job.print_placements[-1].plate
    layout = SimpleNamespace(
        exception_plate=exception_plate,
        plate_count=max(placement.plate for placement in job.print_placements) + 1,
        groups=[],
        plate_groups=defaultdict(set),
        group_plates=defaultdict(set),
        ramp_joins=set(),
        ramp_members=defaultdict(set),
        perimeter_sets={},
    )
    for design, placement in zip(job.designs, job.print_placements):
        group, connector = _design_group(design)
        if design.parameters.get("family") == "ramp":
            join = design.parameters.get("ramp_join", "female")
            layout.ramp_joins.add((design.parameters["nx"], join))
            layout.ramp_members[join].add((design.parameters["nx"], design.quantity))
        elif connector is not None:
            perimeter_set = layout.perimeter_sets.setdefault(
                (
                    design.parameters.get("edge_outward", 10.0),
                    design.parameters.get("complete_edge_holes", False),
                ),
                {"plates": set(), "names": [], "connectors": set()},
            )
            perimeter_set["plates"].add(placement.plate)
            perimeter_set["names"].append(design.name)
            perimeter_set["connectors"].add(connector)
        if placement.plate == exception_plate:
            layout.groups.append(EXCEPTION_PLATE_NAME)
        else:
            layout.groups.append(group)
            layout.plate_groups[placement.plate].add(group)
            layout.group_plates[group].add(placement.plate)
    return layout


def test_h2d_dual_safe_plan_keeps_full_family_inventory_and_hardware_zones(h2d_plan):
    job = h2d_plan.job
    assert h2d_plan.measured == Counter(id(design) for design in job.designs)
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
    exception = job.designs[-1]
    assert "family" not in exception.parameters
    assert (exception.parameters["nx"], exception.parameters["ny"]) == (5, 5)
    assert exception.parameters["hole_diameter"] == 10
    assert exception.parameters["hole_scope"] == "full"


def test_h2d_dual_safe_plan_keeps_common_reach_and_a_left_nozzle_exception_plate(h2d_plan):
    job = h2d_plan.job
    plate_count = h2d_plan.layout.plate_count
    exception_plate = h2d_plan.layout.exception_plate
    assert (
        set(job.plate_names) == {p.plate for p in job.print_placements} == set(range(plate_count))
    )
    assert job.part_gap == H2D_DEFAULT_PART_CLEARANCE_MM
    assert job.omitted == []
    assert job.placement_policy["common_reach_mm"] == {
        "min_x": 25,
        "max_x": 325,
        "min_y": 0,
        "max_y": 320,
        "max_z": 320,
    }
    assert job.plate_names[exception_plate] == EXCEPTION_PLATE_NAME
    assert exception_plate == plate_count - 1
    assert set(job.plate_builds) == set(range(plate_count))
    assert job.plate_builds[exception_plate] == BuildVolume(325, 320, 320, margin=5)
    assert all(
        job.plate_builds[plate]
        == BuildVolume(
            350,
            320,
            320,
            margin=5,
            exclusions=(
                Exclusion(0, 0, 30, 320),
                Exclusion(320, 0, 30, 320),
            ),
        )
        for plate in range(exception_plate)
    )
    assert job.plate_settings == {
        exception_plate: {
            "filament_map_mode": "Manual",
            "filament_maps": "1",
            "filament_volume_maps": "0",
        }
    }
    assert job.placement_policy["minimum_model_gap_mm"] == 4
    assert job.placement_policy["minimum_actual_part_xy_clearance_mm"] == 4


def test_h2d_dual_safe_plan_groups_families_on_named_plates(h2d_plan):
    job = h2d_plan.job
    layout = h2d_plan.layout
    group_plates = layout.group_plates
    assert layout.ramp_joins == {
        (width, join) for width in range(1, 6) for join in ("female", "male")
    }
    assert layout.ramp_members == {
        join: {(width, 1) for width in range(1, 6)} for join in ("female", "male")
    }
    expected_groups = {
        *PLATE_GROUPS.values(),
        "Female ramps",
        "Male ramps",
        "10mm edges and corners - complete holes",
        "20mm edges and corners - complete holes",
        "30mm edges and corners - complete holes",
    }
    assert set(group_plates) == expected_groups
    assert all(len(groups) == 1 for groups in layout.plate_groups.values())
    for group, plates in group_plates.items():
        assert {job.plate_names[index] for index in plates} == (
            {group} if len(plates) == 1 else {f"{group} {n}" for n in range(1, len(plates) + 1)}
        )
    for join in ("female", "male"):
        assert len(group_plates[f"{join.title()} ramps"]) == 1
    assert len(group_plates["Tile brackets - deep and shallow"]) == 1
    assert len(group_plates["Rails and connectors"]) == 1
    assert len(group_plates["10mm edges and corners - complete holes"]) == 1
    assert len(group_plates["20mm edges and corners - complete holes"]) == 1
    assert len(group_plates["30mm edges and corners - complete holes"]) <= 2
    perimeter_sets = layout.perimeter_sets
    assert set(perimeter_sets) == {(10, True), (20, True), (30, True)}
    assert all(len(group["names"]) == 20 for group in perimeter_sets.values())
    assert all(
        group["connectors"] == {"female", "male", "all-female", "male-female", "all-male"}
        for group in perimeter_sets.values()
    )
    actual_group_order = []
    for plate in range(layout.exception_plate):
        group = next(iter(layout.plate_groups[plate]))
        if not actual_group_order or actual_group_order[-1] != group:
            actual_group_order.append(group)
    assert actual_group_order == EXPECTED_GROUP_ORDER
    assert job.placement_policy["perimeter_grouping"] == "outward width and boundary-hole mode"


@pytest.mark.parametrize("group", [*EXPECTED_GROUP_ORDER, EXCEPTION_PLATE_NAME])
def test_h2d_dual_safe_posed_solids_stay_in_reach_with_rectangle_gaps(h2d_plan, group):
    job = h2d_plan.job
    layout = h2d_plan.layout
    exception_plate = layout.exception_plate
    plates = {
        placement.plate
        for placement, member_group in zip(job.print_placements, layout.groups)
        if member_group == group
    }
    assert plates
    nested_plate = next(iter(layout.group_plates[NESTED_GROUP]))
    by_plate = defaultdict(list)
    for design, placement in zip(job.designs, job.print_placements):
        if placement.plate not in plates:
            continue
        assert design.shape.is_valid and len(design.shape.solids()) == 1
        assert design.shape.volume > 0
        # Measure the real posed solid independently of the size used by packing.
        width, depth, height = design.bambu_shape.bounding_box().size
        if placement.rotation == 90:
            width, depth = depth, width
        bounds = (placement.x, placement.x + width, placement.y, placement.y + depth, height)
        assert bounds[4] <= 320 + 1e-6
        by_plate[placement.plate].append(bounds)
        if placement.plate == exception_plate:
            assert bounds[0] >= 5 - 1e-6 and bounds[1] <= 320 + 1e-6
            assert bounds[2] >= 5 - 1e-6 and bounds[3] <= 315 + 1e-6
        else:
            assert bounds[0] >= 30 - 1e-6 and bounds[1] <= 320 + 1e-6
            assert bounds[2] >= 5 - 1e-6 and bounds[3] <= 315 + 1e-6
    if exception_plate in plates:
        assert len(by_plate[exception_plate]) == 1
    for plate, rectangles in by_plate.items():
        if plate == nested_plate:
            continue
        for index, first in enumerate(rectangles):
            for second in rectangles[index + 1 :]:
                dx = max(0, first[0] - second[1], second[0] - first[1])
                dy = max(0, first[2] - second[3], second[2] - first[3])
                assert hypot(dx, dy) >= 4 - 1e-5


def test_h2d_dual_safe_plan_nests_one_perimeter_group_by_projected_footprint(h2d_plan):
    job = h2d_plan.job
    packing = job.placement_policy["projected_footprint_packing"]
    assert set(packing) == {
        "10mm edges and corners - complete holes",
        "20mm edges and corners - complete holes",
        "30mm edges and corners - complete holes",
    }
    assert packing["10mm edges and corners - complete holes"]["status"] == "rectangle retained"
    assert packing[NESTED_GROUP] == {
        "status": "applied",
        "minimum_projected_gap_mm": 4,
        "search_gap_mm": 3.25,
        "grid_mm": 1,
        "maximum_candidate_positions": 4_000_000,
        "maximum_order_attempts": 8,
    }
    assert packing["30mm edges and corners - complete holes"]["status"] == "rectangle fallback"
    nested_plate = next(iter(h2d_plan.layout.group_plates[NESTED_GROUP]))
    assert job.projected_footprint_clearances == {nested_plate: 4}
    assert job.placement_policy["projected_footprint_gap_overrides_mm"] == {NESTED_GROUP: 4}
    nested_footprints = []
    nested_placements = []
    for footprint, placement in zip(job.projected_footprints, job.print_placements):
        if placement.plate != nested_plate:
            assert footprint is None
            continue
        assert footprint is not None
        nested_footprints.append(footprint)
        nested_placements.append(PrintPlacement(0, placement.x, placement.y, placement.rotation))
    assert len(nested_footprints) == 20
    assert {placement.rotation for placement in nested_placements} == {0, 90}
    assert minimum_projected_clearance(
        nested_footprints,
        nested_placements,
        plate=0,
    ) == pytest.approx(4, abs=1e-6)


def test_h2d_dual_safe_prepared_project_keeps_the_planned_plates(h2d_plan):
    job = h2d_plan.job
    prepared = _PreparedProject(
        job,
        BambuSettings((Material("PETG", "PETG", "#637b70"),), 0.8, 0.32),
        None,
        catalogue=True,
    )
    assert prepared.placements == job.print_placements
    assert prepared.plate_count == h2d_plan.layout.plate_count


def test_h2d_dual_safe_plan_requests_support_only_for_supported_families(h2d_plan):
    job = h2d_plan.job
    assert all(
        design.bambu_object_settings["support_type"] == "normal(auto)"
        for design in job.designs
        if (
            design.parameters.get("family") == "ramp"
            and design.parameters.get("ramp_join", "female") == "female"
        )
        or design.parameters.get("family") in {"vertical-stop", "rod"}
        or (
            design.parameters.get("family") == "vertical-tile-bracket"
            and design.parameters.get("panel_height_cells") is not None
        )
    )
    assert all(
        not design.bambu_object_settings
        for design in job.designs
        if design.parameters.get("family") == "rod-brace"
        or (
            design.parameters.get("family") == "ramp"
            and design.parameters.get("ramp_join") == "male"
        )
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


def test_h2d_dual_safe_cli_forwards_an_explicit_packing_gap(monkeypatch, tmp_path):
    supplied = {}

    def fake_job(**kwargs):
        supplied.update(kwargs)
        return SimpleNamespace(
            designs=[],
            omitted=[],
            print_placements=[PrintPlacement(0, 0, 0, 0)],
        )

    monkeypatch.setattr("cargo_grid.cli.h2d_dual_safe_catalogue_job", fake_job)
    monkeypatch.setattr(
        "cargo_grid.cli.export_job",
        lambda *args, **kwargs: tmp_path / "catalogue" / "manifest.json",
    )
    assert (
        main(
            [
                "catalogue",
                "--h2d-dual-safe",
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
                "350",
                "--build-depth-mm",
                "320",
                "--build-height-mm",
                "325",
                "--packing-gap-mm",
                "5",
                "--output",
                str(tmp_path / "catalogue"),
            ]
        )
        == 0
    )
    assert supplied["packing_gap"] == 5


def test_h2d_dual_safe_cli_uses_shared_implicit_packing_gap(monkeypatch, tmp_path):
    supplied = {}

    def fake_job(**kwargs):
        supplied.update(kwargs)
        return SimpleNamespace(
            designs=[],
            omitted=[],
            print_placements=[PrintPlacement(0, 0, 0, 0)],
        )

    monkeypatch.setattr("cargo_grid.cli.h2d_dual_safe_catalogue_job", fake_job)
    monkeypatch.setattr(
        "cargo_grid.cli.export_job",
        lambda *args, **kwargs: tmp_path / "catalogue" / "manifest.json",
    )
    assert (
        main(
            [
                "catalogue",
                "--h2d-dual-safe",
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
                "350",
                "--build-depth-mm",
                "320",
                "--build-height-mm",
                "325",
                "--output",
                str(tmp_path / "catalogue"),
            ]
        )
        == 0
    )
    assert supplied["packing_gap"] == H2D_DEFAULT_PART_CLEARANCE_MM
