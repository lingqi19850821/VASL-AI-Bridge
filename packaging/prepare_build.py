"""Prepare an allowlisted, path-free metadata and license payload for Windows."""

from __future__ import annotations

import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import sys
import tomllib


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "build" / "windows" / "metadata"
EXPECTED_PACKAGES = {
    "PySide6": "6.11.2",
    "PySide6_Addons": "6.11.2",
    "PySide6_Essentials": "6.11.2",
    "shiboken6": "6.11.2",
    "Pillow": "12.2.0",
    "PyInstaller": "6.22.3",
    "pyinstaller-hooks-contrib": "2026.8",
    "altgraph": "0.17.5",
    "packaging": "26.0",
    "pefile": "2024.8.26",
    "pywin32-ctypes": "0.2.3",
    "setuptools": "70.2.0",
}
VENDORED_LICENSE_ROOT = ROOT / "packaging" / "licenses"
VENDORED_LICENSE_GROUPS = {"QtPdf", "QtMultimedia", "QtGui", "QtImageFormats"}
REQUIRED_QT_LICENSES = {"LGPL-3.0-only.txt", "GPL-3.0-only.txt"}
SUPPORTED_INNO_VERSIONS = {"6.7.1", "6.7.3"}
PROJECT_LICENSE = "AGPL-3.0-only"
SOURCE_REPOSITORY = "https://github.com/lingqi19850821/VASL-AI-Bridge"


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("The installer requires a three-part numeric project version.")
    return version


def source_notice(source_revision: str) -> str:
    """Describe exact committed source, or identify an uncommitted local candidate."""
    if source_revision != "local" and not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", source_revision):
        raise ValueError("SourceRevision must be a full Git commit SHA or 'local'.")
    lines = [
        "# VASL AI Bridge source and license", "",
        f"Project code license: {PROJECT_LICENSE}. The complete license text is in `LICENSE`.", "",
        f"Source repository: {SOURCE_REPOSITORY}", "",
    ]
    if source_revision == "local":
        lines += [
            "Source revision: `local`.", "",
            "This build is a local candidate and is not bound to a committed source revision.",
            "The repository URL alone does not identify the exact source tree used for this build.",
            "A release build must record the full commit SHA of its corresponding source.", "",
        ]
    else:
        revision = source_revision.lower()
        lines += [
            f"Source revision: `{revision}`.", "",
            f"Exact source tree: {SOURCE_REPOSITORY}/tree/{revision}", "",
        ]
    lines += [
        "The source tree includes the project code, packaging scripts, and pinned dependency lists.",
        "Third-party components retain their own licenses; their source links and notices are in",
        "`licenses/THIRD_PARTY_NOTICES.md`.", "",
    ]
    return "\n".join(lines)


def build_info(source_revision: str, inno_version: str) -> dict:
    if inno_version not in SUPPORTED_INNO_VERSIONS:
        raise ValueError("The verified Inno Setup compiler must be 6.7.1 or 6.7.3.")
    if sys.platform != "win32" or struct.calcsize("P") != 8 or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("Builds require Windows x64 and an x64 Python interpreter.")
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Builds require Python 3.12.")
    if source_revision != "local" and not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", source_revision):
        raise ValueError("SourceRevision must be a full Git commit SHA or 'local'.")
    packages = {name: metadata.version(name) for name in EXPECTED_PACKAGES}
    mismatches = [name for name, version in packages.items() if version != EXPECTED_PACKAGES[name]]
    if mismatches:
        raise RuntimeError("Install the pinned requirements-build.txt packages: " + ", ".join(mismatches))
    result = {
        "schema": 1,
        "application": "VASL AI Bridge",
        "version": project_version(),
        "platform": "windows-x64",
        "python": platform.python_version(),
        "packages": packages,
        "source_revision": source_revision.lower(),
        "project_license": PROJECT_LICENSE,
        "source_repository": SOURCE_REPOSITORY,
        "inno_setup": inno_version,
    }
    if source_revision != "local":
        result["source_tree_url"] = f"{SOURCE_REPOSITORY}/tree/{source_revision.lower()}"
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) and re.fullmatch(r"\d+", run_id):
        result["github_repository"] = repository
        result["github_run_id"] = run_id
        result["github_run_url"] = f"https://github.com/{repository}/actions/runs/{run_id}"
    release_tag = os.environ.get("VASL_AI_BRIDGE_RELEASE_TAG", "")
    if release_tag:
        if release_tag != "v" + result["version"]:
            raise ValueError("VASL_AI_BRIDGE_RELEASE_TAG must match the project version.")
        result["release_tag"] = release_tag
    return result


def copy_license(source: Path, relative: Path, included: list[str]) -> None:
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("A required license file is missing or is a symlink.")
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("License destinations must be relative to the build metadata directory.")
    destination = OUTPUT / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    included.append(relative.as_posix())


