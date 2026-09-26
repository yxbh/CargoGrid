import json
from copy import deepcopy
from dataclasses import replace
from functools import lru_cache
from math import cos, pi, sin, sqrt

import pytest
from build123d import Axis, Box, GeomType, Location, Part, Vector
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.gp import gp_Pnt
from OCP.TopAbs import TopAbs_IN

from cargo_grid import BuildVolume, Interface, Tile, make_tile
from cargo_grid.accessories import Accessory, accessory_datums, make_accessory
from cargo_grid.catalogue import accessory_design, accessory_variants
from cargo_grid.cli import main
from cargo_grid.export import _checked_step_roundtrip
from cargo_grid.meshes import checked_mesh
from cargo_grid.tiles import hole_placements

_POINT_CLASSIFIER_TOLERANCE = 1e-6
# Hole checks sample radii up to 5.01 mm; the clip keeps every sample well inside its box.
_LOCAL_CLIP_HALF_WIDTH = 8
_LOCAL_CLIP_MARGIN = 0.5
# Portable runs keep the narrowest and widest standard projections; the slow tier adds 20 mm.
_OUTWARD_WIDTHS = [10, pytest.param(20, marks=pytest.mark.slow), 30]


@lru_cache(maxsize=128)
def _accessory_template(spec):
    return make_accessory(spec)


def _accessory_shape(spec):
    return deepcopy(_accessory_template(spec))


@lru_cache(maxsize=16)
def _tile_template(tile):
    return make_tile(tile)


def _tile_shape(tile):
    return deepcopy(_tile_template(tile))


def _assembly_contains(shapes_and_bounds, point):
    return any(
        bounds.min.X - _POINT_CLASSIFIER_TOLERANCE
        <= point.X
        <= bounds.max.X + _POINT_CLASSIFIER_TOLERANCE
        and bounds.min.Y - _POINT_CLASSIFIER_TOLERANCE
        <= point.Y
        <= bounds.max.Y + _POINT_CLASSIFIER_TOLERANCE
        and bounds.min.Z - _POINT_CLASSIFIER_TOLERANCE
        <= point.Z
        <= bounds.max.Z + _POINT_CLASSIFIER_TOLERANCE
        and shape.is_inside(point, _POINT_CLASSIFIER_TOLERANCE)
        for shape, bounds in shapes_and_bounds
    )


def _prepared_assembly_contains(shapes):
    """Keep query state local to one case, after its shapes stop changing."""
    queries = []
    for shape in shapes:
        bounds = shape.bounding_box()
        queries.append(
            (tuple(bounds.min), tuple(bounds.max), BRepClass3d_SolidClassifier(shape.wrapped))
        )

    def contains(point):
        coordinates = tuple(Vector(point))
        for lower, upper, classifier in queries:
            if all(
                minimum - _POINT_CLASSIFIER_TOLERANCE
                <= value
                <= maximum + _POINT_CLASSIFIER_TOLERANCE
                for minimum, maximum, value in zip(lower, upper, coordinates)
            ):
                classifier.Perform(gp_Pnt(*coordinates), _POINT_CLASSIFIER_TOLERANCE)
                if classifier.State() == TopAbs_IN or classifier.IsOnAFace():
                    return True
        return False

    return contains


def _local_assembly_contains(shapes, center, height, half_width=_LOCAL_CLIP_HALF_WIDTH):
    """Classify points near one hole against the shapes clipped to a small box around it.

    A point strictly inside the clip box is inside a shape exactly when it is inside the shape's
    clipped part, so this answers the whole-solid question without testing every distant face.
    Queries outside the box are rejected rather than answered.
    """
    lower = (center[0] - half_width, center[1] - half_width, -1)
    upper = (center[0] + half_width, center[1] + half_width, height + 1)
    clip = Box(2 * half_width, 2 * half_width, height + 2).moved(
        Location((center[0], center[1], height / 2))
    )
    pieces = []
    for shape in shapes:
        bounds = shape.bounding_box()
        if any(
            maximum < low or minimum > high
            for minimum, maximum, low, high in zip(bounds.min, bounds.max, lower, upper)
        ):
            continue
        common = shape.intersect(clip)
        if common:
            pieces.extend(common.solids())
    assert all(piece.is_valid and piece.volume > 0 for piece in pieces)
    contains = _prepared_assembly_contains(pieces)

    def local_contains(point):
        coordinates = tuple(Vector(point))
        assert all(
            low + _LOCAL_CLIP_MARGIN < value < high - _LOCAL_CLIP_MARGIN
            for value, low, high in zip(coordinates, lower, upper)
        ), f"query {coordinates} is outside the local clip around {center}"
        return contains(point)

    return local_contains


def _solid_intersection_volume(first, second):
    intersection = first.intersect(second)
    return sum(solid.volume for solid in intersection.solids()) if intersection else 0


def _part(shape):
    return shape if isinstance(shape, Part) else Part(shape.solids())


