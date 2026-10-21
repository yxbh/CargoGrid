"""Fixed PETG/PLA Auto support and the female pockets it may occupy."""

from dataclasses import dataclass
from math import hypot
from typing import Literal

from build123d import Face, GeomType, Vector

from cargo_grid.accessories import FAMILIES, Accessory, accessory_datums
from cargo_grid.jobs import Design, Job
from cargo_grid.parameters import DEFAULT_HOLE_DIAMETER_MM, Interface, Tile
from cargo_grid.printers import PRINTERS
from cargo_grid.tiles import hole_placements, tile_joins

GLOBAL_AUTO_SUPPORT_SETTINGS = {"enable_support": "1", "support_type": "normal(auto)"}
# Compatibility name for callers checking an accessory's support recommendation.
OBJECT_AUTO_SUPPORT = GLOBAL_AUTO_SUPPORT_SETTINGS
# Bambu Studio 02.08.02.61: fdm_process_common and the supported H2D/X2D processes.
# Other BBL processes can override these values, particularly the top gap.
BAMBU_SUPPORT_DEFAULTS = {
    "support_on_build_plate_only": "0",
    "support_interface_top_layers": "2",
    "support_top_z_distance": "0.2",
    "support_interface_spacing": "0.5",
    "support_object_xy_distance": "0.35",
    "support_expansion": "0",
}
BAMBU_SUPPORT_DEFAULT_PROCESSES = frozenset(
    profile.process for printer in PRINTERS.values() for profile in printer.profiles.values()
)


@dataclass(frozen=True)
class RoofSupportSettings:
    """Intentional PETG-base/PLA-interface contact, not a chemistry guarantee."""

    @property
    def contact_mode(self) -> Literal["zero-contact"]:
        return "zero-contact"

    @property
    def process_override_keys(self) -> set[str]:
        return self.native_process_override_keys()

    def native_process_override_keys(self, *, process_profile: str | None = None) -> set[str]:
        return set(self.native_settings(process_profile=process_profile))

    def native_settings(self, *, process_profile: str | None = None) -> dict[str, str | list[str]]:
        """Inherit audited defaults only for the common base or supported printer processes."""
        inherit_defaults = (
            process_profile is None or process_profile in BAMBU_SUPPORT_DEFAULT_PROCESSES
        )
        settings: dict[str, str | list[str]] = {
            "enable_support": "1",
            "support_type": "normal(auto)",
            "support_interface_filament": "2",
            "support_interface_top_layers": "2",
            "support_top_z_distance": "0",
            "support_interface_spacing": "0",
            "independent_support_layer_height": "0",
            "support_object_xy_distance": "0.4",
        }
        if not inherit_defaults:
            settings["support_on_build_plate_only"] = "0"
        return {
            key: value
            for key, value in settings.items()
            if not inherit_defaults or BAMBU_SUPPORT_DEFAULTS.get(key) != value
        }


RoofSide = Literal["west", "south", "north", "east"]
# A female tool at this Z angle cuts its pocket into the part from the side named here.
_SIDE_BY_ANGLE = {0: "south", 270: "west", 180: "north", 90: "east"}
_SIDE_ORDER = ("west", "south", "north", "east")


@dataclass(frozen=True)
class FemaleRoof:
    """One retained original female pocket roof and the round openings beneath it."""

    side: RoofSide
    index: int
    center: tuple[float, float]
    faces: tuple[Face, ...]
    hole_radius: float
    occupied_openings: tuple[tuple[float, float], ...]


def _tile_spec(design: Design) -> Tile:
    parameters = dict(design.parameters)
    parameters["interface"] = Interface(**parameters["interface"])
    return Tile(**parameters)


def _design_interface(design: Design) -> Interface | None:
    data = design.parameters.get("interface")
    return Interface(**data) if isinstance(data, dict) else None


def _female_joins(design: Design) -> list[dict]:
    """Female tile-edge joins from the same plans and datums the geometry is built from."""
    interface = _design_interface(design)
    if interface is None:
        return []
    family = design.parameters.get("family")
    if design.mating_datums.get("joins") is not None:
        joins = design.mating_datums["joins"]
    elif family is None:
        joins = tile_joins(_tile_spec(design))
    elif family in FAMILIES:
        fields = {
            key: value
            for key, value in design.parameters.items()
            if key in Accessory.__dataclass_fields__
        }
        fields["interface"] = interface
        # Identity parameters leave out a False completion flag; None would mean automatic.
        fields.setdefault("complete_edge_holes", False)
        joins = accessory_datums(Accessory(**fields))["joins"]
    else:
        joins = []
    return [
        join
        for join in joins
        if join["sex"] == "female" and join.get("interface", "tile-dovetail") == "tile-dovetail"
    ]


