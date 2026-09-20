"""Derived geometry owned by one export, never cached on mutable public designs."""

from dataclasses import dataclass
from functools import cached_property
from math import cos, radians, sin

import numpy as np
from build123d import Shape
from OCP.BRepTools import BRepTools

from cargo_grid.meshes import checked_mesh


@dataclass(frozen=True)
class Bounds:
    minimum: tuple[float, float, float]
    maximum: tuple[float, float, float]

    @classmethod
    def measure(cls, shape: Shape) -> "Bounds":
        box = shape.bounding_box()
        return cls(tuple(box.min), tuple(box.max))

    @classmethod
    def union(cls, bounds: list["Bounds"]) -> "Bounds":
        if not bounds:
            raise ValueError("cannot measure an empty set of model volumes")
        return cls(
            tuple(min(box.minimum[axis] for box in bounds) for axis in range(3)),
            tuple(max(box.maximum[axis] for box in bounds) for axis in range(3)),
        )

    @property
    def size(self) -> tuple[float, float, float]:
        return tuple(high - low for low, high in zip(self.minimum, self.maximum))

    def rotated_z(self, degrees: int) -> "Bounds":
        if degrees == 0:
            return self
        if degrees != 90:
            raise ValueError("packing rotation must be 0 or 90 degrees")
        x0, y0, z0 = self.minimum
        x1, y1, z1 = self.maximum
        return Bounds((-y1, x0, z0), (-y0, x1, z1))


@dataclass
class PreparedShape:
    shape: Shape

    @cached_property
    def bounds(self) -> Bounds:
        return Bounds.measure(self.shape)

    @cached_property
    def mesh(self) -> tuple[np.ndarray, np.ndarray, dict]:
        # build123d's precise bounds query removes native triangulation.
        # Finish measurements first and retain the checked arrays, not a BRep mesh cache.
        self.bounds
        points, faces, report = checked_mesh(self.shape)
        BRepTools.Clean_s(self.shape.wrapped)
        points.setflags(write=False)
        faces.setflags(write=False)
        return points, faces, report

    def release_mesh(self) -> None:
        self.__dict__.pop("mesh", None)


def rotated_points(
    points: np.ndarray, rotation_x: float, rotation_y: float, rotation_z: int
) -> np.ndarray:
    """Apply the same world-axis X, then Y, then packing Z rotations as CAD."""
    if not (rotation_x or rotation_y or rotation_z):
        return points
    cx, sx = cos(radians(rotation_x)), sin(radians(rotation_x))
    cy, sy = cos(radians(rotation_y)), sin(radians(rotation_y))
    cz, sz = cos(radians(rotation_z)), sin(radians(rotation_z))
    matrix = np.array(
        [
            [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
            [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
            [-sy, cy * sx, cy * cx],
        ]
    )
    return points @ matrix.T
