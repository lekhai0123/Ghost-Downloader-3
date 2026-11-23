from __future__ import annotations

from dataclasses import dataclass, replace
from time import monotonic

from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPaintEvent
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, FluentIcon, isDarkTheme, setFont, themeColor

from app.format import toReadableSize
from app.models.task import Task, TaskStatus, SpecialFileSize
from app.view.cards.task_cards import TaskCard, ButtonState, TOGGLE_BUTTON
from app.view.components.glass import GlassCard, toStatusColor
from app.view.components.scroll_area import ScrollArea
from .task import HttpSubworker, HttpTaskStep

FILL_EASING = 0.25


class SegmentedProgressBar(QWidget):
    def __init__(self, step: HttpTaskStep, parent: QWidget | None = None):
        super().__init__(parent)
        self._step = step
        self._isPaused = False
        self._isError = False
        self._fillProgress: dict[int, float] = {}
        self._spans: list[tuple[float, float]] = []
        self._fillTimer = QTimer(self)
        self._fillTimer.setInterval(16)
        self._fillTimer.timeout.connect(self._onFillTimeout)

    def setValue(self, value: float) -> None:
        self._fillTimer.start()
        self._onFillTimeout()

    def _onFillTimeout(self):
        live = {sw.start: sw.position for sw in list(self._step.subworkers)}
        fresh = not self._fillProgress
        settled = True
        for start, target in live.items():
            shown = self._fillProgress.get(start, target if fresh else start)
            if target - shown > 1:
                self._fillProgress[start] = shown + (target - shown) * FILL_EASING
                settled = False
            else:
                self._fillProgress[start] = target
        self._fillProgress = {start: self._fillProgress[start] for start in live}
        self._spans = self._toMergedSpans()
        if not self._spans and self._step.receivedBytes > 0:
            self._spans = [(0, self._step.receivedBytes)]
        if settled:
            self._fillTimer.stop()
        self.update()

    def setError(self, isError: bool) -> None:
        self._isError = isError
        if not isError:
            self._isPaused = False
        self.update()

    def pause(self) -> None:
        self._isPaused = True
        self.update()

    def barColor(self) -> QColor:
        if self._isPaused:
            return QColor(252, 225, 0) if isDarkTheme() else QColor(157, 93, 0)
        if self._isError:
            return QColor(255, 153, 164) if isDarkTheme() else QColor(196, 43, 28)
        return themeColor()

    def _toMergedSpans(self) -> list[tuple[float, float]]:
        intervals = sorted(
            (start, shown) for start, shown in self._fillProgress.items() if shown > start
        )
        merged: list[tuple[float, float]] = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def paintEvent(self, event: QPaintEvent) -> None:
        fileSize = self._step.fileSize
        if fileSize <= 0 or not self._spans:
            return

        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.barColor())

        width = self.width()
        height = self.height()
        radius = height / 2
        for start, end in self._spans:
            x = start / fileSize * width
            w = (end - start) / fileSize * width
            painter.drawRoundedRect(QRectF(x, 0, w, height), radius, radius)


def finishableStep(task: Task) -> HttpTaskStep | None:
    for step in task.steps:
        if (step.status == TaskStatus.RUNNING and isinstance(step, HttpTaskStep)
                and not step.canUseRangeRequests and step.fileSize == SpecialFileSize.UNKNOWN):
            return step
    return None


def toRunningToggle(task: Task) -> ButtonState:
    if finishableStep(task) is not None:
        return ButtonState(icon=FluentIcon.ACCEPT)
    return ButtonState(icon=FluentIcon.PAUSE, enabled=task.canPause)


HTTP_TOGGLE_BUTTON = replace(TOGGLE_BUTTON, states={**TOGGLE_BUTTON.states, TaskStatus.RUNNING: toRunningToggle})


class HttpTaskCard(TaskCard):
    buttons = [HTTP_TOGGLE_BUTTON if b.name == "toggle" else b for b in TaskCard.buttons]

    def _refreshButtons(self, names=None) -> None:
        super()._refreshButtons(names)
        if finishableStep(self._task) is not None:
            self.toggleButton.setToolTip(self.tr("停止并定案"))

    def _onToggleClicked(self) -> None:
        if step := finishableStep(self._task):
            self.toggleButton.setEnabled(False)
            step.finish()
            return
        super()._onToggleClicked()

    def _createProgressBar(self) -> QWidget:
        step = self.task.steps[0] if self.task.steps else None
        if isinstance(step, HttpTaskStep) and step.canUseRangeRequests and step.fileSize > 0 and step.subworkerCount > 1:
            return SegmentedProgressBar(step, self)
        return super()._createProgressBar()


CONNECTION_ROW_HEIGHT = 30
VISIBLE_CONNECTION_ROWS = 6
SPEED_SMOOTHING = 0.5


