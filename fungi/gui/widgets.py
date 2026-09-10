"""Small layout helpers shared by every page."""

from PyQt5.QtGui import QGuiApplication
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    InfoBar,
    ToolButton,
)


def _copy(text: str, parent, what: str) -> None:
    QGuiApplication.clipboard().setText(text)
    InfoBar.success("已复制", what, duration=1500, parent=parent)


def _copy_button() -> ToolButton:
    return ToolButton(FluentIcon.COPY)


def _row(label: str, widget: QWidget, parent=None) -> QWidget:
    box = QHBoxLayout()
    labelw = BodyLabel(label)
    labelw.setFixedWidth(90)
    box.addWidget(labelw)
    box.addWidget(widget)
    if parent is not None:
        box.addWidget(parent)
    box.addStretch(1)
    holder = QWidget()
    holder.setLayout(box)
    return holder
