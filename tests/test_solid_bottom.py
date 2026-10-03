"""Optional solid bottom: closed floors under sockets and holes; print and fit remain unverified."""

import json
from dataclasses import replace
from math import inf, nan

import pytest
from build123d import Axis, Location, Plane, Vector, section

from cargo_grid import Interface, Tile, make_tile
from cargo_grid.accessories import RAMP_RUN_MM, Accessory, accessory_datums, make_accessory
from cargo_grid.catalogue import accessory_identity, accessory_variants
from cargo_grid.cli import main
from cargo_grid.jobs import Job, tile_design, tile_identity
from cargo_grid.layout import exact_layout
from cargo_grid.parameters import BuildVolume
from cargo_grid.roof_support import roof_enforcers
from cargo_grid.tiles import hole_placements, socket_centers
from cargo_grid.vehicles import zeekr_7x, zeekr_7x_rear_review

T = 1.92  # six 0.32 mm layers
H = 13.0
PLATED = Interface(solid_bottom_mm=T)
BUILD = ["--build-width-mm", "350", "--build-depth-mm", "320", "--build-height-mm", "325"]


def volume(shape) -> float:
    return sum(solid.volume for solid in shape.solids()) if shape else 0


def cut_face(shape, z):
    faces = section(shape, section_by=Plane.XY.offset(z)).faces()
    assert len(faces) == 1
    return faces[0]


@pytest.fixture(scope="module")
def plated_tile():
    return make_tile(Tile(2, 1, PLATED))


@pytest.fixture(scope="module")
def default_tile():
    return make_tile(Tile(2, 1))


def test_off_is_the_existing_interface_and_identity():
    assert Interface(solid_bottom_mm=0) == Interface()
    assert Interface().body_height == 13 and Interface().male_height == 10
    name, parameters = tile_identity(Tile(2, 1, Interface(solid_bottom_mm=0)))
    assert name == tile_identity(Tile(2, 1))[0]
    assert "solid_bottom_mm" not in parameters["interface"]
    assert not {"solid_bottom_mm", "body_thickness_mm", "solid_bottom_note"} & set(
        Interface().compatibility()
    )
    edge = Accessory("edge-y", nx=2)
    assert accessory_identity(edge) == accessory_identity(
        replace(edge, interface=Interface(solid_bottom_mm=0))
    )


def test_interface_heights_and_compatibility_follow_the_floor():
    assert PLATED.height == H and PLATED.body_height == pytest.approx(H + T)
    assert PLATED.plug_depth == pytest.approx(H - 0.2)
    assert PLATED.male_height == pytest.approx(H + T - 3)
    assert PLATED.female_opening_height == pytest.approx(H + T - 2.8)
    compatibility = PLATED.compatibility()
    assert compatibility["solid_bottom_mm"] == T
    assert compatibility["body_thickness_mm"] == pytest.approx(H + T)
    assert compatibility["original_x_attachment_dimensions"] is True
    assert compatibility["original_tile_edge_dimensions"] is False
    assert "same solid bottom" in compatibility["geometry_warning"]
    name, parameters = tile_identity(Tile(2, 1, PLATED))
    assert name != tile_identity(Tile(2, 1))[0]
    assert parameters["interface"]["solid_bottom_mm"] == T


@pytest.mark.parametrize("value", [-1, nan, inf])
def test_solid_bottom_rejects_invalid_thickness(value):
    with pytest.raises(ValueError, match="solid bottom thickness"):
        Interface(solid_bottom_mm=value)


def test_solid_bottom_rejects_full_height_joints():
    with pytest.raises(ValueError, match="full-height"):
        Interface(joint_style="full-height", solid_bottom_mm=T)


def test_plated_tile_is_one_valid_solid_with_the_floor_below_the_standard_body(plated_tile):
    assert plated_tile.is_valid and len(plated_tile.solids()) == 1 and plated_tile.volume > 0
    bounds = plated_tile.bounding_box()
    assert tuple(bounds.min) == pytest.approx((0, 0, 0), abs=1e-6)
    assert tuple(bounds.size) == pytest.approx((126, 66, H + T), abs=1e-6)


