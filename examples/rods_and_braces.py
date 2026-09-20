"""Four standard divider parts in their Bambu print poses, on one H2D plate."""

from build123d import Compound, Location

from cargo_grid import BuildVolume, Rod, RodBrace
from cargo_grid.catalogue import accessory_design
from cargo_grid.jobs import Job
from cargo_grid.packing import PrintPlacement


def make_job():
    return Job(
        [accessory_design(s) for s in (Rod(120), Rod(240), RodBrace(60), RodBrace(120))],
        BuildVolume(350, 320, 320),
        "part",
        part_gap=10,
        print_placements=[
            PrintPlacement(0, 55, 75, 0),
            PrintPlacement(0, 55, 40, 0),
            PrintPlacement(0, 70, 120, 0),
            PrintPlacement(0, 160, 120, 0),
        ],
        plate_names={0: "Rods and upper braces"},
    )


def gen_step():
    job = make_job()
    parts = []
    for design, placement in zip(job.designs, job.print_placements):
        shape = design.bambu_shape
        lower = shape.bounding_box().min
        parts.append(
            shape.moved(Location((placement.x - lower.X, placement.y - lower.Y, -lower.Z)))
        )
    return Compound(children=parts, label="Rods and upper braces")
