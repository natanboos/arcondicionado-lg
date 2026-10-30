"""Ponte entre a interface e a nuvem LG ThinQ (usa a biblioteca wideq do ha-smartthinq-sensors)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

from wideq import DeviceType, get_lge_device
from wideq.core_exceptions import UseOfficialAPIError
from wideq.core_async import ClientAsync
from wideq.devices import ac as acmod
from wideq.devices.ac import ACMode, AirConditionerDevice

_LOGGER = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".arcondicionado-lg"
CONFIG_FILE = CONFIG_DIR / "config.json"

POLL_INTERVAL = 60  # segundos entre leituras do aparelho
MIN_REFRESH_GAP = 45  # leitura extra ao abrir o painel só se a última for mais antiga que isso
# a LG bloqueia a conta (erro 9012) quando acha que há requisições demais: ao receber
# um aviso dela, o app para de consultar a nuvem por um tempo
BLOCK_PAUSE = 24 * 3600
WARNING_PAUSE = 15 * 60

# Modo "Auto" da interface: usa o primeiro modo automático suportado
AUTO_MODES = [ACMode.ACO.name, ACMode.AI.name]

FAN_LABELS = {
    "SLOW": "Muito baixo",
    "SLOW_LOW": "Muito baixo",
    "LOW": "Baixo",
    "LOW_MID": "Médio-baixo",
    "MID": "Médio",
    "MID_HIGH": "Médio-alto",
    "HIGH": "Alto",
    "POWER": "Potente",
    "AUTO": "Auto",
    "NATURE": "Natural",
}


class AccountBlockedError(Exception):
    """A LG bloqueou temporariamente o acesso desta conta."""


def _lg_block_message(ex: BaseException) -> str | None:
    """Mensagem do aviso de bloqueio/uso da API oficial, se for esse o erro."""
    seen = set()
    while ex is not None and id(ex) not in seen:
        seen.add(id(ex))
        if isinstance(ex, UseOfficialAPIError):
            return str(ex) or "9006"
        ex = ex.__cause__ or ex.__context__
    return None


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


class LGBackend:
    """Executa a wideq num loop asyncio próprio, em uma thread separada."""

    def __init__(self) -> None:
        self.cfg = load_config()
        self.cfg.setdefault("country", "BR")
        self.cfg.setdefault("language", "pt-BR")
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()
        self.client: ClientAsync | None = None
        self.device: AirConditionerDevice | None = None
        self.devices: list[dict] = []
        self.error: str | None = None
        self.last_poll = 0.0
        self._poll_task = None
        self._lock = None

    # ---------- utilidades ----------
    def run(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    @property
    def logged_in(self) -> bool:
        return bool(self.cfg.get("refresh_token"))

    @property
    def blocked_until(self) -> float:
        return float(self.cfg.get("blocked_until", 0))

    def _check_blocked(self) -> None:
        remaining = self.blocked_until - time.time()
        if remaining > 0:
            hours, minutes = int(remaining // 3600), int(remaining % 3600 // 60)
            when = f"{hours}h{minutes:02d}" if hours else f"{minutes} min"
            raise AccountBlockedError(
                "A LG pausou o acesso desta conta por excesso de consultas. "
                f"O app volta a tentar sozinho em {when}."
            )

    def _register_block(self, message: str) -> None:
        pause = BLOCK_PAUSE if "block" in message.lower() else WARNING_PAUSE
        _LOGGER.warning("LG pediu para reduzir as consultas; pausando por %s s: %s",
                        pause, message)
        self.cfg["blocked_until"] = time.time() + pause
        save_config(self.cfg)
        self.error = message

    def _client_id_cb(self, client_id: str) -> None:
        self.cfg["client_id"] = client_id
        save_config(self.cfg)

    # ---------- autenticação ----------
    def get_login_url(self) -> str:
        return self.run(
            ClientAsync.get_login_url(self.cfg["country"], self.cfg["language"])
        )

    def login_with_url(self, url: str) -> None:
        info = self.run(
            ClientAsync.oauth_info_from_url(url, self.cfg["country"], self.cfg["language"])
        )
        self._store_oauth(info)

    def login_with_password(self, user: str, password: str) -> None:
        info = self.run(
            ClientAsync.oauth_info_from_user_login(
                user, password, self.cfg["country"], self.cfg["language"]
            )
        )
        self._store_oauth(info)

    def _store_oauth(self, info: dict) -> None:
        if not info or not info.get("refresh_token"):
            raise RuntimeError("Não foi possível obter o token da LG")
        self.cfg["refresh_token"] = info["refresh_token"]
        self.cfg["oauth_url"] = info.get("oauth_url")
        save_config(self.cfg)

    def logout(self) -> None:
        self.run(self._close())
        for key in ("refresh_token", "oauth_url", "client_id", "device_id"):
            self.cfg.pop(key, None)
        save_config(self.cfg)

    async def _close(self):
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        if self.client:
            await self.client.close()
        self.client = None
        self.device = None

    # ---------- conexão e aparelhos ----------
    def connect(self) -> None:
        self._check_blocked()
        try:
            self.run(self._connect(), timeout=120)
        except Exception as ex:
            if msg := _lg_block_message(ex):
                self._register_block(msg)
                self._check_blocked()
            raise

    async def _connect(self):
        await self._close()
        self._lock = asyncio.Lock()
        self.client = await ClientAsync.from_token(
            self.cfg["refresh_token"],
            country=self.cfg["country"],
            language=self.cfg["language"],
            oauth_url=self.cfg.get("oauth_url"),
            client_id=self.cfg.get("client_id"),
            update_clientid_callback=self._client_id_cb,
        )
        if not self.cfg.get("client_id") and self.client.client_id:
            self._client_id_cb(self.client.client_id)

        acs = [d for d in (self.client.devices or []) if d.type == DeviceType.AC]
        self.devices = [{"id": d.device_id, "name": d.name, "model": d.model_name} for d in acs]
        if not acs:
            raise RuntimeError("Nenhum ar-condicionado encontrado na sua conta LG ThinQ")

        wanted = self.cfg.get("device_id")
        info = next((d for d in acs if d.device_id == wanted), acs[0])
        devs = get_lge_device(self.client, info)
        if not devs:
            raise RuntimeError(f"Aparelho '{info.name}' não é suportado")
        self.device = devs[0]
        if not await self.device.init_device_info():
            raise RuntimeError("Falha ao carregar as informações do modelo")
        self.cfg["device_id"] = info.device_id
        save_config(self.cfg)
        await self._poll()
        self._poll_task = asyncio.ensure_future(self._poll_loop())

    async def _poll(self):
        if self.blocked_until > time.time():
            return
        async with self._lock:
            try:
                await self.device.poll()
                self.error = None
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.warning("Falha ao ler o aparelho: %s", ex)
                if msg := _lg_block_message(ex):
                    self._register_block(msg)
                else:
                    self.error = str(ex) or ex.__class__.__name__
            self.last_poll = time.time()

    async def _poll_loop(self):
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            await self._poll()

    def refresh(self) -> None:
        if self.device and time.time() - self.last_poll > MIN_REFRESH_GAP:
            self.run(self._poll())

    # ---------- estado ----------
    def _swing_support(self) -> tuple[bool, bool]:
        dev = self.device
        v = "Swing" in dev.vertical_step_modes or bool(
            dev._is_mode_supported(acmod.SUPPORT_VANE_VSWING)  # pylint: disable=protected-access
        )
        h = "Swing" in dev.horizontal_step_modes or bool(
            dev._is_mode_supported(acmod.SUPPORT_VANE_HSWING)  # pylint: disable=protected-access
        )
        return v, h

    def state(self) -> dict:
        dev = self.device
        if not dev:
            return {"ready": False}
        st = dev.status
        v_sup, h_sup = self._swing_support()
        modes = dev.op_modes
        auto_mode = next((m for m in AUTO_MODES if m in modes), None)
        fan_speeds = [f for f in dev.fan_speeds if f in FAN_LABELS]

        data = {
            "ready": True,
            "name": dev.name,
            "devices": self.devices,
            "device_id": dev.device_info.device_id,
            "error": self.error,
            "blocked": self.blocked_until > time.time(),
            "online": st is not None and self.error is None,
            "min_temp": dev.target_temperature_min or 16,
            "max_temp": dev.target_temperature_max or 30,
            "temp_step": dev.target_temperature_step or 1,
            "modes": {
                "COOL": ACMode.COOL.name in modes,
                "DRY": ACMode.DRY.name in modes,
                "FAN": ACMode.FAN.name in modes,
                "AUTO": auto_mode is not None,
                "HEAT": ACMode.HEAT.name in modes,
            },
            "fan_speeds": [{"key": f, "label": FAN_LABELS[f]} for f in fan_speeds],
            "directions": self._direction_options(v_sup, h_sup),
            "jet_supported": dev.supported_mode_jet != acmod.JetModeSupport.NONE,
            "airclean_supported": dev.is_mode_airclean_supported,
            "sleep_range": dev.sleep_time_range,
        }
        if st is None:
            return data

        op_mode = st.operation_mode
        if op_mode in AUTO_MODES:
            op_mode = "AUTO"
        data.update(
            {
                "is_on": st.is_on,
                "mode": op_mode,
                "target_temp": st.target_temp,
                "room_temp": st.current_temp,
                "humidity": st.humidity,
                "fan_speed": st.fan_speed,
                "direction": self._current_direction(st, v_sup, h_sup),
                "jet": bool(st.mode_jet),
                "jet_available": dev.is_mode_jet_available,
                "airclean": bool(st.mode_airclean),
                "sleep_time": st.reservation_sleep_time or 0,
                "sleep_available": dev.is_reservation_sleep_time_available,
            }
        )
        return data

    @staticmethod
    def _direction_options(v_sup: bool, h_sup: bool) -> list[dict]:
        opts = [{"key": "OFF", "label": "Fixa"}]
        if v_sup:
            opts.append({"key": "VERTICAL", "label": "Vertical"})
        if h_sup:
            opts.append({"key": "HORIZONTAL", "label": "Horizontal"})
        if v_sup and h_sup:
            opts.append({"key": "BOTH", "label": "Ambas"})
        return opts

    @staticmethod
    def _current_direction(st, v_sup: bool, h_sup: bool) -> str:
        v = v_sup and (st.is_vertical_swing_on or st.vertical_step_mode == "Swing")
        h = h_sup and (st.is_horizontal_swing_on or st.horizontal_step_mode == "Swing")
        if v and h:
            return "BOTH"
        if v:
            return "VERTICAL"
        if h:
            return "HORIZONTAL"
        return "OFF"

    # ---------- comandos ----------
    def command(self, name: str, value) -> dict:
        self._check_blocked()
        try:
            self.run(self._command(name, value))
        except Exception as ex:
            if msg := _lg_block_message(ex):
                self._register_block(msg)
                self._check_blocked()
            raise
        return self.state()

    async def _command(self, name: str, value):
        dev = self.device
        if not dev:
            raise RuntimeError("Aparelho não conectado")
        async with self._lock:
            if name == "power":
                await dev.power(bool(value))
            elif name == "temp":
                await dev.set_target_temp(float(value))
            elif name == "mode":
                if value == "AUTO":
                    value = next(m for m in AUTO_MODES if m in dev.op_modes)
                await dev.set_op_mode(value)
            elif name == "fan":
                await dev.set_fan_speed(value)
            elif name == "direction":
                await self._set_direction(value)
            elif name == "jet":
                await dev.set_mode_jet(bool(value))
            elif name == "airclean":
                await dev.set_mode_airclean(bool(value))
            elif name == "sleep":
                await dev.set_reservation_sleep_time(int(value))
            else:
                raise ValueError(f"Comando desconhecido: {name}")

    async def _set_direction(self, value: str):
        dev = self.device
        v_sup, h_sup = self._swing_support()
        want_v = value in ("VERTICAL", "BOTH")
        want_h = value in ("HORIZONTAL", "BOTH")
        if v_sup:
            if "Swing" in dev.vertical_step_modes:
                off = "Off" if "Off" in dev.vertical_step_modes else dev.vertical_step_modes[0]
                await dev.set_vertical_step_mode("Swing" if want_v else off)
            else:
                await dev.vertical_swing_mode(want_v)
        if h_sup:
            if "Swing" in dev.horizontal_step_modes:
                off = "Off" if "Off" in dev.horizontal_step_modes else dev.horizontal_step_modes[0]
                await dev.set_horizontal_step_mode("Swing" if want_h else off)
            else:
                await dev.horizontal_swing_mode(want_h)