def currentStep(task: Task) -> HttpTaskStep | None:
    steps = [step for step in task.steps if isinstance(step, HttpTaskStep)]
    running = [step for step in steps if step.status == TaskStatus.RUNNING]
    withSubworkers = [step for step in steps if step.subworkers]
    return (running or withSubworkers[-1:] or steps or [None])[0]


def toMutedColor() -> QColor:
    return QColor(255, 255, 255, 150) if isDarkTheme() else QColor(0, 0, 0, 140)


def toDoneColor() -> QColor:
    return QColor(108, 203, 95) if isDarkTheme() else QColor(15, 123, 15)


class SegmentMap(QWidget):
    """整个文件的字节轴：每段是一个连接负责的范围，亮色为已下载，白色刻线为正在写入的位置。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.subworkers: list[HttpSubworker] = []
        self.fileSize = 0
        self.isRunning = False
        self.isPaused = False
        self.setFixedHeight(18)

    def setSubworkers(self, subworkers: list[HttpSubworker], fileSize: int, isRunning: bool, isPaused: bool) -> None:
        self.subworkers, self.fileSize, self.isRunning, self.isPaused = subworkers, fileSize, isRunning, isPaused
        self.update()

    def paintEvent(self, e: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        isDark = isDarkTheme()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        track = QPainterPath()
        track.addRoundedRect(rect, 6, 6)
        painter.fillPath(track, QColor(0, 0, 0, 70) if isDark else QColor(0, 0, 0, 16))
        if self.fileSize <= 0:
            return
        painter.save()
        painter.setClipPath(track)
        color = toStatusColor(self.isPaused, False)
        fill = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        fill.setColorAt(0, color.lighter(125))
        fill.setColorAt(1, color.darker(110))
        divider = QColor(255, 255, 255, 90) if isDark else QColor(255, 255, 255, 220)
        scale = rect.width() / self.fileSize
        painter.setPen(Qt.PenStyle.NoPen)
        for sw in self.subworkers:
            x = rect.left() + sw.start * scale
            done = min(sw.position, sw.end + 1) - sw.start
            painter.fillRect(QRectF(x, rect.top(), done * scale, rect.height()), fill)
            if sw.start > 0:
                painter.fillRect(QRectF(x, rect.top(), 1, rect.height()), divider)
            if self.isRunning and sw.position <= sw.end:
                painter.setBrush(QColor(255, 255, 255, 230))
                painter.drawRoundedRect(QRectF(x + done * scale - 1.5, rect.top() + 3, 3, rect.height() - 6), 1.5, 1.5)
        gloss = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gloss.setColorAt(0, QColor(255, 255, 255, 70))
        gloss.setColorAt(0.5, QColor(255, 255, 255, 0))
        painter.fillPath(track, gloss)
        painter.restore()
        painter.setPen(QColor(255, 255, 255, 50) if isDark else QColor(255, 255, 255, 230))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(track)


@dataclass(frozen=True)
class ConnectionRow:
    number: str
    fraction: float | None
    received: str
    speed: str
    status: str
    isActive: bool
    isFinished: bool


class ConnectionList(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[ConnectionRow] = []
        self.isPaused = False
        setFont(self, 12)

    def setRows(self, rows: list[ConnectionRow]) -> None:
        self.rows = rows
        self.setFixedHeight(max(1, len(rows)) * CONNECTION_ROW_HEIGHT)
        self.update()

    def paintEvent(self, e: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        isDark = isDarkTheme()
        # 行数超出可见范围时给滚动条让出位置
        width = self.width() - (14 if len(self.rows) > VISIBLE_CONNECTION_ROWS else 0)
        foreground = QColor(255, 255, 255) if isDark else QColor(24, 24, 24)
        muted = toMutedColor()
        accent = themeColor().lighter(130) if isDark else themeColor()
        right = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight
        for i, row in enumerate(self.rows):
            top = i * CONNECTION_ROW_HEIGHT
            if i % 2 == 0:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 255, 255, 8) if isDark else QColor(255, 255, 255, 70))
                painter.drawRoundedRect(QRectF(0, top + 1, width, CONNECTION_ROW_HEIGHT - 2), 8, 8)
            painter.setPen(muted)
            painter.drawText(QRectF(8, top, 32, CONNECTION_ROW_HEIGHT), Qt.AlignmentFlag.AlignVCenter, row.number)

            barRect = QRectF(44, top + CONNECTION_ROW_HEIGHT / 2 - 3, max(0, width - 44 - 330), 6)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 80) if isDark else QColor(0, 0, 0, 20))
            painter.drawRoundedRect(barRect, 3, 3)
            if row.fraction:
                painter.setBrush(toDoneColor() if row.isFinished else toStatusColor(self.isPaused, False))
                painter.drawRoundedRect(QRectF(barRect.left(), barRect.top(), barRect.width() * row.fraction, 6), 3, 3)

            painter.setPen(foreground)
            painter.drawText(QRectF(width - 322, top, 90, CONNECTION_ROW_HEIGHT), right, row.received)
            painter.drawText(QRectF(width - 226, top, 90, CONNECTION_ROW_HEIGHT), right, row.speed)
            painter.setPen(accent if row.isActive else toDoneColor() if row.isFinished else muted)
            painter.drawText(QRectF(width - 130, top, 122, CONNECTION_ROW_HEIGHT), right, row.status)


class ConnectionPanel(GlassCard):
    """下载窗口中的连接详情：分段图和每个连接的已下载量、速度与状态。"""

    def __init__(self, task: Task, parent=None):
        super().__init__(parent)
        self._task = task
        self._lastSamples: dict[int, tuple[int, float]] = {}
        self._speeds: dict[int, float] = {}
        self.titleLabel = BodyLabel(self.tr("连接"), self)
        self.summaryLabel = CaptionLabel(self)
        self.segmentMap = SegmentMap(self)
        self.emptyLabel = CaptionLabel(self.tr("开始下载后显示各连接的进度"), self)
        self.scrollArea = ScrollArea(self)
        self.connectionList = ConnectionList()
        self._initWidget()
        self._initLayout()

    def _initWidget(self) -> None:
        setFont(self.titleLabel, 14, QFont.Weight.DemiBold)
        for label in (self.summaryLabel, self.emptyLabel):
            label.setTextColor(QColor(0, 0, 0, 150), QColor(255, 255, 255, 160))
        self.scrollArea.setWidget(self.connectionList)
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scrollArea.enableTransparentBackground()
        self.connectionList.setStyleSheet("background: transparent")

    def _initLayout(self) -> None:
        title = QHBoxLayout()
        title.addWidget(self.titleLabel)
        title.addStretch(1)
        title.addWidget(self.summaryLabel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)
        layout.addLayout(title)
        layout.addWidget(self.segmentMap)
        layout.addWidget(self.emptyLabel)
        layout.addWidget(self.scrollArea)

    def refresh(self) -> None:
        step = currentStep(self._task)
        subworkers = sorted(list(step.subworkers), key=lambda sw: sw.start) if step else []
        isRunning = step is not None and step.status == TaskStatus.RUNNING
        isPaused = step is not None and step.status in {TaskStatus.PAUSED, TaskStatus.WAITING}
        hasRanges = step is not None and step.canUseRangeRequests and step.fileSize > 0

        self.segmentMap.setVisible(hasRanges)
        self.segmentMap.setSubworkers(subworkers, step.fileSize if hasRanges else 0, isRunning, isPaused)
        self.emptyLabel.setVisible(not subworkers)
        self.scrollArea.setVisible(bool(subworkers))

        rows = [self._toRow(number, sw, isRunning, hasRanges) for number, sw in enumerate(subworkers, 1)]
        alive = {id(sw) for sw in subworkers}
        self._lastSamples = {k: v for k, v in self._lastSamples.items() if k in alive}
        self._speeds = {k: v for k, v in self._speeds.items() if k in alive}

        self.connectionList.isPaused = isPaused
        self.connectionList.setRows(rows)
        self.scrollArea.setFixedHeight(min(len(rows), VISIBLE_CONNECTION_ROWS) * CONNECTION_ROW_HEIGHT + 2)
        activeCount = sum(row.isActive for row in rows)
        if isRunning:
            self.summaryLabel.setText(self.tr("{0} / {1} 个连接正在工作").format(activeCount, len(rows)))
        else:
            self.summaryLabel.setText(self.tr("共 {0} 个连接").format(len(rows)) if rows else "")

    def _toRow(self, number: int, sw: HttpSubworker, isRunning: bool, hasRanges: bool) -> ConnectionRow:
        key, now = id(sw), monotonic()
        lastReceived, lastTime = self._lastSamples.get(key, (sw.receivedBytes, now))
        if now > lastTime:
            instant = max(0, sw.receivedBytes - lastReceived) / (now - lastTime)
            self._speeds[key] = self._speeds.get(key, instant) * (1 - SPEED_SMOOTHING) + instant * SPEED_SMOOTHING
        self._lastSamples[key] = (sw.receivedBytes, now)
        speed = self._speeds.get(key, 0)

        isFinished = hasRanges and sw.position > sw.end
        isActive = isRunning and not isFinished
        size = sw.end - sw.start + 1 if hasRanges else 0
        if isFinished:
            status = self.tr("已完成")
        elif not isRunning:
            status = self.tr("已暂停")
        elif speed >= 1:
            status = self.tr("正在接收数据")
        else:
            status = self.tr("正在连接")
        return ConnectionRow(
            number=f"#{number}",
            fraction=min(1.0, sw.receivedBytes / size) if size > 0 else None,
            received=toReadableSize(sw.receivedBytes),
            speed=f"{toReadableSize(int(speed))}/s" if isActive else "--",
            status=status,
            isActive=isActive,
            isFinished=isFinished,
        )
