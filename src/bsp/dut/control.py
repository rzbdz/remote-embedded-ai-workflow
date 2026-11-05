"""DUT control backends (strategy): how a board is reset / put into FDL.

    gpio-relay      bsp gpio_relay on the same host (default; replaces Home Assistant)
    manual          a human presses the buttons (bring-up, no wiring yet)
    home-assistant  extension point for the former HA-based board-ctrl (not wired up)

FDL strategies (control.fdl.strategy):
    strapped        the board always boots to BROM download mode: reset == enter FDL
    button          hold the FDL line, pulse reset, release (needs control.fdl.pin)
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Callable

from ..core.errors import InvalidRequest, BspError
from ..core.registry import Registry
from .catalog import DutSpec

Log = Callable[[str], None]


class ControlBackend(ABC):
    def __init__(self, dut: DutSpec, services: Callable[[str], Any]):
        self.dut = dut
        self.cfg = dut.control
        self._services = services

    def setup(self) -> None:
        """Called at host start: prepare lines, register aliases."""

    @abstractmethod
    def reset(self, log: Log) -> None: ...

    def enter_fdl(self, log: Log) -> None:
        strategy = (self.cfg.get("fdl") or {}).get("strategy", "strapped")
        if strategy == "strapped":
            log("FDL is strapped on this board: a reset lands in BROM download mode")
            self.reset(log)
            return
        if strategy == "button":
            self._fdl_button(log)
            return
        raise InvalidRequest(f"{self.dut.id}: unknown fdl strategy {strategy!r}", hint="use strapped or button")

    def _fdl_button(self, log: Log) -> None:
        raise InvalidRequest(f"{self.dut.id}: backend {type(self).__name__} cannot drive an FDL button")

    def describe(self) -> dict[str, Any]:
        return {"backend": self.cfg.get("backend")}


CONTROL_BACKENDS: Registry[type[ControlBackend]] = Registry("dut control backend")


@CONTROL_BACKENDS.register("gpio-relay")
class GpioRelayControl(ControlBackend):
    """Uses the host's gpio_relay service. Lines are registered as aliases
    `<dut>/reset` and `<dut>/fdl` (pass-through rule: callers may use either)."""

    def _gpio(self):
        return self._services("gpio_relay")

    def _alias(self, role: str) -> str:
        return f"{self.dut.id}/{role}"

    def setup(self) -> None:
        gpio = self._gpio()
        for role in ("reset", "fdl"):
            line = self.cfg.get(role) or {}
            if not line.get("pin"):
                continue
            changes = {k: line[k] for k in ("mode", "active_low", "drive") if k in line}
            changes.setdefault("mode", "relay")
            gpio.configure(line["pin"], changes)
            gpio.alias_add(self._alias(role), line["pin"], tags=f"dut,{self.dut.id},{role}",
                           owner="dut", note=f"{self.dut.id} {role} line", replace=True)
        if self.cfg.get("reset", {}).get("pin"):
            # idle = released; for push-pull relay mode that drives the inactive level
            gpio.switch(self._alias("reset"), "off")

    def reset(self, log: Log) -> None:
        line = self.cfg.get("reset") or {}
        if not line.get("pin"):
            raise InvalidRequest(f"{self.dut.id}: control.reset.pin is not configured")
        ms = int(line.get("pulse_ms", 1000))
        log(f"reset: pulse {self._alias('reset')} ({line['pin']}) active for {ms} ms")
        self._gpio().pulse(self._alias("reset"), ms, None, True, False)

    def _fdl_button(self, log: Log) -> None:
        if not (self.cfg.get("fdl") or {}).get("pin"):
            raise InvalidRequest(f"{self.dut.id}: fdl strategy `button` needs control.fdl.pin")
        gpio = self._gpio()
        log(f"FDL: hold {self._alias('fdl')}, reset, release")
        gpio.switch(self._alias("fdl"), "on")
        try:
            self.reset(log)
            time.sleep(float(self.cfg["fdl"].get("hold_after_reset_s", 1.0)))  # strap sampling time
        finally:
            gpio.switch(self._alias("fdl"), "off")

    def describe(self) -> dict[str, Any]:
        gpio = self._gpio()
        out = {"backend": "gpio-relay"}
        for role in ("reset", "fdl"):
            if (self.cfg.get(role) or {}).get("pin"):
                try:
                    snap = gpio.get(self._alias(role))
                    out[role] = {"alias": self._alias(role), "pin": snap["target"], "state": snap.get("state")}
                except BspError as e:
                    out[role] = {"error": e.message}
        out["fdl_strategy"] = (self.cfg.get("fdl") or {}).get("strategy", "strapped")
        return out


@CONTROL_BACKENDS.register("manual")
class ManualControl(ControlBackend):
    def reset(self, log: Log) -> None:
        raise InvalidRequest(
            f"{self.dut.id} has no automated reset (control backend: manual)",
            hint="press RESET on the board (and FDL if needed), then continue with `bsp dut boot ... ` steps",
        )


@CONTROL_BACKENDS.register("home-assistant")
class HomeAssistantControl(ControlBackend):
    """Extension point: the old board-ctrl.sh called HA services
    (switch.turn_on <reset entity>, button.press <fastboot entity>). Re-implement
    here when the HA instance is back; config: control.ha.{url, token_env, reset, fastboot}."""

    def reset(self, log: Log) -> None:
        raise InvalidRequest(
            f"{self.dut.id}: home-assistant control backend is not wired up on this host",
            hint="switch control.backend to gpio-relay, or implement HomeAssistantControl.reset",
        )


def make_control(dut: DutSpec, services: Callable[[str], Any]) -> ControlBackend:
    return CONTROL_BACKENDS.get(dut.control.get("backend", "gpio-relay"))(dut, services)