def _clipped_volume(shape, clip):
    intersection = shape.intersect(clip)
    return sum(solid.volume for solid in intersection.solids()) if intersection else 0


def _pair_target_symmetric_difference(parts, target, clip):
    pair_volume = sum(_clipped_volume(part, clip) for part in parts)
    target_clipped = target.intersect(clip)
    target_volume = sum(solid.volume for solid in target_clipped.solids()) if target_clipped else 0
    common_volume = sum(
        _solid_intersection_volume(_part(part.intersect(clip)), _part(target_clipped))
        for part in parts
        if part.intersect(clip) and target_clipped
    )
    return pair_volume + target_volume - 2 * common_volume


def _matching_tiles(spec):
    pitch = spec.interface.pitch
    placements = {}
    for join in accessory_datums(spec)["joins"]:
        x, y, _ = join["position"]
        if join["angle"] == 0:
            origin = (x - pitch / 2, y - pitch if join["sex"] == "female" else y, 0)
        elif join["angle"] == -90:
            origin = (x - pitch if join["sex"] == "female" else x, y - pitch / 2, 0)
        else:
            raise AssertionError(f"unsupported perimeter join angle: {join['angle']}")
        placements[origin] = _tile_shape(Tile(interface=spec.interface)).moved(Location(origin))
    return tuple(placements.values())


def _three_by_three_perimeter(outward, complete):
    def part(family, *, variant=1):
        return _accessory_shape(
            Accessory(
                family,
                variant=variant,
                edge_outward=outward,
                complete_edge_holes=complete,
            )
        )

    return {
        "tile": _tile_shape(Tile(3, 3)),
        "v6": part("corner-out", variant=6),
        "south": part("edge-x").moved(Location((60, 0, 0))),
        "v5": part("corner-out", variant=5).moved(Location((120, 0, 0))),
        "v4": part("corner-out", variant=4).moved(Location((180, 0, 0))),
        "east": part("edge-y").rotate(Axis.Z, -90).moved(Location((180, 120, 0))),
        "v3": part("corner-out", variant=3).moved(Location((120, 120, 0))),
        "north": part("edge-y").moved(Location((60, 180, 0))),
        "v2": part("corner-out", variant=2).moved(Location((0, 180, 0))),
        "v1": part("corner-out", variant=1).moved(Location((0, 120, 0))),
        "west": part("edge-x").rotate(Axis.Z, -90).moved(Location((0, 120, 0))),
    }


def _corner_half_pairs(outward, complete, interface=Interface()):
    def part(variant):
        return _accessory_shape(
            Accessory(
                "corner-out",
                variant=variant,
                interface=interface,
                edge_outward=outward,
                complete_edge_holes=complete,
            )
        )

    pitch = interface.pitch
    return (
        (part(1), part(2).moved(Location((0, pitch, 0)))),
        (part(5), part(4).moved(Location((pitch, 0, 0)))),
    )


def _whole_assembly_contains(shapes):
    shapes_and_bounds = tuple((shape, shape.bounding_box()) for shape in shapes)

    def contains(point):
        return _assembly_contains(shapes_and_bounds, point)

    return contains


def _assert_completed_circle(shapes, center, height, minimum_ring_samples=62, *, contains=None):
    if contains is None:
        contains = _local_assembly_contains(shapes, center, height)

    for z in (1, height / 2, height - 1):
        for index in range(64):
            angle = 2 * pi * index / 64
            inside = Vector(
                center[0] + 4.99 * cos(angle),
                center[1] + 4.99 * sin(angle),
                z,
            )
            assert not contains(inside)
        assert (
            sum(
                contains(
                    Vector(
                        center[0] + 5.01 * cos(2 * pi * index / 64),
                        center[1] + 5.01 * sin(2 * pi * index / 64),
                        z,
                    ),
                )
                for index in range(64)
            )
            >= minimum_ring_samples
        )


def test_cached_geometry_templates_are_isolated_from_consumers():
    accessory_spec = Accessory(
        "corner-out",
        variant=3,
        edge_outward=30,
        complete_edge_holes=True,
    )
    tile_spec = Tile(2, 1)
    for template, first, second in (
        (
            _accessory_template(accessory_spec),
            _accessory_shape(accessory_spec),
            _accessory_shape(accessory_spec),
        ),
        (_tile_template(tile_spec), _tile_shape(tile_spec), _tile_shape(tile_spec)),
    ):
        template_bounds = (*template.bounding_box().min, *template.bounding_box().max)
        template_location = template.location
        first_bounds = (*first.bounding_box().min, *first.bounding_box().max)
        first_location = first.location
        assert not first.wrapped.IsSame(template.wrapped)
        assert not second.wrapped.IsSame(template.wrapped)
        assert not first.wrapped.IsSame(second.wrapped)
        placed = first.moved(Location((120, 30, 4))).rotate(Axis.Z, 90)
        assert placed.location != first_location
        assert (*first.bounding_box().min, *first.bounding_box().max) == pytest.approx(first_bounds)
        assert first.location == first_location
        assert (*template.bounding_box().min, *template.bounding_box().max) == pytest.approx(
            template_bounds
        )
        assert template.location == template_location