def copy_vendored_qt_licenses(included: list[str]) -> list[dict[str, str]]:
    """Copy local notices with version, source, path and required-file checks."""
    manifest_path = VENDORED_LICENSE_ROOT / "manifest.json"
    if manifest_path.is_symlink():
        raise RuntimeError("The vendored Qt license manifest must not be a symlink.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("schema") != 1
            or manifest.get("qt_version") != EXPECTED_PACKAGES["PySide6"]
            or not isinstance(manifest.get("files"), list)):
        raise RuntimeError("The vendored Qt license manifest does not match this Qt version.")
    records = []
    paths = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            raise RuntimeError("A vendored Qt license entry is invalid.")
        name, source = (entry.get(key) for key in ("path", "source"))
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.txt", name)
                or name in paths):
            raise RuntimeError("Vendored Qt license paths must be unique relative text filenames.")
        relative = Path(name)
        if (".." in relative.parts or relative.is_absolute()
                or (len(relative.parts) == 1 and name not in REQUIRED_QT_LICENSES)
                or (len(relative.parts) != 1 and relative.parts[0] not in VENDORED_LICENSE_GROUPS)):
            raise RuntimeError("The vendored Qt license path is outside the reviewed groups.")
        if (not isinstance(source, str) or not source.startswith((
                "https://doc.qt.io/qt-6.11/", "https://raw.githubusercontent.com/qt/qtbase/v6.11.2/LICENSES/"))):
            raise RuntimeError("The vendored Qt license source is invalid.")
        original = VENDORED_LICENSE_ROOT / relative
        if original.is_symlink() or original.resolve().relative_to(VENDORED_LICENSE_ROOT.resolve()).as_posix() != name:
            raise RuntimeError("Vendored Qt license files must remain inside their source directory.")
        destination = Path("licenses/Qt") / relative
        copy_license(original, destination, included)
        records.append({"path": destination.as_posix(), "source": source})
        paths.add(name)
    if not REQUIRED_QT_LICENSES <= paths:
        raise RuntimeError("The vendored Qt LGPL/GPL license texts are required.")
    if not VENDORED_LICENSE_GROUPS <= {Path(name).parts[0] for name in paths}:
        raise RuntimeError("The reviewed Qt module attribution groups are required.")
    copy_license(manifest_path, Path("licenses/Qt/manifest.json"), included)
    return records


def prepare(source_revision: str, inno_version: str) -> None:
    info = build_info(source_revision, inno_version)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    included = []
    copy_license(ROOT / "LICENSE", Path("LICENSE"), included)
    (OUTPUT / "SOURCE.md").write_text(source_notice(info["source_revision"]), encoding="utf-8", newline="\n")
    included.append("SOURCE.md")
    for name in EXPECTED_PACKAGES:
        distribution = metadata.distribution(name)
        for item in distribution.files or []:
            if "licenses" in item.parts or item.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                source = Path(distribution.locate_file(item))
                # Keep package-provided licenses, never the environment metadata,
                # installer records, direct_url.json, or a whole package directory.
                if source.is_file():
                    suffix = Path(*item.parts[item.parts.index("licenses") + 1:]) if "licenses" in item.parts else Path(item.name)
                    copy_license(source, Path("licenses") / name / suffix, included)
    python_license = next((path for path in (
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE_PYTHON.txt",
        Path(sys.base_prefix) / "LICENSE",
    ) if path.is_file()), None)
    if python_license is None:
        raise RuntimeError("The Python installation must supply its license text.")
    copy_license(python_license, Path("licenses/Python/LICENSE.txt"), included)
    copy_license(ROOT / "packaging" / "THIRD_PARTY_NOTICES.md", Path("licenses/THIRD_PARTY_NOTICES.md"), included)
    info["vendored_qt_licenses"] = copy_vendored_qt_licenses(included)
    (OUTPUT / "build-info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    included.append("build-info.json")
    (OUTPUT / "bundle-data.json").write_text(json.dumps(sorted(set(included)), indent=2) + "\n", encoding="utf-8")
    version = tuple(int(number) for number in info["version"].split(".")) + (0,)
    resource = "\n".join([
        "VSVersionInfo(",
        f"  ffi=FixedFileInfo(filevers={version!r}, prodvers={version!r}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),",
        "  kids=[StringFileInfo([StringTable('040904B0', [",
        "    StringStruct('CompanyName', 'VASL AI Bridge'),",
        "    StringStruct('FileDescription', 'VASL AI Bridge desktop companion'),",
        f"    StringStruct('FileVersion', {info['version']!r}),",
        "    StringStruct('ProductName', 'VASL AI Bridge'),",
        f"    StringStruct('ProductVersion', {info['version']!r})",
        "  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])",
        "",
    ])
    (OUTPUT / "windows-version-info.txt").write_text(resource, encoding="utf-8")
    print(info["version"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-revision", default="local")
    parser.add_argument("--inno-version", choices=sorted(SUPPORTED_INNO_VERSIONS))
    parser.add_argument("--version-only", action="store_true")
    args = parser.parse_args()
    if args.version_only:
        print(project_version())
    else:
        if args.inno_version is None:
            parser.error("--inno-version is required when preparing build metadata.")
        prepare(args.source_revision, args.inno_version)
