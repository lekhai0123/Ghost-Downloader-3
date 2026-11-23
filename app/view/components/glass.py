from __future__ import annotations

from collections import deque
from functools import cache
from math import sin
from random import Random

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient,
)
from PySide6.QtWidgets import QAbstractButton, QSizePolicy, QWidget
from qfluentwidgets import FluentIconBase, isDarkTheme, qconfig, setFont, themeColor

from app.format import toReadableSize

GLASS_RADIUS = 14
WINDOW_SHADOW = 16
WINDOW_RADIUS = 18
# 背景按 1/4 分辨率绘制再平滑放大，色块本身就是柔和的渐变，放大后看不出差别
BACKDROP_SCALE = 4
SHIMMER_PERIOD = 2.4


def toStatusColor(isPaused: bool, isError: bool) -> QColor:
    if isPaused:
        return QColor(252, 200, 60) if isDarkTheme() else QColor(196, 120, 0)
    if isError:
        return QColor(255, 120, 130) if isDarkTheme() else QColor(196, 43, 28)
    return themeColor()


def buildBackdrop(size: QSize, isDark: bool) -> QImage:
    """磨砂玻璃的底：中性灰底上只透出一点主题色，再撒一层细噪点。"""
    width, height = max(1, size.width() // BACKDROP_SCALE), max(1, size.height() // BACKDROP_SCALE)
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(32, 33, 38) if isDark else QColor(236, 238, 242))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    hue = max(themeColor().hsvHue(), 0)
    radius = max(width, height) * 0.7
    # (色相偏移, 横向位置, 纵向位置)
    for hueOffset, x, y in ((0, 0.95, 0.05), (-18, 0.05, 0.95)):
        center = QPointF(width * x, height * y)
        color = QColor.fromHsv((hue + hueOffset) % 360, 120 if isDark else 70, 170 if isDark else 255)
        gradient = QRadialGradient(center, radius)
        color.setAlpha(70 if isDark else 90)
        gradient.setColorAt(0, color)
        color.setAlpha(0)
        gradient.setColorAt(1, color)
        painter.setBrush(gradient)
        painter.drawEllipse(center, radius, radius)
    painter.end()
    return image


@cache
def buildGrain(isDark: bool) -> QPixmap:
    """磨砂质感的噪点贴图，平铺使用。"""
    size = 96
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    rng = Random(7)
    for y in range(size):
        for x in range(size):
            level = rng.randrange(0, 256)
            alpha = 9 if isDark else 12
            image.setPixelColor(x, y, QColor(level, level, level, alpha))
    return QPixmap.fromImage(image)