@pytest.mark.parametrize("family", ["edge-x", "edge-y"])
@pytest.mark.parametrize(
    "outward,complete",
    [(10, False), (10, True), (20, False), (20, True), (30, False), (30, True)],
)
def test_straight_edge_projection_and_hole_mode(family, outward, complete):
    spec = Accessory(
        family,
        nx=2,
        edge_outward=outward,
        complete_edge_holes=complete,
    )
    part = _accessory_shape(spec)
    expected_depth = outward + (spec.interface.male_join_depth if family == "edge-x" else 0)
    assert tuple(part.bounding_box().size) == pytest.approx((120, expected_depth, 13))
    datums = accessory_datums(spec)
    assert datums["edge_outward"] == outward
    assert datums["complete_edge_holes"] is complete
    assert datums["edge_hole_diameter"] == (10 if complete else None)
    assert datums["edge_hole_centers"] == (
        [(0, 0), (30, 0), (60, 0), (90, 0), (120, 0)] if complete else []
    )


@pytest.mark.parametrize("style", ["original", "full-height"])
@pytest.mark.parametrize("outward", _OUTWARD_WIDTHS)
@pytest.mark.parametrize("complete", [False, True])
def test_corner_variants_remain_single_valid_solids(style, outward, complete):
    interface = Interface(joint_style=style)
    specs = [
        *(
            Accessory(
                "corner-in",
                variant=v,
                interface=interface,
                edge_outward=outward,
                complete_edge_holes=complete,
            )
            for v in range(1, 5)
        ),
        *(
            Accessory(
                "corner-out",
                variant=v,
                interface=interface,
                edge_outward=outward,
                complete_edge_holes=complete,
            )
            for v in range(1, 7)
        ),
    ]
    for spec in specs:
        part = _accessory_shape(spec)
        assert part.is_valid and len(part.solids()) == 1
        assert part.volume > 0
        assert all(join["position"][0] in (0, 30, 60) for join in accessory_datums(spec)["joins"])


@pytest.mark.parametrize(
    "family,variants",
    [
        ("edge-x", range(1, 2)),
        ("edge-y", range(1, 2)),
        ("corner-in", range(1, 5)),
        ("corner-out", range(1, 7)),
    ],
)
@pytest.mark.parametrize("outward", _OUTWARD_WIDTHS)
def test_every_perimeter_join_completes_assembled_ten_mm_holes(family, variants, outward):
    for variant in variants:
        spec = Accessory(
            family,
            variant=variant,
            edge_outward=outward,
            complete_edge_holes=True,
        )
        shapes = (_accessory_shape(spec), *_matching_tiles(spec))
        for join in accessory_datums(spec)["joins"]:
            _assert_completed_circle(shapes, join["position"], spec.interface.height)


@pytest.mark.parametrize("outward", _OUTWARD_WIDTHS)
@pytest.mark.parametrize("variant,center", [(3, (60, 60)), (6, (0, 0))])
def test_completed_one_piece_outer_corner_makes_full_ten_mm_hole(outward, variant, center):
    tile = _tile_shape(Tile())
    corner = _accessory_shape(
        Accessory(
            "corner-out",
            variant=variant,
            edge_outward=outward,
            complete_edge_holes=True,
        )
    )
    shapes = (tile, corner)
    _assert_completed_circle(shapes, center, 13)


@pytest.mark.parametrize("outward", [10, 20, 30])
def test_completed_miter_terminations_cut_the_tile_corner_site(outward):
    for variant in (1, 2, 4, 5):
        spec = Accessory(
            "corner-out",
            variant=variant,
            edge_outward=outward,
            complete_edge_holes=True,
        )
        centers = accessory_datums(spec)["edge_hole_centers"]
        assert len(centers) == 3
        part = _accessory_shape(spec)
        for x, y in centers:
            for z in (1, 6.5, 12):
                assert not part.is_inside(Vector(x, y, z))


