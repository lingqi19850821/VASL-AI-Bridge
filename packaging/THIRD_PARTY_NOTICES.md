# Third-party components

VASL AI Bridge includes the Python runtime, PySide6/Shiboken/Qt, Pillow, and the
PyInstaller bootloader. The exact package versions are recorded in
`build-info.json`. License texts supplied by these packages and the Python
installation are copied into this directory during the build. Additional
Qt license texts and module attribution notices are maintained in the public
repository under `packaging/licenses`. Their explicit file list, official
source URLs, and SHA-256 hashes are in `Qt/manifest.json` in this directory.
The build verifies those hashes and copies the reviewed files without making
network requests. The same file records appear in `build-info.json`.

Python source and license: https://www.python.org/downloads/source/

PySide6 and Shiboken source for version 6.11.2:
https://code.qt.io/cgit/pyside/pyside-setup.git/tree/?h=v6.11.2

Qt source for version 6.11.2:
https://download.qt.io/archive/qt/6.11/6.11.2/submodules/

Qt uses dynamically linked DLLs in this distribution. The included Qt and
PySide6 libraries can be replaced with compatible modified builds; VASL AI Bridge
does not impose a restriction on reverse engineering for debugging such
library modifications. The applicable third-party license terms remain in
effect. The Qt distribution includes third-party components. The following
official Qt 6.11.2 attribution text is supplied locally in `Qt/`:

- `QtPdf/`: PDFium, Chromium, Abseil, FreeType, fast_float, ICU,
  libjpeg-turbo, libpng, and zlib.
- `QtMultimedia/`: FFmpeg 7.1.3, DR Libs, Signalsmith Stretch, TLSF,
  and the Boost, libjpeg, and zlib attribution listed by Qt Multimedia.
- `QtGui/`: the Qt GUI libjpeg-turbo, libpng, FreeType, HarfBuzz, and
  smooth-scaling attribution.
- `QtImageFormats/`: libtiff and libwebp, corresponding to the TIFF/WebP
  image-format plugins present in the supplied PySide6 wheel.

Each text retains the component description, copyright, and license text
from the official versioned Qt attribution page, with a source link. These
notices cover the named components and are not an assertion that every
third-party component in every possible Qt build has been inventoried.
The payload manifest identifies the DLLs/plugins actually distributed.
Further module/source attribution is available in the Qt source archives and:

- https://doc.qt.io/qt-6.11/qtpdf-licensing.html
- https://doc.qt.io/qt-6.11/qtmultimedia-index.html#licenses-and-attributions
- https://doc.qt.io/qt-6.11/qtgui-index.html#licenses-and-attributions
- https://doc.qt.io/qt-6.11/qtimageformats-index.html#licenses-and-attributions
- https://doc.qt.io/qt-6.11/licenses.html

FFmpeg source for the version identified by Qt Multimedia:
https://github.com/FFmpeg/FFmpeg/tree/n7.1.3
Qt's FFmpeg binary build scripts:
https://code.qt.io/cgit/qt/qt5.git/tree/coin/provisioning?h=v6.11.2

This software is based in part on the work of the Independent JPEG Group.
The distribution also uses the FreeType Project and HarfBuzz for font-related
Qt code; see their included component notices. VASL AI Bridge does not modify those
third-party library binaries during packaging.

Pillow source, version 12.2.0: https://github.com/python-pillow/Pillow/tree/12.2.0

PyInstaller source, version 6.22.3:
https://github.com/pyinstaller/pyinstaller/tree/v6.22.3
Its bootloader exception is included with the package license text.

Microsoft Visual C++ runtime DLLs needed by Python and Qt are collected
with the application by PyInstaller. No Visual Studio installation or
administrator-level runtime installer is invoked by VASL AI Bridge Setup.
Microsoft runtime information:
https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist
Microsoft Visual C++ Runtime 2015-2022 license terms:
https://visualstudio.microsoft.com/license-terms/vs2022-cruntime/
Visual Studio 2022 distributable-code list:
https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution

Codex, Whisper engines, speech models, credentials, and user data are not
part of this distribution. They retain their own respective licenses.

VASL AI Bridge project code is licensed under AGPL-3.0-only. Its license text is
in `../LICENSE`; `../SOURCE.md` identifies the project source revision or marks
an uncommitted local candidate. These third-party components retain their own
license terms independently of the project code license.
