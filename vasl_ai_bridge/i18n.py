"""Application UI localization, independent of chat and speech languages.

Catalog keys are the original English UI text. Only known static widget chrome
is bound automatically; editable content and user-created list entries are never
translation sources. Dynamic producers should call ``tr`` when updating text.
"""

from __future__ import annotations

from functools import lru_cache
import re
from string import Formatter

from PySide6.QtCore import QCoreApplication, QLocale, QTranslator, Qt
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QComboBox, QGroupBox, QLabel, QLineEdit,
    QPlainTextEdit, QTabWidget, QTextEdit, QToolBox, QWidget,
)

# Keep catalog imports explicit so PyInstaller discovers every language module
# through the ordinary import graph, without hidden-import configuration.
from .translations_app import TRANSLATIONS as _APP_TRANSLATIONS
from .translations_settings import TRANSLATIONS as _SETTINGS_TRANSLATIONS
from .translations_widgets import TRANSLATIONS as _WIDGET_TRANSLATIONS
from .translations_errors import TRANSLATIONS as _ERROR_TRANSLATIONS, ERROR_TEMPLATES
from .translations_startup import TRANSLATIONS as _STARTUP_TRANSLATIONS

UI_LANGUAGES = ("en", "zh_CN", "zh_TW")
LANGUAGE_NAMES = {"en": "English", "zh_CN": "简体中文", "zh_TW": "繁體中文"}
_language = "en"
_translator: QTranslator | None = None
_translator_application: QCoreApplication | None = None


def system_language(locale: QLocale | str | None = None) -> str:
    """Resolve Chinese script first, then territory, and otherwise use English."""
    selected = QLocale.system() if locale is None else QLocale(locale)
    if locale is None:
        # The display language can differ from regional date/number settings.
        ui_languages = selected.uiLanguages()
        if ui_languages:
            selected = QLocale(ui_languages[0])
    if selected.language() != QLocale.Language.Chinese:
        return "en"
    if selected.script() == QLocale.Script.TraditionalHanScript:
        return "zh_TW"
    if selected.script() == QLocale.Script.SimplifiedHanScript:
        return "zh_CN"
    if selected.territory() in {
        QLocale.Territory.Taiwan, QLocale.Territory.HongKong, QLocale.Territory.Macao,
    }:
        return "zh_TW"
    return "zh_CN"


def normalize_language(language: object) -> str:
    if not isinstance(language, str):
        return "en"
    normalized = language.replace("-", "_").strip()
    if normalized in UI_LANGUAGES:
        return normalized
    return system_language(normalized) if normalized else "en"


def get_language() -> str:
    return _language


@lru_cache(maxsize=1)
def catalog() -> dict[str, tuple[str, str]]:
    """Return all checked-in catalogs for translation and validation."""
    result: dict[str, tuple[str, str]] = {}
    for translations in (
        _APP_TRANSLATIONS, _SETTINGS_TRANSLATIONS, _WIDGET_TRANSLATIONS,
        _ERROR_TRANSLATIONS, _STARTUP_TRANSLATIONS,
    ):
        for source, pair in translations.items():
            if source in result and result[source] != pair:
                raise ValueError(f"Conflicting UI translation: {source!r}")
            result[source] = pair
    return result


def tr(source: str, /, **values: object) -> str:
    """Translate application-owned English text, then insert untouched values."""
    pair = catalog().get(source)
    translated = pair[0 if _language == "zh_CN" else 1] if pair and _language != "en" else source
    return translated.format(**values) if values else translated