@pytest.mark.parametrize(
    "outward,complete",
    [
        (10, False),
        (10, True),
        pytest.param(20, False, marks=pytest.mark.slow),
        pytest.param(20, True, marks=pytest.mark.slow),
        (30, False),
        (30, True),
    ],
)
def test_outer_corner_half_pairs_close_without_overlap(outward, complete):
    pitch = 60
    diagonal = outward / sqrt(2)
    pairs = _corner_half_pairs(outward, complete)
    for (first, second), seam_point, expected_min, expected_max in zip(
        pairs,
        (
            lambda fraction: Vector(
                -diagonal * fraction,
                pitch + diagonal * fraction,
                6.5,
            ),
            lambda fraction: Vector(
                pitch + diagonal * fraction,
                -diagonal * fraction,
                6.5,
            ),
        ),
        (
            (-outward, 0, 0),
            (0, -outward, 0),
        ),
        (
            (pitch, pitch + outward, 13),
            (pitch + outward, pitch, 13),
        ),
    ):
        assert first.distance_to(second) < 1e-6
        assert _solid_intersection_volume(first, second) < 1e-7
        fractions = (
            tuple(sorted({6 / outward, (outward - 4) / outward})) if complete else (0.1, 0.5, 0.9)
        )
        for fraction in fractions:
            point = seam_point(fraction)
            assert first.distance_to(point) < 1e-6
            assert second.distance_to(point) < 1e-6
        actual_min = tuple(
            min(getattr(shape.bounding_box().min, axis) for shape in (first, second))
            for axis in "XYZ"
        )
        actual_max = tuple(
            max(getattr(shape.bounding_box().max, axis) for shape in (first, second))
            for axis in "XYZ"
        )
        assert actual_min == pytest.approx(expected_min, abs=1e-5)
        assert actual_max == pytest.approx(expected_max, abs=1e-5)


@pytest.mark.parametrize(
    "outward,complete,interface",
    [
        (10, False, Interface()),
        (10, True, Interface()),
        (20, False, Interface()),
        (20, True, Interface()),
        (30, False, Interface()),
        (30, True, Interface()),
        (20, False, Interface(45, 8)),
    ],
)
def test_outer_corner_pairs_match_whole_l_exterior_sections(
    outward,
    complete,
    interface,
):
    pitch = interface.pitch
    height = interface.height
    northwest, southeast = _corner_half_pairs(outward, complete, interface)
    whole_northeast = _accessory_shape(
        Accessory(
            "corner-out",
            variant=3,
            interface=interface,
            edge_outward=outward,
            complete_edge_holes=complete,
        )
    )
    whole_southwest = _accessory_shape(
        Accessory(
            "corner-out",
            variant=6,
            interface=interface,
            edge_outward=outward,
            complete_edge_holes=complete,
        )
    )
    comparisons = (
        (
            northwest,
            whole_northeast.rotate(Axis.Z, 90).moved(Location((pitch, 0, 0))),
            (-outward, pitch),
        ),
        (
            southeast,
            whole_southwest.rotate(Axis.Z, 90).moved(Location((pitch, 0, 0))),
            (pitch, -outward),
        ),
    )
    for parts, target, (x, y) in comparisons:
        corner = Box(outward, outward, height).moved(Location((x, y, 0)))
        assert _pair_target_symmetric_difference(parts, target, corner) < 1e-6
        for z in (1, height / 2, height - 1):
            section = Box(outward, outward, 0.02).moved(Location((x, y, z - 0.01)))
            assert _pair_target_symmetric_difference(parts, target, section) / 0.02 < 1e-6


_REAL_PERIMETER_OUTWARD = 30
_REAL_PERIMETER_NEIGHBORS = [
    ("v6", "south"),
    ("south", "v5"),
    ("v5", "v4"),
    ("v4", "east"),
    ("east", "v3"),
    ("v3", "north"),
    ("north", "v2"),
    ("v2", "v1"),
    ("v1", "west"),
    ("west", "v6"),
]
_REAL_PERIMETER_CORNERS = {
    "v1": (1, (0, 120)),
    "v2": (2, (0, 180)),
    "v3": (3, (120, 120)),
    "v4": (4, (180, 0)),
    "v5": (5, (120, 0)),
    "v6": (6, (0, 0)),
}


@pytest.mark.parametrize(
    "first,second",
    _REAL_PERIMETER_NEIGHBORS,
    ids=[f"{first}-{second}" for first, second in _REAL_PERIMETER_NEIGHBORS],
)
def test_outer_corner_halves_and_whole_corners_close_a_real_perimeter(first, second):
    assembly = _three_by_three_perimeter(_REAL_PERIMETER_OUTWARD, True)
    assert assembly[first].distance_to(assembly[second]) < 1e-6
    assert _solid_intersection_volume(assembly[first], assembly[second]) < 1e-7


@pytest.mark.parametrize("name", sorted(_REAL_PERIMETER_CORNERS))
def test_real_perimeter_corners_join_the_opposite_tile_sex(name):
    assembly = _three_by_three_perimeter(_REAL_PERIMETER_OUTWARD, True)
    variant, (offset_x, offset_y) = _REAL_PERIMETER_CORNERS[name]
    joins = accessory_datums(
        Accessory(
            "corner-out",
            variant=variant,
            edge_outward=_REAL_PERIMETER_OUTWARD,
            complete_edge_holes=True,
        )
    )["joins"]
    assert joins
    for join in joins:
        x = join["position"][0] + offset_x
        y = join["position"][1] + offset_y
        tile_sex = "female" if x == 0 or y == 0 else "male"
        assert x in (0, 180) or y in (0, 180)
        assert join["sex"] != tile_sex
    assert assembly[name].distance_to(assembly["tile"]) < 1e-6


