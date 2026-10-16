"""Explicitly prepare a pinned CPU voice engine and multilingual model locally.

Run this script only when choosing to download local voice support. Ordinary
application startup and recording never download programs or model weights.
The download contains no user audio, account or game data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile


ENGINE_URL = "https://github.com/ggml-org/whisper.cpp/releases/download/v1.8.2/whisper-bin-x64.zip"
ENGINE_SHA256 = "b1514ebc099765e39fa37eb780b92a140a94c86bb0b3b3d98226b38825979732"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"
MODEL_SHA256 = "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe"
MODEL_SIZE = 147951465


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url: str, path: Path, expected: str, maximum: int) -> None:
    if path.is_file() and digest(path) == expected:
        return
    temporary = path.with_suffix(path.suffix + ".download")
    size = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ASL_AI local voice setup"})
        with urllib.request.urlopen(request, timeout=30) as response, temporary.open("wb") as stream:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > maximum:
                    raise ValueError("The downloaded file exceeds its expected size.")
                stream.write(chunk)
        if digest(temporary) != expected:
            raise ValueError("The downloaded file did not match the pinned SHA256.")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    report = {"result": "FAIL", "engine_version": "1.8.2", "mode": "CPU",
              "engine_url": ENGINE_URL, "model_url": MODEL_URL,
              "user_data_uploaded": False, "inference_started": False}
    try:
        archive = root / "whisper-bin-x64.zip"
        print("Downloading the verified Windows CPU engine...", flush=True)
        download(ENGINE_URL, archive, ENGINE_SHA256, 8 * 1024 * 1024)
        engine_directory = root / "bin"
        engine_directory.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            if sum(item.file_size for item in bundle.infolist()) > 64 * 1024 * 1024:
                raise ValueError("The engine archive is unexpectedly large.")
            for item in bundle.infolist():
                destination = (engine_directory / item.filename).resolve()
                if not destination.is_relative_to(engine_directory):
                    raise ValueError("The engine archive contains an unsafe path.")
            bundle.extractall(engine_directory)
        print("Downloading the verified multilingual base model (148 MB)...", flush=True)
        model = root / "ggml-base.bin"
        download(MODEL_URL, model, MODEL_SHA256, MODEL_SIZE)
        if model.stat().st_size != MODEL_SIZE:
            raise ValueError("The model size did not match its pinned manifest.")
        programs = list(engine_directory.rglob("whisper-cli.exe"))
        if len(programs) != 1:
            raise ValueError("The CPU engine archive did not contain one whisper-cli executable.")
        report.update(result="PASS", engine_program=str(programs[0]), model_path=str(model),
                      engine_archive_sha256=digest(archive), model_sha256=digest(model))
    except Exception as exc:
        report["error"] = str(exc)
    (root / "setup-verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report), flush=True)
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
