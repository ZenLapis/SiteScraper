"""SiteScraper desktop app (WebView2 UI on top of sitescraper_core)."""
import ctypes
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import webview

import sitescraper_core as core

RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
UI_FILE = RESOURCE_DIR / "ui" / "sitescraper.html"
DATA_DIR = Path(os.getenv("LOCALAPPDATA", Path.home())) / "SiteScraper"
SETTINGS_FILE = DATA_DIR / "settings.json"


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


APP_TITLE = "SiteScraper"


class Api:
    def __init__(self):
        self._window = None
        self.settings = load_settings()
        self.records = []
        self.stop_event = None
        self.running = False

    def _emit(self, event, payload=None):
        if self._window:
            self._window.evaluate_js(f"app.emit({json.dumps(event)}, {json.dumps(payload, ensure_ascii=False, default=str)})")

    def get_state(self):
        return {"options": self.settings.get("options", {}), "recent": self.settings.get("recent", [])}

    def start(self, opts):
        if self.running:
            return {"error": "A crawl is already running."}
        try:
            options = core.CrawlOptions(
                start_url=opts["url"], max_pages=int(opts["max_pages"]), max_depth=int(opts["max_depth"]),
                delay=float(opts["delay"]), timeout=float(opts["timeout"]),
                same_domain=bool(opts["same_domain"]), respect_robots=bool(opts["robots"]),
            ).validated()
        except (ValueError, KeyError, TypeError) as exc:
            return {"error": str(exc)}
        self.settings["options"] = {k: v for k, v in opts.items() if k != "url"}
        recent = [options.start_url] + [u for u in self.settings.get("recent", []) if u != options.start_url]
        self.settings["recent"] = recent[:8]
        save_settings(self.settings)
        self.records = []
        self.stop_event = threading.Event()
        self.running = True

        def on_record(rec):
            self.records.append(rec)
            row = {k: rec[k] for k in ("url", "final_url", "depth", "status", "title", "word_count",
                                       "links_found", "emails", "phones", "error", "content_type")}
            row["i"] = len(self.records) - 1
            self._emit("record", row)

        source = core.detect_source(options.start_url)
        if source == "tga":
            def tga_job():
                src = core.TgaFacilitySource(self.stop_event)
                self._emit("dataset", {"columns": src.columns, "title": "TGA certified facilities"})
                try:
                    def on_rows(batch, total):
                        start = len(self.records)
                        self.records.extend(batch)
                        self._emit("rows", {"start": start, "rows": batch, "total": total})
                    rows = src.fetch(on_rows=on_rows, on_status=lambda m: self._emit("status", m))
                    self.records = rows  # export in the site's own order
                except Exception as exc:
                    self._emit("status", f"Error: {exc}")
                finally:
                    self.running = False
                    self._emit("done", {"stopped": self.stop_event.is_set(), "count": len(self.records), "unit": "facilities"})
                    notify(self._window, "Download finished", f"{len(self.records):,} facilities collected.")
            threading.Thread(target=tga_job, daemon=True).start()
            return {"ok": True, "url": options.start_url, "max": 1, "dataset": True}

        def job():
            try:
                crawler = core.SiteCrawler(options, self.stop_event)
                crawler.crawl(on_record=on_record, on_status=lambda m: self._emit("status", m))
            except Exception as exc:
                self._emit("status", f"Error: {exc}")
            finally:
                self.running = False
                self._emit("done", {"stopped": self.stop_event.is_set(), "count": len(self.records)})
                notify(self._window, "Crawl finished", f"{len(self.records)} pages collected.")

        threading.Thread(target=job, daemon=True).start()
        return {"ok": True, "url": options.start_url, "max": options.max_pages}

    def stop(self):
        if self.stop_event:
            self.stop_event.set()

    def detail(self, i):
        rec = dict(self.records[i])
        return rec

    def export(self, fmt):
        if not self.records:
            return {"error": "There are no results to export yet."}
        host = core.site_key(self.records[0].get("url", "")) or "tga_tesisler"
        name = f"{host}_{datetime.now():%Y-%m-%d_%H%M}.{fmt}"
        folder = self.settings.get("export_dir") or str(Path.home() / "Desktop")
        res = self._window.create_file_dialog(webview.SAVE_DIALOG, directory=folder, save_filename=name)
        if not res:
            return None
        path = res if isinstance(res, str) else res[0]
        if not Path(path).suffix:
            path += "." + fmt
        try:
            out = core.export_records(self.records, path)
        except Exception as exc:
            return {"error": str(exc)}
        self.settings["export_dir"] = str(out.parent)
        save_settings(self.settings)
        return {"path": str(out)}

    def open_path(self, path, reveal=False):
        if reveal:
            subprocess.Popen(["explorer", "/select,", path])
        else:
            os.startfile(path)

    def open_url(self, url):
        if urlsplit(url).scheme in ("http", "https"):
            os.startfile(url)


def notify(window, title, text, ok=True):
    """Finish sound plus a Windows notification, so a long job can run in the background."""
    try:
        import winsound
        winsound.PlaySound("SystemAsterisk" if ok else "SystemHand",
                           winsound.SND_ALIAS | winsound.SND_ASYNC)
    except Exception:
        pass
    try:
        import clr  # noqa: F401
        from System import Action
        from System.Drawing import Icon
        from System.Windows.Forms import NotifyIcon, ToolTipIcon

        def show():
            tray = NotifyIcon()
            tray.Icon = Icon(str(RESOURCE_DIR / "ui" / "app.ico"))
            tray.Text = APP_TITLE
            tray.Visible = True
            tray.ShowBalloonTip(6000, title, text, ToolTipIcon.Info if ok else ToolTipIcon.Warning)
            threading.Timer(12, lambda: window.native.Invoke(Action(tray.Dispose))).start()

        window.native.Invoke(Action(show))
    except Exception:
        pass


def set_window_icon(window):
    try:
        import clr  # noqa: F401  (pythonnet, loaded by pywebview)
        from System import Action
        from System.Drawing import Icon
        icon = Icon(str(RESOURCE_DIR / "ui" / "app.ico"))
        window.native.Invoke(Action(lambda: setattr(window.native, "Icon", icon)))
    except Exception:
        pass


def style_titlebar(window):
    set_window_icon(window)
    try:
        hwnd = window.native.Handle.ToInt32()
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(ctypes.c_int(0x00110F0E)), 4)
    except Exception:
        pass


def main():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ZenLapis.SiteScraper")
    except Exception:
        pass
    api = Api()
    geo = api.settings.get("window", {})
    window = webview.create_window("SiteScraper", url=UI_FILE.as_uri(), js_api=api,
                                   width=geo.get("w", 1280), height=geo.get("h", 820), min_size=(1000, 660),
                                   background_color="#0E0F11", text_select=False)
    api._window = window
    window.events.shown += lambda: style_titlebar(window)
    window.events.resized += lambda w, h: api.settings.__setitem__("window", {"w": w, "h": h})
    window.events.closing += lambda: (api.stop(), save_settings(api.settings))
    webview.start(gui="edgechromium", private_mode=False, storage_path=str(DATA_DIR / "webview"))


if __name__ == "__main__":
    main()
