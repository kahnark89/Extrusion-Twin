"""
io_bus.py -- every way a signal gets into the twin, plus the simulator.

PXRBus        the 18 Fuji PXR-9 controllers over RS-485 MODBUS RTU (minimalmodbus).
ModbusDevice  any other Modbus register: VFD amps / Hz, the adapter melt-T readout, the two
              TCU controllers -- described in the config, not in code.
AnalogSerial  a microcontroller on USB sending "key=value,key=value" lines: thermistors
              (tower, air, feed jacket), 4-20 mA loops (melt pressure, VFD analog outs),
              contact states.
OpsFile       a JSON file the PWA (or a person) writes: adapter temperature read off the
              display, needle-gauge pressure, recipe id, notes. Goes stale after a while.
LiveBus       the poller that assembles one Inputs per sweep from all of the above, with a
              per-channel source flag ('bus' | 'analog' | 'ops' | 'manual' | 'stale').
SimPlant      a hidden 'true' plant with its own constants, PI loops, and injected drifts.

PXR MODBUS facts (Fuji manual INP-TN512642d-E): 9600 bps fixed, 8N1/8E1/8O1 (CoM), function 04
engineering-unit block 31001 PV, 31002 SV, 31003 DV, 31004 MV1 (x0.01 %), 31005 MV2, 31007
alarm bits, 31008 input/unit abnormal bits, 31010 heater current (x0.1 A, CT option). Relative
address = lower four digits - 1 (31001 -> 1000). 41020 P-dP decimal places (relative 1019),
41017 P-F unit 0=degC 1=degF (relative 1016). >= 10 ms silence between frames.
"""
from __future__ import annotations

import json
import math
import os
import random
import time
from typing import Dict, List, Optional

from .model import (ADAPTER, BARREL, DIE, N, Inputs, Plant, Recipe, State, clamp, mech_power_meas,
                    step, viscous_weights)

PARITY = {"O": "O", "E": "E", "N": "N"}


def degc(value: float, unit: str) -> float:
    return (value - 32.0) / 1.8 if unit.upper().startswith("F") else value


def bar(value: float, unit: str) -> float:
    return value * 0.0689476 if unit.lower().startswith("psi") else value


# ------------------------------------------------------------------ transports
def _crc16(frame: bytes) -> bytes:
    crc = 0xFFFF
    for b in frame:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes((crc & 0xFF, crc >> 8))


class TcpInstrument:
    """A Modbus master over a TCP socket, so the bus can be reached over the network instead of
    a serial port. Two framings, because gateways come both ways:

      mode "tcp"          proper Modbus TCP: 7-byte MBAP header, no CRC
      mode "rtu-over-tcp" a transparent serial gateway: the RTU frame, CRC and all, over the socket

    Pure standard library. Nothing to compile, which is the whole point on a phone.
    """

    def __init__(self, host: str, port: int, station: int, mode: str = "rtu-over-tcp", timeout: float = 1.0):
        self.host, self.port, self.address, self.mode, self.timeout = host, port, station, mode, timeout
        self.sock = None
        self.tid = 0

    def _connect(self):
        import socket
        if self.sock is None:
            self.sock = socket.create_connection((self.host, self.port), self.timeout)
            self.sock.settimeout(self.timeout)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return self.sock

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None

    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise IOError("gateway closed the connection")
            buf += chunk
        return buf

    def _transact(self, fc: int, addr: int, count: int) -> list:
        s = self._connect()
        pdu = bytes((fc, addr >> 8, addr & 0xFF, count >> 8, count & 0xFF))
        try:
            if self.mode == "tcp":
                self.tid = (self.tid + 1) & 0xFFFF
                head = bytes((self.tid >> 8, self.tid & 0xFF, 0, 0, 0, len(pdu) + 1, self.address))
                s.sendall(head + pdu)
                mbap = self._recv(7)
                body = self._recv(max(1, (mbap[4] << 8 | mbap[5]) - 1))
            else:
                frame = bytes((self.address,)) + pdu
                s.sendall(frame + _crc16(frame))
                head = self._recv(3)                      # station, function, byte count
                if head[1] & 0x80:
                    self._recv(2)
                    raise IOError(f"modbus exception {head[2]}")
                body = head[1:3] + self._recv(head[2] + 2)
                body = body[:-2]                          # drop the CRC; the gateway already framed it
            if body[0] & 0x80:
                raise IOError(f"modbus exception {body[1]}")
            nbytes = body[1]
            data = body[2:2 + nbytes]
            return [data[i] << 8 | data[i + 1] for i in range(0, nbytes, 2)]
        except Exception:
            self.close()                                  # next call reconnects rather than hanging
            raise

    # -- the two calls the rest of the twin makes, matching minimalmodbus's names
    def read_registers(self, addr: int, count: int, functioncode: int = 3) -> list:
        return self._transact(functioncode, addr, count)

    def read_register(self, addr: int, functioncode: int = 3, signed: bool = False) -> int:
        v = self._transact(functioncode, addr, 1)[0]
        return v - 65536 if signed and v > 32767 else v

    def read_float(self, addr: int, functioncode: int = 3) -> float:
        import struct
        r = self._transact(functioncode, addr, 2)
        return struct.unpack(">f", bytes((r[0] >> 8, r[0] & 0xFF, r[1] >> 8, r[1] & 0xFF)))[0]


