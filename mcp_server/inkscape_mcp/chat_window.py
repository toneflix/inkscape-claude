"""The chat as a small desktop window (GTK 3), so no browser is needed. Uses the same ChatSession as the page."""

from __future__ import annotations

import base64
import importlib.machinery
import sys
import threading
import types
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse


def skip_gio_unix_overrides() -> None:
    """Stands in an empty module for PyGObject's GioUnix overrides on macOS.

    Those overrides subclass GioUnix.DesktopAppInfo, which macOS builds of GLib (Inkscape's included) do not have,
    so importing Gtk fails. The overrides only matter for Linux desktop files.
    """
    name = "gi.overrides.GioUnix"
    if name in sys.modules:
        return

    stub = types.ModuleType(name)
    stub.__all__ = []
    stub.__spec__ = importlib.machinery.ModuleSpec(name, importlib.machinery.BuiltinImporter)
    sys.modules[name] = stub


try:
    import gi

    if sys.platform == "darwin":
        skip_gio_unix_overrides()

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango
except Exception:
    Gtk = None

from .attachments import MAX_BYTES, Attachment
from .chat import EXPECTED_ERRORS, ChatSession
from .claude import BACKENDS, EFFORTS, ClaudeError, Config
from .usage import Limit

STYLE = b"""
.bubble { padding: 8px 10px; border-radius: 8px; }
.user { background: #dbeafe; }
.assistant { background: #f1f5f9; }
.system { background: #fef3c7; }
.steps { color: #64748b; font-size: smaller; }
.badge { color: #64748b; }
.badge.on { color: #15803d; }
levelbar block.filled.ok { background: #16a34a; border-color: #16a34a; }
levelbar block.filled.warn { background: #d97706; border-color: #d97706; }
levelbar block.filled.over { background: #dc2626; border-color: #dc2626; }
"""

PREVIEW_WIDTH = 340
THUMBNAIL_SIZE = 56
IMAGE_SIZE = 160
LARGEST_SIDE = 2048
IMAGE_PATTERNS = ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.PNG", "*.JPG", "*.JPEG", "*.GIF", "*.WEBP")

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


class UsagePanel:
    """Context used of the model's window and each usage limit, with a bar and when it resets."""

    def __init__(self):
        self.box = Gtk.Grid(row_spacing=2, column_spacing=10, no_show_all=True)
        self.usage: dict | None = None

    def update(self, usage: dict | None) -> bool:
        if usage and (usage.get("context_tokens") or usage.get("limits")):
            self.usage = usage
            self.render()

        return False

    def refresh(self) -> bool:
        if self.usage:
            self.render()

        return True

    def render(self) -> None:
        for child in self.box.get_children():
            child.destroy()

        rows = [(self.usage["context_text"], self.usage.get("context_share"))]
        rows += [(Limit(**limit).describe(), limit["used"]) for limit in self.usage.get("limits", [])]
        for row, (text, share) in enumerate(rows):
            label = Gtk.Label(label=text, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                              tooltip_text=text)
            label.get_style_context().add_class("steps")
            self.box.attach(label, 0, row, 1, 1)
            if share is not None:
                self.box.attach(self.bar(share), 1, row, 1, 1)
        for child in self.box.get_children():
            child.show_all()
        self.box.show()

    @staticmethod
    def bar(share: float):
        bar = Gtk.LevelBar(min_value=0, max_value=1, value=max(0.0, min(1.0, share)), valign=Gtk.Align.CENTER)
        bar.set_size_request(90, 6)
        for name in (Gtk.LEVEL_BAR_OFFSET_LOW, Gtk.LEVEL_BAR_OFFSET_HIGH, Gtk.LEVEL_BAR_OFFSET_FULL):
            bar.remove_offset_value(name)
        bar.add_offset_value("ok", 0.7)
        bar.add_offset_value("warn", 0.9)
        bar.add_offset_value("over", 1.0)

        return bar


