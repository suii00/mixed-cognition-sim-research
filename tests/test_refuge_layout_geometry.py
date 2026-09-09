import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import build_refuge_layout_study as study
from tools import plot_refuge_layout_geometry as geometry


def fixed_configs():
    return {layout: study.build_config("mixed", layout, 60, 6301) for layout in ("edge", "inset")}


class RefugeLayoutGeometryTests(unittest.TestCase):
    def test_exact_cell_bounds_shared_scale_and_24_initial_positions(self):
        result = geometry.geometry_from_configs(fixed_configs())
        edge, inset = result["panels"]
        self.assertEqual(result["shared_color_limits"], [0, 61])
        self.assertEqual(edge["reachable_cell_counts"], {"60": 2120, "120": 2121})
        self.assertEqual(inset["reachable_cell_counts"], {"60": 2121, "120": 2121})
        self.assertEqual(edge["maximum_distance"], 61)
        self.assertEqual(inset["maximum_distance"], 46)
        self.assertEqual(edge["maximum_distance_cells"], [[0, -25]])
        self.assertEqual(inset["maximum_distance_cells"], [[-25, -25], [25, -25]])
        starts = [[{k: a[k] for k in ("agent_id", "position", "model_name")} for a in panel["initial_agents"]]
                  for panel in (edge, inset)]
        self.assertEqual(*starts)
        self.assertEqual(len(starts[0]), 24)
        self.assertEqual(len({tuple(a["position"]) for a in starts[0]}), 24)
        self.assertEqual([a["model_name"] for a in starts[0]], ["qwen"] * 8 + ["llama"] * 8 + ["gemma"] * 8)
        self.assertFalse(result["raw_inputs_read"])
        self.assertFalse(result["llm_observations"])

    def test_rejects_mismatched_seed_and_eligible_set_without_modifying_configs(self):
        configs = fixed_configs()
        original = copy.deepcopy(configs)
        geometry.geometry_from_configs(configs)
        self.assertEqual(configs, original)
        configs["inset"]["simulation"]["seed"] = 6302
        with self.assertRaisesRegex(ValueError, "fixed population"):
            geometry.geometry_from_configs(configs)
        configs = fixed_configs()
        configs["inset"]["scenario"]["initial_eligible_rectangles"][0]["x_min"] = -24
        with self.assertRaisesRegex(ValueError, "eligible cell count"):
            geometry.geometry_from_configs(configs)

    def test_output_must_be_fresh_direct_derived_leaf(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = geometry.PLOT_VERSION + "_20260910T010203Z"
            with mock.patch.object(geometry, "REPO_ROOT", root):
                output = root / "derived" / name
                self.assertEqual(geometry.destination_path(output), output.resolve())
                for invalid in (root / name, root / "runs" / name,
                                root / "derived" / "existing-artifact" / name,
                                root / "derived" / "without-version-and-time"):
                    with self.subTest(path=invalid.name), self.assertRaises(ValueError):
                        geometry.destination_path(invalid)
                output.mkdir(parents=True)
                with self.assertRaisesRegex(ValueError, "collision"):
                    geometry.destination_path(output)

    def test_unsafe_inputs_fail_before_render_or_output_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "derived" / (geometry.PLOT_VERSION + "_20260910T010203Z")
            with mock.patch.object(geometry, "REPO_ROOT", root), \
                    mock.patch.object(geometry, "load_inputs", side_effect=ValueError("unsafe geometry input")), \
                    mock.patch.object(geometry, "render_figure") as render:
                with self.assertRaisesRegex(ValueError, "unsafe"):
                    geometry.create_plot(output)
                render.assert_not_called()
                self.assertFalse(output.exists())

    def test_windows_reparse_point_is_rejected_even_when_not_a_symlink(self):
        candidate = mock.Mock()
        candidate.is_symlink.return_value = False
        candidate.exists.return_value = True
        candidate.lstat.return_value.st_file_attributes = 0x400
        with mock.patch.object(geometry.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, create=True):
            with self.assertRaisesRegex(ValueError, "reparse"):
                geometry.reject_links([candidate])


if __name__ == "__main__":
    unittest.main()