def parse_port(port: str):
    """'COM3' or '/dev/ttyUSB0' -> serial. 'tcp://192.168.4.1:502' -> Modbus TCP.
    'rtu://192.168.4.1:8899' -> RTU frames over a transparent gateway."""
    p = str(port)
    for scheme, mode in (("tcp://", "tcp"), ("rtu://", "rtu-over-tcp"), ("rtutcp://", "rtu-over-tcp")):
        if p.startswith(scheme):
            rest = p[len(scheme):]
            host, _, prt = rest.partition(":")
            return mode, host, int(prt or (502 if mode == "tcp" else 8899))
    return "serial", p, 0


_instruments: Dict[tuple, object] = {}


def instrument(port: str, station: int, parity: str = "O", baud: int = 9600, timeout: float = 0.3):
    """One instrument per (port, station). Serial ports are shared between stations; a network
    gateway gets its own socket per station, which every gateway on the market handles."""
    key = (port, station)
    if key not in _instruments:
        mode, host, prt = parse_port(port)
        if mode != "serial":
            _instruments[key] = TcpInstrument(host, prt, station, mode, max(timeout, 1.0))
        else:
            import minimalmodbus
            import serial
            inst = minimalmodbus.Instrument(port, station)
            inst.serial.baudrate = baud
            inst.serial.bytesize = 8
            inst.serial.parity = {"O": serial.PARITY_ODD, "E": serial.PARITY_EVEN, "N": serial.PARITY_NONE}[parity]
            inst.serial.stopbits = 1
            inst.serial.timeout = timeout
            inst.mode = minimalmodbus.MODE_RTU
            inst.clear_buffers_before_each_transaction = True
            _instruments[key] = inst
    return _instruments[key]


# ------------------------------------------------------------------ PXR-9 zones
class PXRBus:
    def __init__(self, port: str, stations: List[int], parity: str = "O"):
        self.insts = [instrument(port, s, parity) for s in stations]
        self.dp, self.degf = [], []
        for inst in self.insts:
            self.degf.append(bool(inst.read_register(1016, functioncode=3)))   # 41017 P-F
            time.sleep(0.02)
            self.dp.append(inst.read_register(1019, functioncode=3))           # 41020 P-dP
            time.sleep(0.02)
        self.any_ct = False

    @staticmethod
    def _read(inst, dp: int, degf: bool) -> dict:
        r = inst.read_registers(1000, 10, functioncode=4)                     # 31001..31010
        s16 = lambda v: v - 65536 if v > 32767 else v
        sc = 10.0 ** dp
        pv, sv, dv = s16(r[0]) / sc, s16(r[1]) / sc, s16(r[2]) / sc
        if degf:
            pv, sv, dv = (pv - 32.0) / 1.8, (sv - 32.0) / 1.8, dv / 1.8
        return dict(PV=pv, SV=sv, DV=dv, MV=s16(r[3]) / 10000.0, MV2=s16(r[4]) / 10000.0,
                    alarm=r[6], fault=r[7], CT=r[9] / 10.0)

    def sweep(self):
        PV, SV, MV, CT, faults = [], [], [], [], []
        for inst, dp, degf in zip(self.insts, self.dp, self.degf):
            try:
                d = self._read(inst, dp, degf)
            except Exception as exc:                                          # a dead station must not stop the sweep
                d = dict(PV=float("nan"), SV=float("nan"), MV=0.0, MV2=0.0, alarm=0, fault=0, CT=0.0)
                faults.append(f"[bus] station {inst.address}: {exc}")
            time.sleep(0.02)
            PV.append(d["PV"]); SV.append(d["SV"]); MV.append(clamp(d["MV"], 0.0, 1.0)); CT.append(d["CT"])
            if d["fault"] & 0x0F:
                faults.append(f"[fault] station {inst.address} input abnormal bits {d['fault'] & 0x0F:04b}")
            if d["CT"] > 0:
                self.any_ct = True
        return PV, SV, MV, (CT if self.any_ct else None), faults