class PendingImages:
    """Images attached to the message being written, shown as thumbnails above the message box."""

    def __init__(self, chat: ChatWindow):
        self.chat = chat
        self.images: list[Attachment] = []
        self.box = Gtk.Box(spacing=6, no_show_all=True)

    def add(self, image: Attachment) -> None:
        self.images.append(image)
        remove = Gtk.Button(label="×", relief=Gtk.ReliefStyle.NONE, tooltip_text=f"Remove {image.name}",
                            valign=Gtk.Align.START)
        chip = Gtk.Box(spacing=0)
        chip.pack_start(Gtk.Image.new_from_pixbuf(ChatWindow.thumbnail(image.path, THUMBNAIL_SIZE)), False, False, 0)
        chip.pack_start(remove, False, False, 0)
        chip.set_tooltip_text(image.name)
        remove.connect("clicked", lambda *_: self.remove(image, chip))
        self.box.pack_start(chip, False, False, 0)
        chip.show_all()
        self.box.show()

    def remove(self, image: Attachment, chip) -> None:
        self.images.remove(image)
        chip.destroy()
        if not self.images:
            self.box.hide()

    def take(self) -> list[Attachment]:
        images, self.images = self.images, []
        for child in self.box.get_children():
            child.destroy()
        self.box.hide()

        return images


