import tkinter as tk
from core.i18n import get_text

class ToolTip:
    """
    Hover tooltip popup component for CustomTkinter / Tkinter widgets.
    Displays a floating explanation box when mouse hovers over the target widget.
    When 'Help Mode' is enabled, popups trigger immediately (0ms delay) with prominent styling.
    """
    def __init__(self, widget, key, app_ref, delay_ms=300):
        self.widget = widget
        self.key = key
        self.app_ref = app_ref
        self.delay_ms = delay_ms
        self.tip_window = None
        self.after_id = None
        self.leave_id = None

        self._bind_events(self.widget)

    def _bind_events(self, w):
        """Recursively binds hover/click events to widget and all child elements."""
        try:
            w.bind("<Enter>", self.on_enter, add="+")
            w.bind("<Leave>", self.on_leave, add="+")
            w.bind("<ButtonPress>", self.on_leave, add="+")
        except Exception:
            pass

        # Check internal sub-widgets of CustomTkinter components
        for attr in ("_canvas", "_text_label", "_entry", "_label", "_image_label", "_button"):
            sub = getattr(w, attr, None)
            if sub and hasattr(sub, "bind"):
                try:
                    sub.bind("<Enter>", self.on_enter, add="+")
                    sub.bind("<Leave>", self.on_leave, add="+")
                    sub.bind("<ButtonPress>", self.on_leave, add="+")
                except Exception:
                    pass

        # Recursively bind any child Tkinter widgets
        try:
            for child in w.winfo_children():
                self._bind_events(child)
        except Exception:
            pass

    def on_enter(self, event=None):
        self.unschedule_leave()
        self.unschedule_enter()

        # Check if Help Mode is active on app_ref
        is_help = getattr(self.app_ref, "is_help_mode", lambda: True)()
        if not is_help:
            return  # Tooltips strictly appear when Help Mode is ON

        delay = 0  # Instant popup when in Help Mode
        self.after_id = self.widget.after(delay, self.show_tip)

    def on_leave(self, event=None):
        self.unschedule_enter()
        self.unschedule_leave()
        # Delay leave check slightly so moving between internal sub-widgets doesn't cause flicker
        self.leave_id = self.widget.after(50, self._check_leave)

    def _check_leave(self):
        self.leave_id = None
        if not self.widget or not self.widget.winfo_exists():
            self.hide_tip()
            return

        try:
            # Check mouse pointer coordinates relative to widget bounds
            mx = self.widget.winfo_pointerx()
            my = self.widget.winfo_pointery()

            wx = self.widget.winfo_rootx()
            wy = self.widget.winfo_rooty()
            ww = self.widget.winfo_width()
            wh = self.widget.winfo_height()

            # If mouse is still inside the parent widget's bounding box, do not hide tip
            if wx <= mx <= wx + ww and wy <= my <= wy + wh:
                return
        except Exception:
            pass

        self.hide_tip()

    def unschedule_enter(self):
        if self.after_id:
            try:
                self.widget.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None

    def unschedule_leave(self):
        if self.leave_id:
            try:
                self.widget.after_cancel(self.leave_id)
            except Exception:
                pass
            self.leave_id = None

    def show_tip(self):
        if self.tip_window or not self.widget.winfo_exists():
            return

        is_help = getattr(self.app_ref, "is_help_mode", lambda: True)()
        if not is_help:
            return

        lang = getattr(self.app_ref, "current_language", "pt_PT")
        text = get_text(self.key, lang)
        if not text or text == self.key:
            return

        # Position popup slightly below widget root coordinates
        try:
            x = self.widget.winfo_rootx() + 15
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except Exception:
            return

        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.attributes("-topmost", True)
        tw.geometry(f"+{x}+{y}")

        bg_color = "#1F4E78"
        fg_color = "#FFFFFF"
        border_color = "#007ACC"

        frame = tk.Frame(tw, background=border_color, bd=1)
        frame.pack(fill="both", expand=True)

        lbl = tk.Label(
            frame,
            text=text,
            justify="left",
            background=bg_color,
            foreground=fg_color,
            font=("Segoe UI", 12, "bold"),
            wraplength=480,
            padx=14,
            pady=8
        )
        lbl.pack(fill="both", expand=True)

    def hide_tip(self):
        if self.tip_window:
            try:
                self.tip_window.destroy()
            except Exception:
                pass
            self.tip_window = None
