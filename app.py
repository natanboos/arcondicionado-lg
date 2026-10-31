"""Ar-condicionado LG ThinQ: painel flutuante para Windows com ícone na bandeja."""

from __future__ import annotations

import ctypes
import logging
import math
import os
import sys
import time
import webbrowser
import winreg
from ctypes import wintypes
from pathlib import Path

import pystray
import webview
from PIL import Image, ImageDraw

from lg_backend import CONFIG_DIR, PAT_URL, LGBackend, save_config

APP_DIR = Path(__file__).resolve().parent
UI_FILE = APP_DIR / "ui" / "index.html"
ICON_FILE = APP_DIR / "ui" / "icon.ico"
WIDTH, HEIGHT = 372, 626
MARGIN = 12
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ArCondicionadoLG"

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=CONFIG_DIR / "app.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_LOGGER = logging.getLogger("app")

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi


def work_area() -> tuple[float, float, float, float]:
    """Área útil da tela (sem a barra de tarefas), em pixels lógicos."""
    rect = wintypes.RECT()
    user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)
    scale = user32.GetDpiForSystem() / 96
    return rect.left / scale, rect.top / scale, rect.right / scale, rect.bottom / scale


def flyout_position() -> tuple[int, int]:
    _, _, right, bottom = work_area()
    return int(right - WIDTH - MARGIN), int(bottom - HEIGHT - MARGIN)


def style_native_window(hwnd: int) -> None:
    """Cantos arredondados do Windows 11 e sem botão na barra de tarefas."""
    pref = ctypes.c_int(2)  # DWMWCP_ROUND
    dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
    border = ctypes.c_int(0x00403A33)  # cor da borda (BGR)
    dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
    ex_style = user32.GetWindowLongW(hwnd, -20)
    user32.SetWindowLongW(hwnd, -20, (ex_style | 0x80) & ~0x40000)  # TOOLWINDOW, sem APPWINDOW


def make_icon_image(size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, size - 2, size - 2), fill=(59, 130, 246, 255))
    c, r, w = size / 2, size * 0.30, max(2, size // 14)
    for i in range(3):
        a = math.pi / 3 * i
        dx, dy = r * math.cos(a), r * math.sin(a)
        d.line((c - dx, c - dy, c + dx, c + dy), fill="white", width=w)
    return img


class Api:
    """Métodos chamados pelo JavaScript da interface."""

    def __init__(self, backend: LGBackend) -> None:
        self._backend = backend
        self._window: webview.Window | None = None
        self._hidden_at = 0.0
        self._shown_at = 0.0
        self._visible = True

    # ---- janela ----
    def hide(self):
        if self._window:
            self._window.hide()
            self._visible = False
            self._hidden_at = time.time()
            self._backend.set_panel_visible(False)

    def on_blur(self):
        # ao abrir pela bandeja o Windows devolve o foco à barra de tarefas por um instante
        if time.time() - self._shown_at < 0.8:
            self._focus()
            return
        if self._backend.cfg.get("autohide", True) and self._backend.logged_in:
            self.hide()

    def toggle(self):
        """Clique no ícone da bandeja: abre ou fecha o painel."""
        # se o clique na bandeja acabou de esconder a janela (perda de foco), não reabre
        if self._visible or time.time() - self._hidden_at < 0.4:
            self.hide()
        else:
            self.show()

    def show(self):
        self._shown_at = time.time()
        self._visible = True
        self._backend.set_panel_visible(True)
        x, y = flyout_position()
        self._window.move(x, y)
        self._window.show()
        self._window.restore()
        self._focus()
        self._window.evaluate_js("window.onShown && window.onShown()")

    def _focus(self):
        try:
            hwnd = self._window.native.Handle.ToInt32()
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
            user32.SetForegroundWindow(hwnd)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Falha ao focar a janela")

    # ---- conta ----
    def init(self):
        return {"logged_in": self._backend.logged_in, "settings": self.get_settings()}

    def login_pat(self, pat):
        try:
            self._backend.login_with_pat(pat)
            return {"ok": True}
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning("Falha no login com token: %s", ex)
            return {"ok": False, "error": str(ex) or "Token inválido"}

    @staticmethod
    def open_pat_site():
        webbrowser.open(PAT_URL)

    def logout(self):
        self._backend.logout()
        return True

    # ---- aparelho ----
    def connect(self):
        try:
            self._backend.connect()
            return {"ok": True, "state": self._backend.state()}
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception("Falha ao conectar")
            return {"ok": False, "error": str(ex) or ex.__class__.__name__}

    def get_state(self):
        return self._backend.state()

    def refresh(self):
        try:
            self._backend.refresh()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Falha ao atualizar")
        return self._backend.state()

    def command(self, name, value):
        try:
            return {"ok": True, "state": self._backend.command(name, value)}
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception("Falha no comando %s=%s", name, value)
            return {"ok": False, "error": str(ex) or ex.__class__.__name__,
                    "state": self._backend.state()}

    def select_device(self, device_id):
        self._backend.cfg["device_id"] = device_id
        save_config(self._backend.cfg)
        return self.connect()

    # ---- preferências ----
    def get_settings(self):
        return {
            "autohide": self._backend.cfg.get("autohide", True),
            "autostart": self._get_autostart(),
        }

    def set_setting(self, key, value):
        if key == "autostart":
            self._set_autostart(bool(value))
        else:
            self._backend.cfg[key] = value
            save_config(self._backend.cfg)
        return self.get_settings()

    @staticmethod
    def _get_autostart() -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, RUN_NAME)
                return True
        except OSError:
            return False

    @staticmethod
    def _set_autostart(enable: bool) -> None:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ,
                                  f'"{pythonw}" "{APP_DIR / "app.py"}"')
            else:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                except OSError:
                    pass

    def quit(self):
        os._exit(0)


def main():
    # uma única instância (evita dois ícones na bandeja)
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, "ArCondicionadoLG_Mutex")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return

    backend = LGBackend()
    api = Api(backend)
    x, y = flyout_position()
    window = webview.create_window(
        "Ar-condicionado",
        str(UI_FILE),
        js_api=api,
        width=WIDTH,
        height=HEIGHT,
        x=x,
        y=y,
        frameless=True,
        easy_drag=False,
        resizable=False,
        on_top=True,
        shadow=True,
        background_color="#1C1F26",
    )
    api._window = window

    def on_shown():
        try:
            # sem a borda o WinForms encolhe a janela: reaplica o tamanho exato
            window.resize(WIDTH, HEIGHT)
            window.move(*flyout_position())
            style_native_window(window.native.Handle.ToInt32())
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Falha ao estilizar a janela")

    window.events.shown += on_shown

    if not ICON_FILE.exists():
        make_icon_image(256).save(ICON_FILE, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])

    tray = pystray.Icon(
        "ArCondicionadoLG",
        make_icon_image(),
        "Ar-condicionado LG",
        menu=pystray.Menu(
            pystray.MenuItem("Abrir/fechar", lambda: api.toggle(), default=True, visible=False),
            pystray.MenuItem("Abrir", lambda: api.show()),
            pystray.MenuItem("Sair", lambda: (tray.stop(), api.quit())),
        ),
    )
    tray.run_detached()

    # modo privado: sem cache em disco, assim a interface nunca fica numa versão antiga
    webview.start(icon=str(ICON_FILE), private_mode=True)


if __name__ == "__main__":
    main()
