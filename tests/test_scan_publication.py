import shutil
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from tools.scan_publication import apply_allowlist, scan_text, scan_tree


@contextmanager
def workspace_tempdir():
    base = Path.cwd() / ".tmp"
    base.mkdir(exist_ok=True)
    path = base / f"publication-test-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield str(path)
    finally:
        shutil.rmtree(path)


class PublicationScannerTests(unittest.TestCase):
    def test_private_identifiers_are_detected_without_echoing_values(self):
        findings = scan_text(
            "sample.txt",
            "host "
            + "gpu-"
            + "sv-01 at "
            + "10."
            + "2.3.4 with "
            + "GPU-"
            + "11111111-2222-3333-4444-555555555555\n",
        )
        self.assertEqual(
            {finding.pattern_id for finding in findings},
            {"internal_hostname", "private_ip", "gpu_uuid"},
        )
        self.assertTrue(all(not hasattr(finding, "value") for finding in findings))

    def test_allowlist_requires_exact_pattern_and_path(self):
        findings = scan_text("tests/fixture.txt", "ssh" + "d: synthetic\n")
        remaining, usage = apply_allowlist(
            findings,
            [
                {
                    "path": "tests/fixture.txt",
                    "pattern": "sshd_session",
                    "reason": "synthetic scanner fixture",
                }
            ],
        )
        self.assertEqual(remaining, [])
        self.assertEqual(usage[0]["matches"], 1)

    def test_archive_file_is_rejected_by_path(self):
        with workspace_tempdir() as temp:
            root = Path(temp)
            (root / "payload.bundle").write_bytes(b"fixture")
            findings = scan_tree(root)
            self.assertEqual([(row.pattern_id, row.line) for row in findings], [("archive_file", 0)])


if __name__ == "__main__":
    unittest.main()
