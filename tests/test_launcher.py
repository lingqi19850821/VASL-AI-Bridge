"""Synthetic startup checks; no desktop, browser, or model process is started."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vasl_ai_bridge import launcher


def temporary_directory(**kwargs):
    # Keep synthetic log files inside the workspace, including sandboxed runs.
    return tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent, **kwargs)


class LauncherTests(unittest.TestCase):
    @staticmethod
    def _raised_error(error, filename, source="raise failure\n"):
        try:
            exec(compile(source, str(filename), "exec"), {"failure": error})
        except Exception as raised:
            return raised
        raise AssertionError("Synthetic code did not raise an error")

    def test_frozen_diagnostics_use_renamed_package_without_source_tree(self):
        with temporary_directory() as directory, \
                patch.object(launcher.sys, "frozen", True, create=True), \
                patch.object(launcher, "_PACKAGE_ROOT", Path(directory) / "vasl_ai_bridge"):
            for filename in ("vasl_ai_bridge/app.py", r"vasl_ai_bridge\app.py"):
                with self.subTest(filename=filename):
                    self.assertEqual(launcher._frame_location(filename, 27),
                                     "vasl_ai_bridge/app.py:27")
            for filename in ("asl_ai/app.py", "vasl_ai_bridge/unknown.py",
                             "vasl_ai_bridge/private/app.py", "vasl_ai_bridge/../app.py"):
                with self.subTest(filename=filename):
                    self.assertEqual(launcher._frame_location(filename, 27), "<external>")

    def test_success_starts_the_app_without_diagnostics(self):
        application = SimpleNamespace(main=Mock(return_value=0))
        with patch.object(launcher.importlib, "import_module", return_value=application), \
                patch.object(launcher, "_report_startup_error") as report:
            self.assertEqual(launcher.main(), 0)
        application.main.assert_called_once_with()
        report.assert_not_called()

    def test_import_failure_saves_local_diagnostic_and_shows_error(self):
        with temporary_directory() as directory, \
                patch.dict(launcher.os.environ, {"LOCALAPPDATA": directory}), \
                patch.object(launcher.importlib, "import_module", side_effect=ImportError("Missing test dependency")), \
                patch.object(launcher, "_show_error") as show:
            self.assertEqual(launcher.main(), 1)
            diagnostic = Path(directory) / "ASL_AI" / "startup-error.log"
            content = diagnostic.read_text(encoding="utf-8")
            self.assertIn("Error category: ImportError", content)
            self.assertRegex(content, r"vasl_ai_bridge/launcher\.py:\d+")
            self.assertIn("<external>", content)
            self.assertIn(r"%LOCALAPPDATA%\ASL_AI\startup-error.log", show.call_args.args[0])
            for output in (content, show.call_args.args[0]):
                self.assertNotIn("Missing test dependency", output)
                self.assertNotIn(directory, output)

    def test_app_initialization_failure_is_visible(self):
        application = SimpleNamespace(main=Mock(side_effect=RuntimeError("Synthetic startup failure")))
        with patch.object(launcher.importlib, "import_module", return_value=application), \
                patch.object(launcher, "_report_startup_error") as report:
            self.assertEqual(launcher.main(), 1)
        self.assertEqual(str(report.call_args.args[0]), "Synthetic startup failure")

    def test_log_failure_still_shows_startup_error(self):
        with patch.object(launcher.Path, "mkdir", side_effect=PermissionError("PRIVATE_USER permission failure")), \
                patch.object(launcher, "_show_error") as show:
            launcher._report_startup_error(RuntimeError("PRIVATE_TOKEN startup failure"))
        message = show.call_args.args[0]
        self.assertIn("Error category: RuntimeError", message)
        self.assertIn("could not be saved", message)
        self.assertNotIn("PRIVATE_USER", message)
        self.assertNotIn("PRIVATE_TOKEN", message)
        self.assertNotIn("share the diagnostic", message)

    def test_log_write_failure_does_not_echo_filesystem_error(self):
        with temporary_directory() as directory, \
                patch.dict(launcher.os.environ, {"LOCALAPPDATA": directory}), \
                patch.object(launcher.Path, "write_text", side_effect=OSError("PRIVATE_FILE_PATH")), \
                patch.object(launcher, "_show_error") as show:
            launcher._report_startup_error(ValueError("PRIVATE_CONTENT"))
        message = show.call_args.args[0]
        self.assertIn("Error category: ValueError", message)
        self.assertIn("could not be saved", message)
        self.assertNotIn("PRIVATE_FILE_PATH", message)
        self.assertNotIn("PRIVATE_CONTENT", message)
        self.assertNotIn(directory, message)
        self.assertNotIn("share the diagnostic", message)

    def test_logs_and_dialog_exclude_private_data_but_keep_application_location(self):
        private_values = (
            "PRIVATE_USER_271", "private-271@example.invalid", "PRIVATE_TOKEN_271",
            "PRIVATE_CHAT_CONTENT_271", r"C:\Users\PRIVATE_USER_271\secret.txt",
        )
        error = RuntimeError(" | ".join(private_values))
        error.add_note("PRIVATE_NOTE_271")
        source = (
            "def PRIVATE_FUNCTION_271():\n"
            "    PRIVATE_LOCAL_271 = 'PRIVATE_LOCAL_VALUE_271'\n"
            "    raise failure\n"
            "PRIVATE_FUNCTION_271()\n"
        )
        error = self._raised_error(error, launcher._PACKAGE_ROOT / "app.py", source)
        with temporary_directory(prefix="PRIVATE_INSTALL_271_") as directory, \
                patch.dict(launcher.os.environ, {"LOCALAPPDATA": directory}), \
                patch.object(launcher, "_show_error") as show:
            launcher._report_startup_error(error)
            content = (Path(directory) / "ASL_AI" / "startup-error.log").read_text(encoding="utf-8")
        self.assertIn("vasl_ai_bridge/app.py:3", content)
        self.assertIn("vasl_ai_bridge/app.py:4", content)
        self.assertIn("Error category: RuntimeError", content)
        for output in (content, show.call_args.args[0]):
            for value in (*private_values, "PRIVATE_NOTE_271", "PRIVATE_FUNCTION_271",
                          "PRIVATE_LOCAL_271", "PRIVATE_LOCAL_VALUE_271", directory,
                          str(launcher._PACKAGE_ROOT)):
                self.assertNotIn(value, output)

    def test_unknown_filenames_never_appear_even_inside_application_directory(self):
        filenames = (
            r"C:\Users\PRIVATE_USER_492\PRIVATE_DOCUMENT_492.py",
            "/home/PRIVATE_USER_492/PRIVATE_DOCUMENT_492.py",
            launcher._PACKAGE_ROOT / "PRIVATE_DOCUMENT_492.py",
            launcher._PACKAGE_ROOT / "PRIVATE_USER_492" / "app.py",
        )
        for filename in filenames:
            with self.subTest(filename=filename):
                error = self._raised_error(RuntimeError("PRIVATE_SECRET_492"), filename)
                content = launcher._startup_diagnostic(error)
                self.assertIn("<external>", content)
                self.assertNotIn("PRIVATE_", content)
                self.assertNotIn("vasl_ai_bridge/app.py", content)
                self.assertNotIn(str(launcher._PACKAGE_ROOT), content)

    def test_matching_application_basename_outside_package_is_external(self):
        with temporary_directory() as directory:
            error = self._raised_error(RuntimeError("PRIVATE_CONTENT"), Path(directory) / "app.py")
            content = launcher._startup_diagnostic(error)
        self.assertIn("<external>", content)
        self.assertNotIn("vasl_ai_bridge/app.py", content)
        self.assertNotIn(directory, content)

    def test_dynamic_exception_class_name_and_string_method_are_not_used(self):
        def forbidden_string(error):
            raise AssertionError("The private error must not be formatted")

        private_type = type("PRIVATE_EXCEPTION_NAME_183", (Exception,), {"__str__": forbidden_string})
        error = private_type("PRIVATE_ARGS_183")
        with temporary_directory() as directory, \
                patch.dict(launcher.os.environ, {"LOCALAPPDATA": directory}), \
                patch.object(launcher, "_show_error") as show:
            launcher._report_startup_error(error)
            content = (Path(directory) / "ASL_AI" / "startup-error.log").read_text(encoding="utf-8")
        for output in (content, show.call_args.args[0]):
            self.assertIn("Error category: Exception", output)
            self.assertNotIn("PRIVATE_", output)

    def test_syntax_error_does_not_include_filename_or_source(self):
        error = SyntaxError("PRIVATE_MESSAGE_615", (
            r"C:\Users\PRIVATE_USER_615\PRIVATE_FILE_615.py", 7, 2,
            "PRIVATE_SOURCE_615 = PRIVATE_PASSWORD_615",
        ))
        content = launcher._startup_diagnostic(error)
        self.assertIn("Error category: SyntaxError", content)
        self.assertNotIn("PRIVATE_", content)

    def test_causes_and_context_keep_categories_without_messages(self):
        for relation in ("__cause__", "__context__"):
            with self.subTest(relation=relation):
                inner = self._raised_error(ImportError("PRIVATE_CAUSE_932"), launcher._PACKAGE_ROOT / "provider.py")
                inner.add_note("PRIVATE_CAUSE_NOTE_932")
                outer = RuntimeError("PRIVATE_OUTER_932")
                setattr(outer, relation, inner)
                content = launcher._startup_diagnostic(outer)
                self.assertIn("Error category: RuntimeError", content)
                self.assertIn("Error category: ImportError", content)
                self.assertIn("vasl_ai_bridge/provider.py:1", content)
                self.assertNotIn("PRIVATE_", content)

    def test_suppressed_context_is_not_reported(self):
        error = RuntimeError("PRIVATE_OUTER")
        error.__context__ = ImportError("PRIVATE_CONTEXT")
        error.__suppress_context__ = True
        content = launcher._startup_diagnostic(error)
        self.assertNotIn("ImportError", content)
        self.assertNotIn("PRIVATE_", content)

    def test_passed_error_is_reported_instead_of_ambient_exception(self):
        error = self._raised_error(ValueError("PRIVATE_VALUE"), launcher._PACKAGE_ROOT / "storage.py")
        try:
            raise ImportError("PRIVATE_AMBIENT")
        except ImportError:
            content = launcher._startup_diagnostic(error)
        self.assertIn("Error category: ValueError", content)
        self.assertIn("vasl_ai_bridge/storage.py:1", content)
        self.assertNotIn("ImportError", content)
        self.assertNotIn("PRIVATE_", content)

    def test_exception_cycles_and_long_chains_are_bounded(self):
        first = RuntimeError("PRIVATE_FIRST")
        second = ValueError("PRIVATE_SECOND")
        first.__cause__ = second
        second.__cause__ = first
        content = launcher._startup_diagnostic(first)
        self.assertEqual(content.count("Error category:"), 2)
        self.assertIn("<additional errors omitted>", content)
        current = first
        for index in range(20):
            next_error = RuntimeError(f"PRIVATE_{index}")
            current.__cause__ = next_error
            current = next_error
        content = launcher._startup_diagnostic(first)
        self.assertLessEqual(content.count("Error category:"), 8)
        self.assertIn("<additional errors omitted>", content)
        self.assertNotIn("PRIVATE_", content)

    def test_home_fallback_uses_symbolic_location_in_dialog(self):
        with temporary_directory(prefix="PRIVATE_HOME_") as directory, \
                patch.dict(launcher.os.environ, {"LOCALAPPDATA": ""}), \
                patch.object(launcher.Path, "home", return_value=Path(directory)), \
                patch.object(launcher, "_show_error") as show:
            launcher._report_startup_error(RuntimeError("PRIVATE_ERROR"))
            diagnostic = Path(directory) / ".asl_ai" / "startup-error.log"
            self.assertTrue(diagnostic.is_file())
            content = diagnostic.read_text(encoding="utf-8")
        message = show.call_args.args[0]
        self.assertIn("~/.asl_ai/startup-error.log", message)
        for output in (content, message):
            self.assertNotIn("PRIVATE_", output)
            self.assertNotIn(directory, output)

    def test_unknown_paths_are_rejected_without_filesystem_resolution(self):
        filenames = (
            r"\\PRIVATE_SERVER\PRIVATE_SHARE\app.py",
            r"C:\Users\PRIVATE_USER\app.py",
            str(launcher._PACKAGE_ROOT / "PRIVATE_DOCUMENT.py"),
        )
        with patch.object(launcher.Path, "resolve", side_effect=AssertionError("Unknown paths must not be resolved")):
            for filename in filenames:
                with self.subTest(filename=filename):
                    self.assertEqual(launcher._frame_location(filename, 1), "<external>")

    def test_known_source_resolving_outside_package_or_failing_is_external(self):
        filename = str(launcher._PACKAGE_ROOT / "app.py")
        outside = launcher._PACKAGE_ROOT.parent / "PRIVATE_USER" / "app.py"
        with patch.object(launcher.Path, "resolve", return_value=outside):
            self.assertEqual(launcher._frame_location(filename, 1), "<external>")
        with patch.object(launcher.Path, "resolve", side_effect=PermissionError("PRIVATE_PATH")):
            self.assertEqual(launcher._frame_location(filename, 1), "<external>")


if __name__ == "__main__":
    unittest.main()
