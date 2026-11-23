from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QFileInfo, Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication, QFileIconProvider, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CheckBox, Dialog, FluentIcon, TitleLabel, TransparentToolButton, isDarkTheme, setFont,
)

from app.format import toReadableSize
from app.models.task import TaskStatus
from app.platform.desktop import openFile, revealInFolder
from app.view.components.glass import (
    WINDOW_SHADOW, GlassBadge, GlassButton, GlassCard, GlassWindow, LiquidProgressBar, SpeedGraph, toStatusColor,
)
from app.view.shell.task_popup import toRemainingText

if TYPE_CHECKING:
    from app.models.task import Task
    from app.services.feature_service import FeatureService
    from app.services.task_service import TaskService

WINDOW_WIDTH = 600
# 只有进度条按动画帧率重绘；半透明窗口整窗重绘一次约 40ms，所以背景是静止的
FRAME_INTERVAL = 33
REFRESH_INTERVAL = 1000


def toMutedStyle(label: QLabel) -> None:
    label.setTextColor(QColor(0, 0, 0, 150), QColor(255, 255, 255, 160))


class StatTile(QWidget):
    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        self.captionLabel = CaptionLabel(caption, self)
        self.valueLabel = BodyLabel("--", self)
        toMutedStyle(self.captionLabel)
        setFont(self.valueLabel, 14, QFont.Weight.DemiBold)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.captionLabel)
        layout.addWidget(self.valueLabel)

    def setValue(self, text: str) -> None:
        self.valueLabel.setText(text)