# Qt asks platform-theme and dialog-specific contexts for these labels. Keeping
# these in Python makes frozen builds independent of external .qm files.
_QT_TRANSLATIONS: dict[str, tuple[str, str]] = {
    "OK": ("确定", "確定"), "Cancel": ("取消", "取消"),
    "Save": ("保存", "儲存"), "Open": ("打开", "開啟"),
    "Close": ("关闭", "關閉"), "Apply": ("应用", "套用"),
    "Reset": ("重置", "重設"), "Help": ("帮助", "說明"),
    "Yes": ("是", "是"), "No": ("否", "否"),
    "Yes to All": ("全部选是", "全部選是"), "No to All": ("全部选否", "全部選否"),
    "Save All": ("全部保存", "全部儲存"), "Discard": ("放弃", "捨棄"),
    "Don't Save": ("不保存", "不儲存"), "Restore Defaults": ("恢复默认值", "還原預設值"),
    "Abort": ("中止", "中止"), "Retry": ("重试", "重試"), "Ignore": ("忽略", "忽略"),
    "Choose": ("选择", "選擇"), "Select": ("选择", "選擇"),
    "File name:": ("文件名：", "檔案名稱："), "Files of type:": ("文件类型：", "檔案類型："),
    "Look in:": ("查找位置：", "尋找位置："), "Directory:": ("文件夹：", "資料夾："),
    "Folder": ("文件夹", "資料夾"), "Folder:": ("文件夹：", "資料夾："),
    "Show ": ("显示", "顯示"), "All Files (*)": ("所有文件 (*)", "所有檔案 (*)"),
    "All Files (*) (*)": ("所有文件 (*) (*)", "所有檔案 (*) (*)"),
    "Back": ("后退", "返回"), "Forward": ("前进", "前進"),
    "Parent Directory": ("上级文件夹", "上層資料夾"),
    "Go to the parent directory": ("转到上级文件夹", "移至上層資料夾"),
    "Create New Folder": ("新建文件夹", "建立新資料夾"),
    "Create a New Folder": ("新建文件夹", "建立新資料夾"),
    "List View": ("列表视图", "清單檢視"), "Detail View": ("详细视图", "詳細檢視"),
    "Show hidden files": ("显示隐藏文件", "顯示隱藏檔案"),
    "New Folder": ("新建文件夹", "新增資料夾"), "New Folder (%1)": ("新建文件夹 (%1)", "新增資料夾 (%1)"),
    "Rename": ("重命名", "重新命名"), "Delete": ("删除", "刪除"),
    "Name": ("名称", "名稱"), "Size": ("大小", "大小"), "Type": ("类型", "類型"),
    "Date Modified": ("修改日期", "修改日期"), "Date modified": ("修改日期", "修改日期"),
    "Computer": ("此电脑", "本機"), "My Computer": ("此电脑", "本機"),
    "This PC": ("此电脑", "本機"), "Recent Places": ("最近位置", "最近位置"),
    "Find Directory": ("选择文件夹", "選擇資料夾"),
    "Open File": ("打开文件", "開啟檔案"), "Save As": ("另存为", "另存新檔"),
    "File": ("文件", "檔案"), "Directory": ("文件夹", "資料夾"),
    "Unknown": ("未知", "未知"), "Show Details...": ("显示详细信息...", "顯示詳細資訊..."),
    "Hide Details...": ("隐藏详细信息...", "隱藏詳細資訊..."),
    "Undo": ("撤销", "復原"), "Redo": ("重做", "取消復原"),
    "Cut": ("剪切", "剪下"), "Copy": ("复制", "複製"),
    "Paste": ("粘贴", "貼上"), "Select All": ("全选", "全選"),
    "Press shortcut": ("按下快捷键", "按下快速鍵"),
    "Paste and Match Style": ("粘贴并匹配样式", "貼上並符合樣式"),
    "Copy Link Location": ("复制链接地址", "複製連結位置"),
    "Show Date Modified": ("显示修改日期", "顯示修改日期"),
    "Show Size": ("显示大小", "顯示大小"), "Show Type": ("显示类型", "顯示類型"),
    "%1 already exists.\nDo you want to replace it?": ("%1 已存在。\n是否替换？", "%1 已存在。\n是否取代？"),
    "%1\nFile not found.\nPlease verify the correct file name was given.": (
        "%1\n找不到文件。\n请检查文件名是否正确。", "%1\n找不到檔案。\n請檢查檔案名稱是否正確。"),
    "Directory not found.\nPlease verify the correct directory name was given.": (
        "找不到文件夹。\n请检查文件夹名称是否正确。", "找不到資料夾。\n請檢查資料夾名稱是否正確。"),
}


class _UiTranslator(QTranslator):
    def __init__(self, language: str, parent: QCoreApplication):
        super().__init__(parent)
        self.language = language

    def isEmpty(self) -> bool:
        return False

    def translate(self, context: str, source_text: str, disambiguation: str | None = None,
                  n: int = -1) -> str | None:
        if context not in {
            "QPlatformTheme", "QDialogButtonBox", "QMessageBox", "QFileDialog",
            "QFileSystemModel", "QFileSystemWatcher", "QLineEdit", "QWidgetTextControl",
            "QTextControl", "QTextEdit", "QPlainTextEdit", "QAbstractSpinBox", "QKeySequenceEdit",
        }:
            return None
        source = source_text.replace("&", "")
        pair = _QT_TRANSLATIONS.get(source)
        if pair is None:
            # PySide converts None to a null QString, which lets Qt continue
            # searching/fall back to English. An empty Python string is a real
            # empty translation and would erase unrelated Qt labels/shortcuts.
            return None
        return source_text if self.language == "en" else pair[0 if self.language == "zh_CN" else 1]


def _install_qt_translator() -> None:
    global _translator, _translator_application
    application = QCoreApplication.instance()
    if application is None:
        return
    # Native Windows pickers use the OS UI language. Qt pickers honor the
    # application language and keep their contents editable as ordinary paths.
    if isinstance(application, QApplication):
        application.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
    if _translator is not None and _translator_application is application:
        application.removeTranslator(_translator)
        _translator.deleteLater()
    _translator = _UiTranslator(_language, application)
    _translator_application = application
    application.installTranslator(_translator)


