"""Project license/source packaging and rejection of inconsistent payload notices."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_packaging_module(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare_build = load_packaging_module("prepare_build")
audit_payload = load_packaging_module("audit_payload")


class SourceNoticeTests(unittest.TestCase):
    def test_project_metadata_uses_existing_license_file(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(project["license"], {"file": "LICENSE"})
        self.assertIn("License :: OSI Approved :: GNU Affero General Public License v3", project["classifiers"])

    def test_local_candidate_does_not_claim_a_committed_tree(self):
        notice = prepare_build.source_notice("local")
        self.assertIn("local candidate", notice)
        self.assertIn("not bound to a committed source revision", notice)
        self.assertIn(prepare_build.SOURCE_REPOSITORY, notice)
        self.assertNotIn("/tree/", notice)

    def test_committed_notice_points_to_exact_revision_and_rejects_short_refs(self):
        revision = "ABCDEF1234" * 4
        notice = prepare_build.source_notice(revision)
        self.assertIn(prepare_build.SOURCE_REPOSITORY + "/tree/" + revision.lower(), notice)
        with self.assertRaises(ValueError):
            prepare_build.source_notice("main")


class PreparedPayloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.output = self.directory / "metadata"
        python_root = self.directory / "python"
        python_root.mkdir()
        (python_root / "LICENSE.txt").write_text("Python license fixture\n", encoding="utf-8")
        self.addCleanup(patch.stopall)
        patch.object(prepare_build, "OUTPUT", self.output).start()
        patch.object(prepare_build.sys, "base_prefix", str(python_root)).start()
        patch.object(prepare_build.sys, "platform", "win32").start()
        patch.object(prepare_build.sys, "version_info", (3, 12, 0)).start()
        patch.object(prepare_build.struct, "calcsize", return_value=8).start()
        patch.object(prepare_build.platform, "machine", return_value="AMD64").start()
        patch.object(prepare_build.metadata, "version", side_effect=prepare_build.EXPECTED_PACKAGES.__getitem__).start()
        patch.object(prepare_build.metadata, "distribution", return_value=SimpleNamespace(files=[])).start()
        patch.dict(prepare_build.os.environ, {"GITHUB_REPOSITORY": "", "GITHUB_RUN_ID": "", "ASL_AI_RELEASE_TAG": ""}).start()
        with patch("builtins.print"):
            prepare_build.prepare("local", "6.7.3")
        self.info = json.loads((self.output / "build-info.json").read_text(encoding="utf-8"))
        self.payload = self.directory / "payload"
        self.internal = self.payload / "_internal"
        self.internal.mkdir(parents=True)
        for name in audit_payload.EXECUTABLES:
            (self.payload / name).write_bytes(b"Executable fixture; not a runnable build.")
        for name in audit_payload.REQUIRED_FILES:
            (self.internal / name).write_bytes(b"Runtime fixture; not a runnable build.")
        for name in ("LICENSE", "SOURCE.md", "build-info.json"):
            (self.internal / name).write_bytes((self.output / name).read_bytes())
        patch.object(audit_payload, "PAYLOAD", self.payload).start()
        patch.dict("sys.modules", {"prepare_build": prepare_build}).start()

    def test_license_and_source_enter_real_spec_data_list(self):
        included = json.loads((self.output / "bundle-data.json").read_text(encoding="utf-8"))
        self.assertIn("LICENSE", included)
        self.assertIn("SOURCE.md", included)
        self.assertEqual((self.output / "LICENSE").read_bytes(), (ROOT / "LICENSE").read_bytes())
        self.assertEqual(self.info["project_license"], "AGPL-3.0-only")
        self.assertEqual(self.info["project_license_sha256"], hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest())
        self.assertEqual(self.info["source_revision"], "local")
        self.assertNotIn("source_tree_url", self.info)
        spec_path = ROOT / "packaging" / "ASL_AI.spec"
        tree = ast.parse(spec_path.read_text(encoding="utf-8"))
        prefix = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "desktop" for target in node.targets):
                break
            prefix.append(node)
        namespace = {"SPECPATH": str(spec_path.parent)}
        with patch.object(Path, "read_text", autospec=True, side_effect=lambda path, **kwargs: (self.output / path.name).read_bytes().decode("utf-8")):
            exec(compile(ast.Module(body=prefix, type_ignores=[]), str(spec_path), "exec"), namespace)
        destinations = {Path(source).name: destination for source, destination in namespace["data_files"]}
        self.assertEqual(destinations["LICENSE"], ".")
        self.assertEqual(destinations["SOURCE.md"], ".")

    def test_full_revision_metadata_and_notice_agree(self):
        revision = "1234567890abcdef" * 4
        with patch("builtins.print"):
            prepare_build.prepare(revision.upper(), "6.7.3")
        info = json.loads((self.output / "build-info.json").read_text(encoding="utf-8"))
        expected_tree = prepare_build.SOURCE_REPOSITORY + "/tree/" + revision
        self.assertEqual(info["source_revision"], revision)
        self.assertEqual(info["source_tree_url"], expected_tree)
        self.assertIn(expected_tree, (self.output / "SOURCE.md").read_text(encoding="utf-8"))

    def test_audit_accepts_consistent_project_notices(self):
        paths = {record["path"] for record in audit_payload.audit()}
        self.assertIn("_internal/LICENSE", paths)
        self.assertIn("_internal/SOURCE.md", paths)

    def test_audit_rejects_missing_or_mismatched_source_notice(self):
        (self.internal / "SOURCE.md").unlink()
        with self.assertRaisesRegex(RuntimeError, "Required runtime files"):
            audit_payload.audit()
        (self.internal / "SOURCE.md").write_text("Unrelated source notice\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "source notice"):
            audit_payload.audit()

    def test_audit_rejects_changed_license_and_false_source_tree(self):
        (self.internal / "LICENSE").write_text("Unrelated license\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "source license"):
            audit_payload.audit()
        (self.internal / "LICENSE").write_bytes((ROOT / "LICENSE").read_bytes())
        self.info["source_tree_url"] = prepare_build.SOURCE_REPOSITORY + "/tree/main"
        (self.internal / "build-info.json").write_text(json.dumps(self.info), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "source tree"):
            audit_payload.audit()


if __name__ == "__main__":
    unittest.main()
