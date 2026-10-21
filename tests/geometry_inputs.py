"""Opt-in fixture adapters, separate from the public construction API."""

from functools import wraps
from importlib import import_module
from inspect import signature

from input_cache import ROOT, InputStore

COMMON = ("parameters", "interfaces", "meshes")
ACCESSORY_SOURCES = (*COMMON, "accessories", "rods", "pull_handles", "tiles")
FAMILIES = {
    "tile": (*COMMON, "tiles"),
    "accessory": ACCESSORY_SOURCES,
    "rod": (*COMMON, "rods"),
    "handle": (*COMMON, "pull_handles"),
    "stop": (*ACCESSORY_SOURCES, "adjustable_stop"),
    "contour": (*ACCESSORY_SOURCES, "vehicles/zeekr_7x_contours"),
}
FACTORIES = (
    ("tiles", "make_tile", "tile", False),
    ("accessories", "make_accessory", "accessory", False),
    ("rods", "make_rod", "rod", False),
    ("rods", "make_rod_brace", "rod", False),
    ("pull_handles", "make_pull_handle", "handle", False),
    ("adjustable_stop", "_make_base", "stop", False),
    ("adjustable_stop", "_make_pusher", "stop", False),
    ("adjustable_stop", "_make_keeper", "stop", False),
    ("adjustable_stop", "_make_pusher_body", "stop", False),
    ("adjustable_stop", "make_adjustable_stop_prong_lock_clip", "stop", False),
    ("vehicles.zeekr_7x_contours", "_side_design", "contour", True),
    ("vehicles.zeekr_7x_contours", "_south_design", "contour", True),
)
CONSUMERS = (
    "",
    "tiles",
    "accessories",
    "rods",
    "jobs",
    "catalogue",
    "cli",
    "plates",
    "prepared",
    "pull_handles",
    "pull_handle_set",
    "adjustable_stop",
    "adjustable_stop_export",
    "vehicles.zeekr_7x",
    "vehicles.zeekr_7x_contours",
)


class GeometryInputs:
    def __init__(self, store: InputStore):
        self.store = store
        self._constructing = False

    def _adapt(self, create, family, design):
        parameters = signature(create)
        sources = [ROOT / "src/cargo_grid" / f"{name}.py" for name in FAMILIES[family]]
        sources.append(ROOT / "tests/geometry_inputs.py")
        if design:
            sources.append(ROOT / "src/cargo_grid/jobs.py")
        factory_name = f"{create.__module__}.{create.__name__}"

        @wraps(create)
        def cached(*args, **kwargs):
            if self._constructing or not self.store.enabled:
                return create(*args, **kwargs)
            bound = parameters.bind(*args, **kwargs)
            bound.apply_defaults()

            def build():
                self._constructing = True
                try:
                    return create(*args, **kwargs)
                finally:
                    self._constructing = False

            load = self.store.design if design else self.store.geometry
            return load(factory_name, bound.arguments, sources, build)

        return cached

    def install(self, monkeypatch, test_module):
        modules = [import_module("cargo_grid" + (f".{name}" if name else "")) for name in CONSUMERS]
        modules.extend((test_module, import_module("zeekr_assembly")))
        replacements = {}
        for name, factory, family, design in FACTORIES:
            original = getattr(import_module(f"cargo_grid.{name}"), factory)
            replacements[id(original)] = self._adapt(original, family, design)
        from cargo_grid.meshes import checked_mesh

        replacements[id(checked_mesh)] = self.store.checked_mesh
        for module in modules:
            for name, value in tuple(vars(module).items()):
                replacement = replacements.get(id(value))
                if replacement is not None:
                    monkeypatch.setattr(module, name, replacement)
