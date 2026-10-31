"""Ponte entre a interface e a API oficial LG ThinQ Connect (SDK thinqconnect)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from pathlib import Path

import aiohttp
from thinqconnect import AirConditionerDevice, ThinQApi, ThinQAPIException
from thinqconnect.const import DeviceType
from thinqconnect.devices.const import Property

_LOGGER = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".arcondicionado-lg"
CONFIG_FILE = CONFIG_DIR / "config.json"

PAT_URL = "https://connect-pat.lgthinq.com"

POLL_VISIBLE = 60  # segundos entre leituras com o painel aberto
POLL_HIDDEN = 10 * 60  # com o painel escondido na bandeja
MIN_REFRESH_GAP = 45  # leitura extra ao abrir o painel só se a última for mais antiga que isso
LIMIT_PAUSE = 60 * 60  # pausa ao receber "limite de chamadas excedido" (1306)

# modo da interface -> modo da API oficial
MODE_TO_API = {"COOL": "COOL", "DRY": "AIR_DRY", "FAN": "FAN", "AUTO": "AUTO", "HEAT": "HEAT"}
API_TO_MODE = {v: k for k, v in MODE_TO_API.items()}

FAN_LABELS = {
    "SLOW": "Muito baixo",
    "LOW": "Baixo",
    "LOW_MID": "Médio-baixo",
    "MID": "Médio",
    "MID_HIGH": "Médio-alto",
    "HIGH": "Alto",
    "AUTO": "Auto",
    "NATURE": "Natural",
}
TURBO = "POWER"  # vento "potente" faz o papel do botão Turbo

ERROR_MESSAGES = {
    "1103": "Token inválido. Gere um novo token pessoal no site da LG.",
    "1218": "Token inválido. Gere um novo token pessoal no site da LG.",
    "1302": "Token não encontrado. Gere um novo token pessoal no site da LG.",
    "1304": "Há termos de uso pendentes. Abra o app LG ThinQ no celular e aceite.",
    "1306": "Limite de consultas da LG atingido. O app espera um pouco e tenta de novo.",
    "1222": "O ar-condicionado está desconectado do Wi-Fi.",
}


class AccountBlockedError(Exception):
    """A LG pediu para o app esperar antes de consultar de novo."""


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def friendly_error(ex: Exception) -> str:
    if isinstance(ex, ThinQAPIException):
        return ERROR_MESSAGES.get(ex.code, f"{ex.message} (código {ex.code})")
    return str(ex) or ex.__class__.__name__


class LGBackend:
    """Executa o SDK num loop asyncio próprio, em uma thread separada."""

    def __init__(self) -> None:
        self.cfg = load_config()
        self.cfg.setdefault("country", "BR")
        if len(self.cfg.get("client_id", "")) != 36:
            # identificador fixo desta instalação (a API oficial pede um por cliente)
            self.cfg["client_id"] = str(uuid.uuid4())
        # dados do login antigo (API não oficial) não servem mais
        for key in ("refresh_token", "oauth_url", "blocked_until", "language"):
            self.cfg.pop(key, None)
        save_config(self.cfg)

        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()
        self._session: aiohttp.ClientSession | None = None
        self.api: ThinQApi | None = None
        self.device: AirConditionerDevice | None = None
        self.devices: list[dict] = []
        self.error: str | None = None
        self.online = False
        self.last_poll = 0.0
        self.paused_until = 0.0
        self.panel_visible = True
        self._poll_task = None
        self._lock: asyncio.Lock | None = None
        self._wake: asyncio.Event | None = None

    # ---------- utilidades ----------
    def run(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    @property
    def logged_in(self) -> bool:
        return bool(self.cfg.get("pat"))

    def _check_paused(self) -> None:
        remaining = self.paused_until - time.time()
        if remaining > 0:
            raise AccountBlockedError(
                "A LG pediu para o app consultar menos. Tentando de novo em "
                f"{int(remaining // 60) + 1} min."
            )

    def _handle_api_error(self, ex: Exception) -> None:
        if isinstance(ex, ThinQAPIException) and ex.code == "1306":
            _LOGGER.warning("Limite de chamadas da LG atingido; pausando %s s", LIMIT_PAUSE)
            self.paused_until = time.time() + LIMIT_PAUSE

    # ---------- autenticação ----------
    def login_with_pat(self, pat: str) -> None:
        pat = pat.strip()
        if not pat:
            raise ValueError("Cole o token pessoal (PAT) gerado no site da LG")
        self.run(self._test_pat(pat))
        self.cfg["pat"] = pat
        save_config(self.cfg)

    async def _test_pat(self, pat: str) -> None:
        async with aiohttp.ClientSession() as session:
            api = ThinQApi(session, pat, self.cfg["country"], self.cfg["client_id"])
            try:
                await api.async_get_device_list()
            except ThinQAPIException as ex:
                raise RuntimeError(friendly_error(ex)) from ex

    def logout(self) -> None:
        self.run(self._close())
        for key in ("pat", "device_id"):
            self.cfg.pop(key, None)
        save_config(self.cfg)

    async def _close(self):
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        if self._session:
            await self._session.close()
        self._session = None
        self.api = None
        self.device = None

    # ---------- conexão e aparelhos ----------
    def connect(self) -> None:
        self._check_paused()
        try:
            self.run(self._connect(), timeout=60)
        except Exception as ex:
            self._handle_api_error(ex)
            raise RuntimeError(friendly_error(ex)) from ex

    async def _connect(self):
        await self._close()
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._session = aiohttp.ClientSession()
        self.api = ThinQApi(self._session, self.cfg["pat"], self.cfg["country"],
                            self.cfg["client_id"])

        acs = [
            d for d in (await self.api.async_get_device_list() or [])
            if d.get("deviceInfo", {}).get("deviceType") == DeviceType.AIR_CONDITIONER
        ]
        self.devices = [
            {"id": d["deviceId"], "name": d["deviceInfo"].get("alias", "Ar-condicionado"),
             "model": d["deviceInfo"].get("modelName", "")}
            for d in acs
        ]
        if not acs:
            raise RuntimeError("Nenhum ar-condicionado encontrado na sua conta LG ThinQ")

        wanted = self.cfg.get("device_id")
        dev = next((d for d in acs if d["deviceId"] == wanted), acs[0])
        info = dev["deviceInfo"]
        profile = await self.api.async_get_device_profile(dev["deviceId"])
        self.device = AirConditionerDevice(
            thinq_api=self.api,
            device_id=dev["deviceId"],
            device_type=info.get("deviceType"),
            model_name=info.get("modelName", ""),
            alias=info.get("alias", "Ar-condicionado"),
            reportable=info.get("reportable", False),
            profile=profile,
        )
        self.cfg["device_id"] = dev["deviceId"]
        save_config(self.cfg)
        await self._poll()
        self._poll_task = asyncio.ensure_future(self._poll_loop())

    async def _poll(self):
        if self.paused_until > time.time():
            return
        async with self._lock:
            try:
                status = await self.api.async_get_device_status(self.device.device_id)
                self.device.set_status(status)
                self.error = None
                self.online = True
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.warning("Falha ao ler o aparelho: %s", ex)
                self._handle_api_error(ex)
                self.error = friendly_error(ex)
                self.online = False
            self.last_poll = time.time()

    async def _poll_loop(self):
        while True:
            interval = POLL_VISIBLE if self.panel_visible else POLL_HIDDEN
            self._wake.clear()
            try:
                # abrir o painel acorda o loop para voltar ao ritmo rápido
                await asyncio.wait_for(self._wake.wait(), interval)
            except asyncio.TimeoutError:
                await self._poll()

    def set_panel_visible(self, visible: bool) -> None:
        self.panel_visible = visible
        if visible and self._wake:
            self.loop.call_soon_threadsafe(self._wake.set)

    def refresh(self) -> None:
        if self.device and time.time() - self.last_poll > MIN_REFRESH_GAP:
            self.run(self._poll())

    # ---------- leitura do perfil e do estado ----------
    def _prop(self, prop: Property) -> dict | None:
        try:
            return self.device.profiles.get_property(prop)
        except AttributeError:
            return None

    def _writable_values(self, prop: Property) -> list:
        p = self._prop(prop)
        values = p.get("w") if p else None
        return values if isinstance(values, list) else []

    def _can_write(self, prop: Property) -> bool:
        try:
            return self.device.profiles.check_attribute_writable(prop)
        except AttributeError:
            return False

    def _get(self, prop: Property):
        try:
            return self.device.get_status(prop)
        except AttributeError:
            return None

    def _temp_range(self) -> tuple[float, float, float]:
        for prop in (Property.TARGET_TEMPERATURE_C, Property.COOL_TARGET_TEMPERATURE_C):
            p = self._prop(prop)
            rng = p.get("w") if p else None
            if isinstance(rng, dict) and "min" in rng:
                return rng["min"], rng["max"], rng.get("step", 1)
        lo = self._get(Property.MIN_TARGET_TEMPERATURE_C) or 16
        hi = self._get(Property.MAX_TARGET_TEMPERATURE_C) or 30
        return lo, hi, 1

    def _target_temp(self, api_mode: str | None):
        by_mode = {
            "COOL": Property.COOL_TARGET_TEMPERATURE_C,
            "HEAT": Property.HEAT_TARGET_TEMPERATURE_C,
            "AUTO": Property.AUTO_TARGET_TEMPERATURE_C,
        }
        for prop in (Property.TARGET_TEMPERATURE_C, by_mode.get(api_mode)):
            if prop and (value := self._get(prop)) is not None:
                return value
        return None

    def state(self) -> dict:
        dev = self.device
        if not dev:
            return {"ready": False}

        api_modes = self._writable_values(Property.CURRENT_JOB_MODE)
        fan_values = self._writable_values(Property.WIND_STRENGTH)
        v_sup = self._can_write(Property.WIND_ROTATE_UP_DOWN)
        h_sup = self._can_write(Property.WIND_ROTATE_LEFT_RIGHT)
        lo, hi, step = self._temp_range()
        api_mode = self._get(Property.CURRENT_JOB_MODE)
        wind = self._get(Property.WIND_STRENGTH)
        sleep_h = self._get(Property.SLEEP_TIMER_RELATIVE_HOUR_TO_STOP) or 0
        sleep_m = self._get(Property.SLEEP_TIMER_RELATIVE_MINUTE_TO_STOP) or 0
        humidity = self._get(Property.HUMIDITY)

        return {
            "ready": True,
            "name": dev.alias,
            "devices": self.devices,
            "device_id": dev.device_id,
            "error": self.error,
            "blocked": self.paused_until > time.time(),
            "online": self.online,
            "min_temp": lo,
            "max_temp": hi,
            "temp_step": step,
            "modes": {m: MODE_TO_API[m] in api_modes for m in MODE_TO_API},
            "fan_speeds": [{"key": f, "label": FAN_LABELS[f]} for f in fan_values if f in FAN_LABELS],
            "directions": self._direction_options(v_sup, h_sup),
            "jet_supported": TURBO in fan_values,
            "airclean_supported": "START" in self._writable_values(Property.AIR_CLEAN_OPERATION_MODE),
            "sleep_range": [0, 7 * 60],
            "is_on": self._get(Property.AIR_CON_OPERATION_MODE) == "POWER_ON",
            "mode": API_TO_MODE.get(api_mode, api_mode),
            "target_temp": self._target_temp(api_mode),
            "room_temp": self._get(Property.CURRENT_TEMPERATURE_C),
            "humidity": humidity if humidity else None,
            "fan_speed": wind,
            "direction": self._current_direction(v_sup, h_sup),
            "jet": wind == TURBO,
            "jet_available": True,
            "airclean": self._get(Property.AIR_CLEAN_OPERATION_MODE) == "START",
            "sleep_time": sleep_h * 60 + sleep_m,
            "sleep_available": self._can_write(Property.SLEEP_TIMER_RELATIVE_HOUR_TO_STOP),
        }

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

    def _current_direction(self, v_sup: bool, h_sup: bool) -> str:
        v = v_sup and bool(self._get(Property.WIND_ROTATE_UP_DOWN))
        h = h_sup and bool(self._get(Property.WIND_ROTATE_LEFT_RIGHT))
        if v and h:
            return "BOTH"
        if v:
            return "VERTICAL"
        if h:
            return "HORIZONTAL"
        return "OFF"

    # ---------- comandos ----------
    def command(self, name: str, value) -> dict:
        self._check_paused()
        try:
            self.run(self._command(name, value))
        except Exception as ex:
            self._handle_api_error(ex)
            raise RuntimeError(friendly_error(ex)) from ex
        return self.state()

    async def _command(self, name: str, value):
        dev = self.device
        if not dev:
            raise RuntimeError("Aparelho não conectado")
        async with self._lock:
            if name == "power":
                await dev.set_air_con_operation_mode("POWER_ON" if value else "POWER_OFF")
            elif name == "temp":
                await self._set_temp(float(value))
            elif name == "mode":
                await dev.set_current_job_mode(MODE_TO_API[value])
            elif name == "fan":
                await dev.set_wind_strength(value)
            elif name == "direction":
                if self._can_write(Property.WIND_ROTATE_UP_DOWN):
                    await dev.set_wind_rotate_up_down(value in ("VERTICAL", "BOTH"))
                if self._can_write(Property.WIND_ROTATE_LEFT_RIGHT):
                    await dev.set_wind_rotate_left_right(value in ("HORIZONTAL", "BOTH"))
            elif name == "jet":
                if value:
                    self.cfg["fan_before_turbo"] = self._get(Property.WIND_STRENGTH)
                    await dev.set_wind_strength(TURBO)
                else:
                    previous = self.cfg.get("fan_before_turbo")
                    fans = self._writable_values(Property.WIND_STRENGTH)
                    await dev.set_wind_strength(previous if previous in fans and previous != TURBO
                                                else "AUTO" if "AUTO" in fans else fans[0])
            elif name == "airclean":
                await dev.set_air_clean_operation_mode("START" if value else "STOP")
            elif name == "sleep":
                minutes = int(value)
                await dev.set_sleep_timer_relative_time_to_stop(minutes // 60, minutes % 60)
            else:
                raise ValueError(f"Comando desconhecido: {name}")
            # o comando foi aceito: relê o estado para a tela refletir o aparelho
            await asyncio.sleep(1.5)
            status = await self.api.async_get_device_status(dev.device_id)
            dev.set_status(status)
            self.last_poll = time.time()

    async def _set_temp(self, temp: float):
        dev = self.device
        temp = int(temp) if float(temp).is_integer() else temp
        if self._can_write(Property.TARGET_TEMPERATURE_C):
            await dev._set_target_temperature(temp, "C")  # pylint: disable=protected-access
            return
        api_mode = self._get(Property.CURRENT_JOB_MODE)
        setter = {
            "COOL": dev.set_cool_target_temperature_c,
            "HEAT": dev.set_heat_target_temperature_c,
            "AUTO": dev.set_auto_target_temperature_c,
        }.get(api_mode, dev.set_cool_target_temperature_c)
        await setter(temp)