def set_language(language: str) -> str:
    """Select an interface language; persistence belongs to user preferences."""
    global _language
    _language = normalize_language(language)
    _install_qt_translator()
    return _language


def _bind_property(widget: QWidget, name: str, getter, setter) -> None:
    bindings = getattr(widget, "_i18n_bindings", None)
    if bindings is None:
        bindings = widget._i18n_bindings = {}
    current = getter()
    previous = bindings.get(name)
    if previous is not None and current != previous[1]:
        # Dynamic status producers own their latest value; never restore an
        # obsolete static notice over a newer runtime state or user value.
        bindings.pop(name, None)
        previous = None
    source = previous[0] if previous else current
    if source not in catalog():
        return
    translated = tr(source)
    if translated != current:
        setter(translated)
    bindings[name] = (source, translated)


def localize_widget_tree(widget: QWidget) -> None:
    """Bind known static chrome and retranslate it without touching user data.

    Call after construction and before adding game/user content. Set the Qt
    property ``i18n_skip`` on a dynamic label/subtree, or ``i18n_skip_items`` on a
    combo containing user-created names. Editable combos are skipped by default.
    Chat message bodies and list/table contents are deliberately excluded.
    """
    if widget.property("i18n_skip") or type(widget).__name__ == "MessageBubble":
        return
    _bind_property(widget, "windowTitle", widget.windowTitle, widget.setWindowTitle)
    for prop in ("toolTip", "statusTip", "whatsThis", "accessibleName", "accessibleDescription"):
        _bind_property(widget, prop, getattr(widget, prop), getattr(widget, "set" + prop[0].upper() + prop[1:]))
    if isinstance(widget, QGroupBox):
        _bind_property(widget, "title", widget.title, widget.setTitle)
    if isinstance(widget, (QLabel, QAbstractButton)):
        _bind_property(widget, "text", widget.text, widget.setText)
    if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit)):
        _bind_property(widget, "placeholderText", widget.placeholderText, widget.setPlaceholderText)
    if isinstance(widget, QComboBox) and not widget.isEditable() and not widget.property("i18n_skip_items"):
        for index in range(widget.count()):
            _bind_property(widget, f"item:{index}", lambda i=index: widget.itemText(i),
                           lambda text, i=index: widget.setItemText(i, text))
    if isinstance(widget, QTabWidget):
        for index in range(widget.count()):
            _bind_property(widget, f"tab:{index}", lambda i=index: widget.tabText(i),
                           lambda text, i=index: widget.setTabText(i, text))
    if isinstance(widget, QToolBox):
        for index in range(widget.count()):
            _bind_property(widget, f"toolbox:{index}", lambda i=index: widget.itemText(i),
                           lambda text, i=index: widget.setItemText(i, text))
    for child in widget.children():
        if isinstance(child, QWidget):
            localize_widget_tree(child)


@lru_cache(maxsize=1)
def _error_templates() -> tuple[tuple[re.Pattern[str], str], ...]:
    templates = []
    sources = dict.fromkeys((*ERROR_TEMPLATES, *(source for source in catalog() if "{" in source)))
    # Structured wrappers must match before the unstructured diagnostic fallback.
    diagnostic_fallback = "AI request failed. {diagnostic}"
    if diagnostic_fallback in sources:
        sources.pop(diagnostic_fallback)
        sources[diagnostic_fallback] = None
    for source in sources:
        fragments = []
        fields = []
        previous_field = False
        for literal, name, spec, conversion in Formatter().parse(source):
            # Adjacent fields cannot be recovered unambiguously from composed
            # English text. Their producers must call tr with original values.
            if spec or conversion or name and (not name.isidentifier() or previous_field and not literal):
                break
            fragments.append(re.escape(literal))
            if name:
                if name not in fields:
                    fragments.append(f"(?P<{name}>.*?)")
                    fields.append(name)
                else:
                    fragments.append(f"(?P={name})")
            previous_field = bool(name)
        else:
            if fields:
                templates.append((re.compile("".join(fragments), re.DOTALL), source))
    return tuple(templates)


def localize_error(text: str) -> str:
    """Translate known app errors while retaining unknown diagnostic payloads.

    Never call this on arbitrary user messages or model replies. Template
    matching is anchored and bounded; filenames, OS codes and exception details
    are inserted without general word replacement.
    """
    return _localize_error(text, 0)


def _localize_error(text: str, depth: int) -> str:
    if _language == "en" or not isinstance(text, str) or depth >= 8:
        return text
    if text in catalog():
        return tr(text)
    if len(text) > 32768:
        return text
    for pattern, source in _error_templates():
        match = pattern.fullmatch(text)
        if match:
            values = match.groupdict()
            # Only a detail field denotes a nested app error. Other fields are
            # exact user/diagnostic values and must remain unchanged.
            for name in ("detail", "error", "reason"):
                if name in values and values[name] != text:
                    values[name] = _localize_error(values[name], depth + 1)
            return tr(source, **values)
    return text