def test_real_perimeter_completes_representative_boundary_holes():
    shapes = tuple(_three_by_three_perimeter(_REAL_PERIMETER_OUTWARD, True).values())
    boundary_holes = [
        (hole.x, hole.y)
        for hole in hole_placements(Tile(3, 3))
        if hole.accepted and (hole.x in (0, 180) or hole.y in (0, 180))
    ]
    assert len(boundary_holes) == 24
    representative_holes = {
        (0, 0),
        (180, 180),
        (0, 180),
        (180, 0),
        (30, 0),
        (0, 30),
        (60, 0),
        (180, 60),
    }
    assert representative_holes < set(boundary_holes)
    for center in representative_holes:
        _assert_completed_circle(
            shapes,
            center,
            13,
            minimum_ring_samples=59 if center in {(60, 0), (180, 60)} else 62,
        )


@pytest.mark.parametrize(
    "variant,outward,complete",
    [
        (1, 10, False),
        (2, 10, True),
        (2, 20, True),
        (4, 20, False),
        (5, 20, True),
        (2, 30, False),
        (4, 30, True),
    ],
)
def test_outer_corner_halves_keep_rounds_step_and_mesh(
    variant,
    outward,
    complete,
    tmp_path,
):
    part = _accessory_shape(
        Accessory(
            "corner-out",
            variant=variant,
            edge_outward=outward,
            complete_edge_holes=complete,
        )
    )
    assert part.is_valid and len(part.solids()) == 1 and part.volume > 0
    assert min(face.area for face in part.faces()) > 0.3
    assert min(edge.length for edge in part.edges()) > 0.3
    assert any(
        face.geom_type == GeomType.CYLINDER
        and BRepAdaptor_Surface(face.wrapped).Cylinder().Radius() == pytest.approx(3, abs=1e-7)
        for face in part.faces()
    )
    restored, _, _, _, _ = _checked_step_roundtrip(
        part,
        tmp_path / f"corner-out-v{variant}-out{outward}-holes{complete}.step",
    )
    assert restored.is_valid and len(restored.solids()) == 1
    assert tuple(restored.bounding_box().size) == pytest.approx(
        tuple(part.bounding_box().size),
        abs=1e-5,
    )
    _, _, report = checked_mesh(part)
    assert report["closed_oriented_manifold"]


def test_outer_corner_half_pair_scales_unit_and_thickness_independently():
    interface = Interface(45, 8)
    for first, second in _corner_half_pairs(20, False, interface):
        assert first.distance_to(second) < 1e-6
        assert _solid_intersection_volume(first, second) < 1e-7
        assert max(first.bounding_box().size.Z, second.bounding_box().size.Z) == pytest.approx(8)


def test_custom_unit_completion_uses_only_tile_accepted_boundary_sites():
    interface = Interface(41)
    spec = Accessory(
        "edge-y",
        nx=2,
        interface=interface,
        edge_outward=30,
        complete_edge_holes=True,
    )
    assert accessory_datums(spec)["edge_hole_centers"] == [(0, 0), (41, 0), (82, 0)]
    part = _accessory_shape(spec)
    for x in (0, 41, 82):
        assert not part.is_inside(Vector(x, 4, 6.5))
    for x in (20.5, 61.5):
        assert part.is_inside(Vector(x, 4.5, 6.5))
    with pytest.raises(ValueError, match="no accepted 10 mm full-pattern boundary holes"):
        Accessory(
            "edge-y",
            interface=Interface(30),
            edge_outward=20,
            complete_edge_holes=True,
        )
    compact_catalogue = accessory_variants(
        BuildVolume(150, 150, 50),
        Interface(30),
    )
    assert any(spec.family == "edge-y" for spec in compact_catalogue)
    assert not any(
        spec.family in {"edge-x", "edge-y", "corner-in", "corner-out"} and spec.complete_edge_holes
        for spec in compact_catalogue
    )


def test_plain_edge_and_corner_design_ids_are_stable():
    assert accessory_design(Accessory("edge-x", complete_edge_holes=False)).name == (
        "edge-x_1x1_v1_original_ffbb300adc"
    )
    assert accessory_design(Accessory("edge-y", nx=5, complete_edge_holes=False)).name == (
        "edge-y_5x1_v1_original_dcd3b5c880"
    )
    assert accessory_design(Accessory("corner-in", variant=4, complete_edge_holes=False)).name == (
        "corner-in_1x1_v4_original_bbac2899f0"
    )
    assert accessory_design(Accessory("corner-out", variant=6, complete_edge_holes=False)).name == (
        "corner-out_1x1_v6_original_349b8d994c"
    )


