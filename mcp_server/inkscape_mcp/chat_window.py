"""The chat as a small desktop window (GTK 3), so no browser is needed. Uses the same ChatSession as the page."""

from __future__ import annotations

import base64
import threading
from typing import Callable

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango
except (ImportError, ValueError):
    Gtk = None

from .chat import EXPECTED_ERRORS, ChatSession
from .claude import BACKENDS, EFFORTS, ClaudeError, Config

STYLE = b"""
.bubble { padding: 8px 10px; border-radius: 8px; }
.user { background: #dbeafe; }
.assistant { background: #f1f5f9; }
.system { background: #fef3c7; }
.steps { color: #64748b; font-size: smaller; }
.badge { color: #64748b; }
.badge.on { color: #15803d; }
"""

PREVIEW_WIDTH = 340

EFFORT_LABELS = {"low": "Low (fastest)", "medium": "Medium (default)", "high": "High", "xhigh": "Extra high",
                 "max": "Max (slowest)"}

BACKEND_LABELS = {"api": "API key", "claude-code": "Claude Code (your subscription)"}


class SettingsDialog:
    """How to reach Claude (API key or Claude Code), model, effort and API key, saved to the config file that
    Ask Claude also reads."""

    def __init__(self, parent, session: ChatSession):
        self.session = session
        self.dialog = Gtk.Dialog(title="Claude settings", transient_for=parent, modal=True)
        self.dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        self.dialog.set_default_response(Gtk.ResponseType.OK)

        self.backend = Gtk.ComboBoxText()
        for backend in BACKENDS:
            self.backend.append(backend, BACKEND_LABELS[backend])
        self.backend.set_active_id(session.backend)
        self.backend.connect("changed", lambda *_: self.api_key.set_sensitive(self.backend.get_active_id() == "api"))
        self.model = Gtk.Entry(text=session.client.model, hexpand=True, activates_default=True)
        self.effort = Gtk.ComboBoxText()
        for effort in EFFORTS:
            self.effort.append(effort, EFFORT_LABELS[effort])
        self.effort.set_active_id(session.client.effort)
        self.api_key = Gtk.Entry(visibility=False, activates_default=True,
                                 placeholder_text="Key in use; type to replace" if session.client.api_key
                                 else "sk-ant-...", sensitive=session.backend == "api")

        note = Gtk.Label(xalign=0, wrap=True, max_width_chars=48,
                         label="Claude Code uses the account you signed in with in the `claude` command, such as "
                               "a Pro or Max subscription. An API key comes from platform.claude.com. "
                               f"Saved in {Config.path()}, which Ask Claude also uses.")
        note.get_style_context().add_class("steps")

        grid = Gtk.Grid(row_spacing=8, column_spacing=10, margin=12)
        rows = (("Connect with", self.backend), ("Model", self.model), ("Effort", self.effort),
                ("API key", self.api_key))
        for row, (text, widget) in enumerate(rows):
            grid.attach(Gtk.Label(label=text, xalign=0), 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
        grid.attach(note, 0, len(rows), 2, 1)
        self.dialog.get_content_area().add(grid)

    def run(self) -> bool:
        self.dialog.show_all()
        if self.session.needs_key:
            self.api_key.grab_focus()
        saved = self.dialog.run() == Gtk.ResponseType.OK
        if saved:
            self.save()
        self.dialog.destroy()

        return saved

    def save(self) -> None:
        model = self.model.get_text().strip()
        effort = self.effort.get_active_id() or "medium"
        backend = self.backend.get_active_id() or "api"
        api_key = self.api_key.get_text().strip()

        self.session.configure(model, effort, api_key or None, backend)
        values = {"model": self.session.client.model, "effort": self.session.client.effort, "backend": backend}
        if api_key:
            values["api_key"] = api_key
        Config.save(**values)


class ChatWindow:
    """A window with the conversation, a preview after each answer, a message box, Send and Undo."""

    def __init__(self, session: ChatSession):
        self.session = session
        self.window = Gtk.Window(title=f"Claude: {session.path.name}")
        self.window.set_default_size(420, 640)
        self.window.connect("destroy", lambda *_: Gtk.main_quit())

        css = Gtk.CssProvider()
        css.load_from_data(STYLE)
        Gtk.StyleContext.add_provider_for_screen(self.window.get_screen(), css,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.badge = Gtk.Label(xalign=0)
        self.badge.get_style_context().add_class("badge")
        settings_button = Gtk.Button(label="Settings")
        settings_button.set_tooltip_text("Model, effort and API key")
        settings_button.set_relief(Gtk.ReliefStyle.NONE)
        settings_button.connect("clicked", lambda *_: self.open_settings())
        header = Gtk.Box(spacing=6)
        header.pack_start(self.badge, True, True, 0)
        header.pack_end(settings_button, False, False, 0)

        self.messages = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.add(self.messages)

        self.prompt = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
        self.prompt.set_size_request(-1, 70)
        self.prompt.connect("key-press-event", self.on_key)
        prompt_frame = Gtk.Frame()
        prompt_frame.add(self.prompt)

        self.status = Gtk.Label(xalign=0)
        self.spinner = Gtk.Spinner()
        self.send_button = Gtk.Button(label="Send")
        self.send_button.get_style_context().add_class("suggested-action")
        self.send_button.connect("clicked", lambda *_: self.send())
        self.undo_button = Gtk.Button(label="Undo")
        self.undo_button.connect("clicked", lambda *_: self.undo())

        actions = Gtk.Box(spacing=6)
        actions.pack_start(self.spinner, False, False, 0)
        actions.pack_start(self.status, True, True, 0)
        actions.pack_end(self.send_button, False, False, 0)
        actions.pack_end(self.undo_button, False, False, 0)

        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        layout.pack_start(header, False, False, 0)
        layout.pack_start(self.scroller, True, True, 0)
        layout.pack_start(prompt_frame, False, False, 0)
        layout.pack_start(actions, False, False, 0)
        self.window.add(layout)

        self.was_live = session.live_enabled
        self.add_message("system", "Ask for a change to the drawing. Enter sends, Shift+Enter adds a line.")
        if session.backend == "claude-code":
            self.add_message("system", self.describe_settings())
        self.refresh_badge()
        GLib.timeout_add_seconds(2, self.watch_inkscape)
        if session.needs_key:
            GLib.idle_add(self.ask_for_key)

    @staticmethod
    def supported() -> bool:
        if Gtk is None:
            return False

        return Gtk.init_check(None)[0]

    @classmethod
    def run(cls, session: ChatSession) -> None:
        chat = cls(session)
        chat.window.show_all()
        chat.prompt.grab_focus()
        Gtk.main()

    def open_settings(self) -> bool:
        try:
            if SettingsDialog(self.window, self.session).run():
                self.add_message("system", self.describe_settings())
        except (OSError, ClaudeError) as exc:
            self.add_message("system", f"Error: could not save the settings: {exc}")

        return False

    def describe_settings(self) -> str:
        client = self.session.client
        via = "Claude Code" if self.session.backend == "claude-code" else "your API key"

        return f"Using {client.model} at {client.effort} effort, through {via}."

    def ask_for_key(self) -> bool:
        self.add_message("system", "Add your Claude API key in Settings, or choose Claude Code to use your "
                                   "subscription.")

        return self.open_settings()

    def on_key(self, _widget, event) -> bool:
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not event.state & Gdk.ModifierType.SHIFT_MASK:
            self.send()
            return True

        return False

    def send(self) -> None:
        buffer = self.prompt.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()
        if not text or not self.send_button.get_sensitive():
            return

        buffer.set_text("")
        self.add_message("user", text)
        self.work("Claude is working…", lambda: self.session.send(text))

    def undo(self) -> None:
        self.work("Undoing…", self.session.undo)

    def work(self, status: str, task: Callable[[], dict]) -> None:
        """Runs a chat call off the GTK thread and shows its result when it is done."""
        self.set_busy(True, status)

        def background():
            try:
                result = task()
            except EXPECTED_ERRORS as exc:
                result = {"role": "system", "text": f"Error: {exc}"}
            GLib.idle_add(self.finish, result, priority=GLib.PRIORITY_DEFAULT)

        threading.Thread(target=background, daemon=True).start()

    def finish(self, result: dict) -> bool:
        self.set_busy(False, "")
        self.add_message(result.get("role", "system"), result.get("text", ""), result.get("steps"),
                         result.get("preview"), result.get("selection"))
        self.refresh_badge()

        return False

    def set_busy(self, busy: bool, status: str) -> None:
        self.status.set_text(status)
        self.send_button.set_sensitive(not busy)
        self.undo_button.set_sensitive(not busy)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()

    def add_message(self, role: str, text: str, steps: list[dict] | None = None, preview: str | None = None,
                    selection: list[str] | None = None) -> None:
        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bubble.get_style_context().add_class("bubble")
        bubble.get_style_context().add_class(role)

        label = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True, can_focus=False)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        bubble.pack_start(label, False, False, 0)

        if selection:
            selected = Gtk.Label(label="Your selection: " + ", ".join(selection), xalign=0, wrap=True,
                                 selectable=True, can_focus=False)
            selected.get_style_context().add_class("steps")
            bubble.pack_start(selected, False, False, 0)

        if steps:
            lines = [("✗ " if step.get("failed") else "• ") + f"{step['tool']}: {step['summary']}" for step in steps]
            expander = Gtk.Expander(label=f"{len(steps)} step{'s' if len(steps) != 1 else ''}")
            details = Gtk.Label(label="\n".join(lines), xalign=0, wrap=True, selectable=True, can_focus=False)
            details.get_style_context().add_class("steps")
            expander.add(details)
            bubble.pack_start(expander, False, False, 0)

        if preview:
            bubble.pack_start(Gtk.Image.new_from_pixbuf(self.pixbuf(preview)), False, False, 0)

        row = Gtk.Box()
        if role == "user":
            row.pack_end(bubble, False, False, 0)
        else:
            row.pack_start(bubble, True, True, 0)
        self.messages.pack_start(row, False, False, 0)
        row.show_all()

        GLib.idle_add(self.scroll_to_end)

    def scroll_to_end(self) -> bool:
        adjustment = self.scroller.get_vadjustment()
        adjustment.set_value(adjustment.get_upper())

        return False

    def refresh_badge(self) -> None:
        live = self.session.live_enabled
        self.badge.set_text("● Inkscape window in sync" if live
                            else "○ Not synced: use File > Revert to see edits")
        context = self.badge.get_style_context()
        if live:
            context.add_class("on")
        else:
            context.remove_class("on")

    def watch_inkscape(self) -> bool:
        """When the chat owns the Inkscape window and it is closed, the chat closes too."""
        live = self.session.live_enabled
        if self.session.owns_window and self.was_live and not live:
            self.window.destroy()
            return False

        self.was_live = self.was_live or live
        self.refresh_badge()

        return True

    @staticmethod
    def pixbuf(png_base64: str):
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(base64.b64decode(png_base64))
        loader.close()
        image = loader.get_pixbuf()
        height = max(1, round(image.get_height() * PREVIEW_WIDTH / image.get_width()))

        return image.scale_simple(PREVIEW_WIDTH, height, GdkPixbuf.InterpType.BILINEAR)
