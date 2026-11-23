from __future__ import annotations

from pathlib import Path
from shutil import disk_usage
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from PySide6.QtCore import QFileInfo, Qt
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication
from PySide6.QtWidgets import QApplication, QFileIconProvider, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CompactSpinBox, FluentIcon, LineEdit, Slider, StrongBodyLabel, TransparentToolButton, isDarkTheme,
    setFont,
)

from app.config.cfg import cfg
from app.format import toReadableSize
from app.i18n import toLocalizedError
from app.models.task import TaskError
from app.view.components.editors import FolderPicker, addRecentFolder
from app.view.components.glass import WINDOW_SHADOW, GlassBadge, GlassButton, GlassCard, GlassWindow, toStatusColor

if TYPE_CHECKING:
    from app.models.task import Task
    from app.services.task_service import TaskService

WINDOW_WIDTH = 580
CAPTION_WIDTH = 76


def toMutedStyle(label: QLabel) -> None:
    label.setTextColor(QColor(0, 0, 0, 150), QColor(255, 255, 255, 160))


def toFreeSpace(folder: Path) -> int | None:
    # 文件夹可能还没创建，向上找到第一个存在的目录
    for path in (folder, *folder.parents):
        try:
            return disk_usage(path).free
        except OSError:
            continue
    return None


