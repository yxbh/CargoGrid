"""Documentation coverage and assets without requiring a renderer or Pillow."""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from cargo_grid.catalogue import accessory_variants

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def gallery():
    spec = importlib.util.spec_from_file_location("docs_gallery", ROOT / "tools/render_docs.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_gallery_covers_every_bounded_catalogue_variant_once(gallery):
    items = gallery.inventory()
    assert [item.spec for item in items] == accessory_variants(gallery.BUILD)
    assert len(items) == len({item.key for item in items}) == 65
    assert {item.key for item in items if item.spec.family in ("rod", "rod-brace")} == {
        "rod-120",
        "rod-240",
        "rod-brace-60-d10",
        "rod-brace-120-d10",
    }
    assert {item.key for item in items if item.spec.family == "ramp"} == {
        *(f"ramp-{width}" for width in range(1, 6)),
        *(f"ramp-male-{width}" for width in range(1, 6)),
    }
    rendered = [
        item.key for family in gallery.FAMILIES for item in items if item.spec.family == family
    ]
    assert sorted(rendered) == sorted(item.key for item in items)
    assert set(gallery.FAMILIES) == {item.spec.family for item in items}


def test_documented_assets_are_bounded_and_inventory_is_complete(gallery):
    assets = gallery.verify_assets()
    assert set(assets) == {
        *gallery.IMAGE_NAMES,
        *(f"attachments/{item.key}.png" for item in gallery.inventory()),
    }
    assert sum(asset["bytes"] for asset in assets.values()) <= 2_000_000


def test_thumbnail_manifest_has_unique_rows_and_family_scale(gallery):
    manifest = json.loads((ROOT / "docs/images/attachments/manifest.json").read_text())
    assert manifest["geometry_commit"] == gallery.GEOMETRY_REVISION
    assert manifest["workbench_commit"] == gallery.WORKBENCH_REVISION
    entries = manifest["items"]
    for field in ("file", "key", "public_name", "alt", "sha256"):
        assert len({entry[field] for entry in entries}) == 65
    for family in gallery.FAMILIES:
        rows = [entry for entry in entries if entry["family"] == family]
        assert len({entry["pixels_per_mm"] for entry in rows}) == 1
    assert all(entry["dimensions"] == [480, 300] for entry in entries)
    assert "docs/attachments.md" in (ROOT / "README.md").read_text()
    ramps = [entry for entry in entries if entry["family"] == "ramp"]
    assert len(ramps) == 10
    assert all("provenance" in entry for entry in ramps)
    ramp_overview = next(
        entry for entry in manifest["overview_images"] if entry["file"] == "images/ramps.png"
    )
    assert set(ramp_overview["items"]) == {entry["key"] for entry in ramps}
    assert all(entry["provenance"] == ramp_overview["provenance"] for entry in ramps)
    assert ramps[0]["provenance"]["generator_commit"] != manifest["geometry_commit"]
    assert "not a full-gallery" in manifest["provenance_scope"]
    round_parts = [entry for entry in entries if entry["family"] in ("rod", "rod-brace")]
    assert len(round_parts) == 4
    rod_overview = next(
        entry
        for entry in manifest["overview_images"]
        if entry["file"] == "images/rods-and-braces.png"
    )
    assert set(rod_overview["items"]) == {entry["key"] for entry in round_parts}
    assert all(entry["provenance"] == rod_overview["provenance"] for entry in round_parts)
    assert round_parts[0]["provenance"]["generator_commit"] != manifest["geometry_commit"]
    assert all("original joints" not in entry["alt"] for entry in round_parts)


def test_bracket_family_context_is_separate_from_the_part_inventory(gallery):
    assert [item.key for item in gallery.bracket_assembly_items()] == [
        "bracket-context-base1x2-wall1x2",
        "bracket-context-base2x1-wall2x1",
        "bracket-context-base2x2-wall2x2",
        "bracket-context-base1x1-wall1x2",
        "bracket-context-base2x1-wall2x2",
    ]
    assert len(gallery.documentation_shape("bracket-context-base1x1-wall1x2").solids()) == 3
    assert len(gallery.documentation_shape("vertical-tile-bracket-1x2").solids()) == 1
    assert len(gallery.documentation_shape("vertical-tile-bracket-base2x1-wall2x2").solids()) == 1
    assert len(gallery.documentation_shape("ramp-3").solids()) == 1
    male = gallery.documentation_shape("ramp-male-3")
    assert len(male.solids()) == 1
    assert tuple(male.bounding_box().size) == pytest.approx((180, 56, 13), abs=1e-5)


def test_provenance_hashes_can_wrap_without_changing_their_text(gallery):
    revision = gallery.GEOMETRY_REVISION
    tag = gallery.revision_tag(revision)
    assert tag.replace("<code>", "").replace("</code>", "").replace("<wbr>", "") == revision
    assert all(
        len(piece) <= 8
        for piece in tag.removeprefix("<code>").removesuffix("</code>").split("<wbr>")
    )


def test_thumbnail_checks_reject_a_wrong_hash(gallery, tmp_path, monkeypatch):
    import shutil

    docs = tmp_path / "docs"
    shutil.copytree(ROOT / "docs/images", docs / "images")
    shutil.copy2(ROOT / "docs/attachments.md", docs / "attachments.md")
    path = docs / "images/attachments/manifest.json"
    manifest = json.loads(path.read_text())
    manifest["items"][0]["sha256"] = "0" * 64
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(gallery, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="hash mismatch"):
        gallery.verify_assets()


@pytest.mark.parametrize(
    "document", ["README.md", "AGENTS.md", "docs/attachments.md", "docs/geometry.md"]
)
def test_document_links_resolve_without_private_or_remote_paths(document):
    path = ROOT / document
    text = path.read_text()
    assert not re.search(r"/Users/|/Applications/|OneDrive|copilot-worktrees", text)
    targets = re.findall(r"\]\(([^)]+)\)", text) + re.findall(r'(?:src|href)="([^"]+)"', text)
    for target in targets:
        target = target.split("#", 1)[0]
        if not target:
            continue
        assert "://" not in target, target
        assert not Path(target).is_absolute(), target
        assert (path.parent / target).exists(), target


def test_only_named_docs_images_are_distribution_exceptions(gallery):
    spec = importlib.util.spec_from_file_location(
        "distribution_gate", ROOT / "tools/check_distributions.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DOCUMENTATION_IMAGES == {
        *(f"docs/images/{name}" for name in gallery.IMAGE_NAMES),
        *(f"docs/images/attachments/{item.key}.png" for item in gallery.inventory()),
    }
    for name in ("docs/images/unapproved.png", "outputs/render.png", "docs/images/hero.step"):
        with pytest.raises(ValueError):
            module.check_path(name)


def test_gallery_parameters_do_not_add_new_defaults_to_unrelated_provenance(gallery):
    from cargo_grid.accessories import Accessory
    from cargo_grid.rods import Rod, RodBrace

    assert "ramp_join" not in gallery.item_parameters(Accessory("plate"))
    assert "ramp_join" not in gallery.item_parameters(Accessory("ramp"))
    assert gallery.item_parameters(Accessory("ramp", ramp_join="male"))["ramp_join"] == "male"
    assert gallery.item_parameters(Rod()) == {
        "above_mat_height_mm": 120,
        "peg_diameter_mm": 10,
        "tile_thickness_mm": 13,
    }
    assert gallery.item_parameters(RodBrace()) == {
        "center_spacing_mm": 60,
        "bore_diameter_mm": 10,
    }


@pytest.mark.parametrize(
    ("key", "expected_size"),
    [
        ("rod-120", (18, 18, 132)),
        ("rod-240", (18, 18, 252)),
        ("rod-brace-60-d10", (78, 18, 6.4)),
        ("rod-brace-120-d10", (138, 18, 6.4)),
    ],
)
def test_round_gallery_parts_use_maintained_source_orientation(gallery, key, expected_size):
    shape = gallery.documentation_shape(key)
    assert shape.is_valid and shape.volume > 0 and len(shape.solids()) == 1
    assert tuple(shape.bounding_box().size) == pytest.approx(expected_size, abs=1e-5)


def test_incremental_ramp_composition_retains_unrelated_assets_and_provenance(
    gallery, tmp_path, monkeypatch
):
    import copy
    import shutil

    shutil.copytree(ROOT / "docs/images", tmp_path / "docs/images")
    path = tmp_path / "docs/images/attachments/manifest.json"
    baseline = json.loads(path.read_text())
    retained = {
        entry["file"]: (tmp_path / "docs" / entry["file"]).read_bytes()
        for entry in [*baseline["items"], *baseline["overview_images"]]
        if entry.get("family") != "ramp" and entry["file"] != "images/ramps.png"
    }
    provenance = {
        "generator_commit": "1" * 40,
        "generator_tree": "2" * 40,
        "workbench_commit": "3" * 40,
        "recipe_sha256": "4" * 64,
    }
    calls = []
    monkeypatch.setattr(gallery, "ROOT", tmp_path)
    monkeypatch.setattr(gallery, "verify_geometry_source", lambda revision: calls.append(revision))
    monkeypatch.setattr(gallery, "verify_assets", lambda: {})

    def thumbnails(work, supplied, *, families, write_manifest):
        assert supplied == provenance and families == {"ramp"} and not write_manifest
        rows = []
        for item in gallery.inventory():
            if item.spec.family != "ramp":
                continue
            entry = copy.deepcopy(
                next(row for row in baseline["items"] if row["key"] == f"ramp-{item.spec.nx}")
            )
            entry.update(
                key=item.key,
                parameters=gallery.item_parameters(item.spec),
                file=f"images/attachments/{item.key}.png",
                provenance=provenance,
            )
            rows.append(entry)
        return rows

    def overview(work, items, filename, title, columns):
        assert filename == "ramps.png" and columns == 3
        assert len(items) == 10 and all(item.spec.family == "ramp" for item in items)
        return {"file": "docs/images/ramps.png", "items": [item.key for item in items]}

    monkeypatch.setattr(gallery, "thumbnail_entries", thumbnails)
    monkeypatch.setattr(gallery, "composite", overview)
    work = tmp_path / "outputs/ramp-update"
    work.mkdir(parents=True)
    gallery.compose_ramps(work, provenance)
    assert calls == [provenance["generator_commit"]]
    updated = json.loads(path.read_text())
    for key in (
        "geometry_commit",
        "workbench_commit",
        "catalogue_build_mm",
        "camera",
        "source_render_recipe_sha256",
    ):
        assert updated[key] == baseline[key]
    assert [row for row in updated["items"] if row["family"] != "ramp"] == [
        row for row in baseline["items"] if row["family"] != "ramp"
    ]
    assert [row for row in updated["overview_images"] if row["file"] != "images/ramps.png"] == [
        row for row in baseline["overview_images"] if row["file"] != "images/ramps.png"
    ]
    assert all((tmp_path / "docs" / name).read_bytes() == data for name, data in retained.items())
    report = json.loads((work / "render-report.json").read_text())
    assert len(report["updated_keys"]) == 10
    assert len(report["sheets"]) == 1
    assert (
        "does not describe a new full-gallery render"
        in (tmp_path / "docs/attachments.md").read_text()
    )


def test_incremental_ramp_composition_rejects_changed_retained_picture(
    gallery, tmp_path, monkeypatch
):
    import shutil

    shutil.copytree(ROOT / "docs/images", tmp_path / "docs/images")
    (tmp_path / "docs/images/hero.png").write_bytes(b"changed")
    monkeypatch.setattr(gallery, "ROOT", tmp_path)
    monkeypatch.setattr(gallery, "verify_geometry_source", lambda revision: None)
    with pytest.raises(ValueError, match="Retained image hash mismatch"):
        gallery.compose_ramps(tmp_path, {"generator_commit": "1" * 40})


@pytest.mark.parametrize("new_family", [False, True])
def test_incremental_rod_composition_preserves_all_other_images(
    gallery, tmp_path, monkeypatch, new_family
):
    import copy
    import shutil

    shutil.copytree(ROOT / "docs/images", tmp_path / "docs/images")
    path = tmp_path / "docs/images/attachments/manifest.json"
    baseline = json.loads(path.read_text())
    families = {"rod", "rod-brace"}
    filename = "images/rods-and-braces.png"
    round_entries = [entry for entry in baseline["items"] if entry["family"] in families]
    if new_family:
        baseline["items"] = [
            entry for entry in baseline["items"] if entry["family"] not in families
        ]
        baseline["overview_images"] = [
            entry for entry in baseline["overview_images"] if entry["file"] != filename
        ]
        path.write_text(json.dumps(baseline))
    retained = {
        entry["file"]: (tmp_path / "docs" / entry["file"]).read_bytes()
        for entry in [*baseline["items"], *baseline["overview_images"]]
        if entry.get("family") not in families and entry["file"] != filename
    }
    provenance = {
        "generator_commit": "1" * 40,
        "generator_tree": "2" * 40,
        "workbench_commit": "3" * 40,
        "recipe_sha256": "4" * 64,
    }
    calls = []
    monkeypatch.setattr(gallery, "ROOT", tmp_path)
    monkeypatch.setattr(gallery, "verify_geometry_source", lambda revision: calls.append(revision))
    monkeypatch.setattr(gallery, "verify_assets", lambda: {})

    def thumbnails(work, supplied, *, families, write_manifest):
        assert supplied == provenance and families == {"rod", "rod-brace"} and not write_manifest
        rows = copy.deepcopy(round_entries)
        for row in rows:
            row["provenance"] = provenance
        return rows

    def overview(work, items, filename, title, columns, *, subtitle, family_scales):
        assert filename == "rods-and-braces.png" and columns == 2
        assert len(items) == 4 and {item.spec.family for item in items} == families
        assert family_scales and subtitle.isascii() and "original joints" not in subtitle
        assert all(item.detail.isascii() for item in items)
        return {"file": "docs/images/rods-and-braces.png", "items": [item.key for item in items]}

    monkeypatch.setattr(gallery, "thumbnail_entries", thumbnails)
    monkeypatch.setattr(gallery, "composite", overview)
    work = tmp_path / "outputs/rod-update"
    work.mkdir(parents=True)
    gallery.compose_rods(work, provenance)
    assert calls == [provenance["generator_commit"]]
    updated = json.loads(path.read_text())
    assert {
        key: value for key, value in updated.items() if key not in ("items", "overview_images")
    } == {key: value for key, value in baseline.items() if key not in ("items", "overview_images")}
    assert [row for row in updated["items"] if row["family"] not in families] == [
        row for row in baseline["items"] if row["family"] not in families
    ]
    assert [row for row in updated["overview_images"] if row["file"] != filename] == [
        row for row in baseline["overview_images"] if row["file"] != filename
    ]
    assert all((tmp_path / "docs" / name).read_bytes() == data for name, data in retained.items())
    report = json.loads((work / "render-report.json").read_text())
    assert len(report["updated_keys"]) == 4 and len(report["sheets"]) == 1


def test_incremental_rod_composition_rejects_changed_retained_image(gallery, tmp_path, monkeypatch):
    import shutil

    shutil.copytree(ROOT / "docs/images", tmp_path / "docs/images")
    (tmp_path / "docs/images/hero.png").write_bytes(b"changed")
    monkeypatch.setattr(gallery, "ROOT", tmp_path)
    monkeypatch.setattr(gallery, "verify_geometry_source", lambda revision: None)
    with pytest.raises(ValueError, match="Retained image hash mismatch"):
        gallery.compose_rods(tmp_path, {"generator_commit": "1" * 40})