def test_sections_inside_the_floor_are_closed_and_openings_keep_their_depth(
    plated_tile, default_tile
):
    assert not cut_face(plated_tile, T / 2).inner_wires()
    assert not cut_face(plated_tile, T - 0.05).inner_wires()
    # Every opening keeps its section, measured down from the top, to the floor. The default
    # tile's R1 underside round sits in its lowest millimetre, so compare areas above it.
    for depth in (0.5, H / 2, H - 1.5, H - 0.1):
        plated = cut_face(plated_tile, T + H - depth)
        standard = cut_face(default_tile, H - depth)
        assert len(plated.inner_wires()) == len(standard.inner_wires()) > 2
        if depth < H - 1:
            assert plated.area == pytest.approx(standard.area, abs=1e-4)
    seated_tip = PLATED.body_height - PLATED.plug_depth
    assert seated_tip == pytest.approx(T + 0.2)


def test_every_socket_and_hole_site_is_closed_across_an_assembled_floor(plated_tile, default_tile):
    tile = Tile(2, 1, PLATED)
    sites = [*socket_centers(tile), *((h.x, h.y) for h in hole_placements(tile) if h.accepted)]
    assert len(sites) == 15
    radius = tile.hole_diameter / 2 * 0.6
    periods = [(i * 120, j * 60) for i in (-1, 0, 1) for j in (-1, 0, 1)]

    def covered(face, x, y, z):
        return any(face.is_inside(Vector(x - dx, y - dy, z)) for dx, dy in periods)

    plated = cut_face(plated_tile, T / 2)
    standard = cut_face(default_tile, T / 2)
    for x, y in sites:
        for sx, sy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            point = (x + sx * radius, y + sy * radius)
            assert covered(plated, *point, T / 2), (x, y, point)
    assert not any(covered(standard, x, y, T / 2) for x, y in socket_centers(tile))


def test_joints_extend_through_the_floor_and_mate_with_plated_ramps_only(plated_tile):
    ramp = make_accessory(Accessory("ramp", nx=2, interface=PLATED))
    assert ramp.is_valid and len(ramp.solids()) == 1 and ramp.volume > 0
    assert tuple(ramp.bounding_box().size) == pytest.approx((120, RAMP_RUN_MM, H + T), abs=1e-5)
    tile = plated_tile.moved(Location((0, -60, 0)))
    assert volume(ramp.intersect(tile)) < 1e-7
    assert ramp.distance_to(tile) < 1e-7
    datums = accessory_datums(Accessory("ramp", nx=2, interface=PLATED))
    assert datums["top_z"] == pytest.approx(H + T) and datums["solid_bottom_mm"] == T
    assert [join["height"] for join in datums["joins"]] == pytest.approx([H + T - 2.8] * 2)
    standard_ramp = make_accessory(Accessory("ramp", nx=2))
    assert volume(standard_ramp.intersect(tile)) > 1


def test_male_ramp_tabs_reach_below_the_plated_female_roof():
    ramp = make_accessory(Accessory("ramp", nx=1, ramp_join="male", interface=PLATED))
    assert ramp.is_valid and len(ramp.solids()) == 1
    assert tuple(ramp.bounding_box().size) == pytest.approx((60, RAMP_RUN_MM + 6, H + T), abs=1e-5)
    south = make_tile(Tile(1, 1, PLATED, hole_diameter=None))
    placed = ramp.rotate(Axis.Z, 180).moved(Location((60, 0, 0)))
    assert volume(south.intersect(placed)) < 1e-7
    assert south.distance_to(placed) < 1e-7