# ------------------------------------------------------------------ other Modbus registers
class ModbusDevice:
    """one register on any Modbus RTU device, described by a config entry:
    {"name","port","station","parity","baud","fc","register","type":"u16|s16|f32","scale","offset","unit","maps_to"}"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.inst = instrument(cfg["port"], cfg["station"], cfg.get("parity", "N"), cfg.get("baud", 9600))

    def read(self) -> float:
        c, fc = self.cfg, self.cfg.get("fc", 3)
        t = c.get("type", "u16")
        if t == "f32":
            v = self.inst.read_float(c["register"], functioncode=fc)
        else:
            v = self.inst.read_register(c["register"], functioncode=fc, signed=(t == "s16"))
        v = v * c.get("scale", 1.0) + c.get("offset", 0.0)
        unit = c.get("unit", "")
        if c["maps_to"] in ("T_adapter", "tcu_b_sv", "tcu_s_sv", "T_tower", "T_air", "T_feed_jacket"):
            v = degc(v, unit)
        elif c["maps_to"] == "P_adapter":
            v = bar(v, unit)
        return v


# ------------------------------------------------------------------ microcontroller line
class AnalogSerial:
    """reads the newest complete 'k=v,k=v,...' line from a USB microcontroller. Keys are mapped
    to Inputs fields by cfg['map']; keys listed in cfg['contacts'] are 0/1 contact states."""

    def __init__(self, cfg: dict):
        import serial
        self.cfg = cfg
        self.ser = serial.Serial(cfg["port"], cfg.get("baud", 115200), timeout=0.05)
        self.units = cfg.get("units", {})
        self.last: Dict[str, float] = {}
        self.t_last = 0.0

    def read(self) -> Dict[str, float]:
        buf = self.ser.read(self.ser.in_waiting or 1).decode(errors="ignore")
        lines = [ln for ln in buf.replace("\r", "").split("\n") if "=" in ln]
        if lines:
            vals = {}
            for kv in lines[-1].split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    try:
                        vals[k.strip()] = float(v)
                    except ValueError:
                        pass
            if vals:
                self.last, self.t_last = vals, time.time()
        return self.last


# ------------------------------------------------------------------ ops file (PWA bridge)
class OpsFile:
    """{"updated": "2026-09-04T14:02:11", "values": {"T_adapter": 348, "T_adapter_unit": "F",
        "P_adapter": 3100, "P_adapter_unit": "psi", "recipe": "...", "note": "..."}}"""

    def __init__(self, path: str, stale_s: float = 1800.0):
        self.path, self.stale_s = path, stale_s

    def read(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path) as f:
                d = json.load(f)
            age = time.time() - os.path.getmtime(self.path)
            vals = dict(d.get("values", {}))
            vals["_stale"] = age > self.stale_s
            return vals
        except Exception:
            return {}


# ------------------------------------------------------------------ the live poller
class LiveBus:
    """assembles one Inputs per sweep. Priority per channel: bus/device > analog > ops > manual."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.pxr = PXRBus(cfg["port"], cfg["stations"], cfg.get("parity", "O"))
        self.devices = [ModbusDevice(d) for d in cfg.get("devices", [])]
        self.analog = AnalogSerial(cfg["analog"]) if cfg.get("analog") else None
        self.ops = OpsFile(cfg["ops_file"], cfg.get("ops_stale_s", 1800)) if cfg.get("ops_file") else None
        self.manual = cfg.get("manual", {})
        self.rpm_per_hz = cfg.get("motor_rpm_per_hz", 29.5) / cfg.get("gear_ratio", 20.0)

    def poll(self):
        PV, SV, MV, CT, faults = self.pxr.sweep()
        vals, live = {}, {}
        for d in self.devices:                                                 # named Modbus registers
            try:
                vals[d.cfg["maps_to"]] = d.read(); live[d.cfg["maps_to"]] = "bus"
            except Exception as exc:
                faults.append(f"[bus] {d.cfg['name']}: {exc}")
            time.sleep(0.02)
        if self.analog:                                                        # microcontroller line
            a = self.analog.read()
            stale = time.time() - self.analog.t_last > 30.0
            for key, target in self.analog.cfg.get("map", {}).items():
                if key in a and target not in vals:
                    v = a[key]
                    unit = self.analog.units.get(key, "")
                    v = (degc(v, unit) if target.startswith("T_") or target.startswith("tcu")
                         else bar(v, unit) if target == "P_adapter" else v)
                    vals[target] = v; live[target] = "stale" if stale else "analog"
            contacts = {k: int(a.get(k, 0)) for k in self.analog.cfg.get("contacts", []) if k in a}
        else:
            contacts = {}
        if self.ops:                                                           # PWA / manual entries
            o = self.ops.read()
            for key in ("T_adapter", "P_adapter", "tcu_b_sv", "tcu_s_sv", "T_tower", "T_air", "T_feed_jacket",
                        "T_oil_b_supply", "T_oil_b_return", "T_oil_s_supply", "T_oil_s_return", "T_tower_return"):
                if key in o and key not in vals:
                    unit = o.get(key + "_unit", "")
                    vals[key] = bar(o[key], unit) if key == "P_adapter" else degc(o[key], unit)
                    live[key] = "stale" if o.get("_stale") else "ops"
        for key, v in self.manual.items():                                     # last resort
            if key not in vals and v is not None:
                vals[key] = v; live[key] = "manual"
        for i in range(N):
            live[f"PV{i}"] = "bus" if not math.isnan(PV[i]) else "stale"
        c_rpm = vals.get("crammer_rpm")
        if c_rpm is None and "crammer_hz" in vals:
            c_rpm = vals["crammer_hz"] * cfg_ratio(self.cfg)
            live["crammer_rpm"] = live.get("crammer_hz", "manual")
        rpm = vals.get("rpm", vals.get("hz", 0.0) * self.rpm_per_hz if "hz" in vals else 0.0)
        if "hz" in vals and "rpm" not in vals:
            live["rpm"] = live.get("hz", "manual")
        return Inputs(PV, SV, MV, rpm=rpm, amps=vals.get("amps", 0.0), T_adapter=vals.get("T_adapter"),
                      CT=CT, tcu_b_sv=vals.get("tcu_b_sv", 140.0), tcu_s_sv=vals.get("tcu_s_sv", 120.0),
                      T_tower=vals.get("T_tower", 30.0), T_air=vals.get("T_air", 35.0),
                      T_feed_jacket=vals.get("T_feed_jacket", 40.0), P_adapter=vals.get("P_adapter"),
                      crammer_rpm=c_rpm, crammer_amps=vals.get("crammer_amps"),
                      rpm_motor=vals.get("rpm_motor", vals.get("hz", 0.0) * self.cfg.get("motor_rpm_per_hz", 29.5)
                                         if "hz" in vals else None),
                      T_oil_b_supply=vals.get("T_oil_b_supply"), T_oil_b_return=vals.get("T_oil_b_return"),
                      T_oil_s_supply=vals.get("T_oil_s_supply"), T_oil_s_return=vals.get("T_oil_s_return"),
                      T_tower_return=vals.get("T_tower_return"),
                      flow_oil_b=vals.get("flow_oil_b"), flow_oil_s=vals.get("flow_oil_s"),
                      contacts=contacts, live=live), faults


