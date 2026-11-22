import os
import re
import sys
import subprocess
import unicodedata
from datetime import datetime, time
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, QDate, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QLabel, QCheckBox, QDateEdit, QFileDialog,
    QFrame, QListWidget, QListWidgetItem, QFileIconProvider, QButtonGroup
)


SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", ".build", "DerivedData"}

TYPE_EXTENSIONS = {
    "All": None,
    "PDF": {".pdf"},
    "Docs": {".doc", ".docx", ".txt", ".md", ".rtf", ".pages", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".key"},
    "Images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".heic", ".bmp", ".tiff"},
    "Code": {".py", ".js", ".ts", ".tsx", ".jsx", ".swift", ".java", ".cpp", ".c", ".h", ".go", ".rs", ".html", ".css", ".json", ".yaml", ".yml", ".sql", ".sh", ".ipynb"},
    "Media": {".mp4", ".mov", ".mkv", ".avi", ".mp3", ".wav", ".m4a", ".flac"},
    "Archives": {".zip", ".tar", ".gz", ".rar", ".7z", ".dmg", ".pkg"},
    "Folders": "FOLDERS_ONLY",
}


def normalize_text(text: str) -> str:
    """Lowercase, strip accents/diacritics, and treat -, _, . as spaces."""
    decomposed = unicodedata.normalize("NFKD", text)
    no_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"[-_.]+", " ", no_accents.lower())
    return cleaned.strip()


def compact_text(text: str) -> str:
    """Remove all spaces and separators so 'feefile' matches 'fee_file'."""
    return re.sub(r"\s+", "", normalize_text(text))


def get_creation_timestamp(stat_result: os.stat_result) -> float:
    """Return macOS file creation time (birthtime), falling back to ctime."""
    return getattr(stat_result, "st_birthtime", stat_result.st_ctime)


def compute_match_score(name: str, full_query: str, tokens: list[str]) -> int:
    """
    Ranking priority for query 'fee':
    1. Exact match: fee.pdf
    2. Starts with whole word: fee_file.pdf
    3. Starts with prefix: feefile.pdf
    4. Middle whole word: file_fee.pdf / _fee.pdf
    5. Middle substring: myfeefile.pdf
    """
    if not tokens:
        return 0

    stem = os.path.splitext(name)[0]
    norm_stem = normalize_text(stem)
    comp_stem = compact_text(stem)
    comp_query = compact_text(full_query)
    words = norm_stem.split()

    score = 0

    # 1. Exact stem match
    if norm_stem == full_query or comp_stem == comp_query:
        score += 1000
    # 2. Starts with exact first word (e.g. 'fee_file.pdf')
    elif words and words[0] == tokens[0]:
        score += 700
    # 3. Starts with full query as prefix (e.g. 'feefile.pdf')
    elif norm_stem.startswith(full_query) or comp_stem.startswith(comp_query):
        score += 550
    # 4. Starts with first token
    elif norm_stem.startswith(tokens[0]):
        score += 400

    # 5. Middle-word and substring bonuses
    for tok in tokens:
        if tok in words:
            score += 180  # e.g. 'file_fee.pdf', '_fee.pdf'
        elif any(w.startswith(tok) for w in words):
            score += 120  # e.g. 'my_feefile.pdf'
        else:
            score += 40   # e.g. 'myfeefile.pdf' (middle substring)

        pos = norm_stem.find(tok)
        if pos == -1:
            pos = comp_stem.find(tok)
        if pos != -1:
            score += max(0, 80 - pos * 2)

    return score