def test_plated_edges_mate_and_keep_completed_holes_blind(plated_tile):
    female = make_accessory(Accessory("edge-y", nx=2, interface=PLATED))
    male = make_accessory(Accessory("edge-x", nx=2, interface=PLATED))
    for edge in (female, male):
        assert edge.is_valid and len(edge.solids()) == 1 and edge.volume > 0
        assert edge.bounding_box().size.Z == pytest.approx(H + T)
    north = plated_tile.moved(Location((0, -60, 0)))
    assert volume(female.intersect(north)) < 1e-7 and female.distance_to(north) < 1e-7
    assert volume(male.intersect(plated_tile)) < 1e-7 and male.distance_to(plated_tile) < 1e-7
    tile_floor = cut_face(plated_tile, T / 2)
    for family, edge, side in (("edge-y", female, 2.5), ("edge-x", male, -2.5)):
        datums = accessory_datums(Accessory(family, nx=2, interface=PLATED))
        assert datums["complete_edge_holes"] and datums["solid_bottom_mm"] == T
        floor = cut_face(edge, T / 2)
        opening = cut_face(edge, T + 0.1)
        for x, _ in datums["edge_hole_centers"]:
            x += 2.5 if x < 60 else -2.5
            # Female pockets stay open through the floor; the tile's male tab fills them.
            covered = floor.is_inside(Vector(x, side, T / 2)) or (
                family == "edge-y" and tile_floor.is_inside(Vector(x, side + 60, T / 2))
            )
            assert covered, (family, x)
            assert not opening.is_inside(Vector(x, side, T + 0.1))


def test_scaled_socket_entry_path_is_closed_underneath():
    interface = Interface(30, 13, solid_bottom_mm=T)
    tile = make_tile(Tile(1, 1, interface))
    assert tile.is_valid and len(tile.solids()) == 1
    assert tile.bounding_box().size.Z == pytest.approx(H + T)
    assert not cut_face(tile, T / 2).inner_wires()
    assert len(cut_face(tile, T + 0.1).inner_wires()) == 1


def test_layout_fillers_take_the_floor_and_check_body_height():
    layout = exact_layout(150, 90, BuildVolume(150, 150, 50), interface=PLATED)
    assert {placed.tile.interface for placed in layout.pieces} == {PLATED}
    filler = next(placed.tile for placed in layout.pieces if placed.tile.filler_west)
    part = make_tile(filler)
    assert part.is_valid and len(part.solids()) == 1
    assert part.bounding_box().size.Z == pytest.approx(H + T)
    assert not cut_face(part, T / 2).inner_wires()
    with pytest.raises(ValueError, match="exceeds usable Z"):
        exact_layout(120, 60, BuildVolume(150, 150, 14), interface=PLATED)


def test_roof_enforcers_follow_the_raised_female_roofs():
    design = tile_design(Tile(2, 1, PLATED))
    volumes = roof_enforcers(design, 0.32)
    assert len(volumes) == len(roof_enforcers(tile_design(Tile(2, 1)), 0.32)) == 6
    ceiling = PLATED.female_opening_height
    for item in volumes:
        bounds = item.shape.bounding_box()
        assert item.shape.is_valid and item.shape.volume > 0
        assert bounds.min.Z == pytest.approx(ceiling - 1) and bounds.max.Z == pytest.approx(
            ceiling + 1
        )


@pytest.mark.parametrize(
    "family,kwargs",
    [
        ("plate", {}),
        ("vertical-tile-bracket", {"nx": 2}),
        ("vertical-stop", {"nx": 2, "height": 60}),
        ("lock-45", {}),
        ("support", {}),
        ("support-bit", {}),
        ("support-end", {}),
    ],
)
def test_unchanged_families_reject_a_solid_bottom(family, kwargs):
    with pytest.raises(ValueError, match="applies only to tiles, edge/corner pieces and ramps"):
        Accessory(family, interface=PLATED, **kwargs)


def test_catalogue_keeps_unchanged_families_at_their_standard_identities():
    build = BuildVolume(350, 320, 325)
    standard = {accessory_identity(spec)[0] for spec in accessory_variants(build)}
    plated = accessory_variants(build, PLATED)
    assert len(plated) == len(standard)
    for spec in plated:
        name, parameters = accessory_identity(spec)
        family = getattr(spec, "family", None)
        if family in ("edge-x", "edge-y", "corner-in", "corner-out", "ramp"):
            assert parameters["interface"]["solid_bottom_mm"] == T and name not in standard
        else:
            assert name in standard