class LiveReply:
    """Claude's answer while it is being written: the text so far and the steps taken, replaced by the
    finished message when the turn ends."""

    def __init__(self, chat: ChatWindow):
        self.chat = chat
        self.text = ""
        self.steps: list[str] = []
        self.new_paragraph = False

        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bubble.get_style_context().add_class("bubble")
        bubble.get_style_context().add_class("assistant")
        self.label = Gtk.Label(xalign=0, wrap=True, selectable=True, can_focus=False)
        self.label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.progress = Gtk.Label(xalign=0, wrap=True, can_focus=False)
        self.progress.get_style_context().add_class("steps")
        bubble.pack_start(self.label, False, False, 0)
        bubble.pack_start(self.progress, False, False, 0)

        self.row = Gtk.Box()
        self.row.pack_start(bubble, True, True, 0)
        chat.messages.pack_start(self.row, False, False, 0)
        self.row.show_all()
        self.label.hide()

    def update(self, kind: str, payload) -> bool:
        if kind == "text":
            if self.new_paragraph and self.text:
                self.text = self.text.rstrip() + "\n\n"
            self.new_paragraph = False
            self.text += str(payload)
            self.label.set_text(self.text)
            self.label.show()
        elif kind == "step":
            mark = "✗ " if payload.get("failed") else "• "
            self.steps.append(f"{mark}{payload['tool']}: {payload['summary']}")
            self.progress.set_text("\n".join(self.steps[-3:]))
            self.new_paragraph = True
            self.chat.status.set_text(f"Claude is working… {len(self.steps)} step{'s' if len(self.steps) != 1 else ''}")
        self.chat.scroll_soon()

        return False

    def remove(self) -> None:
        self.row.destroy()


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
        self.usage = UsagePanel()

        self.messages = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.add(self.messages)

        self.prompt = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
        self.prompt.set_size_request(-1, 70)
        self.prompt.connect("key-press-event", self.on_key)
        self.prompt.connect("paste-clipboard", self.on_paste)
        prompt_frame = Gtk.Frame()
        prompt_frame.add(self.prompt)
        self.pending = PendingImages(self)

        self.status = Gtk.Label(xalign=0)
        self.spinner = Gtk.Spinner()
        self.send_button = Gtk.Button(label="Send")
        self.send_button.get_style_context().add_class("suggested-action")
        self.send_button.connect("clicked", lambda *_: self.send())
        self.undo_button = Gtk.Button(label="Undo")
        self.undo_button.connect("clicked", lambda *_: self.undo())
        self.image_button = Gtk.Button(label="Image…", tooltip_text="Attach images for Claude to see or place. "
                                                                     "You can also paste or drop them here.")
        self.image_button.connect("clicked", lambda *_: self.choose_images())

        actions = Gtk.Box(spacing=6)
        actions.pack_start(self.spinner, False, False, 0)
        actions.pack_start(self.status, True, True, 0)
        actions.pack_end(self.send_button, False, False, 0)
        actions.pack_end(self.undo_button, False, False, 0)
        actions.pack_end(self.image_button, False, False, 0)

        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        layout.pack_start(header, False, False, 0)
        layout.pack_start(self.usage.box, False, False, 0)
        layout.pack_start(self.scroller, True, True, 0)
        layout.pack_start(self.pending.box, False, False, 0)
        layout.pack_start(prompt_frame, False, False, 0)
        layout.pack_start(actions, False, False, 0)
        self.window.add(layout)

        self.was_live = session.live_enabled
        self.retry_buttons: list = []
        self.add_message("system", "Ask for a change to the drawing. Enter sends, Shift+Enter adds a line.")
        for entry in session.transcript:
            self.add_message(entry["role"], entry["text"], entry.get("steps"),
                             retry=entry.get("retry") and entry is session.transcript[-1])
        if session.backend == "claude-code":
            self.add_message("system", self.describe_settings())
        self.refresh_badge()
        self.usage.update(session.usage.to_dict())
        self.window.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.window.drag_dest_add_uri_targets()
        self.window.connect("drag-data-received", self.on_drop)
        GLib.timeout_add_seconds(2, self.watch_inkscape)
        GLib.timeout_add_seconds(30, self.usage.refresh)
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

    def choose_images(self) -> None:
        dialog = Gtk.FileChooserNative(title="Attach images", transient_for=self.window,
                                       action=Gtk.FileChooserAction.OPEN, select_multiple=True)
        images = Gtk.FileFilter()
        images.set_name("Images (PNG, JPEG, GIF, WebP)")
        for pattern in IMAGE_PATTERNS:
            images.add_pattern(pattern)
        dialog.add_filter(images)
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            for name in dialog.get_filenames():
                self.attach(Path(name))
        dialog.destroy()

    def on_paste(self, _widget) -> None:
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        if clipboard.wait_is_image_available():
            image = clipboard.wait_for_image()
            if image is not None:
                self.prompt.stop_emission_by_name("paste-clipboard")
                self.attach_pixbuf("pasted.png", image)
        elif clipboard.wait_is_uris_available():
            uris = clipboard.wait_for_uris() or []
            if uris:
                self.prompt.stop_emission_by_name("paste-clipboard")
                self.attach_uris(uris)

    def on_drop(self, _widget, _context, _x, _y, data, _info, _time) -> None:
        self.attach_uris(data.get_uris() or [])

    def attach_uris(self, uris: list[str]) -> None:
        for uri in uris:
            url = urlparse(uri)
            if url.scheme == "file":
                self.attach(Path(unquote(url.path).lstrip("/") if url.path[2:3] == ":" else unquote(url.path)))

    def attach(self, path: Path) -> None:
        """Attaches an image file, shrinking it first when it is over 5 MB."""
        try:
            if path.is_file() and path.stat().st_size > MAX_BYTES:
                image = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), LARGEST_SIDE, LARGEST_SIDE, True)
                self.attach_pixbuf(path.name, image)
                return
            self.pending.add(self.session.attachments.add(path))
        except (ValueError, OSError, GLib.Error) as exc:
            self.add_message("system", f"Error: could not attach {path.name}: {getattr(exc, 'message', exc)}")

    def attach_pixbuf(self, name: str, image) -> None:
        kind = "png" if image.get_has_alpha() else "jpeg"
        options = ([], []) if kind == "png" else (["quality"], ["90"])
        try:
            _, data = image.save_to_bufferv(kind, *options)
            stem = Path(name).stem or "image"
            self.pending.add(self.session.attachments.add_data(f"{stem}.{'png' if kind == 'png' else 'jpg'}",
                                                               bytes(data)))
        except (ValueError, OSError, GLib.Error) as exc:
            self.add_message("system", f"Error: could not attach {name}: {getattr(exc, 'message', exc)}")

    def send(self) -> None:
        buffer = self.prompt.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()
        if not (text or self.pending.images) or not self.send_button.get_sensitive():
            return

        buffer.set_text("")
        images = self.pending.take()
        self.add_message("user", text or "Here is an image.", images=[image.path for image in images])
        self.reply("Claude is working…", lambda on_event: self.session.send(text, on_event, images))

    def retry(self) -> None:
        self.reply("Trying again…", self.session.retry)

    def reply(self, status: str, task: Callable) -> None:
        """Runs a turn with its answer showing as it is written, and a Retry button if it fails."""
        live = LiveReply(self)

        def on_event(kind: str, payload) -> None:
            target = self.usage.update if kind == "usage" else lambda payload: live.update(kind, payload)
            GLib.idle_add(target, payload, priority=GLib.PRIORITY_DEFAULT)

        self.work(status, lambda: task(on_event), live, retryable=True)

    def undo(self) -> None:
        self.work("Undoing…", self.session.undo)

    def work(self, status: str, task: Callable[[], dict], live: LiveReply | None = None,
             retryable: bool = False) -> None:
        """Runs a chat call off the GTK thread and shows its result, in place of `live`, when it is done."""
        self.set_busy(True, status)

        def background():
            try:
                result = task()
            except EXPECTED_ERRORS as exc:
                result = {"role": "system", "text": f"Error: {exc}", "retry": retryable and self.session.can_retry}
            GLib.idle_add(self.finish, result, live, priority=GLib.PRIORITY_DEFAULT)

        threading.Thread(target=background, daemon=True).start()

    def finish(self, result: dict, live: LiveReply | None = None) -> bool:
        self.set_busy(False, "")
        if live:
            live.remove()
        self.add_message(result.get("role", "system"), result.get("text", ""), result.get("steps"),
                         result.get("preview"), result.get("selection"), retry=result.get("retry"))
        self.usage.update(result.get("usage"))
        self.refresh_badge()

        return False

    def set_busy(self, busy: bool, status: str) -> None:
        self.status.set_text(status)
        self.send_button.set_sensitive(not busy)
        self.undo_button.set_sensitive(not busy)
        self.image_button.set_sensitive(not busy)
        if busy:
            for button in self.retry_buttons:
                button.destroy()
            self.retry_buttons.clear()
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()

    def add_message(self, role: str, text: str, steps: list[dict] | None = None, preview: str | None = None,
                    selection: list[str] | None = None, images: list[Path] | None = None,
                    retry: bool = False) -> None:
        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bubble.get_style_context().add_class("bubble")
        bubble.get_style_context().add_class(role)

        label = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True, can_focus=False)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        bubble.pack_start(label, False, False, 0)

        if images:
            strip = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=2, column_spacing=4,
                                row_spacing=4)
            for path in images:
                picture = Gtk.Image.new_from_pixbuf(self.thumbnail(path, IMAGE_SIZE))
                picture.set_tooltip_text(path.name)
                strip.add(picture)
            bubble.pack_start(strip, False, False, 0)

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

        if retry:
            button = Gtk.Button(label="Retry", halign=Gtk.Align.START,
                                tooltip_text="Send the message again. Changes Claude already made stay.")
            button.connect("clicked", lambda *_: self.retry())
            self.retry_buttons.append(button)
            bubble.pack_start(button, False, False, 0)

        row = Gtk.Box()
        if role == "user":
            row.pack_end(bubble, False, False, 0)
        else:
            row.pack_start(bubble, True, True, 0)
        self.messages.pack_start(row, False, False, 0)
        row.show_all()

        self.scroll_soon()

    def scroll_soon(self) -> None:
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
            print("The Inkscape window the chat opened has closed, so the chat closes too.", flush=True)
            self.window.destroy()
            return False

        self.was_live = self.was_live or live
        self.refresh_badge()

        return True

    @staticmethod
    def thumbnail(path: Path, size: int):
        return GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), size, size, True)

    @staticmethod
    def pixbuf(png_base64: str):
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(base64.b64decode(png_base64))
        loader.close()
        image = loader.get_pixbuf()
        height = max(1, round(image.get_height() * PREVIEW_WIDTH / image.get_width()))

        return image.scale_simple(PREVIEW_WIDTH, height, GdkPixbuf.InterpType.BILINEAR)