class SearchWorker(QThread):
    progress_updated = pyqtSignal(int, int)
    results_ready = pyqtSignal(list, int)

    def __init__(self, folders, full_query, tokens, type_filter, custom_exts, use_date, start_ts, end_ts, max_results=500):
        super().__init__()
        self.folders = folders
        self.full_query = full_query
        self.tokens = tokens
        self.type_filter = type_filter
        self.custom_exts = custom_exts
        self.use_date = use_date
        self.start_ts = start_ts
        self.end_ts = end_ts
        self.max_results = max_results
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _matches_type(self, name: str, is_dir: bool) -> bool:
        # Custom extension box overrides preset pills if non-empty
        if self.custom_exts:
            if is_dir:
                return False
            ext = os.path.splitext(name)[1].lower()
            return ext in self.custom_exts

        allowed = TYPE_EXTENSIONS.get(self.type_filter)
        if allowed is None:
            return True
        if allowed == "FOLDERS_ONLY":
            return is_dir
        if is_dir:
            return False
        ext = os.path.splitext(name)[1].lower()
        return ext in allowed

    def run(self):
        matches = []
        scanned = 0

        for root_folder in self.folders:
            if self._cancelled:
                break
            if not os.path.exists(root_folder):
                continue

            for dirpath, dirnames, filenames in os.walk(root_folder, topdown=True):
                if self._cancelled:
                    break

                kept_dirs = []
                for d in dirnames:
                    if d.startswith(".") or d in SKIP_DIRS:
                        continue
                    full_d = os.path.join(dirpath, d)
                    scanned += 1
                    self._check_entry(d, full_d, True, matches)
                    if not d.endswith(".app"):
                        kept_dirs.append(d)
                dirnames[:] = kept_dirs

                for f in filenames:
                    if self._cancelled:
                        break
                    if f.startswith("."):
                        continue
                    scanned += 1
                    full_f = os.path.join(dirpath, f)
                    self._check_entry(f, full_f, False, matches)

                    if scanned % 400 == 0:
                        self.progress_updated.emit(len(matches), scanned)

        if not self._cancelled:
            # Sort: highest score first, then shorter filename, then newest created
            matches.sort(key=lambda x: (-x["score"], len(x["name"]), -x["created_ts"]))
            self.results_ready.emit(matches[: self.max_results], scanned)

    def _check_entry(self, name: str, full_path: str, is_dir: bool, matches: list):
        if not self._matches_type(name, is_dir):
            return

        if self.tokens:
            norm_name = normalize_text(name)
            comp_name = compact_text(name)
            if not all((tok in norm_name or tok in comp_name) for tok in self.tokens):
                return

        try:
            st = os.stat(full_path)
            created_ts = get_creation_timestamp(st)
        except OSError:
            return

        if self.use_date:
            if not (self.start_ts <= created_ts <= self.end_ts):
                return

        score = compute_match_score(name, self.full_query, self.tokens)
        matches.append({
            "name": name,
            "path": full_path,
            "created": datetime.fromtimestamp(created_ts),
            "created_ts": created_ts,
            "is_dir": is_dir,
            "score": score,
        })


class ResultRowWidget(QWidget):
    def __init__(self, item: dict, icon: QIcon):
        super().__init__()
        self.file_path = item["path"]

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(12)

        icon_label = QLabel()
        icon_label.setPixmap(icon.pixmap(28, 28))
        icon_label.setFixedSize(30, 30)
        layout.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        name_lbl = QLabel(item["name"])
        name_lbl.setObjectName("fileName")
        top_row.addWidget(name_lbl)

        date_str = item["created"].strftime("Created %b %d, %Y %I:%M %p")
        date_lbl = QLabel(date_str)
        date_lbl.setObjectName("dateBadge")
        top_row.addWidget(date_lbl)
        top_row.addStretch()

        path_lbl = QLabel(self.file_path)
        path_lbl.setObjectName("filePath")
        path_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        text_col.addLayout(top_row)
        text_col.addWidget(path_lbl)
        layout.addLayout(text_col, stretch=1)

        self.copy_btn = QPushButton("Copy Path")
        self.copy_btn.setObjectName("secondaryBtn")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.clicked.connect(self.copy_path)
        layout.addWidget(self.copy_btn)

        finder_btn = QPushButton("Show in Finder")
        finder_btn.setObjectName("primaryBtn")
        finder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        finder_btn.clicked.connect(self.show_in_finder)
        layout.addWidget(finder_btn)

    def show_in_finder(self):
        subprocess.run(["open", "-R", self.file_path], check=False)

    def copy_path(self):
        QApplication.clipboard().setText(self.file_path)
        self.copy_btn.setText("Copied ✓")
        QTimer.singleShot(1200, lambda: self.copy_btn.setText("Copy Path"))


class FastSearchApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FastSearch")
        self.resize(940, 660)

        self.icon_provider = QFileIconProvider()
        self.worker = None
        self.selected_type = "All"

        home = str(Path.home())
        self.folder_filters = [
            {"label": "Desktop", "path": os.path.join(home, "Desktop"), "active": True},
            {"label": "Documents", "path": os.path.join(home, "Documents"), "active": True},
            {"label": "Downloads", "path": os.path.join(home, "Downloads"), "active": True},
            {"label": "Applications", "path": "/Applications", "active": False},
        ]
        self.pill_buttons = []

        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(200)
        self.debounce_timer.timeout.connect(self.run_search)

        self._build_ui()
        self._apply_styles()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(18, 18, 18, 14)
        main_layout.setSpacing(10)

        # Search bar
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search files by name (prefix matches ranked first, e.g. 'fee')...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setObjectName("searchInput")
        self.search_input.textChanged.connect(lambda: self.debounce_timer.start())
        main_layout.addWidget(self.search_input)

        # Folder pills row
        pills_row = QHBoxLayout()
        pills_row.setSpacing(8)
        folder_lbl = QLabel("Folders:")
        folder_lbl.setObjectName("sectionLabel")
        folder_lbl.setFixedWidth(52)
        pills_row.addWidget(folder_lbl)

        self.pills_container = QHBoxLayout()
        self.pills_container.setSpacing(6)
        pills_row.addLayout(self.pills_container)

        for f in self.folder_filters:
            self._add_pill_button(f)

        add_folder_btn = QPushButton("+ Add Folder")
        add_folder_btn.setObjectName("addFolderBtn")
        add_folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_folder_btn.clicked.connect(self.add_custom_folder)
        pills_row.addWidget(add_folder_btn)
        pills_row.addStretch()
        main_layout.addLayout(pills_row)

        # Type / Extension filter row
        type_row = QHBoxLayout()
        type_row.setSpacing(8)
        type_lbl = QLabel("Type:")
        type_lbl.setObjectName("sectionLabel")
        type_lbl.setFixedWidth(52)
        type_row.addWidget(type_lbl)

        self.type_group = QButtonGroup(self)
        self.type_group.setExclusive(True)
        for type_name in TYPE_EXTENSIONS.keys():
            btn = QPushButton(type_name)
            btn.setCheckable(True)
            btn.setChecked(type_name == "All")
            btn.setObjectName("typePill")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked, tn=type_name: self._on_type_selected(tn))
            self.type_group.addButton(btn)
            type_row.addWidget(btn)

        self.custom_ext_input = QLineEdit()
        self.custom_ext_input.setPlaceholderText("Custom ext (e.g. csv, json)")
        self.custom_ext_input.setObjectName("extInput")
        self.custom_ext_input.setFixedWidth(170)
        self.custom_ext_input.setClearButtonEnabled(True)
        self.custom_ext_input.textChanged.connect(lambda: self.debounce_timer.start())
        type_row.addWidget(self.custom_ext_input)

        type_row.addStretch()
        main_layout.addLayout(type_row)

        # Date Created range row
        date_row = QHBoxLayout()
        date_row.setSpacing(10)

        self.date_checkbox = QCheckBox("Filter by Date Created")
        self.date_checkbox.stateChanged.connect(self._toggle_date_inputs)
        date_row.addWidget(self.date_checkbox)

        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDate(QDate.currentDate().addMonths(-1))
        self.start_date.setDisplayFormat("dd MMM yyyy")
        self.start_date.setEnabled(False)
        self.start_date.dateChanged.connect(self.run_search)
        date_row.addWidget(self.start_date)

        to_lbl = QLabel("to")
        to_lbl.setObjectName("sectionLabel")
        date_row.addWidget(to_lbl)

        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDate(QDate.currentDate())
        self.end_date.setDisplayFormat("dd MMM yyyy")
        self.end_date.setEnabled(False)
        self.end_date.dateChanged.connect(self.run_search)
        date_row.addWidget(self.end_date)

        date_row.addStretch()

        self.status_label = QLabel("Type a filename, pick a type, or enable Date Created")
        self.status_label.setObjectName("statusLabel")
        date_row.addWidget(self.status_label)
        main_layout.addLayout(date_row)

        # Divider
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("divider")
        main_layout.addWidget(line)

        # Results list
        self.results_list = QListWidget()
        self.results_list.setObjectName("resultsList")
        self.results_list.itemDoubleClicked.connect(self._open_selected_file)
        main_layout.addWidget(self.results_list, stretch=1)

    def _add_pill_button(self, folder_dict: dict):
        btn = QPushButton(folder_dict["label"])
        btn.setCheckable(True)
        btn.setChecked(folder_dict["active"])
        btn.setObjectName("folderPill")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)

        def on_toggle(checked, fd=folder_dict):
            fd["active"] = checked
            self.run_search()

        btn.toggled.connect(on_toggle)
        self.pills_container.addWidget(btn)
        self.pill_buttons.append(btn)

    def _on_type_selected(self, type_name: str):
        self.selected_type = type_name
        self.run_search()

    def _parse_custom_exts(self) -> set[str]:
        raw = self.custom_ext_input.text().strip().lower()
        if not raw:
            return set()
        exts = set()
        for part in re.split(r"[\s,]+", raw):
            part = part.strip()
            if part:
                exts.add(part if part.startswith(".") else f".{part}")
        return exts

    def add_custom_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder to Include")
        if folder and not any(f["path"] == folder for f in self.folder_filters):
            entry = {"label": os.path.basename(folder) or folder, "path": folder, "active": True}
            self.folder_filters.append(entry)
            self._add_pill_button(entry)
            self.run_search()

    def _toggle_date_inputs(self):
        enabled = self.date_checkbox.isChecked()
        self.start_date.setEnabled(enabled)
        self.end_date.setEnabled(enabled)
        self.run_search()

    def run_search(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(200)

        active_folders = [f["path"] for f in self.folder_filters if f["active"]]
        full_query = normalize_text(self.search_input.text())
        tokens = [t for t in full_query.split(" ") if t]
        custom_exts = self._parse_custom_exts()
        use_date = self.date_checkbox.isChecked()
        has_type_filter = (self.selected_type != "All") or bool(custom_exts)

        self.results_list.clear()

        if not active_folders:
            self.status_label.setText("Select at least one folder")
            return
        if not tokens and not use_date and not has_type_filter:
            self.status_label.setText("Type a filename, pick a type, or enable Date Created")
            return

        start_py = self.start_date.date().toPyDate()
        end_py = self.end_date.date().toPyDate()
        start_ts = datetime.combine(start_py, time.min).timestamp()
        end_ts = datetime.combine(end_py, time.max).timestamp()

        self.status_label.setText("Searching & ranking...")
        self.worker = SearchWorker(
            active_folders, full_query, tokens,
            self.selected_type, custom_exts,
            use_date, start_ts, end_ts
        )
        self.worker.progress_updated.connect(self._on_progress)
        self.worker.results_ready.connect(self._render_results)
        self.worker.start()

    def _on_progress(self, found: int, scanned: int):
        self.status_label.setText(f"Scanning... {found} matches ({scanned:,} files checked)")

    def _render_results(self, results: list, scanned: int):
        self.results_list.setUpdatesEnabled(False)
        self.results_list.clear()
        for item in results:
            icon = self.icon_provider.icon(
                QFileIconProvider.IconType.Folder if item["is_dir"] else QFileIconProvider.IconType.File
            )
            row_widget = ResultRowWidget(item, icon)
            list_item = QListWidgetItem(self.results_list)
            list_item.setSizeHint(row_widget.sizeHint())
            list_item.setData(Qt.ItemDataRole.UserRole, item["path"])
            self.results_list.addItem(list_item)
            self.results_list.setItemWidget(list_item, row_widget)
        self.results_list.setUpdatesEnabled(True)
        self.status_label.setText(f"{len(results)} results ({scanned:,} files scanned)")

    def _open_selected_file(self, item: QListWidgetItem):
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            subprocess.run(["open", path], check=False)

    def _apply_styles(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #18181B;
                color: #F4F4F5;
                font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
            }
            #searchInput {
                background-color: #27272A;
                border: 1px solid #3F3F46;
                border-radius: 10px;
                padding: 11px 14px;
                font-size: 15px;
                color: #FAFAFA;
            }
            #searchInput:focus, #extInput:focus {
                border: 1px solid #3B82F6;
            }
            #extInput {
                background-color: #27272A;
                border: 1px solid #3F3F46;
                border-radius: 14px;
                padding: 4px 10px;
                font-size: 12px;
                color: #FAFAFA;
            }
            #sectionLabel, #statusLabel {
                color: #A1A1AA;
                font-size: 12px;
            }
            #folderPill, #typePill {
                background-color: #27272A;
                color: #D4D4D8;
                border: 1px solid #3F3F46;
                border-radius: 14px;
                padding: 5px 11px;
                font-size: 12px;
                font-weight: 500;
            }
            #folderPill:checked {
                background-color: #2563EB;
                color: #FFFFFF;
                border: 1px solid #3B82F6;
            }
            #typePill:checked {
                background-color: #059669;
                color: #FFFFFF;
                border: 1px solid #10B981;
            }
            #addFolderBtn {
                background-color: transparent;
                color: #93C5FD;
                border: 1px dashed #3F3F46;
                border-radius: 14px;
                padding: 5px 12px;
                font-size: 12px;
            }
            #addFolderBtn:hover {
                border-color: #60A5FA;
            }
            QDateEdit {
                background-color: #27272A;
                border: 1px solid #3F3F46;
                border-radius: 6px;
                padding: 4px 8px;
                color: #F4F4F5;
                font-size: 12px;
            }
            QDateEdit:disabled {
                color: #71717A;
                background-color: #202024;
            }
            #divider {
                background-color: #27272A;
                max-height: 1px;
            }
            #resultsList {
                background-color: #18181B;
                border: none;
                outline: none;
            }
            #resultsList::item {
                border-bottom: 1px solid #27272A;
            }
            #resultsList::item:selected {
                background-color: #27272A;
            }
            #fileName {
                font-size: 13px;
                font-weight: 600;
                color: #FAFAFA;
            }
            #dateBadge {
                font-size: 11px;
                color: #A1A1AA;
                background-color: #27272A;
                border-radius: 4px;
                padding: 2px 6px;
            }
            #filePath {
                font-size: 11px;
                color: #71717A;
                font-family: "SF Mono", Menlo, monospace;
            }
            #primaryBtn {
                background-color: #2563EB;
                color: #FFFFFF;
                border: none;
                border-radius: 6px;
                padding: 6px 12px;
                font-size: 12px;
                font-weight: 500;
            }
            #primaryBtn:hover {
                background-color: #3B82F6;
            }
            #secondaryBtn {
                background-color: #27272A;
                color: #E4E4E7;
                border: 1px solid #3F3F46;
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 12px;
            }
            #secondaryBtn:hover {
                background-color: #3F3F46;
            }
        """)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = FastSearchApp()
    window.show()
    sys.exit(app.exec())
