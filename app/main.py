"""Tapo smart plug dashboard.

Discovers TP-Link Tapo plugs on the LAN, polls them in the background and
serves a cached snapshot over HTTP. Devices are keyed by MAC rather than IP so
that a DHCP lease change does not break links, history or control.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.requests import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import ipaddress
from pydantic import BaseModel

from kasa import Device, Discover, Module
from kasa.exceptions import AuthenticationError

from app import schedules

log = logging.getLogger("tapo-dash")
# uvicorn only configures its own loggers; without a root handler our info
# lines (schedule runs, devices moving or being forgotten) are dropped.
logging.basicConfig(format="%(levelname)s:     %(name)s: %(message)s")
log.setLevel(logging.INFO)

USERNAME = os.environ.get("TAPO_EMAIL", "")
PASSWORD = os.environ.get("TAPO_PASSWORD", "")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "10"))
REDISCOVER_INTERVAL = float(os.environ.get("REDISCOVER_INTERVAL", "300"))
DISCOVERY_TIMEOUT = float(os.environ.get("DISCOVERY_TIMEOUT", "8"))
ALLOW_CONTROL = os.environ.get("ALLOW_CONTROL", "true").lower() == "true"
# Newer Tapo firmware requires "Third-Party Compatibility" to be enabled in the
# Tapo app, which issues a local username/password distinct from the cloud
# login. Fall back to the cloud credentials when those are not set.
LOCAL_USERNAME = os.environ.get("TAPO_LOCAL_USERNAME", "") or USERNAME
LOCAL_PASSWORD = os.environ.get("TAPO_LOCAL_PASSWORD", "") or PASSWORD
DISCOVERY_ROUNDS = int(os.environ.get("DISCOVERY_ROUNDS", "2"))
# Comma-separated broadcast targets; add a second one if the plugs sit on
# another VLAN that this host can route to.
BROADCAST_TARGETS = [
    t.strip()
    for t in os.environ.get("BROADCAST_TARGETS", "255.255.255.255").split(",")
    if t.strip()
]
# Optional comma-separated CIDRs to sweep by unicast when broadcast comes up
# short (e.g. "192.168.1.0/24"). Empty disables the sweep.
SWEEP_CIDRS = [
    c.strip() for c in os.environ.get("SWEEP_CIDRS", "").split(",") if c.strip()
]
SWEEP_CONCURRENCY = int(os.environ.get("SWEEP_CONCURRENCY", "150"))
HISTORY_DB = os.environ.get("HISTORY_DB", "/data/history.db")
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "7"))
# Only write a history row this often, regardless of poll rate.
HISTORY_INTERVAL = float(os.environ.get("HISTORY_INTERVAL", "60"))
# Shown in the page header and browser tab.
DASHBOARD_TITLE = os.environ.get("DASHBOARD_TITLE", "Tapo Plugs")
# Forget a device that has not answered discovery for this long (it was
# unplugged, re-homed or factory reset). Broadcast is lossy, so keep this well
# above REDISCOVER_INTERVAL.
STALE_AFTER = float(os.environ.get("STALE_AFTER", "3600"))

templates = Jinja2Templates(directory="app/templates")


def norm_mac(mac: str | None) -> str:
    return (mac or "").replace(":", "").replace("-", "").upper()


def _short_error(exc: BaseException) -> str:
    """A one-line description that keeps the useful part of the message."""
    msg = " ".join(str(exc).split())
    if isinstance(exc, AuthenticationError):
        return "credentials rejected by this plug"
    # An empty response is ambiguous: it happens both when the KLAP handshake
    # was refused and when the link is simply flaky, so do not blame auth here.
    if "not found in {}" in msg:
        return "no data returned by device"
    if isinstance(exc, TimeoutError) or "TimeoutError" in msg:
        return "timed out"
    return msg[:120] if msg else type(exc).__name__


class Registry:
    """Holds the live Device objects and the most recent snapshot of each."""

    def __init__(self) -> None:
        self.devices: dict[str, Device] = {}
        self.state: dict[str, dict[str, Any]] = {}
        self.last_seen: dict[str, float] = {}
        self.last_discovery: float = 0.0
        self.last_poll: float = 0.0
        self.last_error: str | None = None
        self.lock = asyncio.Lock()

    async def discover(self) -> None:
        """Find plugs by broadcast, falling back to a unicast sweep.

        A single UDP broadcast round is lossy - WiFi plugs routinely miss one -
        so we run several rounds and merge. If SWEEP_CIDRS is configured we also
        probe each address directly, which catches plugs that never answer
        broadcast at all.
        """
        if not LOCAL_USERNAME or not LOCAL_PASSWORD:
            self.last_error = (
                "TAPO_EMAIL / TAPO_PASSWORD are not set. "
                "Populate the env file and restart the container."
            )
            return

        found: dict[str, Device] = {}
        errors: list[str] = []

        for _ in range(max(1, DISCOVERY_ROUNDS)):
            for target in BROADCAST_TARGETS:
                try:
                    res = await Discover.discover(
                        target=target,
                        username=LOCAL_USERNAME,
                        password=LOCAL_PASSWORD,
                        timeout=DISCOVERY_TIMEOUT,
                        discovery_timeout=DISCOVERY_TIMEOUT,
                    )
                    found.update(res)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"broadcast {target}: {exc}")

        if SWEEP_CIDRS:
            found.update(await self._sweep(found.keys()))

        if not found:
            self.last_error = "; ".join(errors) or "no devices answered discovery"
            log.warning("discovery found nothing: %s", self.last_error)
            return

        self.last_discovery = time.time()
        seen: set[str] = set()

        for host, dev in found.items():
            mac = norm_mac(getattr(dev, "mac", None)) or norm_mac(host)
            seen.add(mac)
            self.last_seen[mac] = self.last_discovery
            existing = self.devices.get(mac)
            if existing is not None and existing.host == host:
                # Same device at the same address; keep the warm connection.
                continue
            if existing is not None:
                # Address changed under us - drop the stale connection.
                with contextlib.suppress(Exception):
                    await existing.disconnect()
                log.info("device %s moved %s -> %s", mac, existing.host, host)
            self.devices[mac] = dev

        # Devices that stopped answering discovery stay in the registry so the
        # UI can show them as offline, but we mark them so.
        for mac, entry in self.state.items():
            if mac not in seen:
                entry["online"] = False

        # ...until they have been silent for STALE_AFTER, then drop them.
        for mac in list(self.devices):
            if self.last_discovery - self.last_seen.get(mac, 0) > STALE_AFTER:
                log.info("forgetting %s: not seen for %ds", mac, STALE_AFTER)
                with contextlib.suppress(Exception):
                    await self.devices[mac].disconnect()
                del self.devices[mac]
                self.state.pop(mac, None)
                self.last_seen.pop(mac, None)

    async def _sweep(self, already: Any) -> dict[str, Device]:
        """Unicast-probe every address in SWEEP_CIDRS."""
        known = set(already)
        targets: list[str] = []
        for cidr in SWEEP_CIDRS:
            try:
                for ip in ipaddress.ip_network(cidr, strict=False).hosts():
                    if str(ip) not in known:
                        targets.append(str(ip))
            except ValueError as exc:
                log.warning("bad SWEEP_CIDRS entry %r: %s", cidr, exc)

        if not targets:
            return {}

        sem = asyncio.Semaphore(SWEEP_CONCURRENCY)
        out: dict[str, Device] = {}

        async def probe(ip: str) -> None:
            async with sem:
                try:
                    dev = await Discover.discover_single(
                        ip,
                        username=LOCAL_USERNAME,
                        password=LOCAL_PASSWORD,
                        timeout=2,
                        discovery_timeout=2,
                    )
                    out[ip] = dev
                except Exception:  # noqa: BLE001 - most addresses are empty
                    pass

        await asyncio.gather(*(probe(ip) for ip in targets))
        if out:
            log.info("unicast sweep found %d extra device(s)", len(out))
        return out

    async def poll_once(self) -> None:
        if not self.devices:
            return
        macs = list(self.devices)
        results = await asyncio.gather(
            *(self.devices[m].update() for m in macs), return_exceptions=True
        )
        now = time.time()
        self.last_poll = now
        errors: list[str] = []

        for mac, result in zip(macs, results):
            dev = self.devices[mac]
            if isinstance(result, Exception):
                # Keep whatever we knew from the last good poll, but make sure
                # every key the UI reads is present - a device that has never
                # polled successfully would otherwise render as "undefined".
                prev = self.state.get(mac, {})
                for key, default in (
                    ("alias", None),
                    ("model", None),
                    ("device_type", None),
                    ("is_on", None),
                    ("rssi", None),
                    ("power_w", None),
                    ("today_kwh", None),
                    ("month_kwh", None),
                    ("has_energy", False),
                ):
                    prev.setdefault(key, default)
                # Fall back to whatever discovery told us about the device.
                if prev.get("model") is None:
                    prev["model"] = getattr(dev, "model", None)
                prev.update(
                    {
                        "mac": mac,
                        "host": dev.host,
                        "online": False,
                        "error": f"{type(result).__name__}: {result}",
                        "updated": now,
                    }
                )
                if isinstance(result, AuthenticationError):
                    # Sticky: the poll path reports later failures as an empty
                    # response, so remember that this plug refused our login.
                    prev["auth_failed"] = True
                prev.setdefault("auth_failed", False)
                if prev["auth_failed"]:
                    prev["error"] += " - this plug is paired to a different Tapo account"
                self.state[mac] = prev
                errors.append(f"{dev.host}: {_short_error(result)}")
                continue
            self.state[mac] = self._snapshot(mac, dev, now)

        self.last_error = "; ".join(errors) if errors else None

    def _snapshot(self, mac: str, dev: Device, now: float) -> dict[str, Any]:
        info: dict[str, Any] = {
            "mac": mac,
            "host": dev.host,
            "alias": dev.alias,
            "model": dev.model,
            "device_type": str(getattr(dev, "device_type", "")).replace("DeviceType.", ""),
            "is_on": dev.is_on,
            "online": True,
            "error": None,
            "updated": now,
            "rssi": None,
            "power_w": None,
            "today_kwh": None,
            "month_kwh": None,
            "has_energy": False,
            "auth_failed": False,
        }
        with contextlib.suppress(Exception):
            info["rssi"] = dev.rssi

        # Power strips (P300 etc.) switch per socket; the parent has no relay of
        # its own, so report it as "on" when any socket is.
        if dev.children:
            info["children"] = [
                {"id": c.device_id, "alias": c.alias, "is_on": c.is_on}
                for c in sorted(dev.children, key=lambda c: c.device_id)
            ]
            info["is_on"] = any(c["is_on"] for c in info["children"])

        energy = None
        with contextlib.suppress(Exception):
            energy = dev.modules.get(Module.Energy)
        if energy is None:
            with contextlib.suppress(Exception):
                energy = dev.modules.get("Energy")

        if energy is not None:
            info["has_energy"] = True
            for key, attr in (
                ("power_w", "current_consumption"),
                ("today_kwh", "consumption_today"),
                ("month_kwh", "consumption_this_month"),
            ):
                with contextlib.suppress(Exception):
                    value = getattr(energy, attr)
                    if value is not None:
                        info[key] = round(float(value), 3)
        return info

    def by_mac(self, mac: str) -> Device:
        dev = self.devices.get(norm_mac(mac))
        if dev is None:
            raise LookupError(f"unknown device {mac}")
        return dev

    async def close(self) -> None:
        for dev in self.devices.values():
            with contextlib.suppress(Exception):
                await dev.disconnect()
        self.devices.clear()


registry = Registry()


# --------------------------------------------------------------------------
# history
# --------------------------------------------------------------------------
def init_db() -> sqlite3.Connection | None:
    try:
        Path(HISTORY_DB).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(HISTORY_DB, check_same_thread=False)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS readings (
                   ts REAL NOT NULL,
                   mac TEXT NOT NULL,
                   power_w REAL,
                   is_on INTEGER
               )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS readings_mac_ts ON readings (mac, ts)")
        conn.commit()
        return conn
    except Exception as exc:  # noqa: BLE001
        log.warning("history disabled: %s", exc)
        return None


DB = init_db()
if DB is not None:
    schedules.init(DB)
_last_history_write = 0.0


def record_history() -> None:
    global _last_history_write
    if DB is None:
        return
    now = time.time()
    if now - _last_history_write < HISTORY_INTERVAL:
        return
    _last_history_write = now
    rows = [
        (now, mac, s.get("power_w"), 1 if s.get("is_on") else 0)
        for mac, s in registry.state.items()
        if s.get("online")
    ]
    if not rows:
        return
    try:
        DB.executemany(
            "INSERT INTO readings (ts, mac, power_w, is_on) VALUES (?, ?, ?, ?)", rows
        )
        DB.execute("DELETE FROM readings WHERE ts < ?", (now - HISTORY_DAYS * 86400,))
        DB.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("history write failed: %s", exc)


# --------------------------------------------------------------------------
# background loop
# --------------------------------------------------------------------------
async def poller() -> None:
    await registry.discover()
    while True:
        try:
            if time.time() - registry.last_discovery > REDISCOVER_INTERVAL:
                await registry.discover()
            await registry.poll_once()
            await run_schedules()
            record_history()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            log.exception("poll cycle failed: %s", exc)
            registry.last_error = str(exc)
        await asyncio.sleep(POLL_INTERVAL)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poller())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await registry.close()
        if DB is not None:
            with contextlib.suppress(Exception):
                DB.close()


app = FastAPI(title=DASHBOARD_TITLE, lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "allow_control": ALLOW_CONTROL, "title": DASHBOARD_TITLE},
    )


@app.get("/healthz")
async def healthz():
    return {
        "ok": registry.last_error is None and bool(registry.devices),
        "devices": len(registry.devices),
        "last_poll": registry.last_poll,
        "error": registry.last_error,
    }


@app.get("/api/devices")
async def list_devices():
    devices = sorted(
        registry.state.values(), key=lambda d: (d.get("alias") or d.get("host") or "")
    )
    total = sum(d["power_w"] for d in devices if d.get("power_w") is not None)
    return {
        "devices": devices,
        "total_power_w": round(total, 2),
        "last_poll": registry.last_poll,
        "last_discovery": registry.last_discovery,
        "allow_control": ALLOW_CONTROL,
        "error": registry.last_error,
    }


@app.get("/api/devices/{mac}/history")
async def history(mac: str, hours: int = 6):
    if DB is None:
        return {"points": []}
    since = time.time() - max(1, min(hours, HISTORY_DAYS * 24)) * 3600
    cur = DB.execute(
        "SELECT ts, power_w, is_on FROM readings WHERE mac = ? AND ts >= ? ORDER BY ts",
        (norm_mac(mac), since),
    )
    return {
        "points": [
            {"ts": ts, "power_w": p, "is_on": bool(on)} for ts, p, on in cur.fetchall()
        ]
    }


async def switch(mac: str, action: str, child_id: str | None = None) -> dict[str, Any]:
    """Switch a plug, a whole strip, or one strip socket; return the new snapshot.

    Raises LookupError for an unknown device or socket; anything else comes
    from talking to the device.
    """
    dev = registry.by_mac(mac)
    if child_id is not None:
        targets = [c for c in dev.children if c.device_id == child_id]
        if not targets:
            raise LookupError(f"unknown socket {child_id}")
    else:
        # Switching a whole strip means switching every socket on it.
        targets = list(dev.children) or [dev]
    async with registry.lock:
        if action == "toggle":
            action = "off" if any(t.is_on for t in targets) else "on"
        for target in targets:
            await (target.turn_on() if action == "on" else target.turn_off())
        await dev.update()
        snap = registry._snapshot(norm_mac(mac), dev, time.time())
        registry.state[norm_mac(mac)] = snap
    return snap


async def _switch(mac: str, action: str, child_id: str | None = None) -> dict[str, Any]:
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="control is disabled")
    try:
        return await switch(mac, action, child_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def run_schedules() -> None:
    """Fire every rule that is due. Called once per poll cycle."""
    if DB is None or not ALLOW_CONTROL:
        return
    now = datetime.now().astimezone()
    for rule in schedules.list_all(DB):
        if not schedules.is_due(rule, now):
            continue
        target = rule["mac"] + (f"/{rule['child_id']}" if rule["child_id"] else "")
        try:
            await switch(rule["mac"], rule["action"], rule["child_id"])
        except Exception as exc:  # noqa: BLE001
            # Leave last_fired alone so the next cycle retries within GRACE.
            log.warning("schedule %s (%s %s) failed: %s", rule["id"], rule["action"], target, exc)
            schedules.record(DB, rule["id"], None, f"failed: {_short_error(exc)}")
            continue
        log.info("schedule %s: turned %s %s", rule["id"], rule["action"], target)
        schedules.record(DB, rule["id"], now.date().isoformat(), f"ok {now:%H:%M:%S}")


@app.post("/api/devices/{mac}/on")
async def turn_on(mac: str):
    return await _switch(mac, "on")


@app.post("/api/devices/{mac}/off")
async def turn_off(mac: str):
    return await _switch(mac, "off")


@app.post("/api/devices/{mac}/toggle")
async def toggle(mac: str):
    return await _switch(mac, "toggle")


@app.post("/api/devices/{mac}/children/{child_id}/{action}")
async def switch_child(mac: str, child_id: str, action: str):
    if action not in ("on", "off", "toggle"):
        raise HTTPException(status_code=404, detail=f"unknown action {action}")
    return await _switch(mac, action, child_id)


class ScheduleIn(BaseModel):
    mac: str
    child_id: str | None = None
    action: str
    at: str  # HH:MM, container-local time
    days: str = schedules.ALL_DAYS  # Monday=0 .. Sunday=6


class ScheduleUpdate(BaseModel):
    enabled: bool


def _schedules_db() -> sqlite3.Connection:
    if not ALLOW_CONTROL:
        raise HTTPException(status_code=403, detail="control is disabled")
    if DB is None:
        raise HTTPException(status_code=503, detail="schedules need the history database")
    return DB


@app.get("/api/schedules")
async def list_schedules():
    now = datetime.now().astimezone()
    return {
        "schedules": schedules.list_all(DB) if DB is not None else [],
        "timezone": now.tzname(),
        "now": now.strftime("%H:%M"),
        "enabled": DB is not None and ALLOW_CONTROL,
    }


@app.post("/api/schedules", status_code=201)
async def add_schedule(body: ScheduleIn):
    db = _schedules_db()
    mac = norm_mac(body.mac)
    state = registry.state.get(mac)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown device {body.mac}")
    if body.child_id and body.child_id not in {c["id"] for c in state.get("children", [])}:
        raise HTTPException(status_code=404, detail=f"unknown socket {body.child_id}")
    try:
        return schedules.add(
            db, mac, body.child_id, body.action, body.at, body.days,
            datetime.now().astimezone(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.patch("/api/schedules/{rule_id}")
async def update_schedule(rule_id: int, body: ScheduleUpdate):
    if not schedules.set_enabled(_schedules_db(), rule_id, body.enabled):
        raise HTTPException(status_code=404, detail=f"unknown schedule {rule_id}")
    return schedules.get(DB, rule_id)


@app.delete("/api/schedules/{rule_id}", status_code=204)
async def delete_schedule(rule_id: int):
    if not schedules.delete(_schedules_db(), rule_id):
        raise HTTPException(status_code=404, detail=f"unknown schedule {rule_id}")


@app.post("/api/rediscover")
async def rediscover():
    await registry.discover()
    await registry.poll_once()
    return {"devices": len(registry.devices), "error": registry.last_error}
