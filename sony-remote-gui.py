#!/usr/bin/env python3
"""Sony Camera Remote GUI - GTK4/Adw wrapper around Sony Camera Remote SDK"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, GdkPixbuf
import subprocess
import threading
import os
import re
import json

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SDK_DIR = os.path.join(APP_DIR, "RemoteCli", "build")
REMOTECLI = os.path.join(SDK_DIR, "RemoteCli")
LIVEVIEW_PATH = os.path.join(SDK_DIR, "LiveView000000.JPG")
LOG_FILE = os.path.join(APP_DIR, "debug.log")
CREDS_FILE = os.path.join(APP_DIR, "saved_credentials.json")
CAMERA_IMAGES_DIR = os.path.join(APP_DIR, "camera_images")

# ILCE model code → friendly name
SONY_MODEL_NAMES = {
    "ILCE-1M2": "Alpha 1 II",
    "ILCE-1": "Alpha 1",
    "ILCE-9M3": "Alpha 9 III",
    "ILCE-9M2": "Alpha 9 II",
    "ILCE-9": "Alpha 9",
    "ILCE-7RM5": "Alpha 7R V",
    "ILCE-7RM4A": "Alpha 7R IVA",
    "ILCE-7RM4": "Alpha 7R IV",
    "ILCE-7RM3": "Alpha 7R III",
    "ILCE-7CR": "Alpha 7CR",
    "ILCE-7SM3": "Alpha 7S III",
    "ILCE-7M5": "Alpha 7 V",
    "ILCE-7M4": "Alpha 7 IV",
    "ILCE-7M3": "Alpha 7 III",
    "ILCE-7CM2": "Alpha 7C II",
    "ILCE-7C": "Alpha 7C",
    "ILCE-6700": "Alpha 6700",
    "ILCE-6600": "Alpha 6600",
    "ILCE-6400": "Alpha 6400",
    "ILME-FX6": "FX6",
    "ILME-FX3": "FX3",
    "ILME-FX30": "FX30",
    "MPC-2610": "FR7",
}


def friendly_name(model_id):
    return SONY_MODEL_NAMES.get(model_id, model_id)


def load_saved_creds():
    try:
        with open(CREDS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_creds(camera_id, user, pw, auto_connect=False, display_name=None):
    data = load_saved_creds()
    existing = data.get(camera_id, {})
    data[camera_id] = {
        "user": user, "pass": pw,
        "auto_connect": existing.get("auto_connect", auto_connect),
        "display_name": display_name or existing.get("display_name", ""),
    }
    with open(CREDS_FILE, "w") as f:
        json.dump(data, f)


def delete_creds(camera_id):
    data = load_saved_creds()
    data.pop(camera_id, None)
    with open(CREDS_FILE, "w") as f:
        json.dump(data, f)


def debug(msg):
    with open(LOG_FILE, "a") as f:
        f.write(msg + "\n")


class CameraProcess:
    def __init__(self, on_output, credentials=None):
        self.proc = None
        self.on_output = on_output
        self.credentials = credentials  # (user, pass) tuple

    def start(self):
        env = os.environ.copy()
        if self.credentials:
            env["SONY_REMOTE_USER"] = self.credentials[0]
            env["SONY_REMOTE_PASS"] = self.credentials[1]
        self.proc = subprocess.Popen(
            [REMOTECLI],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=SDK_DIR,
            text=True,
            bufsize=1,
            env=env,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def send(self, text):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(text + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def send_raw(self, chars):
        """Send raw characters without appending newline."""
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(chars)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def stop(self):
        if not self.proc:
            return
        if self.proc.poll() is not None:
            self.proc = None
            return
        try:
            self.proc.stdin.write("x\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def _read(self):
        try:
            for line in self.proc.stdout:
                GLib.idle_add(self.on_output, line)
        except Exception:
            pass
        GLib.idle_add(self.on_output, "[process_exited]\n")


class SonyRemoteApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.sony.remote.gui")
        self.cam = None
        self.cameras = []
        self.connected_name = ""
        self.lv_timer_id = None
        self.busy = False  # True while a command sequence is in-flight
        self._lv_active = False
        self._lv_streaming = False
        self._focus_frames = []
        self._tracking_frames = []
        self._face_frames = []
        self._lv_size_set = False

    def do_activate(self):
        css = Gtk.CssProvider()
        css.load_from_string("""
            .mirrored { transform: scaleX(-1); }
            .lv-hud { background: rgba(0,0,0,0.55); border-radius: 8px; padding: 6px 12px; }
            .lv-hud-bottom { background: rgba(0,0,0,0.55); border-radius: 8px; padding: 8px 16px; }
            .lv-prop { color: white; font-size: 15px; font-weight: bold; font-family: monospace; }
            .lv-prop-dim { color: rgba(255,255,255,0.6); font-size: 11px; }
            .shutter-circle { border-radius: 9999px; background: white; min-width: 64px; min-height: 64px; }
            .shutter-circle:hover { background: rgba(255,255,255,0.85); }
            .rec-circle { border-radius: 9999px; background: rgba(255,255,255,0.15); border: 2px solid white; min-width: 44px; min-height: 44px; }
            .rec-circle:hover { background: rgba(255,0,0,0.5); }
            .camera-card { background: alpha(@card_bg_color, 0.8); border-radius: 16px; padding: 20px; min-width: 160px; }
            .camera-card:hover { background: alpha(@card_bg_color, 1.0); }
            .camera-card-icon { opacity: 0.6; }
            .camera-card-name { font-size: 14px; font-weight: bold; }
            .camera-card-model { font-size: 11px; opacity: 0.5; }
            .camera-card-new { border: 2px dashed alpha(@borders, 0.4); background: transparent; border-radius: 16px; padding: 20px; min-width: 160px; opacity: 0.6; }
            .camera-card-new:hover { opacity: 1.0; border-color: @accent_color; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            self.get_active_window().get_display() if self.get_active_window() else __import__('gi').repository.Gdk.Display.get_default(),
            css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.win = Adw.ApplicationWindow(application=self, title="Sony Remote",
                                         default_width=600, default_height=500)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)

        self.discovery_page = self._build_discovery_page()
        self.stack.add_named(self.discovery_page, "discovery")

        self.connecting_page = self._build_connecting_page()
        self.stack.add_named(self.connecting_page, "connecting")

        self.controls_page = self._build_controls_page()
        self.stack.add_named(self.controls_page, "controls")

        toolbar = Adw.ToolbarView()
        self.header = Adw.HeaderBar()
        self.header.set_title_widget(Adw.WindowTitle(title="Sony Remote", subtitle=""))
        toolbar.add_top_bar(self.header)
        toolbar.set_content(self.stack)

        self.win.set_content(toolbar)
        self.win.present()

        # Show saved cameras, auto-connect if configured, otherwise auto-scan
        auto_name = self._show_saved_cameras()
        if auto_name:
            GLib.timeout_add(500, lambda: self._connect_saved(auto_name) or False)
        else:
            GLib.timeout_add(500, lambda: self.on_scan(None) or False)

    def _show_saved_cameras(self):
        """Show saved cameras as cards. Returns name of auto-connect camera if any."""
        # Clear grid
        while (child := self.camera_grid.get_first_child()):
            self.camera_grid.remove(child)

        saved = load_saved_creds()
        auto_connect_name = None

        for cam_id, info in saved.items():
            is_auto = info.get("auto_connect", False)
            display = info.get("display_name", "") or friendly_name(cam_id)
            card = self._make_camera_card(display, cam_id, is_auto, saved=True)
            self.camera_grid.insert(card, -1)
            if is_auto:
                auto_connect_name = cam_id

        return auto_connect_name

    def _make_camera_card(self, display_name, model, is_auto=False, saved=False):
        btn = Gtk.Button(css_classes=["camera-card"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        # Camera image or icon
        img_path = None
        for ext in ("png", "webp", "jpg", "jpeg"):
            p = os.path.join(CAMERA_IMAGES_DIR, f"{model}.{ext}")
            if os.path.exists(p):
                img_path = p
                break
        if img_path:
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(img_path, 140, 95, True)
                texture = __import__('gi').repository.Gdk.Texture.new_for_pixbuf(pb)
                img = Gtk.Picture.new_for_paintable(texture)
                img.set_can_shrink(False)
                img.set_halign(Gtk.Align.CENTER)
                box.append(img)
            except Exception as e:
                debug(f"[img] {e}")
                icon = Gtk.Image.new_from_icon_name("camera-photo-symbolic")
                icon.set_pixel_size(48)
                box.append(icon)
        else:
            icon = Gtk.Image.new_from_icon_name("camera-photo-symbolic")
            icon.set_pixel_size(48)
            icon.add_css_class("camera-card-icon")
            box.append(icon)

        # Name row with inline edit
        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2,
                           halign=Gtk.Align.CENTER)
        name_label = Gtk.Label(label=display_name, css_classes=["camera-card-name"],
                                ellipsize=3)
        name_row.append(name_label)
        if saved:
            rename_btn = Gtk.Button(icon_name="document-edit-symbolic",
                                     css_classes=["flat", "circular"],
                                     tooltip_text="Rename")
            rename_btn.set_size_request(24, 24)
            rename_btn.connect("clicked", lambda b, n=model: self._rename_camera(n))
            name_row.append(rename_btn)
        box.append(name_row)

        # Model subtitle
        model_label = Gtk.Label(label=friendly_name(model), css_classes=["camera-card-model"])
        box.append(model_label)

        if saved:
            # Auto-connect row
            auto_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                               halign=Gtk.Align.CENTER, margin_top=4)
            auto_row.append(Gtk.Label(label="Auto-connect", css_classes=["caption", "dim-label"]))
            auto_sw = Gtk.Switch(valign=Gtk.Align.CENTER, active=is_auto)
            auto_sw.connect("state-set", lambda sw, state, n=model: self._on_auto_connect_toggled(n, state))
            auto_row.append(auto_sw)
            box.append(auto_row)

        btn.set_child(box)
        if saved:
            btn.connect("clicked", lambda b, n=model: self._connect_saved(n))
        else:
            btn.connect("clicked", lambda b, i=model, n=display_name: self._connect_camera(i, n))
        return btn

    def _rename_camera(self, camera_id):
        dialog = Adw.MessageDialog(
            transient_for=self.win,
            heading="Rename Camera",
            body=f"Enter a name for {camera_id}",
        )
        entry = Gtk.Entry()
        saved = load_saved_creds()
        entry.set_text(saved.get(camera_id, {}).get("display_name", "") or camera_id)
        entry.set_margin_start(24)
        entry.set_margin_end(24)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda d, r, e=entry, cid=camera_id: self._on_rename_response(d, r, e, cid))
        dialog.present()

    def _on_rename_response(self, dialog, response, entry, camera_id):
        if response == "save":
            new_name = entry.get_text().strip()
            if new_name:
                saved = load_saved_creds()
                if camera_id in saved:
                    saved[camera_id]["display_name"] = new_name
                    with open(CREDS_FILE, "w") as f:
                        json.dump(saved, f)
                    self._show_saved_cameras()

    def _on_auto_connect_toggled(self, camera_name, state):
        saved = load_saved_creds()
        if camera_name in saved:
            if state:
                for name in saved:
                    saved[name]["auto_connect"] = (name == camera_name)
            else:
                saved[camera_name]["auto_connect"] = False
            with open(CREDS_FILE, "w") as f:
                json.dump(saved, f)
        return False

    def _connect_saved(self, name):
        """Connect to a saved camera - start process with saved creds."""
        saved = load_saved_creds().get(name)
        if not saved:
            return
        self.connected_name = name
        self._camera_id = name
        self._credentials = (saved["user"], saved["pass"])
        self._pending_camera_idx = "1"
        self.remember_check.set_active(True)

        self.login_box.set_visible(False)
        self.spinner_box.set_visible(True)
        self.connecting_spinner.set_spinning(True)
        self.connecting_label.set_label(f"Connecting to {name}...")
        self.connecting_hint.set_label("")
        self.stack.set_visible_child_name("connecting")
        self.header.set_title_widget(
            Adw.WindowTitle(title="Sony Remote", subtitle="Connecting..."))

        if self.cam:
            self.cam.stop()
        self.cam = CameraProcess(self._on_connect_scan_output, credentials=self._credentials)
        self.cam.start()

    # ── Discovery page ──

    def _build_discovery_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, vexpand=True)

        # Card grid fills the middle
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        grid_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0,
                           margin_start=24, margin_end=24, margin_top=12)

        self.camera_grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                        max_children_per_line=3, min_children_per_line=1,
                                        column_spacing=12, row_spacing=12, homogeneous=True,
                                        valign=Gtk.Align.START)
        grid_box.append(self.camera_grid)
        scroll.set_child(grid_box)
        page.append(scroll)

        # Bottom bar — always anchored
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                         margin_start=24, margin_end=24, margin_top=8, margin_bottom=12,
                         halign=Gtk.Align.CENTER)

        self.scan_spinner = Gtk.Spinner()
        self.scan_spinner.set_visible(False)
        bottom.append(self.scan_spinner)

        self.discovery_subtitle = Gtk.Label(label="", css_classes=["dim-label"])
        bottom.append(self.discovery_subtitle)

        self.scan_btn = Gtk.Button(label="Scan", css_classes=["pill"])
        self.scan_btn.connect("clicked", self.on_scan)
        bottom.append(self.scan_btn)

        page.append(bottom)

        # Hidden compat
        self.camera_list_box = Gtk.ListBox(visible=False)

        return page

    # ── Login page ──

    def _build_connecting_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Login form (shown first)
        self.login_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                                  valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                                  vexpand=True, margin_start=32, margin_end=32)

        self.login_title = Gtk.Label(label="Sign in to camera")
        self.login_title.add_css_class("title-2")
        self.login_box.append(self.login_title)

        login_hint = Gtk.Label(label="Enter the access authentication info from your camera's network settings")
        login_hint.add_css_class("dim-label")
        login_hint.set_wrap(True)
        login_hint.set_justify(Gtk.Justification.CENTER)
        self.login_box.append(login_hint)

        fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8)

        self.user_entry = Gtk.Entry(placeholder_text="Username")
        self.user_entry.set_text("admin")
        self.user_entry.set_size_request(280, -1)
        fields.append(self.user_entry)

        self.pass_entry = Gtk.PasswordEntry(show_peek_icon=True)
        self.pass_entry.set_size_request(280, -1)
        # Connect Enter key to submit
        self.pass_entry.connect("activate", lambda w: self._on_login_submit())
        fields.append(self.pass_entry)

        self.remember_check = Gtk.CheckButton(label="Remember for this camera")
        fields.append(self.remember_check)

        self.login_box.append(fields)

        login_btn = Gtk.Button(label="Connect", halign=Gtk.Align.CENTER,
                                css_classes=["suggested-action", "pill"])
        login_btn.set_size_request(200, -1)
        login_btn.connect("clicked", lambda b: self._on_login_submit())
        self.login_box.append(login_btn)

        self.login_error = Gtk.Label(label="", css_classes=["error"])
        self.login_error.set_visible(False)
        self.login_box.append(self.login_error)

        cancel_btn1 = Gtk.Button(label="Cancel", halign=Gtk.Align.CENTER)
        cancel_btn1.connect("clicked", self.on_cancel_connect)
        self.login_box.append(cancel_btn1)

        page.append(self.login_box)

        # Spinner overlay (shown while connecting)
        self.spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                                    valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                                    vexpand=True, margin_start=24, margin_end=24)
        self.spinner_box.set_visible(False)

        self.connecting_spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER)
        self.connecting_spinner.set_size_request(48, 48)
        self.spinner_box.append(self.connecting_spinner)

        self.connecting_label = Gtk.Label(label="Connecting...")
        self.connecting_label.add_css_class("title-2")
        self.spinner_box.append(self.connecting_label)

        self.connecting_hint = Gtk.Label(label="")
        self.connecting_hint.add_css_class("dim-label")
        self.connecting_hint.set_wrap(True)
        self.connecting_hint.set_justify(Gtk.Justification.CENTER)
        self.spinner_box.append(self.connecting_hint)

        cancel_btn2 = Gtk.Button(label="Cancel", halign=Gtk.Align.CENTER)
        cancel_btn2.connect("clicked", self.on_cancel_connect)
        self.spinner_box.append(cancel_btn2)

        page.append(self.spinner_box)

        return page

    # ── Controls page ──

    # AF mode values from SDK
    AF_MODES = [
        ("AF-S", 0x0002),   # CrFocus_AF_S
        ("AF-C", 0x0003),   # CrFocus_AF_C
        ("AF-A", 0x0004),   # CrFocus_AF_A
        ("DMF", 0x0006),    # CrFocus_DMF
        ("MF", 0x0001),     # CrFocus_MF
    ]

    def _build_controls_page(self):
        page = Gtk.Overlay()
        page.set_vexpand(True)

        # Live view fills entire page
        self.lv_picture = Gtk.Picture()
        self.lv_picture.set_content_fit(Gtk.ContentFit.COVER)
        self.lv_picture.add_css_class("mirrored")
        page.set_child(self.lv_picture)

        # Placeholder
        self.lv_placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                                       halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        lv_icon = Gtk.Image.new_from_icon_name("video-display-symbolic")
        lv_icon.set_pixel_size(48)
        lv_icon.add_css_class("dim-label")
        self.lv_placeholder.append(lv_icon)
        self.lv_placeholder.append(Gtk.Label(label="Live view will appear here",
                                              css_classes=["dim-label"]))
        page.add_overlay(self.lv_placeholder)

        # Focus frame drawing layer
        self.focus_draw = Gtk.DrawingArea()
        self.focus_draw.set_draw_func(self._draw_focus_frames)
        self.focus_draw.add_css_class("mirrored")
        page.add_overlay(self.focus_draw)

        # Click to autofocus
        lv_click = Gtk.GestureClick()
        lv_click.connect("released", self._on_lv_click)
        page.add_controller(lv_click)

        # ── Top right: small HUD buttons ──
        top_right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
                            margin_end=12, margin_top=8,
                            halign=Gtk.Align.END, valign=Gtk.Align.START)
        top_right.add_css_class("lv-hud")

        self.overlay_toggle = Gtk.ToggleButton(icon_name="view-reveal-symbolic",
                                                active=True, tooltip_text="Show focus overlay",
                                                css_classes=["flat", "circular"])
        self.overlay_toggle.connect("toggled", lambda b: self.focus_draw.queue_draw())
        top_right.append(self.overlay_toggle)

        disconnect_btn = Gtk.Button(icon_name="process-stop-symbolic",
                                     tooltip_text="Disconnect",
                                     css_classes=["flat", "circular", "destructive-action"])
        disconnect_btn.connect("clicked", self.on_disconnect)
        top_right.append(disconnect_btn)

        page.add_overlay(top_right)

        # ── Bottom overlay ──
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                         margin_start=16, margin_end=16, margin_bottom=16,
                         halign=Gtk.Align.FILL, valign=Gtk.Align.END)

        # Settings bar: shutter | aperture | ISO | AF mode
        settings_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24,
                               halign=Gtk.Align.CENTER)
        settings_bar.add_css_class("lv-hud-bottom")

        self.lbl_shutter = Gtk.Label(label="--", css_classes=["lv-prop"])
        self.lbl_aperture = Gtk.Label(label="--", css_classes=["lv-prop"])
        self.lbl_iso = Gtk.Label(label="--", css_classes=["lv-prop"])

        def prop_col(label_text, value_widget):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, halign=Gtk.Align.CENTER)
            col.append(Gtk.Label(label=label_text, css_classes=["lv-prop-dim"]))
            col.append(value_widget)
            return col

        settings_bar.append(prop_col("SHUTTER", self.lbl_shutter))
        settings_bar.append(prop_col("APERTURE", self.lbl_aperture))
        settings_bar.append(prop_col("ISO", self.lbl_iso))

        # AF mode dropdown inline
        af_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, halign=Gtk.Align.CENTER)
        af_col.append(Gtk.Label(label="FOCUS", css_classes=["lv-prop-dim"]))
        self.af_mode_dropdown = Gtk.DropDown.new_from_strings(
            [m[0] for m in self.AF_MODES])
        self.af_mode_dropdown.set_selected(1)
        self.af_mode_dropdown.connect("notify::selected", self._on_af_mode_changed)
        af_col.append(self.af_mode_dropdown)
        settings_bar.append(af_col)

        bottom.append(settings_bar)

        # Shutter / record buttons
        controls_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20,
                               halign=Gtk.Align.CENTER, margin_top=4)

        self.rec_btn = Gtk.Button(css_classes=["rec-circle"], tooltip_text="Record")
        rec_icon = Gtk.Image.new_from_icon_name("media-record-symbolic")
        rec_icon.set_pixel_size(20)
        self.rec_btn.set_child(rec_icon)
        self.rec_btn.connect("clicked", lambda b: self._action(["1", "6"]))
        controls_row.append(self.rec_btn)

        self.shutter_btn = Gtk.Button(css_classes=["shutter-circle"], tooltip_text="Take Photo")
        shutter_icon = Gtk.Image.new_from_icon_name("camera-photo-symbolic")
        shutter_icon.set_pixel_size(28)
        self.shutter_btn.set_child(shutter_icon)
        self.shutter_btn.connect("clicked", self._on_shutter_clicked)
        controls_row.append(self.shutter_btn)

        bottom.append(controls_row)
        page.add_overlay(bottom)

        # Hidden labels for compat
        self.connected_label = Gtk.Label(visible=False)
        self.connected_status = Gtk.Label(visible=False)
        self.lbl_af_mode = Gtk.Label(visible=False)

        return page



    # ── Scan ──

    def on_scan(self, btn):
        if self.cam:
            self.cam.stop()
            self.cam = None

        self.cameras = []
        self._show_saved_cameras()

        self.scan_btn.set_sensitive(False)
        self.scan_btn.set_label("Scanning...")
        self.scan_spinner.set_visible(True)
        self.scan_spinner.set_spinning(True)
        self.discovery_subtitle.set_label("Looking for cameras on your network...")

        self.cam = CameraProcess(self._on_scan_output)
        self.cam.start()

    def _on_scan_output(self, line):
        debug(f"[scan] {line.rstrip()}")

        m = re.match(r'\[(\d+)\]\s+(.+?)\s+\((.+?)\)', line.strip())
        if m:
            idx, model, addr = m.group(1), m.group(2), m.group(3)
            self.cameras.append((idx, model, addr))

            # Skip if already shown as saved camera
            saved = load_saved_creds()
            if model in saved:
                return

            card = self._make_camera_card(friendly_name(model), idx, saved=False)
            self.camera_grid.insert(card, -1)

        if "Connect to camera with input number" in line:
            self._scan_done()

        if ("No cameras detected" in line or "Failed to initialize" in line
                or "[process_exited]" in line):
            self._scan_done(failed=True)

    def _scan_done(self, failed=False):
        self.scan_btn.set_sensitive(True)
        self.scan_btn.set_label("Scan")
        self.scan_spinner.set_spinning(False)
        self.scan_spinner.set_visible(False)
        if failed or not self.cameras:
            self.discovery_subtitle.set_label("No new cameras found")
        else:
            n = len(self.cameras)
            self.discovery_subtitle.set_label(f"{n} found")

    # ── Connect ──

    def _connect_camera(self, idx, name):
        self.connected_name = name
        self._pending_camera_idx = idx
        self._camera_id = name  # used as key for saved creds

        # Check for saved credentials
        saved = load_saved_creds().get(self._camera_id)
        if saved:
            self._credentials = (saved["user"], saved["pass"])
            self.remember_check.set_active(True)
            # Skip login form, connect directly
            self.login_box.set_visible(False)
            self.spinner_box.set_visible(True)
            self.connecting_spinner.set_spinning(True)
            self.connecting_label.set_label(f"Connecting to {name}...")
            self.connecting_hint.set_label("")
            self.stack.set_visible_child_name("connecting")
            self.header.set_title_widget(
                Adw.WindowTitle(title="Sony Remote", subtitle="Connecting..."))
            if self.cam:
                self.cam.stop()
            self.cam = CameraProcess(self._on_connect_scan_output, credentials=self._credentials)
            self.cam.start()
            return

        # Show login form
        self.login_box.set_visible(True)
        self.spinner_box.set_visible(False)
        self.login_title.set_label(f"Sign in to {name}")
        self.login_error.set_visible(False)
        self.user_entry.set_text("admin")
        self.pass_entry.set_text("")
        self.remember_check.set_active(False)
        self.stack.set_visible_child_name("connecting")
        self.header.set_title_widget(
            Adw.WindowTitle(title="Sony Remote", subtitle=name))
        self.pass_entry.grab_focus()

    def _on_login_submit(self):
        user = self.user_entry.get_text().strip()
        pw = self.pass_entry.get_text()
        if not user:
            self.login_error.set_label("Username is required")
            self.login_error.set_visible(True)
            return
        self._credentials = (user, pw)
        # Switch to spinner
        self.login_box.set_visible(False)
        self.spinner_box.set_visible(True)
        self.connecting_spinner.set_spinning(True)
        self.connecting_label.set_label(f"Connecting to {self.connected_name}...")
        self.connecting_hint.set_label("")

        # Restart the process with credentials
        if self.cam:
            self.cam.stop()
        self.cam = CameraProcess(self._on_connect_scan_output, credentials=self._credentials)
        self.cam.start()

    def _on_connect_scan_output(self, line):
        """Handle scan output during credential-based reconnect, auto-select camera."""
        debug(f"[rescan] {line.rstrip()}")

        if "No cameras detected" in line or "[process_exited]" in line:
            self._go_to_discovery("No cameras found. Check your network.")
            return

        if "Connect to camera with input number" in line:
            self.cam.on_output = self._on_connect_output
            # Always select camera 1 (re-scan may re-index)
            self.cam.send("1")

    def _on_connect_output(self, line):
        try:
            self.__on_connect_output(line)
        except Exception as e:
            debug(f"[ERROR] _on_connect_output: {e}")

    def __on_connect_output(self, line):
        debug(f"[connect] {line.rstrip()}")

        if "<< TOP-MENU >>" in line:
            GLib.timeout_add(200, lambda: self.cam.send("1") or False)

        if "Connecting..." in line:
            self.connecting_hint.set_label("Waiting for camera to accept...")

        if "Failed to connect" in line:
            # Note: 0x8,213 is not fatal — SDK still proceeds to REMOTE-MENU
            debug(f"[connect] Warning: {line.rstrip()} (may not be fatal)")

        if "<< REMOTE-MENU >>" in line:
            # Successfully connected
            if self.remember_check.get_active() and hasattr(self, '_credentials'):
                save_creds(self._camera_id, self._credentials[0], self._credentials[1])
            self.cam.on_output = self._on_control_output
            self.busy = False
            self._in_submenu = False
            self.connected_label.set_label(self.connected_name)
            self.header.set_visible(False)
            self.stack.set_visible_child_name("controls")
            self._start_live_view()

        if "[process_exited]" in line:
            self._go_to_discovery("Connection lost")

    def _cleanup_and_rediscover(self):
        if self.cam:
            self.cam.stop()
            self.cam = None
        self._go_to_discovery("Connection failed. Check your camera settings.")
        return False

    def on_cancel_connect(self, btn):
        if self.cam:
            self.cam.stop()
            self.cam = None
        self._go_to_discovery()

    # ── Controls ──

    def _on_af_mode_changed(self, dropdown, param):
        idx = dropdown.get_selected()
        _, mode_val = self.AF_MODES[idx]
        if self.cam and self._lv_streaming:
            self.cam.send(f"afmode {mode_val}")
            # If switching to AF-C, also set tracking focus area
            if mode_val == 0x0003:  # AF-C
                self.cam.send(f"afarea {0x0014}")  # CrFocusArea_Tracking_Flexible_Spot_S

    def _on_shutter_clicked(self, btn):
        if self.cam and self._lv_streaming:
            self.cam.send("shoot")
        else:
            self._action(["1", "1"])

    def _on_lv_click(self, gesture, n_press, x, y):
        """Click on live view to set AF position and trigger autofocus."""
        if not self.cam or not self._lv_streaming:
            return
        width = self.lv_picture.get_width()
        height = self.lv_picture.get_height()
        if width <= 0 or height <= 0:
            return
        # Mirror X since live view is flipped
        af_x = int((1.0 - x / width) * 639)
        af_y = int(y / height * 479)
        af_x = max(0, min(639, af_x))
        af_y = max(0, min(479, af_y))

        # Check if AF-C (tracking mode)
        af_idx = self.af_mode_dropdown.get_selected()
        _, af_mode = self.AF_MODES[af_idx]
        if af_mode == 0x0003:  # AF-C - set tracking area then AF
            self.cam.send(f"afarea {0x0014}")  # Tracking_Flexible_Spot_S

        self.cam.send(f"af {af_x} {af_y}")

    def _draw_focus_frames(self, area, cr, width, height):
        """Draw focus, tracking, and face frame overlays on the live view."""
        if not self.overlay_toggle.get_active():
            return
        # CrFocusFrameState values
        STATE_FOCUSED = 2
        STATE_MOVING = 4

        def draw_rect(xn, yn, xd, yd, fw, fh, r, g, b, a, line_w=2):
            if xd == 0 or yd == 0 or (fw == 0 and fh == 0):
                return
            cx = (xn / xd) * width
            cy = (yn / yd) * height
            rw = (fw / xd) * width
            rh = (fh / yd) * height
            rx = cx - rw / 2
            ry = cy - rh / 2
            cr.set_source_rgba(r, g, b, a)
            cr.set_line_width(line_w)
            cr.rectangle(rx, ry, rw, rh)
            cr.stroke()

        # AF frames - white when acquiring, green when focused
        for frame in (self._focus_frames or []):
            try:
                xn, yn, xd, yd, fw, fh = frame[:6]
                state = frame[6] if len(frame) > 6 else 0
                if state == STATE_FOCUSED:
                    draw_rect(xn, yn, xd, yd, fw, fh, 0.2, 1.0, 0.2, 0.9)
                elif fw > 0 and fh > 0:
                    draw_rect(xn, yn, xd, yd, fw, fh, 1.0, 1.0, 1.0, 0.7)
            except Exception:
                pass

        # Tracking frames - cyan, thicker
        for frame in (self._tracking_frames or []):
            try:
                xn, yn, xd, yd, fw, fh = frame[:6]
                ftype = frame[6] if len(frame) > 6 else 0
                state = frame[7] if len(frame) > 7 else 0
                if fw == 0 and fh == 0:
                    continue
                if state == STATE_FOCUSED:
                    draw_rect(xn, yn, xd, yd, fw, fh, 0.2, 1.0, 0.2, 0.9, 3)
                elif state == STATE_MOVING:
                    draw_rect(xn, yn, xd, yd, fw, fh, 0.2, 0.8, 1.0, 0.9, 3)
                else:
                    draw_rect(xn, yn, xd, yd, fw, fh, 0.2, 0.8, 1.0, 0.7, 2)
            except Exception:
                pass

        # Face frames - yellow, thin
        for frame in (self._face_frames or []):
            try:
                xn, yn, xd, yd, fw, fh = frame[:6]
                state = frame[6] if len(frame) > 6 else 0
                if fw == 0 and fh == 0:
                    continue
                draw_rect(xn, yn, xd, yd, fw, fh, 1.0, 1.0, 0.3, 0.7, 1.5)
            except Exception:
                pass

    def _action(self, cmds):
        """Stop stream, execute action, return to REMOTE-MENU, restart stream."""
        if not self.cam:
            return
        # Stop live view stream first
        if self._lv_streaming:
            self.cam.send("stop")
            self._lv_streaming = False
            # Wait for stream to stop and return to REMOTE-MENU, then do action
            GLib.timeout_add(1000, lambda: self._do_action(cmds) or False)
        else:
            if self.busy:
                return
            self._do_action(cmds)

    def _do_action(self, cmds):
        self.busy = True
        for i, cmd in enumerate(cmds):
            GLib.timeout_add(i * 500, lambda c=cmd: self.cam.send(c) or False)
        # Send "0" to return from submenu after action completes
        GLib.timeout_add(len(cmds) * 500 + 3000, lambda: self.cam.send("0") or False)
        # Restart live view stream after returning
        if self._lv_active:
            GLib.timeout_add(len(cmds) * 500 + 4000, self._start_lv_stream)

    def _on_control_output(self, line):
        debug(f"[ctrl] {line.rstrip()}")

        if "LVSTREAM_PROPS " in line:
            try:
                props = line.strip().split("LVSTREAM_PROPS ", 1)[1]
                parts = props.split(",", 3)
                if len(parts) >= 3:
                    self.lbl_aperture.set_label(parts[0].strip())
                    self.lbl_shutter.set_label(parts[1].strip())
                    self.lbl_iso.set_label(parts[2].strip())
            except Exception:
                pass

        if "<< REMOTE-MENU >>" in line:
            self.busy = False
            self._lv_streaming = False

        if "LVSTREAM_FRAME" in line:
            # Parse: LVSTREAM_FRAME N|AF:x,y,xd,yd,w,h,state;...|TRK:...|FACE:...
            self._focus_frames = []
            self._tracking_frames = []
            self._face_frames = []
            parts = line.strip().split("|")
            for part in parts[1:]:  # skip "LVSTREAM_FRAME N"
                if part.startswith("AF:") and len(part) > 3:
                    for finfo in part[3:].split(";"):
                        vals = [v for v in finfo.split(",") if v]
                        if len(vals) >= 6:
                            try:
                                self._focus_frames.append(tuple(int(v) for v in vals[:7] if v))
                            except ValueError:
                                pass
                elif part.startswith("TRK:") and len(part) > 4:
                    for finfo in part[4:].split(";"):
                        vals = [v for v in finfo.split(",") if v]
                        if len(vals) >= 6:
                            try:
                                self._tracking_frames.append(tuple(int(v) for v in vals))
                            except ValueError:
                                pass
                elif part.startswith("FACE:") and len(part) > 5:
                    for finfo in part[5:].split(";"):
                        vals = [v for v in finfo.split(",") if v]
                        if len(vals) >= 6:
                            try:
                                self._face_frames.append(tuple(int(v) for v in vals))
                            except ValueError:
                                pass
            self._update_liveview_image()
            self.focus_draw.queue_draw()

        if "LVSTREAM_STOP" in line:
            self._lv_streaming = False
            self.busy = False
            # If still active, we need to return to REMOTE-MENU
            if self._lv_active:
                self.cam.send("0")  # return from Other Menu

        if "LVSTREAM_ERROR" in line:
            self._lv_streaming = False

        if "GetLiveView SUCCESS" in line:
            self._update_liveview_image()

        if "<< TOP-MENU >>" in line:
            self._stop_live_view()
            if self.cam:
                self.cam.send("x")
                GLib.timeout_add(1000, self._cleanup_disconnect)

        if "[process_exited]" in line or "Disconnect successfully" in line:
            self._stop_live_view()
            self._go_to_discovery("Camera disconnected")

    # ── Live view ──

    def _start_live_view(self):
        try:
            os.remove(LIVEVIEW_PATH)
        except FileNotFoundError:
            pass
        self._lv_active = True
        self._lv_streaming = False
        # Enter Other Menu → start stream
        self._start_lv_stream()

    def _start_lv_stream(self):
        if not self._lv_active or not self.cam:
            return
        self.busy = True
        self.cam.send("7")  # Other Menu
        GLib.timeout_add(500, lambda: self.cam.send("stream") or False)
        GLib.timeout_add(1000, lambda: self.cam.send("0") or False)  # 0 = max fps
        self._lv_streaming = True

    def _update_liveview_image(self):
        try:
            if os.path.exists(LIVEVIEW_PATH) and os.path.getsize(LIVEVIEW_PATH) > 100:
                # Resize window to match image aspect ratio on first frame
                if not self._lv_size_set:
                    try:
                        pb = GdkPixbuf.Pixbuf.new_from_file(LIVEVIEW_PATH)
                        iw, ih = pb.get_width(), pb.get_height()
                        if iw > 0 and ih > 0:
                            aspect = iw / ih
                            # Target ~900px wide, scale height to match
                            new_w = 900
                            new_h = int(new_w / aspect)
                            self.win.set_default_size(new_w, new_h)
                            self._lv_size_set = True
                    except Exception:
                        pass
                self.lv_picture.set_filename(None)
                self.lv_picture.set_filename(LIVEVIEW_PATH)
                self.lv_placeholder.set_visible(False)
        except Exception as e:
            debug(f"[lv] Error: {e}")

    def _stop_live_view(self):
        self._lv_active = False
        if self._lv_streaming and self.cam:
            self.cam.send("stop")
        self._lv_streaming = False

    # ── Disconnect ──

    def on_disconnect(self, btn):
        self._stop_live_view()
        if self.cam:
            if self.busy:
                # Try to get back to REMOTE-MENU first
                GLib.timeout_add(0, lambda: self.cam.send("0") or False)
                GLib.timeout_add(600, lambda: self.cam.send("0") or False)
                GLib.timeout_add(1200, lambda: self.cam.send("x") or False)
            else:
                self.cam.send("0")
                GLib.timeout_add(500, lambda: self.cam.send("x") or False)
            GLib.timeout_add(2000, self._cleanup_disconnect)

    def _cleanup_disconnect(self):
        if self.cam:
            self.cam.stop()
            self.cam = None
        self._go_to_discovery()
        return False

    # ── Navigation ──

    def _go_to_discovery(self, message=None):
        self._stop_live_view()
        self.header.set_visible(True)
        self.stack.set_visible_child_name("discovery")
        self.header.set_title_widget(Adw.WindowTitle(title="Sony Remote", subtitle=""))
        self.scan_btn.set_sensitive(True)
        self.scan_btn.set_label("Scan")
        self.scan_spinner.set_spinning(False)
        self.scan_spinner.set_visible(False)
        self.lv_placeholder.set_visible(True)
        self.lv_picture.set_filename(None)
        self._focus_frames = []
        self._tracking_frames = []
        self._face_frames = []
        self._lv_size_set = False
        if message:
            self.discovery_subtitle.set_label(message)
        else:
            self.discovery_subtitle.set_label("")
        self.cameras = []
        self._show_saved_cameras()

    def do_shutdown(self):
        self._stop_live_view()
        if self.cam:
            self.cam.stop()
        Adw.Application.do_shutdown(self)


if __name__ == "__main__":
    import sys, traceback
    def exception_hook(exctype, value, tb):
        debug(f"[CRASH] {''.join(traceback.format_exception(exctype, value, tb))}")
        sys.__excepthook__(exctype, value, tb)
    sys.excepthook = exception_hook
    app = SonyRemoteApp()
    app.run()