@pytest.mark.parametrize(
    "extra,message",
    [
        (["--family", "plate"], "applies only to tiles"),
        (["--family", "support", "--length-cells", "1"], "applies only to tiles"),
        (["--family", "rod"], "rod is unchanged"),
        (["--joint-style", "full-height"], "full-height"),
        (["--stack-count", "2"], "stacking does not support"),
    ],
)
def test_cli_rejects_unsupported_solid_bottom_combinations(extra, message, tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(
            [
                "part",
                *BUILD,
                "--solid-bottom-thickness-mm",
                "1.92",
                *extra,
                "--output",
                str(tmp_path / "job"),
            ]
        )
    assert error.value.code == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "job").exists()


def test_cli_rejects_invalid_values(tmp_path, capsys):
    for value in ("-1", "nan", "inf"):
        with pytest.raises(SystemExit) as error:
            main(["part", *BUILD, "--solid-bottom-thickness-mm", value, "--output", str(tmp_path)])
        assert error.value.code == 2
        assert "0 or greater" in capsys.readouterr().err


def test_h2d_catalogue_accepts_the_solid_bottom_on_the_standard_interface(
    monkeypatch, tmp_path, capsys
):
    received = {}

    def stop(**kwargs):
        received.update(kwargs)
        raise ValueError("stopped before building")

    monkeypatch.setattr("cargo_grid.cli.h2d_dual_safe_catalogue_job", stop)
    with pytest.raises(SystemExit):
        main(
            [
                "catalogue",
                "--h2d-dual-safe",
                *BUILD,
                "--bambu",
                "--material",
                "Bambu PETG Basic @BBL H2D 0.8 nozzle",
                "PETG",
                "#637b70",
                "--nozzle-diameter-mm",
                "0.8",
                "--layer-height-mm",
                "0.32",
                "--solid-bottom-thickness-mm",
                "1.92",
                "--output",
                str(tmp_path / "h2d"),
            ]
        )
    assert "stopped before building" in capsys.readouterr().err
    assert received["solid_bottom_mm"] == T


def test_stacked_export_rejects_solid_bottom_tiles(tmp_path):
    from cargo_grid.export import BambuSettings, Material, export_job
    from cargo_grid.stacking import StackSettings

    design = tile_design(Tile(1, 1, PLATED, hole_diameter=None))
    materials = (Material("A", "PETG", "#778877"), Material("B", "PLA", "#dddddd"))
    with pytest.raises(ValueError, match="solid-bottom"):
        export_job(
            Job([design], BuildVolume(150, 150, 70), "part"),
            tmp_path / "stack",
            bambu=BambuSettings(materials, 0.4, 0.2),
            stack=StackSettings(2, 1, 0.2, 1, 1, 2),
        )


