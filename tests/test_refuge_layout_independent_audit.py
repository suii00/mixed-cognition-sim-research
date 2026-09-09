import base64
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import audit_refuge_layout_observations as audit


class RefugeLayoutIndependentAuditTests(unittest.TestCase):
    def test_preserved_synthetic_observation_checks(self):
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            audit.self_test()
        self.assertIn("PASS:", captured.getvalue())

    def test_decoded_response_body_rejects_private_address(self):
        private_address = "http://" + "10." + "1." + "2." + "3"
        response = json.dumps({"choices": [{"message": {"content": private_address}}]}).encode("utf-8")
        payload = json.dumps({"response_body_base64": base64.b64encode(response).decode("ascii")}).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            audit.inspect_public_payload("llm_attempts.jsonl", payload + b"\n")
        valid = json.dumps({"response_body_base64": base64.b64encode(b'{"message":"safe public observation"}').decode("ascii")}).encode("utf-8")
        audit.inspect_public_payload("llm_attempts.jsonl", valid + b"\n")

    def test_invalid_base64_and_escaped_unsafe_json_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "encoded"):
            audit.inspect_public_payload("attempt.json", b'{"response_body_base64":"%%%"}')
        address = "10." + "1." + "2." + "3"
        escaped = "".join("\\u%04x" % ord(char) for char in address)
        payload = ('{"content":"' + escaped + '"}').encode("utf-8")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            audit.inspect_public_payload("escaped.json", payload)

    def test_output_is_fresh_direct_repository_derived_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = audit.AUDIT_VERSION + "_20260910T010203Z"
            with mock.patch.object(audit, "REPO_ROOT", root):
                output = root / "derived" / name
                self.assertEqual(audit.safe_output(output), output.resolve())
                for path in (root / name, root / "runs" / name,
                             root / "derived" / "existing-artifact" / name,
                             root / "derived" / "without-version-time"):
                    with self.subTest(name=path.name), self.assertRaises(ValueError):
                        audit.safe_output(path)
                output.mkdir(parents=True)
                with self.assertRaisesRegex(ValueError, "collision"):
                    audit.safe_output(output)

    def test_reparse_point_ancestry_is_rejected(self):
        candidate = mock.Mock()
        candidate.parents = ()
        candidate.is_symlink.return_value = False
        candidate.exists.return_value = True
        candidate.lstat.return_value.st_file_attributes = 0x400
        with mock.patch.object(audit.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, create=True):
            with self.assertRaisesRegex(ValueError, "reparse"):
                audit.reject_links(candidate)

    def test_unsafe_input_is_rejected_before_raw_read_or_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "derived" / (audit.AUDIT_VERSION + "_20260910T010203Z")
            with mock.patch.object(audit, "REPO_ROOT", root), \
                    mock.patch.object(audit, "inspect_manifest_inputs", side_effect=ValueError("unsafe input")), \
                    mock.patch.object(audit, "load_runs") as read_raw:
                with self.assertRaisesRegex(ValueError, "unsafe"):
                    audit.audit(root / "manifest.json", root / "runs", output)
                read_raw.assert_not_called()
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