class DraftWindow(GlassWindow):
    """浏览器接管的下载在开始前先在这里确认文件名、保存位置和连接数。"""

    def __init__(self, task: Task, taskService: TaskService):
        super().__init__()
        self._task = task
        self._taskService = taskService
        self._isPositioned = False

        self.titleLabel = StrongBodyLabel(self.tr("新的下载"), self)
        self.closeButton = TransparentToolButton(FluentIcon.CLOSE, self)

        self.fileCard = GlassCard(self)
        self.iconLabel = QLabel(self.fileCard)
        self.nameEdit = LineEdit(self.fileCard)
        self.infoLabel = CaptionLabel(self.fileCard)
        self.resumableBadge = GlassBadge(self.fileCard)
        self.urlLabel = CaptionLabel(self.fileCard)
        self.copyButton = TransparentToolButton(FluentIcon.COPY, self.fileCard)

        self.optionCard = GlassCard(self)
        self.folderCaption = BodyLabel(self.tr("保存到"), self.optionCard)
        self.folderPicker = FolderPicker(lambda: cfg.downloadFolder.value, self.tr("默认"), self.optionCard)
        self.spaceLabel = CaptionLabel(self.optionCard)
        self.connectionCaption = BodyLabel(self.tr("连接数"), self.optionCard)
        self.connectionSlider = Slider(Qt.Orientation.Horizontal, self.optionCard)
        self.connectionBox = CompactSpinBox(self.optionCard)

        self.errorLabel = CaptionLabel(self)
        self.cancelButton = GlassButton(self.tr("取消"), parent=self)
        self.laterButton = GlassButton(self.tr("稍后下载"), FluentIcon.HISTORY, parent=self)
        self.startButton = GlassButton(self.tr("开始下载"), FluentIcon.DOWNLOAD, isPrimary=True, parent=self)

        self._initWidget()
        self._initLayout()
        self._bind()
        self._refreshSpace()

    def _initWidget(self) -> None:
        task = self._task
        icon = QFileIconProvider().icon(QFileInfo(task.name))
        self.setWindowIcon(icon)
        self.setWindowTitle(self.tr("新的下载"))
        self.setFixedWidth(WINDOW_WIDTH)

        self.closeButton.setFixedSize(30, 30)
        self.closeButton.setIconSize(self.closeButton.iconSize() * 0.8)
        self.iconLabel.setPixmap(icon.pixmap(48, 48))
        self.iconLabel.setFixedSize(48, 48)
        setFont(self.nameEdit, 15, QFont.Weight.DemiBold)
        self.nameEdit.setText(task.name)
        self.nameEdit.setClearButtonEnabled(True)

        parts = [toReadableSize(task.fileSize) if task.fileSize > 0 else self.tr("大小未知")]
        if host := urlparse(task.url).hostname:
            parts.append(host)
        self.infoLabel.setText(" · ".join(parts))
        toMutedStyle(self.infoLabel)
        if task.canPause:
            done = QColor(108, 203, 95) if isDarkTheme() else QColor(15, 123, 15)
            self.resumableBadge.setStatus(self.tr("支持断点续传"), done)
        else:
            self.resumableBadge.setStatus(self.tr("不支持断点续传"), toStatusColor(True, False))
        toMutedStyle(self.urlLabel)
        self.urlLabel.setToolTip(task.url)
        self.copyButton.setFixedSize(28, 28)
        self.copyButton.setToolTip(self.tr("复制下载链接"))

        for caption in (self.folderCaption, self.connectionCaption):
            caption.setFixedWidth(CAPTION_WIDTH)
        self.folderPicker.setPath(str(task.outputFolder))
        for widget in (self.connectionSlider, self.connectionBox):
            widget.setRange(*cfg.preBlockNum.range)
            widget.setValue(cfg.preBlockNum.value)
        self.connectionBox.setFixedWidth(96)

        self.errorLabel.setTextColor(QColor(196, 43, 28), QColor(255, 153, 164))
        self.errorLabel.setWordWrap(True)
        self.errorLabel.hide()

    def _initLayout(self) -> None:
        header = QHBoxLayout()
        header.addWidget(self.titleLabel)
        header.addStretch(1)
        header.addWidget(self.closeButton)

        file = QGridLayout(self.fileCard)
        file.setContentsMargins(18, 16, 14, 14)
        file.setHorizontalSpacing(14)
        file.setVerticalSpacing(6)
        file.addWidget(self.iconLabel, 0, 0, 2, 1, Qt.AlignmentFlag.AlignTop)
        file.addWidget(self.nameEdit, 0, 1, 1, 2)
        file.addWidget(self.infoLabel, 1, 1)
        file.addWidget(self.resumableBadge, 1, 2, Qt.AlignmentFlag.AlignRight)
        urlRow = QHBoxLayout()
        urlRow.setSpacing(4)
        urlRow.addWidget(self.urlLabel, 1)
        urlRow.addWidget(self.copyButton)
        file.addLayout(urlRow, 2, 0, 1, 3)
        file.setColumnStretch(1, 1)

        options = QGridLayout(self.optionCard)
        options.setContentsMargins(18, 14, 18, 14)
        options.setHorizontalSpacing(12)
        options.setVerticalSpacing(6)
        options.addWidget(self.folderCaption, 0, 0)
        options.addWidget(self.folderPicker, 0, 1, 1, 2)
        options.addWidget(self.spaceLabel, 1, 1, 1, 2)
        options.setRowMinimumHeight(2, 6)
        options.addWidget(self.connectionCaption, 3, 0)
        options.addWidget(self.connectionSlider, 3, 1)
        options.addWidget(self.connectionBox, 3, 2, Qt.AlignmentFlag.AlignRight)
        options.setColumnStretch(1, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.cancelButton)
        buttons.addStretch(1)
        buttons.addWidget(self.laterButton)
        buttons.addWidget(self.startButton)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(WINDOW_SHADOW + 20, WINDOW_SHADOW + 12, WINDOW_SHADOW + 20, WINDOW_SHADOW + 18)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addWidget(self.fileCard)
        layout.addWidget(self.optionCard)
        layout.addWidget(self.errorLabel)
        layout.addLayout(buttons)

    def _bind(self) -> None:
        self.closeButton.clicked.connect(self.close)
        self.cancelButton.clicked.connect(self.close)
        self.copyButton.clicked.connect(lambda: QApplication.clipboard().setText(self._task.url))
        self.folderPicker.lineEdit.textChanged.connect(self._refreshSpace)
        self.connectionSlider.valueChanged.connect(self.connectionBox.setValue)
        self.connectionBox.valueChanged.connect(self.connectionSlider.setValue)
        self.laterButton.clicked.connect(lambda: self._add(autoStart=False))
        self.startButton.clicked.connect(lambda: self._add(autoStart=True))
        self.nameEdit.returnPressed.connect(lambda: self._add(autoStart=True))

    def _refreshSpace(self) -> None:
        folder = self.folderPicker.path()
        free = toFreeSpace(Path(folder)) if folder else None
        needed = self._task.fileSize
        if free is None:
            self.spaceLabel.setText("")
            return
        if needed > free:
            self.spaceLabel.setText(self.tr("空间不足：需要 {0}，可用 {1}").format(
                toReadableSize(needed), toReadableSize(free)))
            self.spaceLabel.setTextColor(QColor(196, 43, 28), QColor(255, 153, 164))
        else:
            self.spaceLabel.setText(self.tr("可用空间 {0}").format(toReadableSize(free)))
            toMutedStyle(self.spaceLabel)

    def _add(self, autoStart: bool) -> None:
        task = self._task
        folder = self.folderPicker.path()
        if not folder:
            self._showError(self.tr("请选择保存位置"))
            return
        task.setName(self.nameEdit.text().strip() or task.name)
        task.setOptions({"outputFolder": Path(folder), "subworkerCount": self.connectionBox.value()})
        try:
            self._taskService.add(task, autoStart)
        except TaskError as e:
            self._showError(toLocalizedError(e))
            return
        addRecentFolder(folder)
        self.close()

    def _showError(self, text: str) -> None:
        self.errorLabel.setText(text)
        self.errorLabel.show()
        self.adjustSize()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.urlLabel.setText(self.urlLabel.fontMetrics().elidedText(
            self._task.url, Qt.TextElideMode.ElideMiddle, max(self.urlLabel.width(), 400)))
        if not self._isPositioned:
            self._isPositioned = True
            screen = QGuiApplication.screenAt(QCursor.pos()) or self.screen()
            self.adjustSize()
            self.move(screen.availableGeometry().center() - self.rect().center())
        self.nameEdit.setFocus()
        # 只选中主文件名，改名时不会误删扩展名；光标放在开头，长文件名从头显示
        stemLength = len(Path(self.nameEdit.text()).stem)
        self.nameEdit.setSelection(stemLength, -stemLength)
