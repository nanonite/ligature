"""Contact sheet generator (chainlink #32).

plan.md §16.3. Built on real workspaces on disk, reusing the exact
fixture helpers chainlink #34's own feature-ledger test suite already
established (concept specs, witness specs, canonical results, a real
witness_backend dispatch via the stand-in producer) -- the ledger and
the contact sheet are two projections over the identical underlying
state, so there is no reason for this file to invent a second
workspace builder.
"""
import base64
import io
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import generate_contact_sheet as gcs  # noqa: E402
import witness_renderer  # noqa: E402
from test_generate_feature_ledger import Workspace  # noqa: E402
from test_generate_feature_ledger import descriptor_with  # noqa: E402
from test_generate_feature_ledger import happy_path_witness_and_rendering  # noqa: E402
from test_gate_g19 import PRODUCER_BACKEND, run_producer  # noqa: E402
from test_validate_witness import witness_spec  # noqa: E402


class GateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ws = Workspace(self.root)
        self.descriptor = descriptor_with("crates/scheduler", backend_command=PRODUCER_BACKEND)

    def tearDown(self):
        self._tmp.cleanup()

    def generate(self, descriptor: dict | None = None) -> str:
        svg, _ = gcs.generate_contact_sheet(self.root, descriptor or self.descriptor)
        return svg


class BannerTest(GateTestCase):
    def test_the_banner_text_is_exact(self):
        svg = self.generate()
        self.assertIn("∃-witness evidence — not verification.", svg)


