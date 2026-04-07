#!/usr/bin/env python3
"""System tray helper for Sony Remote (runs as separate process using GTK3)."""

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AyatanaAppIndicator3', '0.1')
from gi.repository import Gtk, AyatanaAppIndicator3, GLib
import subprocess
import sys
import os

APP_ID = "com.sony.remote.gui"


def activate_action(action_name):
    """Send an action to the main GTK4 app via D-Bus."""
    try:
        subprocess.Popen([
            "gdbus", "call", "--session",
            "--dest", APP_ID,
            "--object-path", "/com/sony/remote/gui",
            "--method", "org.freedesktop.Application.ActivateAction",
            action_name, "[]", "{}"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def activate_app():
    """Bring the main app window to front."""
    try:
        subprocess.Popen([
            "gdbus", "call", "--session",
            "--dest", APP_ID,
            "--object-path", "/com/sony/remote/gui",
            "--method", "org.freedesktop.Application.Activate",
            "{}"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def main():
    # Prevent duplicate tray icons
    import fcntl
    lock_file = "/tmp/sony-remote-tray.lock"
    fp = open(lock_file, "w")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        sys.exit(0)  # Another tray is already running

    indicator = AyatanaAppIndicator3.Indicator.new(
        "sony-remote",
        "camera-video-symbolic",
        AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS)
    indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
    indicator.set_title("Sony Remote")

    menu = Gtk.Menu()

    show_item = Gtk.MenuItem.new_with_label("Show Sony Remote")
    show_item.connect("activate", lambda w: activate_app())
    menu.append(show_item)

    sep = Gtk.SeparatorMenuItem()
    menu.append(sep)

    quit_item = Gtk.MenuItem.new_with_label("Quit")
    quit_item.connect("activate", lambda w: (activate_action("quit"), Gtk.main_quit()))
    menu.append(quit_item)

    menu.show_all()
    indicator.set_menu(menu)

    # Quit when parent dies
    GLib.timeout_add(2000, lambda: os.getppid() == 1 and Gtk.main_quit() or True)

    Gtk.main()


if __name__ == "__main__":
    main()
