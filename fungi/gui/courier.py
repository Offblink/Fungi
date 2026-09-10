"""Courier page: the memory the courier may use, and the calendar."""

import datetime as _dt

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QShortcut,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    InfoBar,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    TextEdit,
)

from .. import config as config_mod
from .. import todos
from .const import _ACCENT_GUI


class _DayDialog(QDialog):
    """One calendar day's to-dos: one item per line."""

    def __init__(self, date_iso: str, items: list[str], parent):
        super().__init__(parent)
        self.setWindowTitle(date_iso)
        self._date = date_iso
        lay = QVBoxLayout(self)
        lay.addWidget(BodyLabel(date_iso + " 的待办（一行一条）"))
        self.edit = TextEdit()
        self.edit.setPlainText("\n".join(items))
        self.edit.setFixedHeight(120)
        lay.addWidget(self.edit)
        btns = QHBoxLayout()
        save = PrimaryPushButton(FluentIcon.SAVE, "保存")
        save.clicked.connect(self.accept)
        save.setToolTip("Ctrl+Enter 也能保存（一行一条，回车留给换行）")
        # Same rule as the courier memory box: a multi-line list keeps Enter as
        # a newline, Ctrl+Enter is the keyboard commit.
        self.save_sc = QShortcut(QKeySequence("Ctrl+Return"), self.edit)
        self.save_sc.setContext(Qt.WidgetWithChildrenShortcut)
        self.save_sc.activated.connect(self.accept)
        cancel = PushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(save)
        lay.addLayout(btns)

    def items(self) -> list[str]:
        return [ln.strip() for ln in self.edit.toPlainText().splitlines() if ln.strip()]


class CourierPage(QWidget):
    """信使页：长期记忆 + 四周日历待办。信使回答时自动注入两者。"""

    WEEKS = 4

    def __init__(self, window):
        super().__init__()
        self.window_ref = window
        self.setObjectName("courierPage")
        root = QVBoxLayout(self)
        root.setContentsMargins(48, 14, 48, 14)
        root.setSpacing(6)
        root.addWidget(SubtitleLabel("信使"))
        hint = BodyLabel(
            "开启后，对面的留言由本机信使代收代复：重要消息转告你，寻常消息代答。\n"
            "开关在设置页；下面的记忆与待办，信使每轮回复都会读取。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        # ── 长期记忆 ──
        root.addSpacing(12)
        root.addWidget(StrongBodyLabel("长期记忆"))
        self.memory_edit = TextEdit()
        self.memory_edit.setPlainText(config_mod.load_config().courier_memory)
        self.memory_edit.setPlaceholderText(
            "例：工作日 8:00-17:00 我在上课没空回消息；有人找我就这样代答，紧急事项记下来等我回来汇报。"
        )
        self.memory_edit.setFixedHeight(72)
        root.addWidget(self.memory_edit)
        self.memory_save_btn = PushButton(FluentIcon.SAVE, "保存记忆")
        self.memory_save_btn.clicked.connect(self._save_courier_memory)
        self.memory_save_btn.setToolTip("Ctrl+Enter 也能保存（回车留给换行：记忆是多行的）")
        # Ctrl+Enter commits without reaching for the mouse; plain Enter has to
        # stay a newline in a multi-line box. Scoped to this box (not window-
        # wide) so it never fires while you are editing another page.
        self.memory_save_sc = QShortcut(QKeySequence("Ctrl+Return"), self.memory_edit)
        self.memory_save_sc.setContext(Qt.WidgetWithChildrenShortcut)
        self.memory_save_sc.activated.connect(self._save_courier_memory)
        root.addWidget(self.memory_save_btn)

        # ── 短期待办 ──
        root.addSpacing(18)
        root.addWidget(StrongBodyLabel("短期待办"))
        cal = QGridLayout()
        cal.setSpacing(6)
        for col, wd in enumerate("一二三四五六日"):
            head = BodyLabel(wd)
            head.setAlignment(Qt.AlignCenter)
            cal.addWidget(head, 0, col)
        self._buttons: dict[str, QPushButton] = {}
        today = _dt.date.today()
        monday = today - _dt.timedelta(days=today.weekday())
        for i in range(self.WEEKS * 7):
            day = monday + _dt.timedelta(days=i)
            btn = QPushButton(str(day.day))
            btn.setFixedSize(44, 44)
            btn.setCursor(Qt.PointingHandCursor)
            iso = day.isoformat()
            btn.clicked.connect(lambda _=False, d=iso: self._open_day(d))
            self._buttons[iso] = btn
            cal.addWidget(btn, 1 + i // 7, i % 7)
        root.addLayout(cal)
        cal_hint = BodyLabel("点击某天录入（一行一条）；有安排的日期会亮橙色。")
        cal_hint.setWordWrap(True)
        root.addWidget(cal_hint)
        root.addSpacing(8)
        root.addStretch(1)
        self.status = BodyLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self._refresh()

    # ── calendar ──

    def _refresh(self) -> None:
        data = todos.load()
        today = _dt.date.today().isoformat()
        for iso, btn in self._buttons.items():
            if iso == today:
                btn.setStyleSheet(
                    f"QPushButton{{background:{_ACCENT_GUI};color:white;border-radius:22px;font-weight:bold}}"
                )
            elif iso in data:
                # accent tint beats a bullet glyph: it survives any theme and
                # never depends on font glyph coverage
                btn.setStyleSheet(
                    "QPushButton{background:rgba(224,122,95,0.30);border-radius:22px;font-weight:bold}"
                )
            else:
                # translucent fill so the grid reads on both themes (a bare
                # QPushButton renders flat under the fluent style)
                btn.setStyleSheet(
                    "QPushButton{border-radius:22px;background:rgba(127,127,127,0.10)}"
                )
        overdue = [d for d in data if d < today]
        self.status.setText(
            f"过去 30 天内有 {len(overdue)} 天仍挂着未清待办：{'、'.join(overdue)}"
            if overdue
            else ""
        )

    def _open_day(self, iso: str) -> None:
        dlg = _DayDialog(iso, todos.load().get(iso, []), self.window_ref)
        if dlg.exec_():
            todos.set_day(iso, dlg.items())
            self._refresh()

    # ── memory ──

    def _save_courier_memory(self) -> None:
        """信使记忆：即时写盘；通讯 clone 每轮重建 prompt 时重读，无需重启。"""
        cfg = config_mod.load_config()
        cfg.courier_memory = self.memory_edit.toPlainText().strip()
        config_mod.save_config(cfg)
        InfoBar.success(
            "已保存",
            "信使记忆已更新，对下一封留言立即生效",
            duration=2500,
            parent=self.window_ref,
        )