class GreenWitnessBesideUnsupportedAssuranceTest(GateTestCase):
    def test_a_fully_clean_panel_shows_green_witness_and_a_neutral_unsupported_assurance_box(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        svg = self.generate()

        panel = svg.split('<g transform="translate(12,52)">', 1)[1]
        panel = panel.split("</g>", 1)[0]

        # The traffic-light columns are genuinely green: a real pass.
        self.assertIn(f'fill="{gcs.GREEN}"', panel)
        self.assertIn("witness: present", panel)
        self.assertIn("implementation_observed: true", panel)
        self.assertIn("determinism: pass", panel)
        self.assertIn("degeneracy: ok", panel)

        # assurance is drawn, present, and legible -- but never in the
        # traffic-light palette, so it cannot be scanned as "also green".
        self.assertIn("assurance: unsupported", panel)
        assurance_fragment = panel.split("assurance: unsupported", 1)[0].rsplit("<rect", 1)[-1]
        self.assertIn(f'fill="{gcs.AXIS_FILL}"', assurance_fragment)
        self.assertNotIn(f'fill="{gcs.GREEN}"', assurance_fragment)
        self.assertNotIn(f'fill="{gcs.AMBER}"', assurance_fragment)
        self.assertNotIn(f'fill="{gcs.RED}"', assurance_fragment)


class AssuranceClosureIndependenceTest(GateTestCase):
    def test_closure_kind_is_shown_in_the_same_neutral_palette_regardless_of_witness_state(self):
        self.ws.write_concept_spec()
        # No witness at all: witness_present is false, yet closure_kind
        # must still be reported, in the identical non-green box style.
        svg = self.generate()
        self.assertIn("cluster: scheduling / closure: n/a", svg)
        fragment = svg.split("cluster: scheduling / closure: n/a", 1)[0].rsplit("<rect", 1)[-1]
        self.assertIn(f'fill="{gcs.AXIS_FILL}"', fragment)
        self.assertNotIn(f'fill="{gcs.GREEN}"', fragment)


class MissingStatesTest(GateTestCase):
    def test_missing_rendering_shows_an_explicit_placeholder_and_keeps_the_panel(self):
        self.ws.write_concept_spec()
        baseline = run_producer()
        self.ws.write_witness(witness_spec(value_hash=baseline["value_hash"]))
        # Deliberately no write_rendering(): G18 sees no rendering file,
        # so witness_present is false even though the spec is valid.
        svg = self.generate()
        self.assertIn("TaskQueue.load_factor", svg)
        self.assertIn("rendering unavailable", svg)
        self.assertIn("witness: absent", svg)
        self.assertNotIn("data:image/svg+xml;base64,", svg)

    def test_a_failed_regeneration_still_shows_the_existing_rendering(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        broken_backend = descriptor_with(
            "crates/scheduler", backend_command=f"{sys.executable} /no/such/producer.py"
        )
        svg = self.generate(broken_backend)
        self.assertIn("determinism: fail", svg)
        self.assertIn("degeneracy: not-checked", svg)
        # witness_present is still true (spec + rendering both exist on
        # disk) -- a failed regeneration must not hide what IS there.
        self.assertIn("witness: present", svg)
        self.assertIn("data:image/svg+xml;base64,", svg)

    def test_no_backend_configured_still_shows_the_existing_rendering(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        no_backend = descriptor_with("crates/scheduler")
        svg = self.generate(no_backend)
        self.assertIn("determinism: not-checked", svg)
        self.assertIn("degeneracy: not-checked", svg)
        self.assertIn("witness: present", svg)
        self.assertIn("data:image/svg+xml;base64,", svg)


class MixedPanelsTest(GateTestCase):
    def test_one_passing_and_one_failing_feature_both_appear_correctly_disposed(self):
        self.ws.write_concept_spec(extra_queries=(
            {"english": "How deep is the queue right now?", "rust_sig": "fn depth(&self) -> usize",
             "pure": True, "witness_required": True},
        ))
        happy_path_witness_and_rendering(self.ws)

        # A declared hash computed against a DIFFERENT --ready value
        # than the real backend (self.descriptor, default ready=3) will
        # ever regenerate -- the identical "drifted producer" technique
        # test_generate_feature_ledger.py's own G19StateTest uses,
        # scoped to just one of the two features under one shared
        # backend so this stays an honest determinism failure, not a
        # confound with degeneracy.
        drifted = run_producer("--ready", "5", witness_id="W-TQ-DEPTH", query="depth")
        depth_spec = witness_spec(value_hash=drifted["value_hash"])
        depth_spec["witness_id"] = "W-TQ-DEPTH"
        depth_spec["query"] = "depth"
        depth_spec["output"]["path"] = "docs/witnesses/task_queue.depth.svg"
        self.ws.write_witness(depth_spec)
        self.ws.write_rendering("docs/witnesses/task_queue.depth.svg")

        svg = self.generate()
        self.assertIn("TaskQueue.load_factor", svg)
        self.assertIn("TaskQueue.depth", svg)
        self.assertIn("determinism: pass", svg)
        self.assertIn("determinism: fail", svg)
        # Alphabetical (concept, query) order: depth before load_factor.
        self.assertLess(svg.index("TaskQueue.depth"), svg.index("TaskQueue.load_factor"))


class DeterminismAndOrderingTest(GateTestCase):
    def test_panel_order_is_independent_of_on_disk_discovery_order(self):
        self.ws.write_concept_spec(extra_queries=(
            {"english": "How deep is the queue right now?", "rust_sig": "fn depth(&self) -> usize",
             "pure": True, "witness_required": True},
        ))
        # Write the "depth" witness first on disk -- alphabetically it
        # must still render second is false: depth < load_factor, so
        # writing load_factor's files first (as happy_path_witness_and_rendering
        # does) but depth's spec would sort first regardless of write order.
        depth_result = run_producer(witness_id="W-TQ-DEPTH", query="depth")
        depth_spec = witness_spec(value_hash=depth_result["value_hash"])
        depth_spec["witness_id"] = "W-TQ-DEPTH"
        depth_spec["query"] = "depth"
        depth_spec["output"]["path"] = "docs/witnesses/task_queue.depth.svg"
        self.ws.write_witness(depth_spec)
        self.ws.write_rendering("docs/witnesses/task_queue.depth.svg")
        self.ws.write_result(depth_result)

        happy_path_witness_and_rendering(self.ws)

        svg = self.generate()
        self.assertLess(svg.index("TaskQueue.depth"), svg.index("TaskQueue.load_factor"))

    def test_repeated_generation_over_the_same_inputs_is_byte_identical(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        first = self.generate()
        second = self.generate()
        self.assertEqual(first, second)


class SvgValidityTest(GateTestCase):
    def test_output_is_well_formed_xml(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        svg = self.generate()
        root = ET.fromstring(svg)
        self.assertTrue(root.tag.endswith("svg"))

    def test_the_embedded_witness_image_is_freshly_rendered_not_the_stale_on_disk_file(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        svg = self.generate()
        encoded = svg.split("base64,", 1)[1].split('"', 1)[0]
        embedded = base64.b64decode(encoded)

        baseline = run_producer()
        expected = witness_renderer.render("scalar_field_svg", baseline["result"]).svg.encode("utf-8")
        self.assertEqual(embedded, expected)

        stale_stub = (self.root / "docs" / "witnesses" / "task_queue.load_factor.svg").read_bytes()
        self.assertNotEqual(embedded, stale_stub)

    def test_special_characters_in_a_panel_label_are_escaped_not_injected(self):
        malicious = 'TaskQueue.<script>&"evil"'
        fragment = gcs._pill_svg(0, 0, 100, 20, gcs.GREEN, malicious)
        self.assertNotIn("<script>", fragment)
        self.assertIn("&lt;script&gt;", fragment)
        self.assertIn("&amp;", fragment)
        self.assertIn("&quot;evil&quot;", fragment)


class ThumbnailFreshnessTest(GateTestCase):
    """External review, high severity: an earlier version embedded
    whatever bytes already sat at the witness's output.path, trusting
    witness_present alone -- a stale or unrelated on-disk SVG, or a
    file that was not valid SVG at all, was embedded beside an
    honestly green status row with nothing to say otherwise. The fix
    renders the thumbnail from the SAME fresh document determinism/
    degeneracy were computed from whenever one exists, and validates
    the on-disk fallback (only reached when no witness_backend is
    configured) as well-formed SVG before ever trusting it."""

    def test_a_stale_unrelated_on_disk_svg_is_not_embedded_when_a_fresh_document_exists(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        stale = '<svg xmlns="http://www.w3.org/2000/svg"><text>unrelated old rendering</text></svg>'
        (self.root / "docs" / "witnesses" / "task_queue.load_factor.svg").write_text(stale)

        svg = self.generate()
        self.assertIn("determinism: pass", svg)
        encoded = svg.split("base64,", 1)[1].split('"', 1)[0]
        embedded = base64.b64decode(encoded).decode("utf-8")
        self.assertNotIn("unrelated old rendering", embedded)

    def test_an_on_disk_file_that_is_not_svg_does_not_get_embedded_when_a_fresh_document_exists(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        (self.root / "docs" / "witnesses" / "task_queue.load_factor.svg").write_text("not an svg at all")

        svg = self.generate()
        self.assertIn("determinism: pass", svg)
        # A fresh document WAS available, so this must be the genuine
        # rendering, not the garbage on disk and not a silent placeholder.
        self.assertIn("data:image/svg+xml;base64,", svg)
        encoded = svg.split("base64,", 1)[1].split('"', 1)[0]
        embedded = base64.b64decode(encoded)
        self.assertTrue(gcs._is_well_formed_svg(embedded))
        self.assertNotIn(b"not an svg at all", embedded)

    def test_no_backend_and_invalid_on_disk_content_falls_back_to_the_placeholder(self):
        # The "at minimum" floor: with no witness_backend configured
        # there is no fresh document to render from at all, but an
        # invalid on-disk file must still never be embedded.
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        (self.root / "docs" / "witnesses" / "task_queue.load_factor.svg").write_text("not an svg at all")
        no_backend = descriptor_with("crates/scheduler")

        svg = self.generate(no_backend)
        self.assertIn("determinism: not-checked", svg)
        self.assertIn("rendering unavailable", svg)
        self.assertNotIn("data:image/svg+xml;base64,", svg)

    def test_no_backend_and_a_genuinely_valid_on_disk_svg_is_still_embedded(self):
        # The positive case for the on-disk fallback: with no fresh
        # document to prefer, a genuinely well-formed on-disk rendering
        # is still shown rather than treated as untrustworthy by default.
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        no_backend = descriptor_with("crates/scheduler")

        svg = self.generate(no_backend)
        self.assertIn("determinism: not-checked", svg)
        self.assertIn("data:image/svg+xml;base64,", svg)
        self.assertNotIn("rendering unavailable", svg)


class AmbiguousDeclarationTest(GateTestCase):
    def test_an_ambiguous_declaration_fails_generation_explicitly(self):
        self.ws.write_concept_spec()
        self.ws.write_concept_spec(filename="task_queue_dup.json")
        with self.assertRaises(gcs.GenerationError) as caught:
            self.generate()
        self.assertIn("ambiguous", str(caught.exception))
        with self.assertRaises(gcs.GenerationError):
            gcs.write_contact_sheet(self.root, self.descriptor)
        self.assertFalse((self.root / "docs" / "witnesses" / "_contact_sheet.svg").exists())


class WriteContactSheetTest(GateTestCase):
    def test_write_contact_sheet_creates_the_file_at_the_canonical_path(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        destination, count = gcs.write_contact_sheet(self.root, self.descriptor)
        self.assertEqual(destination, self.root / "docs" / "witnesses" / "_contact_sheet.svg")
        self.assertTrue(destination.is_file())
        self.assertEqual(count, 1)

    def test_atomic_write_failure_leaves_the_previous_contact_sheet_unchanged_and_no_temp_file(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        first, _ = gcs.write_contact_sheet(self.root, self.descriptor)
        original = first.read_bytes()

        with patch("atomic_write.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                gcs.write_contact_sheet(self.root, self.descriptor)

        self.assertEqual(first.read_bytes(), original)
        leftovers = list(first.parent.glob(".{}.*.tmp".format("_contact_sheet.svg")))
        self.assertEqual(leftovers, [])


class CliTest(GateTestCase):
    def _run(self, *args) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            code = gcs.main(list(args))
        return code, buffer.getvalue()

    def test_cli_end_to_end_reports_the_feature_count(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        (self.root / "project-descriptor.json").write_text(json.dumps(self.descriptor))
        code, printed = self._run(str(self.root))
        self.assertEqual(code, gcs.EXIT_OK)
        self.assertIn("1 declared feature(s)", printed)

    def test_cli_zero_features_reports_honestly(self):
        (self.root / "project-descriptor.json").write_text(json.dumps(self.descriptor))
        code, printed = self._run(str(self.root))
        self.assertEqual(code, gcs.EXIT_OK)
        self.assertIn("0 declared features", printed)
        self.assertIn("nothing to check", printed)

    def test_cli_refuses_a_missing_workspace(self):
        code, _ = self._run(str(self.root / "nope"))
        self.assertEqual(code, gcs.EXIT_INPUT_ERROR)

    def test_cli_refuses_a_missing_descriptor(self):
        code, _ = self._run(str(self.root))
        self.assertEqual(code, gcs.EXIT_INPUT_ERROR)

    def test_cli_refuses_an_ambiguous_declaration(self):
        self.ws.write_concept_spec()
        self.ws.write_concept_spec(filename="task_queue_dup.json")
        (self.root / "project-descriptor.json").write_text(json.dumps(self.descriptor))
        code, printed = self._run(str(self.root))
        self.assertEqual(code, gcs.EXIT_GENERATION_FAILED)
        self.assertIn("ambiguous", printed)


if __name__ == "__main__":
    unittest.main()
