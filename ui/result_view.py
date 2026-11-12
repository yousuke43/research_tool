"""結果 (Markdown) を表示する共通ウィジェット.

プレビュー / Markdown ソースの切り替えと、クリップボードへのコピーを持つ。
Decision Items タブ・各モデルのタブの両方で使う。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from utils import file_manager


class ResultView(QWidget):
    def __init__(self, text: str = "", caption: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text

        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(True)

        self.source = QPlainTextEdit()
        self.source.setReadOnly(True)
        self.source.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.source.setFont(QFont("Consolas", 10))

        self.stack = QStackedWidget()
        self.stack.addWidget(self.preview)
        self.stack.addWidget(self.source)

        self.mode = QComboBox()
        self.mode.addItems(["プレビュー", "Markdown ソース"])
        self.mode.currentIndexChanged.connect(self.stack.setCurrentIndex)

        self.copy_button = QPushButton("コピー")
        self.copy_button.clicked.connect(self._copy)

        self.caption_label = QLabel(caption)
        self.caption_label.setStyleSheet("color: #555;")

        bar = QHBoxLayout()
        bar.addWidget(self.caption_label)
        bar.addStretch(1)
        bar.addWidget(QLabel("表示:"))
        bar.addWidget(self.mode)
        bar.addWidget(self.copy_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(bar)
        layout.addWidget(self.stack, 1)

        self.set_text(text)

    def set_text(self, text: str) -> None:
        self._text = text or ""
        self.preview.setMarkdown(file_manager.unwrap_code_fence(self._text))
        self.source.setPlainText(self._text)

    def text(self) -> str:
        return self._text

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self._text)
        self.copy_button.setText("コピーしました")
        self.copy_button.setEnabled(False)
        QTimer.singleShot(1200, self._reset_copy_button)

    def _reset_copy_button(self) -> None:
        self.copy_button.setText("コピー")
        self.copy_button.setEnabled(True)
