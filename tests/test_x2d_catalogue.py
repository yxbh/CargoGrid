"""Smaller-bed planning records omissions without changing source geometry or H2D routes."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from build123d import Box, Pos

from cargo_grid import catalogue, cli
from cargo_grid.catalogue import dual_safe_catalogue_job
from cargo_grid.export import BambuSettings, Material, _PreparedProject
from cargo_grid.jobs import Design, Job, tile_identity
from cargo_grid.packing import PrintPlacement
from cargo_grid.plates import PlateGroup, plan_plates
from cargo_grid.printers import H2D, X2D
from cargo_grid.rods import Rod
from cargo_grid.roof_support import RoofSupportSettings


@pytest.fixture(scope="module")
def wide_perimeter_designs():
    return [
        catalogue.accessory_design(spec)
        for spec in catalogue.accessory_variants(H2D.build)
        if spec.family in {"edge-x", "edge-y", "corner-in", "corner-out"}
        and spec.edge_outward == 30
        and spec.nx <= 4
    ]


@pytest.mark.slow
@pytest.mark.parametrize("layer", [0.32, 0.24])
def test_real_wide_perimeter_family_uses_multi_plate_outline_packing(wide_perimeter_designs, layer):
    plan = plan_plates(
        [
            PlateGroup(
                "30mm edges and corners - complete holes",
                wide_perimeter_designs,
                4,
                projected=True,
                max_layout_attempts=catalogue.PERIMETER_PACKING_ATTEMPTS,
            )
        ],
        X2D.common_build(),
        size=lambda design: design.bambu_size,
        auto_roof_support=True,
        layer_height_mm=layer,
        tower_width_mm=18,
    )
    assert not plan.unfit and len(plan.designs) == 18
    record = plan.projected_packing["30mm edges and corners - complete holes"]
    assert plan.plate_count == 2 and record["rectangle_plates"] == 3
    assert record["maximum_layout_attempts"] == catalogue.PERIMETER_PACKING_ATTEMPTS
    assert set(plan.projected_clearances) <= set(range(plan.plate_count))
    job = Job(
        plan.designs,
        X2D.build,
        "catalogue",
        print_placements=plan.placements,
        projected_footprints=plan.projected_footprints,
        projected_footprint_clearances=plan.projected_clearances,
        plate_builds=plan.plate_builds,
    )
    assert _PreparedProject(job, None, None).placements == plan.placements


@pytest.fixture
def inventory_stand_ins(monkeypatch):
    def tile(spec):
        name, parameters = tile_identity(spec)
        return Design(
            name,
            Box(spec.nx * 60 + 6, spec.ny * 60 + 6, 13 + spec.interface.solid_bottom_mm),
            parameters,
        )

    monkeypatch.setattr(catalogue, "tile_design", tile)

    def accessory(spec):
        if isinstance(spec, Rod):
            length = spec.above_mat_height_mm + 12
            return Box(10, 10, length) + Pos(0, 0, -length / 2 + 13.6) * Box(18, 18, 3.2)
        return Box(20, 30, 10)

    monkeypatch.setattr(catalogue, "make_accessory", accessory)


@pytest.mark.parametrize("layer", [0.32, 0.24])
@pytest.mark.parametrize("supported", [False, True])
def test_smaller_printer_accounts_for_the_whole_bounded_inventory(
    inventory_stand_ins, layer, supported
):
    job = dual_safe_catalogue_job(X2D, auto_roof_support=supported, layer_height_mm=layer)
    tiles = {
        (d.parameters["nx"], d.parameters["ny"])
        for d in job.designs
        if "family" not in d.parameters
    }
    expected = {(x, y) for x in range(1, 5) for y in range(1, 5)} - {(4, 4)}
    assert tiles == expected
    missing_tiles = {
        (entry["parameters"]["nx"], entry["parameters"]["ny"])
        for entry in job.omitted
        if "family" not in entry["parameters"]
    }
    assert missing_tiles == {(x, y) for x in range(1, 6) for y in range(1, 6)} - expected
    all_names = [d.name for d in job.designs] + [entry["name"] for entry in job.omitted]
    assert len(all_names) == len(set(all_names)) == 130
    rod = next(
        d
        for d in job.designs
        if d.parameters.get("family") == "rod" and d.parameters["above_mat_height_mm"] == 240
    )
    assert rod.recommended_print_rotation_y == 90
    assert rod.recommended_print_rotation_z == 45
    assert all(entry["reason"] and len(entry["size_mm"]) == 3 for entry in job.omitted)
    assert set(job.placement_policy["omitted_tiles"]) == {
        entry["name"] for entry in job.omitted if "family" not in entry["parameters"]
    }
    assert job.build == X2D.build
    assert job.plate_settings == {}
    assert job.prime_tower_width_mm == (18 if supported else None)
    if supported:
        assert set(job.prime_tower_positions.values()) == {(35.5, 15), (223, 15)}
        for design, placement in zip(job.designs, job.print_placements, strict=True):
            if "family" not in design.parameters and 4 not in (
                design.parameters["nx"],
                design.parameters["ny"],
            ):
                assert placement.x >= 25.5
                assert job.plate_builds[placement.plate].margin == 6
        turned = next(
            d
            for d in job.designs
            if "family" not in d.parameters and (d.parameters["nx"], d.parameters["ny"]) == (3, 4)
        )
        assert turned.recommended_print_rotation_z == 180 and turned.apply_orientation_to_bambu
    assert all("raft_first_layer_expansion" not in d.bambu_object_settings for d in job.designs)


def _arguments(nozzle=0.8):
    profiles = X2D.profiles[nozzle]
    return [
        "catalogue",
        "--printer",
        "x2d",
        "--bambu",
        "--material",
        profiles.petg,
        "PETG",
        "#688197",
        "--material",
        profiles.pla,
        "PLA",
        "#0A2989",
        "--nozzle-diameter-mm",
        str(nozzle),
        "--layer-height-mm",
        str(profiles.layer_height_mm),
        "--roof-support",
        "--roof-support-mode",
        "auto",
    ]


@pytest.mark.parametrize(
    "options",
    [
        {"solid_bottom_mm": 3.0},
        {"layer_height_mm": 0.2},
        {"hole_diameter": None},
        {"hole_scope": "interior"},
    ],
)
def test_unmeasured_settings_keep_the_original_support_foot_inset(inventory_stand_ins, options):
    job = dual_safe_catalogue_job(X2D, auto_roof_support=True, **options)
    assert all(build.margin == 6 for build in job.plate_builds.values())
    assert not any(
        "family" not in d.parameters and 4 in (d.parameters["nx"], d.parameters["ny"])
        for d in job.designs
    )
    assert job.prime_tower_width_mm is None


def test_catalogue_policy_works_with_a_distinct_printer_value(inventory_stand_ins):
    printer = replace(X2D, name="Test")
    job = dual_safe_catalogue_job(printer, auto_roof_support=True)
    assert job.prime_tower_width_mm == 18
    assert any(d.recommended_print_rotation_z == 45 for d in job.designs)
    wide = [
        (design, placement)
        for design, placement in zip(job.designs, job.print_placements)
        if "family" not in design.parameters
        and sorted((design.parameters["nx"], design.parameters["ny"])) == [3, 4]
    ]
    assert len(wide) == 2
    assert all(p.x == 64.9 and p.y == 5 for _, p in wide)
    assert all(printer.shared_x_mm[1] - (p.x + 186) == pytest.approx(5.1) for _, p in wide)
    assert all("Test" in entry["reason"] for entry in job.omitted)
    strict = replace(printer, omit_unfit=False, diagonal_fallback=False)
    with pytest.raises(
        ValueError, match="Test dual-safe catalogue unexpectedly omitted an accessory"
    ):
        dual_safe_catalogue_job(strict)


@pytest.mark.parametrize("nozzle", [0.8, 0.4])
def test_cli_selects_x2d_profiles_and_preserves_requested_contact_settings(
    monkeypatch, tmp_path, nozzle
):
    captured = {}

    def build(printer, **kwargs):
        captured.update(printer=printer, plan=kwargs)
        return SimpleNamespace(
            designs=[], omitted=[], print_placements=[PrintPlacement(0, 0, 0, 0)]
        )

    def export(job, output, **kwargs):
        captured.update(kwargs)
        return output / "manifest.json"

    monkeypatch.setattr(cli, "dual_safe_catalogue_job", build)
    monkeypatch.setattr(cli, "export_job", export)
    assert (
        cli.main(
            [
                *_arguments(nozzle),
                "--roof-interface-layer-count",
                "3",
                "--packing-gap-mm",
                "5",
                "--output",
                str(tmp_path / "job"),
            ]
        )
        == 0
    )
    assert captured["printer"] is X2D
    assert captured["plan"]["packing_gap"] == 5
    bambu = captured["bambu"]
    profiles = X2D.profiles[nozzle]
    assert bambu.printer_settings_id == profiles.printer
    assert bambu.print_settings_id == profiles.process
    assert bambu.printer is X2D and bambu.machine_nozzle_count == 2
    settings = bambu.roof_support.native_settings(process_profile=profiles.process)
    assert settings["support_interface_top_layers"] == "3"
    assert settings["support_object_xy_distance"] == "0.4"
    assert "support_on_build_plate_only" not in settings
    assert "support_filament" not in settings


@pytest.mark.parametrize(
    "extra,message",
    [
        (["--build-width-mm", "255"], "cannot be combined with --build-width-mm"),
        (["--build-margin-mm", "1"], "cannot be combined with --build-margin-mm"),
        (["--unit-size-mm", "61"], "cannot be combined with --unit-size-mm"),
        (["--layer-height-mm", "0.2"], "--layer-height-mm 0.24"),
        (["--nozzle-diameter-mm", "0.6"], "--nozzle-diameter-mm 0.8"),
        (["--material", H2D.profiles[0.8].petg, "PETG", "#112233"], "two official"),
        (["--packing-gap-mm", "0"], "X2D packing gap"),
    ],
)
def test_x2d_cli_rejects_incompatible_hardware_requests(extra, message, tmp_path, capsys):
    output = tmp_path / "job"
    with pytest.raises(SystemExit) as error:
        cli.main([*_arguments(), *extra, "--output", str(output)])
    assert error.value.code == 2
    assert message in capsys.readouterr().err
    assert not output.exists()


def test_retired_h2d_selector_is_an_unknown_option(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main([*_arguments(), "--h2d-dual-safe", "--output", str(tmp_path / "job")])
    assert error.value.code == 2
    assert "unrecognized arguments: --h2d-dual-safe" in capsys.readouterr().err


@pytest.fixture(scope="module")
def x2d_plan():
    return dual_safe_catalogue_job(X2D, auto_roof_support=True)


@pytest.mark.slow
def test_real_x2d_catalogue_solids_and_prepared_placements_stay_in_reach(x2d_plan):
    job = x2d_plan
    assert len(job.designs) + len(job.omitted) == 130
    assert {
        tuple(d.parameters[k] for k in ("nx", "ny"))
        for d in job.designs
        if "family" not in d.parameters
    } == {(x, y) for x in range(1, 5) for y in range(1, 5)} - {(4, 4)}
    for design, placement in zip(job.designs, job.print_placements, strict=True):
        assert design.shape.is_valid and len(design.shape.solids()) == 1
        assert design.shape.volume > 0
        if "family" not in design.parameters:
            assert placement.plate not in job.projected_footprint_clearances
        w, d, h = design.bambu_size
        if placement.rotation % 180 == 90:
            w, d = d, w
        assert job.plate_builds[placement.plate].contains_box(placement.x, placement.y, w, d, h)
        assert 25.5 <= placement.x and placement.x + w <= 251 + 1e-6
        margin = job.plate_builds[placement.plate].margin
        assert margin in (5, 6)
        assert margin <= placement.y and placement.y + d <= 256 - margin + 1e-6
        assert h <= 256
    profile = X2D.profiles[0.8]
    bambu = BambuSettings(
        (Material(profile.petg, "PETG", "#688197"), Material(profile.pla, "PLA", "#0A2989")),
        0.8,
        0.32,
        roof_support=RoofSupportSettings(mode="auto"),
        printer_settings_id=profile.printer,
        print_settings_id=profile.process,
        machine_nozzle_count=2,
        printer=X2D,
    )
    prepared = _PreparedProject(job, bambu, None, True)
    assert 0 < prepared.plate_count <= 36
    assert set(job.prime_tower_positions) == set(job.plate_builds)
    rod = next(
        d
        for d in job.designs
        if d.parameters.get("family") == "rod" and d.parameters["above_mat_height_mm"] == 240
    )
    assert rod.size == pytest.approx((18, 18, 252))
    assert rod.recommended_print_rotation_y == 90
    assert rod.recommended_print_rotation_z == 45
    assert max(rod.bambu_size[:2]) < 191