def test_completed_ten_mm_edge_and_corner_design_ids_are_stable():
    assert accessory_design(Accessory("edge-x")).name == (
        "edge-x_1x1_v1_complete-holes_original_95df61de95"
    )
    assert accessory_design(Accessory("edge-y", nx=5)).name == (
        "edge-y_5x1_v1_complete-holes_original_eb850c30fe"
    )
    assert accessory_design(Accessory("corner-in", variant=4)).name == (
        "corner-in_1x1_v4_complete-holes_original_6af0c74987"
    )
    assert accessory_design(Accessory("corner-out", variant=6)).name == (
        "corner-out_1x1_v6_complete-holes_original_1f6ce28998"
    )
    assert (
        accessory_design(Accessory("edge-y", edge_outward=20, complete_edge_holes=False)).name
        == "edge-y_1x1_v1_out20mm_original_5c1a493dd5"
    )
    assert accessory_design(Accessory("edge-y", edge_outward=20)).name == (
        "edge-y_1x1_v1_out20mm_complete-holes_original_4d8df041a3"
    )


def test_nondefault_edge_name_parameters_step_and_mesh(tmp_path):
    spec = Accessory(
        "corner-out",
        variant=3,
        edge_outward=30,
        complete_edge_holes=True,
    )
    design = accessory_design(spec)
    assert "_out30mm_complete-holes_" in design.name
    assert design.parameters["edge_outward"] == 30
    assert design.parameters["complete_edge_holes"] is True
    assert len(design.holes) == 5
    restored, _, _, _, _ = _checked_step_roundtrip(design.shape, tmp_path / "edge.step")
    assert restored.is_valid and len(restored.solids()) == 1
    assert tuple(restored.bounding_box().size) == pytest.approx(
        tuple(design.shape.bounding_box().size), abs=1e-5
    )
    _, _, report = checked_mesh(design.shape)
    assert report["closed_oriented_manifold"]