def test_cli_part_exports_and_records_the_solid_bottom(tmp_path):
    output = tmp_path / "plated"
    assert (
        main(
            [
                "part",
                "--build-width-mm",
                "150",
                "--build-depth-mm",
                "150",
                "--build-height-mm",
                "50",
                "--no-holes",
                "--solid-bottom-thickness-mm",
                "1.92",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    manifest = json.loads((output / "manifest.json").read_text())
    (entry,) = manifest["designs"]
    assert entry["size_mm"][2] == pytest.approx(H + T)
    assert entry["parameters"]["interface"]["solid_bottom_mm"] == T
    assert entry["compatibility"]["solid_bottom_mm"] == T
    assert entry["mesh"]["closed_oriented_manifold"]


def test_plated_zeekr_straight_edge_keeps_its_plan_and_closes_its_floor():
    build = BuildVolume(350, 320, 325)
    (spec,) = [v for v in zeekr_7x.variants(build, PLATED) if v.family == "edge-x" and v.nx == 1]
    standard = make_accessory(replace(spec, interface=Interface())).bounding_box()
    edge = make_accessory(spec)
    bounds = edge.bounding_box()
    assert edge.is_valid and len(edge.solids()) == 1 and edge.volume > 0
    assert (bounds.min.X, bounds.min.Y, bounds.max.X, bounds.max.Y) == pytest.approx(
        (standard.min.X, standard.min.Y, standard.max.X, standard.max.Y), abs=1e-6
    )
    assert bounds.min.Z == pytest.approx(0, abs=1e-6) and bounds.size.Z == pytest.approx(H + T)
    floor = cut_face(edge, T / 2)
    assert not floor.inner_wires()
    datums = accessory_datums(spec)
    assert datums["complete_edge_holes"] and datums["solid_bottom_mm"] == T
    for x, _ in datums["edge_hole_centers"]:
        x += 2.5 if x < 30 else -2.5
        assert floor.is_inside(Vector(x, -2.5, T / 2))
        assert not cut_face(edge, T + 0.1).is_inside(Vector(x, -2.5, T + 0.1))


def test_plated_zeekr_side_cap_keeps_its_contour_and_closes_sockets_and_holes():
    parameters = zeekr_7x_rear_review.RearReviewParameters(interface=PLATED)
    design = zeekr_7x_rear_review._side_design("west", "south", parameters)
    shape = design.shape
    assert shape.is_valid and len(shape.solids()) == 1 and shape.volume > 0
    # Standard bounds are (68.565, 171.152, 13); the measured contour stays in plan.
    assert tuple(shape.bounding_box().size) == pytest.approx((68.565, 171.152, H + T), abs=1e-3)
    assert shape.bounding_box().min.Z == pytest.approx(0, abs=1e-6)
    assert design.name == "zeekr_rear_west_south_cap_solid-bottom-1.92mm"
    assert design.parameters["interface"]["solid_bottom_mm"] == T
    assert design.mating_datums["top_z"] == pytest.approx(H + T)
    datums = design.mating_datums
    openings = [
        *((x, y) for x, y, _ in datums["socket_cutter_origins"]),
        *datums["completed_10mm_interior_hole_centers"],
    ]
    assert openings
    floor = cut_face(shape, T / 2)
    above = cut_face(shape, T + 0.1)
    assert not floor.inner_wires() and len(above.inner_wires()) >= len(openings)
    for x, y in openings:
        assert floor.is_inside(Vector(x + 3, y, T / 2))
        assert not above.is_inside(Vector(x + 3, y, T + 0.1))


@pytest.mark.slow
def test_plated_zeekr_south_ramp_rises_through_the_floor():
    parameters = zeekr_7x_rear_review.RearReviewParameters(interface=PLATED)
    x = parameters.field_x_min_mm + 8 * parameters.interface.pitch
    design = zeekr_7x_rear_review._south_design(2, 2, x, parameters)
    shape = design.shape
    assert shape.is_valid and len(shape.solids()) == 1 and shape.volume > 0
    assert tuple(shape.bounding_box().size) == pytest.approx((120, 101.0, H + T), abs=1e-3)
    assert not cut_face(shape, T / 2).inner_wires()
    assert design.mating_datums["top_z"] == pytest.approx(H + T)
    assert {join["height"] for join in design.mating_datums["joins"]} == {H + T - 3}


def test_zeekr_extras_accept_the_standard_interface_with_a_solid_bottom():
    parameters = zeekr_7x_rear_review.RearReviewParameters(interface=PLATED)
    assert parameters.interface.solid_bottom_mm == T
    with pytest.raises(ValueError, match="standard 60x13"):
        zeekr_7x_rear_review.RearReviewParameters(interface=Interface(height=14, solid_bottom_mm=T))
    with pytest.raises(ValueError, match="standard 60 mm unit"):
        zeekr_7x.extras_job(
            BuildVolume(350, 320, 325), interface=Interface(30, 13, 0, "original", T)
        )
    parts = zeekr_7x.standard_rear_panel_parts(T)
    assert {part["solid_bottom_thickness_mm"] for part in parts} == {T}
    assert all(
        "solid_bottom_thickness_mm" not in part for part in zeekr_7x.standard_rear_panel_parts()
    )
