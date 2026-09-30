"""Unit tests for the path guard shared by the two path-taking validators.

chainlink #83 (date-ligature-030): `validate-work-package` and
`validate-promotion` both took their path argument straight to
`pathlib`, so a missing path (FileNotFoundError) or a directory
(IsADirectoryError) escaped as a raw traceback. The CLI-level coverage
of that repro lives in tests/test_pipeline.py (pipeline.main()) and in
each validator's own standalone-CLI test class; this file pins the
guard's own contract -- which shapes it refuses, the exact wording each
one gets, and the fact that it is not an OSError, so callers' existing
`except OSError` blocks (an unreadable project descriptor, for one)
cannot swallow it and print the wrong message.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from manifest_input import ManifestInputError, ensure_manifest_path, read_manifest_text  # noqa: E402


class EnsureManifestPathTest(unittest.TestCase):
    def test_missing_path_names_the_path_and_the_expected_kind(self):
        with self.assertRaises(ManifestInputError) as ctx:
            ensure_manifest_path(Path("/tmp/nonexistent-wp.json"), "manifest")
        self.assertEqual(str(ctx.exception), "manifest not found: /tmp/nonexistent-wp.json")

    def test_a_directory_is_refused_with_the_directory_shape_of_the_same_message(self):
        with self.assertRaises(ManifestInputError) as ctx:
            ensure_manifest_path(Path("/tmp"), "manifest")
        self.assertEqual(str(ctx.exception), "manifest path is a directory, expected a file: /tmp")

    def test_the_kind_noun_is_the_callers_own_so_commands_cannot_cross(self):
        """validate-promotion must not report a missing argument as a
        missing "manifest", and vice versa -- the noun is the caller's
        argument name, repeated back in the message."""
        with self.assertRaises(ManifestInputError) as ctx:
            ensure_manifest_path(Path("/tmp/nonexistent-promo.json"), "receipt")
        self.assertEqual(str(ctx.exception), "receipt not found: /tmp/nonexistent-promo.json")

    def test_a_regular_file_passes_silently(self):
        with tempfile.NamedTemporaryFile(suffix=".json") as handle:
            ensure_manifest_path(Path(handle.name), "manifest")  # no raise

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX fifos")
    def test_a_non_regular_file_is_refused_rather_than_read(self):
        """exists() is true but reading would block forever -- a socket,
        fifo or device is not a manifest under any reading."""
        with tempfile.TemporaryDirectory() as tmp:
            special = Path(tmp) / "pipe.json"
            os.mkfifo(special)
            with self.assertRaises(ManifestInputError) as ctx:
                ensure_manifest_path(special, "manifest")
            self.assertEqual(str(ctx.exception), f"manifest path is not a regular file: {special}")


class ReadManifestTextTest(unittest.TestCase):
    def test_reads_the_files_bytes_as_text(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write('{"schema_version": "1.0"}')
            path = Path(handle.name)
        try:
            self.assertEqual(read_manifest_text(path, "manifest"), '{"schema_version": "1.0"}')
        finally:
            path.unlink()

    def test_a_missing_path_raises_the_guard_error_not_file_not_found(self):
        """The library caller (validate_file) lets this propagate to the
        CLI, so it must already carry the user-facing message -- a bare
        FileNotFoundError would reach the user as the original defect."""
        with self.assertRaises(ManifestInputError) as ctx:
            read_manifest_text(Path("/tmp/nonexistent-wp.json"), "manifest")
        self.assertNotIsInstance(ctx.exception, OSError)
        self.assertEqual(str(ctx.exception), "manifest not found: /tmp/nonexistent-wp.json")

    def test_a_directory_raises_the_guard_error_not_is_a_directory_error(self):
        with self.assertRaises(ManifestInputError) as ctx:
            read_manifest_text(Path("/tmp"), "receipt")
        self.assertNotIsInstance(ctx.exception, OSError)
        self.assertEqual(str(ctx.exception), "receipt path is a directory, expected a file: /tmp")

    def test_the_guard_error_is_not_an_oserror_subclass(self):
        """Callers catch OSError around *other* reads (an unreadable
        project descriptor is reported as
        `error: cannot read project descriptor ...`). If this were an
        OSError subclass, a bad manifest path would be reported as a bad
        descriptor -- the wrong file, blamed on the wrong argument."""
        self.assertFalse(issubclass(ManifestInputError, OSError))
        self.assertFalse(issubclass(ManifestInputError, ValueError))

    def test_messages_carry_no_error_prefix_the_cli_adds_itself(self):
        """Every CLI surface prints `error: {e}`; a guard that also
        prefixed its own message would print `error: error: ...`."""
        with self.assertRaises(ManifestInputError) as ctx:
            ensure_manifest_path(Path("/tmp"), "manifest")
        self.assertFalse(str(ctx.exception).startswith("error:"), str(ctx.exception))
        self.assertTrue(all(not line.startswith("Traceback") for line in str(ctx.exception).splitlines()))


if __name__ == "__main__":
    unittest.main()