def paintGlass(painter: QPainter, rect: QRectF, radius: float = GLASS_RADIUS) -> None:
    """乳白半透明底 + 顶部柔光 + 细描边，像一块磨砂玻璃。"""
    isDark = isDarkTheme()
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.fillPath(path, QColor(255, 255, 255, 14) if isDark else QColor(255, 255, 255, 150))

    sheen = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + min(rect.height(), 60)))
    sheen.setColorAt(0, QColor(255, 255, 255, 14 if isDark else 70))
    sheen.setColorAt(1, QColor(255, 255, 255, 0))
    painter.fillPath(path, sheen)

    rim = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    rim.setColorAt(0, QColor(255, 255, 255, 40 if isDark else 255))
    rim.setColorAt(1, QColor(255, 255, 255, 12 if isDark else 150))
    painter.setPen(QPen(QBrush(rim), 1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


class GlassCard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def paintEvent(self, e) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paintGlass(painter, QRectF(self.rect()))


class GlassWindow(QWidget):
    """无边框的磨砂玻璃窗口：自绘阴影和背景，拖动空白处移动，Esc 关闭。

    内容须留出 WINDOW_SHADOW 的边距给阴影。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._background = QPixmap()
        self._backgroundKey: tuple | None = None
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        qconfig.themeChanged.connect(self._onThemeChanged)

    def _onThemeChanged(self) -> None:
        self._backgroundKey = None
        self.update()

    def closeEvent(self, e) -> None:
        qconfig.themeChanged.disconnect(self._onThemeChanged)
        super().closeEvent(e)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(e)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()
        super().mousePressEvent(e)

    def paintEvent(self, e) -> None:
        # 子控件每次重绘都会让背后这一块跟着重绘，阴影和背景缓存成位图后只需贴图
        ratio = self.devicePixelRatioF()
        key = (self.size(), ratio, isDarkTheme())
        if self._backgroundKey != key:
            self._background = self._buildBackground(ratio)
            self._backgroundKey = key
        QPainter(self).drawPixmap(0, 0, self._background)

    def _buildBackground(self, ratio: float) -> QPixmap:
        isDark = isDarkTheme()
        pixmap = QPixmap(self.size() * ratio)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        body = QRectF(self.rect()).adjusted(WINDOW_SHADOW, WINDOW_SHADOW, -WINDOW_SHADOW, -WINDOW_SHADOW)

        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(WINDOW_SHADOW, 0, -2):
            alpha = int(36 * (1 - i / WINDOW_SHADOW) ** 2)
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(body.adjusted(-i, -i + 4, i, i + 4), WINDOW_RADIUS + i, WINDOW_RADIUS + i)

        path = QPainterPath()
        path.addRoundedRect(body, WINDOW_RADIUS, WINDOW_RADIUS)
        painter.setClipPath(path)
        painter.drawImage(body, buildBackdrop(body.size().toSize(), isDark))
        painter.fillRect(body, QColor(24, 25, 30, 70) if isDark else QColor(255, 255, 255, 90))
        painter.fillRect(body, QBrush(buildGrain(isDark)))
        painter.setClipping(False)

        painter.setPen(QColor(255, 255, 255, 30) if isDark else QColor(255, 255, 255, 220))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), WINDOW_RADIUS, WINDOW_RADIUS)
        painter.end()
        return pixmap


class GlassButton(QAbstractButton):
    """药丸形玻璃按钮；isPrimary 时以主题色填充。"""

    def __init__(self, text: str = "", icon: FluentIconBase | None = None, isPrimary: bool = False, parent=None):
        super().__init__(parent)
        self._icon = icon
        self.isPrimary = isPrimary
        self._isHovered = False
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        setFont(self, 13, QFont.Weight.DemiBold)

    def setIcon(self, icon: FluentIconBase | None) -> None:
        self._icon = icon
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        textWidth = self.fontMetrics().horizontalAdvance(self.text())
        iconWidth = 16 + (6 if self.text() else 0) if self._icon else 0
        return QSize(max(34, textWidth + iconWidth + (32 if self.text() else 18)), 34)

    def enterEvent(self, e) -> None:
        self._isHovered = True
        self.update()

    def leaveEvent(self, e) -> None:
        self._isHovered = False
        self.update()

    def paintEvent(self, e) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        isDark = isDarkTheme()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2
        if not self.isEnabled():
            painter.setOpacity(0.45)
        if self.isPrimary:
            color = themeColor()
            fill = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            fill.setColorAt(0, color.lighter(125 if self._isHovered else 112))
            fill.setColorAt(1, color.darker(105 if self._isHovered else 115))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, radius, radius)
            gloss = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.center().y()))
            gloss.setColorAt(0, QColor(255, 255, 255, 90))
            gloss.setColorAt(1, QColor(255, 255, 255, 0))
            painter.setBrush(gloss)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -rect.height() / 2), radius - 1, radius - 1)
            painter.setPen(QPen(QColor(255, 255, 255, 70), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, radius, radius)
            foreground = QColor(255, 255, 255) if color.lightness() < 170 else QColor(0, 0, 0)
        else:
            paintGlass(painter, rect, radius)
            if self._isHovered or self.isDown():
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 255, 255, 24 if isDark else 120) if not self.isDown()
                                 else QColor(0, 0, 0, 30 if isDark else 14))
                painter.drawRoundedRect(rect, radius, radius)
            foreground = QColor(255, 255, 255) if isDark else QColor(26, 26, 26)

        textWidth = self.fontMetrics().horizontalAdvance(self.text()) if self.text() else 0
        contentWidth = textWidth + ((16 + (6 if textWidth else 0)) if self._icon else 0)
        x = (self.width() - contentWidth) / 2
        if self._icon:
            iconRect = QRectF(x, (self.height() - 16) / 2, 16, 16)
            self._icon.render(painter, iconRect, fill=foreground.name())
            x += 22
        if textWidth:
            painter.setPen(foreground)
            painter.drawText(QRectF(x, 0, textWidth + 1, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text())


class LiquidProgressBar(QWidget):
    """玻璃管中的液体进度条。value 为 None 时显示来回流动的液滴。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._target: float | None = 0
        self._shown = 0.0
        self._phase = 0.0
        self.isPaused = False
        self.isError = False
        self.setFixedHeight(14)

    def setValue(self, value: float | None) -> None:
        self._target = value
        self.update()

    def setPhase(self, phase: float) -> None:
        """由窗口的动画时钟推进；进度朝目标值缓动。"""
        self._phase = phase
        if self._target is not None:
            self._shown += (self._target - self._shown) * 0.12
        self.update()

    def setStatus(self, isPaused: bool, isError: bool) -> None:
        self.isPaused, self.isError = isPaused, isError
        self.update()

    def paintEvent(self, e) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        isDark = isDarkTheme()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2
        track = QPainterPath()
        track.addRoundedRect(rect, radius, radius)
        painter.fillPath(track, QColor(0, 0, 0, 60) if isDark else QColor(0, 0, 0, 18))

        if self._target is None:
            width = rect.width() * 0.28
            x = rect.left() + (rect.width() - width) * (0.5 + 0.5 * sin(self._phase * 1.6))
            fillRect = QRectF(x, rect.top(), width, rect.height())
        else:
            if self._shown == 0 and self._target:
                self._shown = self._target
            fillRect = QRectF(rect.left(), rect.top(), rect.width() * min(self._shown, 100) / 100, rect.height())
        if fillRect.width() > 0.5:
            color = toStatusColor(self.isPaused, self.isError)
            painter.save()
            painter.setClipPath(track)
            fill = QLinearGradient(fillRect.topLeft(), fillRect.topRight())
            fill.setColorAt(0, color.darker(115))
            fill.setColorAt(1, color.lighter(130))
            fillPath = QPainterPath()
            fillPath.addRoundedRect(fillRect, radius, radius)
            painter.fillPath(fillPath, fill)
            if not (self.isPaused or self.isError):
                # 一道斜向光带周期性扫过液面
                sweep = (self._phase % SHIMMER_PERIOD) / SHIMMER_PERIOD
                center = fillRect.left() - 60 + (fillRect.width() + 120) * sweep
                shimmer = QLinearGradient(QPointF(center - 50, 0), QPointF(center + 50, rect.height()))
                shimmer.setColorAt(0, QColor(255, 255, 255, 0))
                shimmer.setColorAt(0.5, QColor(255, 255, 255, 110))
                shimmer.setColorAt(1, QColor(255, 255, 255, 0))
                painter.fillPath(fillPath, shimmer)
            gloss = QLinearGradient(fillRect.topLeft(), fillRect.bottomLeft())
            gloss.setColorAt(0, QColor(255, 255, 255, 120))
            gloss.setColorAt(0.5, QColor(255, 255, 255, 0))
            painter.fillPath(fillPath, gloss)
            painter.restore()

        rim = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        rim.setColorAt(0, QColor(255, 255, 255, 30 if isDark else 60))
        rim.setColorAt(1, QColor(255, 255, 255, 90 if isDark else 230))
        painter.setPen(QPen(QBrush(rim), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(track)


class SpeedGraph(QWidget):
    """最近一分钟的速度曲线。"""
    sampleCount = 60

    def __init__(self, parent=None):
        super().__init__(parent)
        self.samples: deque[int] = deque(maxlen=self.sampleCount)
        self.isPaused = False
        self.setMinimumHeight(96)
        setFont(self, 11)

    def add(self, speed: int) -> None:
        self.samples.append(speed)
        self.update()

    def peak(self) -> int:
        return max(self.samples, default=0)

    def paintEvent(self, e) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        isDark = isDarkTheme()
        rect = QRectF(self.rect()).adjusted(0, 18, 0, -2)
        muted = QColor(255, 255, 255, 140) if isDark else QColor(0, 0, 0, 130)

        painter.setPen(QPen(QColor(255, 255, 255, 18) if isDark else QColor(0, 0, 0, 14), 1, Qt.PenStyle.DashLine))
        for i in range(4):
            y = rect.top() + rect.height() * i / 3
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

        peak = self.peak()
        painter.setPen(muted)
        painter.drawText(QRectF(0, 0, self.width(), 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         self.tr("最近 1 分钟"))
        if peak:
            painter.drawText(QRectF(0, 0, self.width(), 16),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             self.tr("峰值 {0}/s").format(toReadableSize(peak)))
        if len(self.samples) < 2 or not peak:
            return

        step = rect.width() / (self.sampleCount - 1)
        offset = self.sampleCount - len(self.samples)
        points = [QPointF(rect.left() + (offset + i) * step, rect.bottom() - rect.height() * value / peak * 0.92)
                  for i, value in enumerate(self.samples)]
        line = QPainterPath(points[0])
        for previous, point in zip(points, points[1:]):
            middle = (previous.x() + point.x()) / 2
            line.cubicTo(QPointF(middle, previous.y()), QPointF(middle, point.y()), point)
        area = QPainterPath(line)
        area.lineTo(points[-1].x(), rect.bottom())
        area.lineTo(points[0].x(), rect.bottom())
        area.closeSubpath()

        color = toStatusColor(self.isPaused, False)
        fill = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        top, bottom = QColor(color), QColor(color)
        top.setAlpha(120)
        bottom.setAlpha(0)
        fill.setColorAt(0, top)
        fill.setColorAt(1, bottom)
        painter.fillPath(area, fill)
        painter.setPen(QPen(color.lighter(115), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawPath(line)

        glow = QRadialGradient(points[-1], 9)
        glowColor = QColor(color)
        glowColor.setAlpha(140)
        glow.setColorAt(0, glowColor)
        glowColor.setAlpha(0)
        glow.setColorAt(1, glowColor)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(points[-1], 9, 9)
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(points[-1], 2.5, 2.5)


class GlassBadge(QWidget):
    """状态胶囊：圆点 + 文字。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._color = themeColor()
        setFont(self, 12, QFont.Weight.DemiBold)
        self.setFixedHeight(24)

    def setStatus(self, text: str, color: QColor) -> None:
        self._text, self._color = text, color
        self.setFixedWidth(self.fontMetrics().horizontalAdvance(text) + 30)
        self.update()

    def paintEvent(self, e) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        tint = QColor(self._color)
        tint.setAlpha(48)
        painter.setPen(QPen(QColor(self._color.red(), self._color.green(), self._color.blue(), 110), 1))
        painter.setBrush(tint)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(QPointF(12, rect.center().y()), 3.5, 3.5)
        painter.setPen(QColor(255, 255, 255) if isDarkTheme() else QColor(30, 30, 30))
        painter.drawText(rect.adjusted(21, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, self._text)
