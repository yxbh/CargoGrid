"""Export checks for the measured Zeekr rear-panel review package."""

import json
from zipfile import ZipFile

import pytest
from build123d import import_step

from cargo_grid.vehicles.zeekr_7x_rear_review import export_rear_review

pytestmark = pytest.mark.slow


def test_review_export_writes_unsliced_bambu_project_steps_manifest_and_preview(tmp_path):
    output = tmp_path / "review"
    manifest_path = export_rear_review(output)
    manifest = json.loads(manifest_path.read_text())
    assert len(manifest["designs"]) == 19
    assert manifest["kind"] == "zeekr-7x-rear-review"
    assert manifest["export"]["format"] == "bambu-project"
    assert manifest["export"]["sliced"] is False
    assert manifest["physical_fit_verified"] is False
    assert manifest["job_metadata"]["scope"].endswith("test section only")
    assert (
        manifest["job_metadata"]["perimeter_connector_contract"]["high_visibility_note"]
        == "Tile male directions in vehicle assembly: NORTH and EAST."
    )
    assert (
        manifest["job_metadata"]["review_assembly_coordinate_frame"]["from_measured_vehicle"]["y"]
        == "assembly_y = 950 - measured_vehicle_y"
    )
    assert manifest["job_metadata"]["selected_test_placement"]["assembly_tile_field_bounds_mm"] == [
        -540,
        60,
        540,
        300,
    ]
    assert manifest["job_metadata"]["scan_registration"]["lateral_residuals_mm"]["rms"] == 4.120
    cap_metadata = manifest["job_metadata"]["side_caps"]
    assert cap_metadata["accessory_sockets"]["count_per_side"] == 3
    assert cap_metadata["accessory_sockets"]["minimum_measured_material_mm"] == pytest.approx(
        1.621825406947976
    )
    assert cap_metadata["completed_10mm_side_boundary_holes"]["diameter_mm"] == 10
    interior_holes = cap_metadata["completed_10mm_socket_column_holes"]
    assert interior_holes["assembly_centers_mm"] == {
        "west": [[-570, y, 0] for y in range(60, 301, 60)],
        "east": [[570, y, 0] for y in range(60, 301, 60)],
    }
    assert interior_holes["minimum_measured_material_mm"] == pytest.approx(3.946810683739102)
    assert all(
        abs(report["assembly_center"][0]) == 600
        for report in interior_holes["rejected_outer_column_candidates"]
    )
    south_holes = manifest["job_metadata"]["south_contour"]["completed_10mm_tile_boundary_holes"]
    assert south_holes["assembly_centers_mm"] == [[x, 60, 0] for x in range(-540, 541, 30)]
    assert south_holes["minimum_high_shelf_material_behind_cut_mm"] == pytest.approx(5)
    assert south_holes["minimum_adjacent_hole_web_mm"] == pytest.approx(20)
    assert south_holes["minimum_male_tab_root_side_web_mm"] == pytest.approx(6.085786437626908)
    north_holes = manifest["job_metadata"]["south_contour"]["north_female_band_hole_completion"]
    assert north_holes["assembly_centers_mm"] == [[x, 300, 0] for x in range(-540, 541, 30)]
    assert len(cap_metadata["accessory_sockets"]["removed_candidates"]) == 2
    outline_metadata = manifest["job_metadata"]["outline"]
    assert outline_metadata["trace_evidence"]["selected_traced_corner_radius_mm"] == 11.4
    assert outline_metadata["trace_evidence"]["pen_corrected_true_corner_radius_mm"] == 6.4
    correction = outline_metadata["trace_evidence"]["pen_offset_correction"]
    assert "test-printed" in correction["physical_test_result"]
    assert correction["se_d_registration"]["selected_rotation_deg"] == -2
    assert correction["se_d_registration"]["superseded_rotation_deg"] == -3.5
    assert outline_metadata["selected_parameters"]["pen_offset_mm"] == 5
    assert outline_metadata["selected_parameters"]["east_edge_x_mm"] == 602.5
    assert outline_metadata["selected_parameters"]["se_along_edge_shift_mm"] == 0
    assert outline_metadata["selected_parameters"]["centre_depth_mm"] == 365
    assert outline_metadata["superseded_handover_right_wall_stations_y_x_mm"]
    assert outline_metadata["superseded_ne_only_stations_y_x_mm"]
    assert len(list(output.glob("*.step"))) == 21
    assert (output / "assembly-preview.svg").stat().st_size > 1000
    assert (output / "side-cap-orientation.svg").stat().st_size > 1000
    assert (output / "south-contour-scan-comparison.svg").stat().st_size > 1000
    comparison_svg = (output / "south-contour-scan-comparison.svg").read_text()
    assembly_svg = (output / "assembly-preview.svg").read_text()
    assert comparison_svg != assembly_svg
    assert "10 mm completed holes" in assembly_svg
    assert "365 mm centre tape point" in comparison_svg
    assert "blue SE-D -2.0" in comparison_svg
    assert "5 mm pen paths" in comparison_svg
    contour = json.loads((output / "south-contour-scan-comparison.json").read_text())
    assert contour["merged_gauge_cad"]["selected_parameters"] == {
        "traced_north_corner_radius_mm": 11.4,
        "true_north_corner_radius_mm": 6.4,
        "pen_offset_mm": 5,
        "east_edge_x_mm": 602.5,
        "se_along_edge_shift_mm": 0,
        "centre_depth_mm": 365,
    }
    assert contour["merged_gauge_cad"]["analysis"]["centre_depth_status"] == "user tape measurement"
    assert "photogrammetry error" in contour["withdrawn_scan_following_candidate"]["reason"]
    assert "one quadratic" in contour["withdrawn_raised_cosine"]["reason"]
    assert "merged SE trace" in contour["withdrawn_single_parabola"]["reason"]
    orientation = json.loads((output / "side-cap-orientation.json").read_text())
    assert orientation["south_contours_are_mirrored"]
    assert orientation["accessory_socket_contract"]["count_per_side"] == 3
    assert orientation["canonical_boundary_contract"] == {
        "west_tile_boundary": "female",
        "west_cap": "male",
        "east_tile_boundary": "male",
        "east_cap": "female",
        "north_tile_boundary": "male",
        "north_edge": "female",
        "south_tile_boundary": "female",
        "south_ramp": "male",
    }
    west_south = next(
        cap for cap in orientation["caps"] if cap["side"] == "west" and cap["segment"] == "south"
    )
    east_south = next(
        cap for cap in orientation["caps"] if cap["side"] == "east" and cap["segment"] == "south"
    )
    assert west_south["local_bounds_mm"]["minimum"][0] < 0
    assert west_south["assembly_frame_mm"] == [-540, 0, 0]
    assert west_south["assembly_bounds_mm"]["maximum"][0] == pytest.approx(-534)
    assert east_south["local_bounds_mm"]["minimum"][0] == pytest.approx(0, abs=1e-6)
    assert east_south["assembly_frame_mm"] == [540, 0, 0]
    assert east_south["assembly_bounds_mm"]["minimum"][0] == pytest.approx(540, abs=1e-6)
    assembly = import_step(output / "assembly-reference.step")
    side_caps = import_step(output / "side-cap-assembly-reference.step")
    assert assembly.is_valid and len(assembly.solids()) == 19
    assert side_caps.is_valid and len(side_caps.solids()) == 4
    references = json.loads((output / "assembly-reference.json").read_text())
    assert references["references"]["complete_assembly"]["step_roundtrip"] == "passed"
    assert references["references"]["side_caps"]["step_roundtrip"] == "passed"
    assert not (output / "scan-verification.json").exists()
    for design in manifest["designs"]:
        assert design["step_roundtrip"] == "passed"
        assert design["step_volume_delta_mm3"] <= design["step_volume_budget_mm3"]
        assert design["step_bounds_delta_mm"] <= 1e-5
        assert design["mesh"]["closed_oriented_manifold"]
        assert design["compatibility"]["tile_edge_interface_present"]
        if design["parameters"].get("family") == "zeekr-rear-contour-side":
            assert design["compatibility"]["x_attachment_interface_present"]
            assert design["parameters"]["complete_edge_holes"]
            expected_count = 2 if design["parameters"]["segment"] == "north" else 1
            assert design["parameters"]["accessory_socket_count"] == expected_count
    with ZipFile(output / "job.3mf") as archive:
        assert archive.testzip() is None
        assert "Metadata/project_settings.config" in archive.namelist()
        assert "Metadata/model_settings.config" in archive.namelist()