def _hole_openings(design: Design) -> tuple[float, list[tuple[float, float]]]:
    parameters = design.parameters
    if "family" not in parameters:
        spec = _tile_spec(design)
        placements = hole_placements(spec)
        radius = spec.hole_diameter / 2 if spec.hole_diameter is not None else 0.0
        return radius, [(h.x, h.y) for h in placements if h.accepted]
    diameter = (
        parameters.get("hole_diameter")
        or parameters.get("edge_hole_diameter")
        or DEFAULT_HOLE_DIAMETER_MM
    )
    return diameter / 2, [(h["x"], h["y"]) for h in design.holes if h["accepted"]]


def female_roofs(design: Design) -> list[FemaleRoof]:
    """Locate the planar ceiling of every retained original female pocket on one design."""
    joins = _female_joins(design)
    if not joins:
        return []
    interface = _design_interface(design)
    ceiling = interface.female_opening_height
    half_wall = 25 * interface.unit_scale
    faces = [
        face
        for face in design.shape.faces()
        if face.geom_type == GeomType.PLANE
        and abs(face.bounding_box().min.Z - ceiling) < 1e-5
        and abs(face.bounding_box().max.Z - ceiling) < 1e-5
        and face.normal_at().Z < -0.99
    ]
    radius, openings = _hole_openings(design)
    by_side: dict[str, list[tuple[dict, float]]] = {}
    for join in joins:
        side = _SIDE_BY_ANGLE.get(round(join["angle"]) % 360)
        if side is None or abs(join["angle"] - round(join["angle"])) > 1e-9:
            raise ValueError(f"{design.name}: unsupported female joint angle {join['angle']}")
        x, y = join["position"][:2]
        by_side.setdefault(side, []).append((join, y if side in ("west", "east") else x))
    roofs = []
    for side in _SIDE_ORDER:
        for index, (join, coordinate) in enumerate(
            sorted(by_side.get(side, []), key=lambda item: item[1]), start=1
        ):
            x, y = join["position"][:2]
            depth = join["depth"]
            across_y = side in ("west", "east")
            sign = 1 if side in ("west", "south") else -1
            group = []
            for face in faces:
                bounds = face.bounding_box()
                if across_y:
                    inward = (bounds.min.X - x, bounds.max.X - x)
                    along = face.center().Y - y
                else:
                    inward = (bounds.min.Y - y, bounds.max.Y - y)
                    along = face.center().X - x
                low, high = sorted(sign * value for value in inward)
                if low >= -1e-5 and high <= depth + 1e-5 and abs(along) <= half_wall:
                    group.append(face)
            if not group:
                raise ValueError(
                    f"{design.name}: expected a planar {side} pocket roof at female join {index}"
                )
            group.sort(key=lambda face: (face.center().Y, face.center().X))
            at_center = any(hypot(hx - x, hy - y) < 1e-6 for hx, hy in openings)
            occupied = tuple(
                (hx, hy)
                for hx, hy in openings
                if radius
                and min(face.distance_to(Vector(hx, hy, ceiling)) for face in group)
                <= radius + 1e-6
            )
            roofs.append(
                FemaleRoof(
                    side,
                    index,
                    (x, y),
                    tuple(group),
                    radius if at_center else 0.0,
                    occupied,
                )
            )
    return roofs


def global_auto_support_reason(design: Design) -> str:
    """Why a design participates in global Auto support, without predicting sliced support."""
    if female_roofs(design):
        return "retained original female pocket roof"
    if design.bambu_object_settings == GLOBAL_AUTO_SUPPORT_SETTINGS:
        return "normal Auto support already recommended for this accessory"
    return "global normal Auto support; the slicer detects overhangs"


def effective_object_settings(design: Design, settings: RoofSupportSettings | None) -> dict:
    """Per-object Bambu settings written for one design in a roof-support job."""
    if settings is not None:
        return {
            key: value
            for key, value in design.bambu_object_settings.items()
            if GLOBAL_AUTO_SUPPORT_SETTINGS.get(key) != value
        }
    return dict(design.bambu_object_settings)


def validate_roof_job(job: Job, settings: RoofSupportSettings, layer_height: float) -> None:
    if job.kind not in ("part", "layout", "catalogue", "extras"):
        raise ValueError("Auto roof support requires a part, layout, catalogue or extras job")
    found_female = False
    for design in job.designs:
        interface = _design_interface(design)
        if interface is None:
            continue
        if not _female_joins(design):
            continue
        if 2 * layer_height >= interface.female_opening_height:
            raise ValueError("roof support interface consumes the entire pocket height")
        if layer_height >= interface.body_height - interface.female_opening_height:
            raise ValueError("layer height is too large for the pocket roof")
        found_female = True
    if found_female:
        return
    # Auto mode also accepts accessories that already request object support on their own.
    if any(d.bambu_object_settings == GLOBAL_AUTO_SUPPORT_SETTINGS for d in job.designs):
        return
    raise ValueError(
        "this job has no retained original female pocket roofs to support; "
        "add a tile with a west or south female edge, a female edge, corner or ramp"
    )