def test_edge_cli_records_projection_completion_and_rejects_other_families(tmp_path, capsys):
    common = [
        "part",
        "--build-width-mm",
        "160",
        "--build-depth-mm",
        "160",
        "--build-height-mm",
        "30",
        "--no-stl",
    ]
    output = tmp_path / "edge"
    assert (
        main(
            [
                *common,
                "--family",
                "edge-y",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    design = json.loads((output / "manifest.json").read_text())["designs"][0]
    assert "edge_outward" not in design["parameters"]
    assert design["parameters"]["complete_edge_holes"] is True
    assert design["compatibility"]["edge_outward_mm"] == 10
    assert design["compatibility"]["complete_edge_holes"] is True
    assert design["compatibility"]["edge_hole_diameter_mm"] == 10
    assert len(design["hole_placements"]) == 3

    plain_output = tmp_path / "plain-edge"
    assert (
        main(
            [
                *common,
                "--family",
                "edge-y",
                "--plain-edge",
                "--output",
                str(plain_output),
            ]
        )
        == 0
    )
    plain = json.loads((plain_output / "manifest.json").read_text())["designs"][0]
    assert "edge_outward" not in plain["parameters"]
    assert "complete_edge_holes" not in plain["parameters"]
    assert plain["compatibility"]["edge_hole_diameter_mm"] is None

    with pytest.raises(SystemExit) as error:
        main(
            [
                *common,
                "--family",
                "plate",
                "--complete-edge-holes",
                "--output",
                str(tmp_path / "invalid"),
            ]
        )
    assert error.value.code == 2
    assert "--complete-edge-holes does not apply to plate" in capsys.readouterr().err


@pytest.mark.parametrize(
    "unit,thickness,diameter",
    [(45, 8, 8), (60, 18, 10), (90, 13, 12)],
)
def test_completed_edge_keeps_hole_diameter_physical(unit, thickness, diameter):
    interface = Interface(unit, thickness)
    spec = Accessory(
        "edge-y",
        nx=2,
        interface=interface,
        edge_outward=10,
        complete_edge_holes=True,
        edge_hole_diameter=diameter,
    )
    part = _accessory_shape(spec)
    center = unit
    radius = diameter / 2
    for z in (1, thickness / 2, thickness - 1):
        assert not part.is_inside(Vector(center, radius - 0.01, z))
        assert part.is_inside(Vector(center, radius + 0.01, z))
    assert accessory_datums(spec)["edge_hole_diameter"] == diameter
    design = accessory_design(spec)
    if diameter == 10:
        assert "edge_hole_diameter" not in design.parameters
    else:
        assert design.parameters["edge_hole_diameter"] == diameter
        assert f"_complete-{diameter:g}mm-holes_" in design.name


def test_ten_mm_perimeters_match_holes_and_retain_explicit_plain_choice():
    assert Accessory("edge-y").complete_edge_holes is True
    custom = Accessory("edge-y", edge_hole_diameter=8)
    assert custom.complete_edge_holes is True
    assert custom.edge_hole_diameter == 8
    assert Accessory("corner-out", variant=3, complete_edge_holes=True).complete_edge_holes is True
    plain = Accessory("corner-out", variant=3, complete_edge_holes=False)
    assert plain.complete_edge_holes is False
    assert plain.edge_hole_diameter is None


def test_replacing_interface_preserves_edge_options():
    spec = Accessory("edge-y", edge_outward=20, complete_edge_holes=True)
    updated = replace(spec, interface=Interface(height=18))
    assert updated.edge_outward == 20
    assert updated.complete_edge_holes is True


@pytest.mark.parametrize("family", ["edge-x", "edge-y"])
@pytest.mark.parametrize("cells", [1, pytest.param(4, marks=pytest.mark.slow), 5])
def test_forty_mm_straights_keep_tile_interfaces_and_complete_middle_holes(family, cells):
    spec = Accessory(family, nx=cells, edge_outward=40)
    shape = _accessory_shape(spec)
    narrow = _accessory_shape(replace(spec, edge_outward=30))
    assert shape.is_valid and len(shape.solids()) == 1 and shape.volume > 0
    assert tuple(shape.bounding_box().size) == pytest.approx(
        (cells * 60, 46 if family == "edge-x" else 40, 13),
        abs=1e-5,
    )
    assert shape.bounding_box().min.Z == pytest.approx(0, abs=1e-6)
    clip = Box(cells * 60 + 2, 20, 15).moved(Location((cells * 30, 0, 6.5)))
    first = _part(shape.intersect(clip))
    second = _part(narrow.intersect(clip))
    assert not first.cut(second).solids()
    assert not second.cut(first).solids()
    tile = _tile_shape(Tile(cells, 1))
    if family == "edge-y":
        tile = tile.moved(Location((0, -60, 0)))
    shapes = (shape, tile)
    datums = accessory_datums(spec)
    assert datums["edge_hole_centers"] == [(x, 0) for x in range(0, cells * 60 + 1, 30)]
    for x in range(30, cells * 60, 30):
        _assert_completed_circle(shapes, (x, 0), 13, minimum_ring_samples=59)
    # End holes still need the neighboring perimeter's quarter; no 40 mm corners are supplied.
    outside_y = -3.6 if family == "edge-x" else 3.6
    assert not any(part.is_inside(Vector(-3.6, outside_y, 6.5)) for part in shapes)
    assert any(
        face.geom_type == GeomType.CYLINDER
        and BRepAdaptor_Surface(face.wrapped).Cylinder().Radius() == pytest.approx(3, abs=1e-7)
        for face in shape.faces()
    )


@pytest.mark.parametrize("family", ["edge-x", "edge-y"])
@pytest.mark.parametrize("unit,thickness,diameter", [(45, 8, 8), (60, 18, 10), (90, 13, 12)])
def test_forty_mm_custom_interface_keeps_physical_width_and_hole_diameter(
    family,
    unit,
    thickness,
    diameter,
    tmp_path,
):
    spec = Accessory(
        family,
        nx=2,
        edge_outward=40,
        interface=Interface(unit, thickness),
        edge_hole_diameter=diameter,
        complete_edge_holes=True,
    )
    shape = _accessory_shape(spec)
    assert tuple(shape.bounding_box().size) == pytest.approx(
        (2 * unit, 40 + (unit * 0.1 if family == "edge-x" else 0), thickness),
        abs=1e-5,
    )
    direction = -1 if family == "edge-x" else 1
    for z in (1, thickness / 2, thickness - 1):
        assert not shape.is_inside(Vector(unit, direction * (diameter / 2 - 0.01), z))
        assert shape.is_inside(Vector(unit, direction * (diameter / 2 + 0.01), z))
    restored, _, delta, budget, bounds_delta = _checked_step_roundtrip(
        shape, tmp_path / "edge.step"
    )
    assert restored.is_valid and len(restored.solids()) == 1
    assert delta <= budget and bounds_delta <= 1e-5
    _, _, report = checked_mesh(shape)
    assert report["closed_oriented_manifold"]


@pytest.mark.parametrize("family", ["edge-x", "edge-y"])
@pytest.mark.parametrize("transformed", [False, True])
def test_prepared_queries_match_legacy_inside_outside_and_boundary(family, transformed):
    edge = _accessory_shape(Accessory(family, edge_outward=40))
    tile = _tile_shape(Tile())
    if family == "edge-y":
        tile = tile.moved(Location((0, -60, 0)))
    shapes = (edge, tile)
    direction = -1 if family == "edge-x" else 1
    known = [
        (Vector(30, direction * 20, 6.5), True),
        (Vector(30, direction * 40, 6.5), True),
        (Vector(30, direction * 45, 6.5), False),
        (Vector(30, 0, 12), False),
    ]
    points = [
        *(point for point, _ in known),
        *(Vector(x, direction * 20, 6.5) for x in (-2e-6, -0.5e-6, 0, 0.5e-6)),
        *(Vector(30, direction * 20, z) for z in (-2e-6, -0.5e-6, 0, 13, 13 + 2e-6)),
        *(
            Vector(30 + radius * cos(angle), radius * sin(angle), 12)
            for radius in (4.99, 5, 5.01)
            for angle in (index * pi / 4 for index in range(8))
        ),
    ]
    if transformed:
        shapes = tuple(shape.rotate(Axis.Z, 90).moved(Location((17, -23, 4))) for shape in shapes)
        points = [Vector(17 - point.Y, -23 + point.X, 4 + point.Z) for point in points]
    expected = [
        any(shape.is_inside(point, _POINT_CLASSIFIER_TOLERANCE) for shape in shapes)
        for point in points
    ]
    assert expected[: len(known)] == [result for _, result in known]
    contains = _prepared_assembly_contains(shapes)
    assert [contains(point) for point in points] == expected
    assert [contains(point) for point in reversed(points)] == list(reversed(expected))


@pytest.mark.parametrize("diameter", [None, 8, 12])
def test_prepared_circle_check_still_rejects_filled_or_wrong_radius_holes(diameter):
    spec = Accessory(
        "edge-x",
        edge_outward=40,
        complete_edge_holes=diameter is not None,
        edge_hole_diameter=diameter,
    )
    shapes = (_accessory_shape(spec), _tile_shape(Tile()))
    for contains in (
        None,
        _prepared_assembly_contains(shapes),
        _whole_assembly_contains(shapes),
    ):
        with pytest.raises(AssertionError):
            _assert_completed_circle(
                shapes,
                (30, 0),
                13,
                minimum_ring_samples=59,
                contains=contains,
            )


@pytest.mark.parametrize(
    "spec,tile_origin,center",
    [
        (Accessory("edge-x", edge_outward=40), (0, 0), (30, 0)),
        (Accessory("edge-y", edge_outward=40), (0, -60), (30, 0)),
        (Accessory("edge-y", edge_outward=40), (0, -60), (0, 0)),
        (Accessory("corner-out", variant=3, complete_edge_holes=True), (0, 0), (60, 60)),
        (Accessory("corner-out", variant=6, complete_edge_holes=True), (0, 0), (0, 0)),
    ],
    ids=["edge-x-middle", "edge-y-middle", "edge-y-end", "corner-out-v3", "corner-out-v6"],
)
def test_local_hole_queries_match_whole_solid_classification(spec, tile_origin, center):
    shapes = (_accessory_shape(spec), _tile_shape(Tile()).moved(Location((*tile_origin, 0))))
    points = [
        Vector(center[0] + radius * cos(angle), center[1] + radius * sin(angle), z)
        for radius in (0, 4.99, 5, 5.01, 6, 7)
        for angle in (index * pi / 8 for index in range(16))
        for z in (0, 1, 6.5, 12, 13)
    ]
    # Prepared whole-solid queries are checked against the legacy classifier above.
    reference = _prepared_assembly_contains(shapes)
    expected = [reference(point) for point in points]
    assert any(expected) and not all(expected)
    contains = _local_assembly_contains(shapes, center, 13)
    assert [contains(point) for point in points] == expected
    with pytest.raises(AssertionError, match="outside the local clip"):
        contains(Vector(center[0] + 7.6, center[1], 6.5))


def test_prepared_queries_are_case_local_and_preserve_pristine_templates(monkeypatch):
    spec = Accessory("edge-y", nx=5, edge_outward=40)
    template = _accessory_template(spec)
    before = (
        template.volume,
        tuple(template.bounding_box().min),
        tuple(template.bounding_box().max),
    )
    first = _accessory_shape(spec)
    second = _accessory_shape(spec).moved(Location((1000, 1000, 0)))
    assert not first.wrapped.IsSame(template.wrapped)
    assert not second.wrapped.IsSame(template.wrapped)
    loaded = []
    bounds_calls = []
    classifier_type = BRepClass3d_SolidClassifier
    shape_type = type(first)
    original_bounds = shape_type.bounding_box

    def classifier(shape):
        result = classifier_type(shape)
        loaded.append(result)
        return result

    def bounds(shape, *args, **kwargs):
        bounds_calls.append(shape)
        return original_bounds(shape, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(f"{__name__}.BRepClass3d_SolidClassifier", classifier)
        context.setattr(shape_type, "bounding_box", bounds)
        query_first = _prepared_assembly_contains((first,))
        query_second = _prepared_assembly_contains((second,))
        for _ in range(3):
            assert query_first(Vector(30, 20, 6.5))
            assert not query_first(Vector(1030, 1020, 6.5))
            assert query_second(Vector(1030, 1020, 6.5))
            assert not query_second(Vector(30, 20, 6.5))
        assert len(loaded) == len(bounds_calls) == 2
        assert loaded[0] is not loaded[1]
    assert (
        template.volume,
        tuple(template.bounding_box().min),
        tuple(template.bounding_box().max),
    ) == before