# ------------------------------------------------------------------ config
def cfg_ratio(cfg: dict) -> float:
    """crammer motor rpm per Hz, divided by its gearbox ratio"""
    return cfg.get("crammer_rpm_per_hz", 29.5) / cfg.get("crammer_gear_ratio", 15.0)


def load_config(path: Optional[str]) -> dict:
    cfg = {}
    if path:
        with open(path) as f:
            cfg = json.load(f)
    cfg.setdefault("stations", list(range(1, N + 1)))
    cfg.setdefault("parity", "O")
    cfg.setdefault("manual", {})
    return cfg


def build_plant_recipe(cfg: dict):
    return Plant.from_dict(cfg.get("plant", {})), Recipe.from_dict(cfg.get("recipe", {}))


# ------------------------------------------------------------------ simulator
class SimPlant:
    """a 'true' plant with the same equations, different constants, its own PXR PI loops, and
    injected drifts: a viscosity front from the feed (1500 s), warm tower water the twin is not
    told about (3000 s), a lost heater element on D3 (3600 s), a chattering fan contactor (2400 s)."""

    def __init__(self, p: Plant, rc: Recipe, dt: float, seed: int = 7, flow_meters: bool = False,
                 event_offset: float = 0.0):
        self.event_offset = event_offset      # push the injected schedule later, to leave a quiet run-in
        random.seed(seed)
        self.p, self.rc, self.dt = p, rc, dt
        self.truth = Plant(hA_w=[x * 1.2 for x in p.hA_w], UA_oil=[x * 0.9 for x in p.UA_oil], k_pump=p.k_pump * 1.05,
                           UA_hx_b=250.0)                                     # a fouled exchanger: less cooling authority
        self.SV = [130, 140, 150, 155, 160, 160, 160, 160, 175, 175, 178, 182, 182, 182, 182, 182, 182, 182]
        self.st = State(T_m=[60 + 115 * min(i, 7) / 7 for i in range(N)], T_b=[float(s) for s in self.SV],
                        T_oil_b=140.0, T_oil_s=120.0, T_s=130.0)
        self.I = [0.0] * N
        self.MV = [0.3] * N
        self.kappa_base, self.kappa_true = 1.8, [1.8] * N
        self.P_heat_true = list(p.P_heat)
        self.T_tower = 30.0
        self.rpm, self.t = 60.0, 0.0
        self.flow_b, self.flow_s = 40.0, 20.0
        self.flow_meters = flow_meters
        self.crammer_rpm = 45.0
        self.crammer_base = 12.0
        self.bulk = 1.0
        self.contacts = {"fan_main": 1, "valve_tcu_b": 0, "contactor_drive": 1}
        self._q_cool_last = 15000.0
        for _ in range(int(1800 / dt)):                                       # let the plant settle first
            self._plant_step()
        self.t = 0.0

    def controllers(self):
        Kp, Ti = 0.08, 200.0
        for i in range(N):
            err = self.SV[i] - self.st.T_b[i]
            self.I[i] = clamp(self.I[i] + err * self.dt / Ti, -1.0 / Kp, 1.0 / Kp)
            self.MV[i] = clamp(Kp * (err + self.I[i]), 0.0, 1.0)

    def events(self):
        """The injected schedule, shifted by `event_offset`. Score against this rather than the
        class attribute, or a shifted run is graded against the unshifted times."""
        return [(t + self.event_offset, n, k) for (t, n, k) in self.EVENTS]

    def disturbances(self, residence):
        t = self.t - self.event_offset
        cum = 0.0
        for i in BARREL:                                                       # 1500 s: viscosity +15 % advancing zone by zone
            cum += residence[i] * 3.0
            self.kappa_true[i] = self.kappa_base * (1.15 if t > 1500 + cum else 1.0)
        # the crammer meets the new material at the throat before any of it reaches the barrel
        self.bulk = 1.22 if t > 1440 else 1.0
        self.T_tower = 30.0 + 25.0 * clamp((t - 3000) / 600.0, 0.0, 1.0)       # 3000 s: hot afternoon, tower fan down
        self.P_heat_true[13] = self.p.P_heat[13] * (0.6 if t > 3600 else 1.0)  # 3600 s: D3 loses 40 % of its heater
        self.flow_b = 40.0 * (0.35 if t > 3900 else 1.0)                       # 3900 s: barrel loop flow drops off
        self.contacts["valve_tcu_b"] = 1 if self.st.T_oil_b > 141.0 else 0
        if 2400 < t < 2580:                                                    # 2400 s: fan contactor chattering 3 min
            self.contacts["fan_main"] = int((t // 5) % 2)
        else:
            self.contacts["fan_main"] = 1

    def loops(self, Q_cool_total: float):
        """supply is the oil leaving the TCU, return is the same oil after the barrel has warmed it"""
        pr = self.truth
        mb = max(self.flow_b, 1.0) / 60000.0 * pr.rho_oil
        ms = max(self.flow_s, 1.0) / 60000.0 * pr.rho_oil
        q_s = pr.UA_screw_oil * (self.st.T_s - self.st.T_oil_s)
        return (self.st.T_oil_b + random.gauss(0, 0.12),
                self.st.T_oil_b + max(Q_cool_total, 0.0) / (mb * pr.cp_oil) + random.gauss(0, 0.12),
                self.st.T_oil_s + random.gauss(0, 0.12),
                self.st.T_oil_s + max(q_s, 0.0) / (ms * pr.cp_oil) + random.gauss(0, 0.12))

    def crammer(self):
        load = self.crammer_base * self.bulk * (self.crammer_rpm / 45.0)
        return self.crammer_rpm + random.gauss(0, 0.2), load + random.gauss(0, 0.25)

    def measure(self):
        w = viscous_weights(self.truth, self.rc, self.st.T_m, self.rpm)
        P = sum(self.kappa_true[i] * w[i] for i in BARREL) / (1.0 - self.truth.solids_share)
        amps = self.truth.I_noload + P / (self.truth.kW_per_amp * 1000.0)
        P_ad = 160.0 * (sum(self.kappa_true) / len(self.kappa_true) / self.kappa_base) * math.exp(
            self.rc.EaR * 0.3 * (1.0 / (self.st.T_m[ADAPTER] + 273.15) - 1.0 / (178.0 + 273.15)))
        return amps, P_ad

    def _plant_step(self):
        self.controllers()
        amps, P_ad = self.measure()
        obs, obr, oss, osr = self.loops(self._q_cool_last)
        u = Inputs(PV=[x + random.gauss(0, 0.1) for x in self.st.T_b], SV=[float(s) for s in self.SV],
                   MV=list(self.MV), rpm=self.rpm, amps=amps + random.gauss(0, 1.0),
                   T_adapter=self.st.T_m[ADAPTER] + random.gauss(0, 0.2),
                   CT=[self.MV[i] * self.P_heat_true[i] / self.truth.V_line for i in range(N)],
                   T_tower=self.T_tower, T_air=35.0, T_feed_jacket=40.0, P_adapter=P_ad + random.gauss(0, 1.0),
                   crammer_rpm=self.crammer()[0], crammer_amps=self.crammer()[1],
                   rpm_motor=self.rpm * self.truth.gear_ratio + random.gauss(0, 1.0),
                   T_oil_b_supply=obs, T_oil_b_return=obr, T_oil_s_supply=oss, T_oil_s_return=osr,
                   flow_oil_b=(self.flow_b + random.gauss(0, 0.3) if self.flow_meters else None),
                   flow_oil_s=(self.flow_s + random.gauss(0, 0.2) if self.flow_meters else None),
                   contacts=dict(self.contacts), live={k: "sim" for k in ("amps", "rpm", "T_adapter", "P_adapter",
                                                                          "crammer_rpm", "crammer_amps", "rpm_motor",
                                            "T_oil_b_supply", "T_oil_b_return",
                                            "T_oil_s_supply", "T_oil_s_return")})
        self.st, diag = step(self.truth, self.rc, self.st, u, self.dt, P_heat=self.P_heat_true, T_tower=self.T_tower)
        self._q_cool_last = sum(diag["Q_cool"])
        return u, diag

    EVENTS = [(1440.0, "denser feed arrives at the crammer", ("feed",)),
              (1500.0, "viscosity front from the feed (+15 %)", ("kappa", "cross", "template")),
              (2400.0, "fan contactor chattering", ("contact",)),
              (3000.0, "tower water 30 -> 55 degC", ("template",)),
              (3600.0, "D3 loses 40 % of its heater", ("heater", "local")),
              (3900.0, "barrel TCU loop loses most of its flow", ("loop",))]

    def ground_truth(self) -> dict:
        """what the twin is trying to estimate -- for validation only"""
        return dict(T_m=list(self.st.T_m), T_b=list(self.st.T_b), T_oil_b=self.st.T_oil_b,
                    T_s=self.st.T_s, kappa=sum(self.kappa_true) / len(self.kappa_true),
                    kappa_rel=sum(self.kappa_true) / len(self.kappa_true) / self.kappa_base,
                    T_tower=self.T_tower, P_heat_D3=self.P_heat_true[13], bulk=self.bulk,
                    flow_b=self.flow_b)

    def advance(self):
        u, diag = self._plant_step()
        self.disturbances(diag["residence"])
        self.t += self.dt
        u.T_tower = 30.0                                                       # the twin is NOT told about the tower
        return u, []


EXAMPLE_CONFIG = {
    "port": "COM3",
    "parity": "O",
    "stations": list(range(1, 19)),
    "motor_rpm_per_hz": 29.5,
    "gear_ratio": 20.0,
    "crammer_rpm_per_hz": 29.5,
    "crammer_gear_ratio": 15.0,
    "plant": {"D": 0.152, "I_noload": 25.0, "V_line": 480.0,
              "P_heat": [8000.0] * 8 + [4000.0, 4000.0, 3000.0] + [5000.0] * 7},
    "recipe": {"name": "flexible PVC, pool sheet", "plasticizer_phr": 45.0, "filler_phr": 30.0},
    "devices": [
        {"name": "vfd_amps", "port": "COM4", "baud": 9600, "parity": "N", "station": 1, "fc": 3,
         "register": 0, "type": "u16", "scale": 0.1, "unit": "A", "maps_to": "amps",
         "_note": "FILL FROM THE DRIVE MANUAL: output-current register and scaling"},
        {"name": "vfd_hz", "port": "COM4", "baud": 9600, "parity": "N", "station": 1, "fc": 3,
         "register": 1, "type": "u16", "scale": 0.01, "unit": "Hz", "maps_to": "hz"},
        {"name": "crammer_amps", "port": "COM4", "baud": 9600, "parity": "N", "station": 2, "fc": 3,
         "register": 0, "type": "u16", "scale": 0.1, "unit": "A", "maps_to": "crammer_amps",
         "_note": "the CRAMMER drive, a second station on the drive bus. Its own output-current register."},
        {"name": "crammer_hz", "port": "COM4", "baud": 9600, "parity": "N", "station": 2, "fc": 3,
         "register": 1, "type": "u16", "scale": 0.01, "unit": "Hz", "maps_to": "crammer_hz"},
        {"name": "adapter_T", "port": "COM3", "parity": "O", "station": 19, "fc": 4,
         "register": 1000, "type": "s16", "scale": 0.1, "unit": "F", "maps_to": "T_adapter",
         "_note": "if the adapter readout is PXR-class; scale = 10^-P-dP"},
        {"name": "tcu_barrel_supply", "port": "COM3", "parity": "O", "station": 20, "fc": 4,
         "register": 1000, "type": "s16", "scale": 0.1, "unit": "F", "maps_to": "tcu_b_sv"},
        {"name": "tcu_screw_supply", "port": "COM3", "parity": "O", "station": 21, "fc": 4,
         "register": 1000, "type": "s16", "scale": 0.1, "unit": "F", "maps_to": "tcu_s_sv"}
    ],
    "analog": {"port": "COM5", "baud": 115200,
               "map": {"tower": "T_tower", "air": "T_air", "jacket": "T_feed_jacket", "pmelt": "P_adapter",
                       "cramps": "crammer_amps", "crhz": "crammer_hz",
                       "oilbs": "T_oil_b_supply", "oilbr": "T_oil_b_return",
                       "oilss": "T_oil_s_supply", "oilsr": "T_oil_s_return",
                       "towret": "T_tower_return", "flowb": "flow_oil_b", "flows": "flow_oil_s"},
               "units": {"tower": "F", "air": "F", "jacket": "F", "pmelt": "psi",
                         "oilbs": "F", "oilbr": "F", "oilss": "F", "oilsr": "F", "towret": "F"},
               "_note": "cramps/crhz are the analog fallback if the crammer drive has no comms port",
               "contacts": ["fan_main", "valve_tcu_b", "contactor_drive"]},
    "ops_file": "ops_inputs.json",
    "ops_stale_s": 1800,
    "manual": {"rpm": 60.0, "amps": 285.0, "crammer_rpm": 45.0, "crammer_amps": 12.0,
               "flow_oil_b": None, "flow_oil_s": None, "T_adapter": None, "tcu_b_sv": 140.0, "tcu_s_sv": 120.0,
               "T_tower": 30.0, "T_air": 35.0, "T_feed_jacket": 40.0}
}
