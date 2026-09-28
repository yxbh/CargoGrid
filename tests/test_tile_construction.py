"""Construction work scales with unique tools, without sharing mutable topology."""

from collections import Counter

import pytest
from build123d import Location, Shape

from cargo_grid import Interface, Tile, interfaces, tiles


@pytest.mark.parametrize("interface", [Interface(), Interface(30, 8), Interface(height=18)])
def test_joint_tools_are_built_once_per_kind_and_isolated_before_booleans(monkeypatch, interface):
    original_tool = tiles.tile_join_tool
    original_boolean = Shape._bool_op
    templates, calls = [], Counter()

    def tool(interface, *, depth, male):
        calls[depth, male] += 1
        shape = original_tool(interface, depth=depth, male=male)
        templates.append((shape, shape.volume))
        return shape

    def boolean(shape, args, tools, operation):
        args, tools = list(args), list(tools)
        assert all(
            not candidate.wrapped.IsPartner(template.wrapped)
            for candidate in [*args, *tools]
            for template, _ in templates
        )
        return original_boolean(shape, args, tools, operation)

    monkeypatch.setattr(tiles, "tile_join_tool", tool)
    monkeypatch.setattr(Shape, "_bool_op", boolean)
    result = tiles.make_tile(Tile(2, 3, interface, hole_diameter=None))
    assert calls == {
        (interface.male_join_depth, True): 1,
        (interface.male_join_depth, False): 1,
        (interface.female_join_depth, False): 1,
    }
    assert result.is_valid and len(result.solids()) == 1 and result.volume > 0
    assert all(shape.volume == volume for shape, volume in templates)


def test_all_standard_socket_bores_share_one_boolean_without_changing_their_spacing(monkeypatch):
    original = Shape.cut
    socket_cuts = []

    def cut(shape, *tools):
        sockets = []
        for tool in tools:
            box = tool.bounding_box()
            if tuple(box.size) == pytest.approx((44.55634918610404, 44.55634918610404, 15)):
                sockets.append(tuple(box.center()))
        if sockets:
            socket_cuts.append(sockets)
        return original(shape, *tools)

    monkeypatch.setattr(Shape, "cut", cut)
    result = tiles.make_tile(Tile(2, 3, hole_diameter=None))
    assert socket_cuts == [[(x, y, 6.5) for x in (30, 90) for y in (30, 90, 150)]]
    assert result.is_valid and len(result.solids()) == 1


@pytest.mark.parametrize(
    "interface", [Interface(), Interface(30, 8), Interface(joint_style="full-height")]
)
@pytest.mark.parametrize("male", [True, False])
def test_cached_joint_tool_templates_hand_out_independent_identical_copies(interface, male):
    depth = interface.male_join_depth if male else interface.female_join_depth
    fresh = interfaces._tile_join_template.__wrapped__(interface, depth, male)
    first = interfaces.tile_join_tool(interface, male=male)
    first.move(Location((5, 7, 11)))
    second = interfaces.tile_join_tool(interface, male=male)
    assert not first.wrapped.IsPartner(second.wrapped)
    assert second.is_valid and len(second.solids()) == 1
    assert second.volume == fresh.volume and second.area == fresh.area
    assert tuple(second.bounding_box().min) == tuple(fresh.bounding_box().min)
    assert tuple(second.bounding_box().max) == tuple(fresh.bounding_box().max)
    assert len(second.faces()) == len(fresh.faces())