class TaskWindow(GlassWindow):
    """单个任务的下载窗口：实时进度、速度曲线和各协议提供的连接详情。"""

    def __init__(self, task: Task, taskService: TaskService, featureService: FeatureService):
        super().__init__()
        self._task = task
        self._taskService = taskService
        self._phase = 0.0
        self._runningSamples: list[int] = []

        self.iconLabel = QLabel(self)
        self.nameLabel = BodyLabel(self)
        self.urlLabel = CaptionLabel(self)
        self.copyButton = TransparentToolButton(FluentIcon.COPY, self)
        self.minimizeButton = TransparentToolButton(FluentIcon.MINIMIZE, self)
        self.closeButton = TransparentToolButton(FluentIcon.CLOSE, self)

        self.heroCard = GlassCard(self)
        self.percentLabel = TitleLabel(self.heroCard)
        self.badge = GlassBadge(self.heroCard)
        self.speedLabel = BodyLabel(self.heroCard)
        self.remainingLabel = CaptionLabel(self.heroCard)
        self.progressBar = LiquidProgressBar(self.heroCard)

        self.statsCard = GlassCard(self)
        self.sizeTile = StatTile(self.tr("文件大小"), self.statsCard)
        self.receivedTile = StatTile(self.tr("已下载"), self.statsCard)
        self.averageTile = StatTile(self.tr("平均速度"), self.statsCard)
        self.finishTile = StatTile(self.tr("预计完成于"), self.statsCard)
        self.resumableTile = StatTile(self.tr("支持断点续传"), self.statsCard)
        self.folderTile = StatTile(self.tr("保存位置"), self.statsCard)

        self.detailsWidget = QWidget(self)
        self.graphCard = GlassCard(self.detailsWidget)
        self.speedGraph = SpeedGraph(self.graphCard)
        self.panels = featureService.progressPanels(task, self.detailsWidget)

        self.detailsButton = GlassButton(self.tr("显示详情"), FluentIcon.DOWN, parent=self)
        self.cancelButton = GlassButton(self.tr("取消下载"), FluentIcon.CLOSE, parent=self)
        self.folderButton = GlassButton(self.tr("打开文件夹"), FluentIcon.FOLDER, parent=self)
        self.toggleButton = GlassButton("", isPrimary=True, parent=self)

        self.frameTimer = QTimer(self)
        self.refreshTimer = QTimer(self)
        self._initWidget()
        self._initLayout()
        self._bind()
        self.refresh()

    def _initWidget(self) -> None:
        task = self._task
        icon = QFileIconProvider().icon(QFileInfo(task.outputPath))
        self.setWindowIcon(icon)
        self.setWindowTitle(task.name)
        self.setFixedWidth(WINDOW_WIDTH)

        self.iconLabel.setPixmap(icon.pixmap(40, 40))
        self.iconLabel.setFixedSize(40, 40)
        setFont(self.nameLabel, 15, QFont.Weight.DemiBold)
        self.nameLabel.setToolTip(task.name)
        toMutedStyle(self.urlLabel)
        self.urlLabel.setToolTip(task.url)
        for button in (self.copyButton, self.minimizeButton, self.closeButton):
            button.setFixedSize(30, 30)
            button.setIconSize(button.iconSize() * 0.8)
        self.copyButton.setToolTip(self.tr("复制下载链接"))

        setFont(self.percentLabel, 40, QFont.Weight.Bold)
        setFont(self.speedLabel, 16, QFont.Weight.DemiBold)
        toMutedStyle(self.remainingLabel)
        self.folderTile.valueLabel.setToolTip(str(task.outputFolder))
        self.speedGraph.setMinimumHeight(110)
        self.detailsWidget.hide()

        self.frameTimer.setInterval(FRAME_INTERVAL)
        self.refreshTimer.setInterval(REFRESH_INTERVAL)

    def _initLayout(self) -> None:
        header = QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(self.iconLabel)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(self.nameLabel)
        urlRow = QHBoxLayout()
        urlRow.setSpacing(2)
        urlRow.addWidget(self.urlLabel, 1)
        urlRow.addWidget(self.copyButton)
        titles.addLayout(urlRow)
        header.addLayout(titles, 1)
        header.addWidget(self.minimizeButton, 0, Qt.AlignmentFlag.AlignTop)
        header.addWidget(self.closeButton, 0, Qt.AlignmentFlag.AlignTop)

        hero = QGridLayout(self.heroCard)
        hero.setContentsMargins(20, 14, 20, 18)
        hero.setHorizontalSpacing(16)
        hero.setVerticalSpacing(6)
        hero.addWidget(self.percentLabel, 0, 0, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        hero.addWidget(self.speedLabel, 0, 1, Qt.AlignmentFlag.AlignBottom)
        hero.addWidget(self.badge, 0, 2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        hero.addWidget(self.remainingLabel, 1, 1, 1, 2, Qt.AlignmentFlag.AlignTop)
        hero.addWidget(self.progressBar, 2, 0, 1, 3)
        hero.setColumnStretch(1, 1)

        stats = QGridLayout(self.statsCard)
        stats.setContentsMargins(20, 14, 20, 14)
        stats.setHorizontalSpacing(16)
        stats.setVerticalSpacing(12)
        tiles = (self.sizeTile, self.receivedTile, self.averageTile,
                 self.finishTile, self.resumableTile, self.folderTile)
        for i, tile in enumerate(tiles):
            stats.addWidget(tile, i // 3, i % 3)
        for column in range(3):
            stats.setColumnStretch(column, 1)

        graph = QVBoxLayout(self.graphCard)
        graph.setContentsMargins(16, 10, 16, 12)
        graph.addWidget(self.speedGraph)

        details = QVBoxLayout(self.detailsWidget)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(12)
        details.addWidget(self.graphCard)
        for panel in self.panels:
            details.addWidget(panel)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.detailsButton)
        buttons.addStretch(1)
        buttons.addWidget(self.cancelButton)
        buttons.addWidget(self.folderButton)
        buttons.addWidget(self.toggleButton)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(WINDOW_SHADOW + 20, WINDOW_SHADOW + 16, WINDOW_SHADOW + 20, WINDOW_SHADOW + 18)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addWidget(self.heroCard)
        layout.addWidget(self.statsCard)
        layout.addWidget(self.detailsWidget)
        layout.addLayout(buttons)

    def _bind(self) -> None:
        self.copyButton.clicked.connect(lambda: QApplication.clipboard().setText(self._task.url))
        self.minimizeButton.clicked.connect(self.showMinimized)
        self.closeButton.clicked.connect(self.close)
        self.detailsButton.clicked.connect(self._onDetailsClicked)
        self.folderButton.clicked.connect(lambda: revealInFolder(self._task.outputPath))
        self.cancelButton.clicked.connect(self._onCancelClicked)
        self.toggleButton.clicked.connect(self._onToggleClicked)
        self.frameTimer.timeout.connect(self._onFrameTimeout)
        self.refreshTimer.timeout.connect(self._onRefreshTimeout)
        self._taskService.taskRemoved.connect(self._onTaskRemoved, owner=self)
        for signal in (self._taskService.taskStarted, self._taskService.taskPaused,
                       self._taskService.taskCompleted, self._taskService.taskFailed):
            signal.connect(self._onTaskChanged, owner=self)

    def refresh(self) -> None:
        task = self._task
        status = task.status
        progress, speed, received = task.currentSnapshot()
        isRunning = status == TaskStatus.RUNNING
        isCompleted = status == TaskStatus.COMPLETED
        hasSize = task.fileSize > 0
        if isCompleted:
            progress, received = 100, max(received, task.fileSize)

        self.nameLabel.setText(self.nameLabel.fontMetrics().elidedText(
            task.name, Qt.TextElideMode.ElideMiddle, self.nameLabel.width() or 380))
        self.urlLabel.setText(self.urlLabel.fontMetrics().elidedText(
            task.url, Qt.TextElideMode.ElideMiddle, self.urlLabel.width() or 340))

        self.percentLabel.setText(f"{progress:.0f}%" if hasSize or isCompleted else "--")
        self.speedLabel.setText(f"{toReadableSize(speed)}/s" if isRunning else "")
        if isRunning and speed > 0 and hasSize:
            self.remainingLabel.setText(self.tr("剩余 {0}").format(
                toRemainingText(int((task.fileSize - received) / speed))))
        elif isRunning:
            self.remainingLabel.setText(self.tr("正在计算剩余时间"))
        else:
            self.remainingLabel.setText(self._toStatusText())
        self.badge.setStatus(self._toStatusText(), self._toStatusColor())

        isPaused = status in {TaskStatus.PAUSED, TaskStatus.WAITING}
        self.progressBar.setValue(progress if hasSize or isCompleted else None)
        self.progressBar.setStatus(isPaused, status == TaskStatus.FAILED)
        self.speedGraph.isPaused = isPaused

        self.sizeTile.setValue(toReadableSize(task.fileSize) if hasSize else self.tr("未知"))
        self.receivedTile.setValue(toReadableSize(received))
        samples = self._runningSamples
        self.averageTile.setValue(f"{toReadableSize(sum(samples) // len(samples))}/s" if samples else "--")
        self.finishTile.setValue(self._toFinishText(speed, received))
        self.resumableTile.setValue(self.tr("是") if task.canPause or isCompleted else self.tr("否"))
        self.folderTile.setValue(self.folderTile.valueLabel.fontMetrics().elidedText(
            str(task.outputFolder), Qt.TextElideMode.ElideLeft, 150))

        self.cancelButton.setVisible(not isCompleted)
        if isCompleted:
            self.toggleButton.setText(self.tr("打开文件"))
            self.toggleButton.setIcon(FluentIcon.DOCUMENT)
            self.toggleButton.setEnabled(True)
        elif status == TaskStatus.FAILED:
            self.toggleButton.setText(self.tr("重试"))
            self.toggleButton.setIcon(FluentIcon.SYNC)
            self.toggleButton.setEnabled(True)
        elif isRunning:
            self.toggleButton.setText(self.tr("暂停"))
            self.toggleButton.setIcon(FluentIcon.PAUSE)
            self.toggleButton.setEnabled(task.canPause)
        else:
            self.toggleButton.setText(self.tr("继续"))
            self.toggleButton.setIcon(FluentIcon.PLAY)
            self.toggleButton.setEnabled(True)

        for panel in self.panels:
            panel.refresh()

        if isRunning and self.isVisible():
            self.frameTimer.start()
        else:
            self.frameTimer.stop()
            self.progressBar.setPhase(self._phase)

    def _toFinishText(self, speed: int, received: int) -> str:
        task = self._task
        if task.status == TaskStatus.COMPLETED and task.completedAt:
            return datetime.fromtimestamp(task.completedAt).strftime("%H:%M")
        if task.status != TaskStatus.RUNNING or speed <= 0 or task.fileSize <= 0:
            return "--"
        finish = datetime.now() + timedelta(seconds=(task.fileSize - received) / speed)
        return finish.strftime("%H:%M" if finish.date() == datetime.now().date() else "%m-%d %H:%M")

    def _toStatusText(self) -> str:
        status = self._task.status
        if status == TaskStatus.RUNNING:
            return self.tr("正在接收数据")
        if status == TaskStatus.COMPLETED:
            return self.tr("下载完成")
        if status == TaskStatus.FAILED:
            return self.tr("下载失败")
        if status == TaskStatus.PAUSED:
            return self.tr("已暂停")
        return self.tr("等待中")

    def _toStatusColor(self) -> QColor:
        status = self._task.status
        if status == TaskStatus.COMPLETED:
            return QColor(108, 203, 95) if isDarkTheme() else QColor(15, 123, 15)
        return toStatusColor(status in {TaskStatus.PAUSED, TaskStatus.WAITING}, status == TaskStatus.FAILED)

    def _onFrameTimeout(self) -> None:
        self._phase += FRAME_INTERVAL / 1000
        self.progressBar.setPhase(self._phase)

    def _onRefreshTimeout(self) -> None:
        _, speed, _ = self._task.currentSnapshot()
        isRunning = self._task.status == TaskStatus.RUNNING
        self.speedGraph.add(speed if isRunning else 0)
        if isRunning:
            self._runningSamples.append(speed)
        self.refresh()

    def _onTaskChanged(self, task: Task) -> None:
        if task is self._task:
            self.refresh()

    def _onTaskRemoved(self, taskId: str) -> None:
        if taskId == self._task.taskId:
            self.close()

    def _onDetailsClicked(self) -> None:
        isShown = not self.detailsWidget.isVisible()
        self.detailsWidget.setVisible(isShown)
        self.detailsButton.setText(self.tr("隐藏详情") if isShown else self.tr("显示详情"))
        self.detailsButton.setIcon(FluentIcon.UP if isShown else FluentIcon.DOWN)
        QTimer.singleShot(0, self.adjustSize)

    def _onCancelClicked(self) -> None:
        # 遮罩式对话框会盖住半透明的阴影边距，改用独立的对话框窗口
        dialog = Dialog(self.tr("取消下载"), self.tr("确定要取消下载这个文件吗？"), self)
        dialog.setTitleBarVisible(False)
        dialog.yesButton.setText(self.tr("取消下载"))
        dialog.cancelButton.setText(self.tr("继续下载"))
        # 未完成的文件不能用，默认连同已下载的部分一起删除
        deleteFiles = CheckBox(self.tr("同时删除已下载的部分"))
        deleteFiles.setChecked(True)
        dialog.textLayout.addWidget(deleteFiles)
        if dialog.exec():
            self._taskService.delete(self._task, deleteFiles.isChecked())
        dialog.deleteLater()

    def _onToggleClicked(self) -> None:
        task = self._task
        if task.status == TaskStatus.COMPLETED:
            openFile(task.outputPath)
        elif task.status == TaskStatus.RUNNING:
            self._taskService.pause(task)
        else:
            self._taskService.start(task)
        self.refresh()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.refreshTimer.start()
        self.refresh()

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self.frameTimer.stop()

    def changeEvent(self, e) -> None:
        super().changeEvent(e)
        if e.type() != QEvent.Type.WindowStateChange:
            return
        if self.isMinimized():
            self.frameTimer.stop()
        elif self._task.status == TaskStatus.RUNNING and self.isVisible():
            self.frameTimer.start()

    def closeEvent(self, e) -> None:
        self.frameTimer.stop()
        self.refreshTimer.stop()
        super().closeEvent(e)
