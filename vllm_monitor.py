#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vllm_monitor — 轻量级、自动识别、可扩展的本地 AI 部署资源监控
=============================================================
监控 GPU / CPU / 内存 / 磁盘 / 网络 / vLLM 服务, 提供 Web 面板 + JSON API。

设计原则:
  * 零第三方依赖 (纯 Python 标准库; 无 psutil, 直接解析 /proc + nvidia-smi)
  * 自动识别: 启动时探测硬件/服务, 数量/型号/服务地址均不写死, 换硬件无缝监控
  * 可扩展: 新增一个 Collector 子类即被自动发现并纳入面板 (面板按 API 返回数据驱动)
  * 可交付: 环形缓冲实时历史 + SQLite 长期趋势 + 阈值告警 + systemd + 桌面脚本

仅用 Python 3.9+ 标准库。
"""
import argparse
import json
import os
import platform
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
PROG = "vllm_monitor"
START_TIME = time.strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------

def now_ts():
    return time.time()


def read_text(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception:
        return None


def read_lines(path):
    t = read_text(path)
    return t.splitlines() if t is not None else None


def run_cmd(cmd, timeout=6):
    """运行命令, 返回 (rc, stdout+stderr)。"""
    try:
        p = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, shell=isinstance(cmd, str),
            executable="/bin/bash" if isinstance(cmd, str) else None,
        )
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:
        return None, str(e)


def parse_num(s, default=0.0):
    try:
        return float(s)
    except Exception:
        return default


def fmt_human_bytes(n):
    if n is None:
        return None
    try:
        n = float(n)
    except Exception:
        return None
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{int(n)}{u}" if u == "B" else f"{round(n, 1)}{u}"
        n /= 1024.0
    return f"{int(n)}TB"


# ----------------------------------------------------------------------------
# 采集器
# ----------------------------------------------------------------------------

class Collector:
    """采集器基类。子类实现 detect()/collect()。
    collect() 返回 dict, 含 'name'、'available'(bool)、可选 'error' 及自定义字段。
    面板按 API 返回的数据驱动渲染, 新增采集器无需改前端。
    """
    name = "base"

    def detect(self):
        return True

    def collect(self):
        raise NotImplementedError


class BaseCollector(Collector):
    """带上次采样缓存, 供增量/速率计算。"""

    def __init__(self):
        self._last = {}
        self._last_t = None


class GPUCollector(Collector):
    name = "gpu"

    _FIELDS = ("index,name,uuid,memory.total,memory.used,memory.free,"
               "utilization.gpu,utilization.memory,temperature.gpu,"
               "power.draw,power.limit,clocks.current.graphics,clocks.max.graphics,"
               "fan.speed,ecc.errors.corrected.volatile.total,"
               "ecc.errors.uncorrected.volatile.total")

    def __init__(self):
        self._have_smi = None

    def detect(self):
        if self._have_smi is None:
            self._have_smi = os.path.exists("/usr/bin/nvidia-smi") or \
                os.path.exists("/usr/local/bin/nvidia-smi")
        return self._have_smi

    def _smi(self, *args):
        rc, out = run_cmd(["nvidia-smi", *args])
        if rc != 0 or not out:
            return None
        return out

    def collect(self):
        out = self._smi("--query-gpu=" + self._FIELDS,
                        "--format=csv,noheader,nounits")
        gpus = []
        if out:
            for line in out.strip().splitlines():
                p = [x.strip() for x in line.split(",")]
                if len(p) < 16:
                    continue
                gpus.append({
                    "index": int(parse_num(p[0])), "name": p[1], "uuid": p[2],
                    "mem_total_mib": parse_num(p[3]), "mem_used_mib": parse_num(p[4]),
                    "mem_free_mib": parse_num(p[5]), "util_gpu": parse_num(p[6]),
                    "util_mem": parse_num(p[7]), "temp_c": parse_num(p[8]),
                    "power_w": parse_num(p[9]), "power_limit_w": parse_num(p[10]),
                    "clock_mhz": parse_num(p[11]), "clock_max_mhz": parse_num(p[12]),
                    "fan_pct": parse_num(p[13]),
                    "ecc_corrected": parse_num(p[14]),
                    "ecc_uncorrected": parse_num(p[15]),
                })
        procs = []
        pout = self._smi("--query-compute-apps=pid,process_name,used_memory,gpu_bus_id",
                         "--format=csv,noheader,nounits")
        if pout:
            for line in pout.strip().splitlines():
                p = [x.strip() for x in line.split(",")]
                if len(p) >= 4:
                    procs.append({"pid": int(parse_num(p[0])), "name": p[1],
                                  "mem_mib": parse_num(p[2]), "bus": p[3]})
        data = {"name": "gpu", "available": bool(gpus), "count": len(gpus),
                "gpus": gpus, "processes": procs}
        if not gpus:
            data["error"] = "nvidia-smi 无输出 (驱动异常?)"
        return data


class CPUCollector(BaseCollector):
    name = "cpu"

    def detect(self):
        return os.path.exists("/proc/stat")

    def collect(self):
        lines = read_lines("/proc/stat")
        if not lines:
            return {"name": "cpu", "available": False, "error": "no /proc/stat"}
        cpu0 = lines[0].split()
        vals = [parse_num(x) for x in cpu0[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        prev_idle, prev_total = self._last.get("idle"), self._last.get("total")
        d_total = total - (prev_total if prev_total is not None else total)
        d_idle = idle - (prev_idle if prev_idle is not None else idle)
        util = max(0.0, min(100.0, (1 - d_idle / d_total) * 100)) if d_total > 0 else 0.0
        self._last["idle"] = idle
        self._last["total"] = total

        cores = []
        for ln in lines[1:]:
            if not ln.startswith("cpu"):
                break
            c = ln.split()
            cv = [parse_num(x) for x in c[1:]]
            c_idle = cv[3] + (cv[4] if len(cv) > 4 else 0)
            cores.append((c[0][3:], sum(cv), c_idle))
        prev = self._last.get("cores") or {}
        per_core = []
        for cid, ct, ci in cores:
            pt, pi = prev.get(cid, (ct, ci))
            dT, di = ct - pt, ci - pi
            cu = max(0.0, min(100.0, (1 - di / dT) * 100)) if dT > 0 else 0.0
            per_core.append({"id": int(cid), "util": round(cu, 1)})
        self._last["cores"] = {cid: (ct, ci) for cid, ct, ci in cores}

        load = read_text("/proc/loadavg")
        load1 = load5 = load15 = None
        if load:
            lf = load.split()
            if len(lf) >= 3:
                load1, load5, load15 = (parse_num(lf[0]), parse_num(lf[1]),
                                        parse_num(lf[2]))
        data = {"name": "cpu", "available": True, "util": round(util, 1),
                "cores": os.cpu_count() or len(per_core),
                "load1": load1, "load5": load5, "load15": load15,
                "per_core": per_core}
        return data


class MemCollector(Collector):
    name = "mem"

    def detect(self):
        return os.path.exists("/proc/meminfo")

    def collect(self):
        mi = {}
        for ln in read_lines("/proc/meminfo") or []:
            if ":" in ln:
                k, v = ln.split(":", 1)
                mi[k.strip()] = parse_num(v.strip().split()[0]) if v.strip() else 0
        total = mi.get("MemTotal", 0)
        avail = mi.get("MemAvailable", mi.get("MemFree", 0))
        used = total - avail
        sw_total = mi.get("SwapTotal", 0)
        sw_free = mi.get("SwapFree", 0)
        data = {"name": "mem", "available": True,
                "total_mb": round(total / 1024), "used_mb": round(used / 1024),
                "avail_mb": round(avail / 1024),
                "buffers_mb": round(mi.get("Buffers", 0) / 1024),
                "cached_mb": round((mi.get("Cached", 0) + mi.get("SReclaimable", 0)) / 1024),
                "used_pct": round(used / total * 100, 1) if total else 0,
                "swap_total_mb": round(sw_total / 1024),
                "swap_used_mb": round((sw_total - sw_free) / 1024)}
        return data


class DiskCollector(BaseCollector):
    name = "disk"
    _SKIP = {"tmpfs", "devtmpfs", "overlay", "shm", "squashfs", "udev",
             "cgroup", "sysfs", "proc"}

    def detect(self):
        return os.path.exists("/proc/diskstats")

    def collect(self):
        t = now_ts()
        mounts, seen = [], set()
        for ln in read_lines("/proc/mounts") or []:
            p = ln.split()
            if len(p) < 3:
                continue
            dev, mp, fstype = p[0], p[1], p[2]
            if fstype in self._SKIP or not dev.startswith("/dev/") or dev in seen:
                continue
            seen.add(dev)
            try:
                st = os.statvfs(mp)
            except Exception:
                continue
            blk = st.f_bsize
            tot = st.f_blocks * blk
            fr = st.f_bavail * blk
            used = (st.f_blocks - st.f_bfree) * blk
            mounts.append({"dev": dev, "mount": mp, "fstype": fstype,
                           "total_gb": round(tot / 1e9, 1), "used_gb": round(used / 1e9, 1),
                           "free_gb": round(fr / 1e9, 1),
                           "used_pct": round(used / tot * 100, 1) if tot else 0})
        io = {}
        for ln in read_lines("/proc/diskstats") or []:
            p = ln.split()
            if len(p) < 14:
                continue
            dev = p[2]
            if re.fullmatch(r"nvme\d+n\d+", dev) or re.fullmatch(r"(sd|vd|hd|xvd)[a-z]", dev):
                io[dev] = (parse_num(p[5]), parse_num(p[9]))
        prev = self._last.get("io") or {}
        dt = (t - self._last["t"]) if self._last.get("t") else 0
        speeds, tot_r, tot_w = [], 0.0, 0.0
        for dev, (r, w) in io.items():
            pr, pw = prev.get(dev, (r, w))
            rs = (r - pr) / dt / 2048 if dt > 0 else 0
            ws = (w - pw) / dt / 2048 if dt > 0 else 0
            tot_r += rs
            tot_w += ws
            speeds.append({"dev": dev, "read_mb_s": round(rs, 2), "write_mb_s": round(ws, 2)})
        self._last["io"] = io
        self._last["t"] = t
        return {"name": "disk", "available": True, "mounts": mounts, "io": speeds,
                "read_mb_s": round(tot_r, 2), "write_mb_s": round(tot_w, 2)}


class NetCollector(BaseCollector):
    name = "net"
    _SKIP = {"lo"}

    def detect(self):
        return os.path.exists("/proc/net/dev")

    def collect(self):
        t = now_ts()
        dev = {}
        for ln in read_lines("/proc/net/dev") or []:
            if ":" not in ln:
                continue
            name, rest = ln.split(":", 1)
            name = name.strip()
            if name in self._SKIP:
                continue
            f = rest.split()
            if len(f) < 10:
                continue
            dev[name] = (parse_num(f[0]), parse_num(f[8]))
        prev = self._last.get("dev") or {}
        dt = (t - self._last["t"]) if self._last.get("t") else 0
        ifaces, tot_rx, tot_tx = [], 0.0, 0.0
        for name, (rx, tx) in dev.items():
            prx, ptx = prev.get(name, (rx, tx))
            rxs = (rx - prx) / dt if dt > 0 else 0
            txs = (tx - ptx) / dt if dt > 0 else 0
            tot_rx += rxs
            tot_tx += txs
            ifaces.append({"if": name, "rx_mb_s": round(rxs / 1e6, 3),
                           "tx_mb_s": round(txs / 1e6, 3),
                           "rx_gb": round(rx / 1e9, 2), "tx_gb": round(tx / 1e9, 2)})
        self._last["dev"] = dev
        self._last["t"] = t
        return {"name": "net", "available": True, "ifaces": ifaces,
                "rx_mb_s": round(tot_rx / 1e6, 3), "tx_mb_s": round(tot_tx / 1e6, 3)}


# ----------------------------------------------------------------------------
# 进程指纹 + 配置识别
# ----------------------------------------------------------------------------

def _url_port(url):
    """从 http://host:port/ 取端口"""
    rest = url.split("//", 1)[-1]
    return int(rest.split("/", 1)[0].rsplit(":", 1)[-1])


def _read_proc_file(path, binary=True):
    try:
        with open(path, "rb") as f:
            return f.read()
    except (OSError, IOError):
        return None


def _read_cmdline(pid):
    raw = _read_proc_file("/proc/%d/cmdline" % pid, binary=True)
    if not raw:
        return None
    parts = [x.decode("utf-8", "replace") for x in raw.split(b"\0") if x]
    return parts or None


def _read_environ(pid):
    raw = _read_proc_file("/proc/%d/environ" % pid, binary=True)
    if not raw:
        return {}
    out = {}
    for line in raw.split(b"\0"):
        if b"=" in line:
            k, v = line.split(b"=", 1)
            out[k.decode("ascii", "replace")] = v.decode("utf-8", "replace")
    return out


def _parse_cli_args(parts):
    """从 vLLM 启动参数里提取关键配置"""
    d = {"model": "", "served": "", "tp": None, "max_len": None,
         "seqs": None, "batch": None, "gpu_mem_util": None, "spec": None}
    positionals = []
    i = 0
    n = len(parts)
    while i < n:
        a = parts[i]
        if a == "--model" and i + 1 < n:
            d["model"] = parts[i + 1]; i += 2; continue
        if a == "--served-model-name" and i + 1 < n:
            d["served"] = parts[i + 1]; i += 2; continue
        if a == "--tensor-parallel-size" and i + 1 < n:
            try: d["tp"] = int(parts[i + 1])
            except ValueError: pass
            i += 2; continue
        if a == "--max-model-len" and i + 1 < n:
            try: d["max_len"] = int(parts[i + 1])
            except ValueError: pass
            i += 2; continue
        if a == "--max-num-seqs" and i + 1 < n:
            try: d["seqs"] = int(parts[i + 1])
            except ValueError: pass
            i += 2; continue
        if a == "--max-num-batched-tokens" and i + 1 < n:
            try: d["batch"] = int(parts[i + 1])
            except ValueError: pass
            i += 2; continue
        if a == "--gpu-memory-utilization" and i + 1 < n:
            try: d["gpu_mem_util"] = float(parts[i + 1])
            except ValueError: pass
            i += 2; continue
        if a == "--speculative-config" and i + 1 < n:
            try:
                d["spec"] = json.loads(parts[i + 1])
            except (ValueError, TypeError):
                d["spec"] = None
            i += 2; continue
        if not a.startswith("-") and a not in ("python", "vllm", "serve") \
                and "vllm" not in a and "python" not in a:
            positionals.append(a)
        i += 1
    if not d["model"] and positionals:
        d["model"] = positionals[0]
    return d


def _short_label(cli, cuda):
    m = cli["model"].lower()
    if "w8a16" in m:
        base = "W8A16"
    elif "w4a16" in m:
        base = "AWQ"
    else:
        base = "未知"
    if cli["spec"]:
        base += "-MTP%s" % cli["spec"].get("num_speculative_tokens", "?")
    else:
        base += "-无MTP"
    if cli["served"]:
        base += " (%s)" % cli["served"]
    return base


def inspect_vllm_proc(port, cache=None, ttl=30.0):
    """扫描 /proc 找到监听该端口的 vLLM 进程, 返回其配置指纹 (带 30s 缓存)"""
    cache = cache if cache is not None else {}
    c = cache.get(port)
    now = time.time()
    if c and now - c["t"] < ttl and _read_cmdline(c["pid"]):
        return c["info"]
    parts = None
    try:
        pids = [int(x) for x in os.listdir("/proc") if x.isdigit()]
    except OSError:
        pids = []
    pids.sort()
    for pid in pids:
        parts = _read_cmdline(pid)
        if not parts:
            continue
        if "vllm" not in " ".join(parts).lower():
            continue
        if ("--port %d" % port) not in " ".join(parts):
            continue
        break
    if not parts:
        cache.pop(port, None)
        return None
    cli = _parse_cli_args(parts)
    env = _read_environ(pid)
    spec = cli["spec"] or {}
    info = {
        "pid": pid,
        "model": cli["model"],
        "served": cli["served"],
        "tp": cli["tp"],
        "max_len": cli["max_len"],
        "seqs": cli["seqs"],
        "batch": cli["batch"],
        "gpu_mem_util": cli["gpu_mem_util"],
        "mtp_on": bool(spec),
        "mtp_tokens": int(spec.get("num_speculative_tokens", 0) or 0),
        "cuda_devices": env.get("CUDA_VISIBLE_DEVICES", ""),
        "short": _short_label(cli, env),
    }
    cache[port] = {"t": now, "pid": pid, "info": info}
    return info


def classify_config(instances):
    """按 plan.md 的 7 个方案识别当前配置; 无法识别则根据实际特征生成描述 (兜底)"""
    on = [i for i in instances if i.get("online") and i.get("proc")]
    if not on:
        return None
    ports = set()
    for i in on:
        try:
            ports.add(_url_port(i["url"]))
        except (KeyError, ValueError):
            pass
    p0 = next((i for i in on if _url_port(i["url"]) == 8000), on[0])
    pr = p0["proc"]
    tp = pr.get("tp") or 0
    ml = pr.get("max_len") or 0
    sq = pr.get("seqs") or 0
    mtp = pr.get("mtp_on")
    short = pr.get("short", "")
    if ports == {8000} and tp == 4 and ml >= 200000 and sq >= 8:
        if "w8a16" in pr["model"].lower() and mtp:
            return {"id": 1, "name": "方案1 · W8A16 + MTP-2 (TP4/8并发/256K)"}
        if "w4a16" in pr["model"].lower() and mtp:
            return {"id": 2, "name": "方案2 · W4A16-AWQ + MTP-2 (TP4/8并发/256K)"}
        if "w8a16" in pr["model"].lower():
            return {"id": 3, "name": "方案3 · W8A16 无MTP (TP4/8并发/256K)"}
        return {"id": 4, "name": "方案4 · W4A16-AWQ 无MTP (TP4/8并发/256K)"}
    if ports == {8000, 8002}:
        return {"id": 5, "name": "方案5 · W8主力(8000) + AWQ备用(8002) 双实例"}
    if ports == {8000, 8002, 8003}:
        return {"id": 6, "name": "方案6 · W8长上下文 + W4×2 短上下文 混合多实例"}
    if ports == {8000, 8001, 8002, 8003}:
        return {"id": 7, "name": "方案7 · 每卡1实例 W4 (8000-8003)"}
    # 兜底: 端口不匹配已知方案 → 根据实际特征生成描述
    on_sorted = sorted(on, key=lambda i: _url_port(i["url"]))
    parts = []
    for i in on_sorted:
        pr_i = i["proc"]
        port_i = _url_port(i["url"])
        short_i = pr_i.get("short", "?")
        parts.append(f"{short_i}({port_i})")
    return {"id": 0, "name": "自定义配置 · " + " + ".join(parts) + f" {len(on)}实例"}


class VLLMCollector(BaseCollector):
    name = "vllm"

    def __init__(self, urls):
        super().__init__()
        self.urls = urls
        self._proc_cache = {}
        self._seen_online = set()  # 曾经在线过的 URL (自适应: 只显示识别到的实例)

    def detect(self):
        return len(self.urls) > 0

    @staticmethod
    def _parse_metrics(text):
        d = {}
        for ln in text.splitlines():
            if not ln or ln.startswith("#"):
                continue
            try:
                head, val = ln.rsplit(" ", 1)
                v = float(val)
            except Exception:
                continue
            name, labels = head, ""
            if "{" in head:
                i = head.index("{")
                name, labels = head[:i], head[i + 1:-1]
            d.setdefault(name, {})[labels] = v
        return d

    def _fetch(self, base, timeout=4):
        url = base.rstrip("/") + "/metrics"
        try:
            with urlopen(Request(url, headers={"User-Agent": PROG}), timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            return None

    @staticmethod
    def _get(m, name, sub=None):
        """取指标值。sub 为标签子串约束; 无约束则对多标签求和 (counter 语义)。"""
        vals = m.get(name, {})
        if not vals:
            return 0.0
        if sub:
            for k, v in vals.items():
                if sub in k:
                    return v
            return 0.0
        return sum(vals.values())

    @staticmethod
    def _label(m, name, key, default="?"):
        for labels in m.get(name, {}):
            mm = re.search(r'%s="([^"]+)"' % re.escape(key), labels)
            if mm:
                return mm.group(1)
        return default

    def collect(self):
        instances, any_ok = [], False
        for base in self.urls:
            text = self._fetch(base)
            inst = {"url": base, "online": False}
            if not text:
                # 自适应: 从未在线过的探测端口不显示、不告警
                if base not in self._seen_online:
                    continue
                inst["error"] = "offline"
                instances.append(inst)
                continue
            self._seen_online.add(base)
            any_ok = True
            m = self._parse_metrics(text)
            model = self._label(m, "vllm:num_requests_running", "model_name") \
                or self._label(m, "vllm:kv_cache_usage_perc", "model_name")
            inst.update(online=True, model=model)
            inst["running"] = int(self._get(m, "vllm:num_requests_running"))
            inst["waiting"] = int(self._get(m, "vllm:num_requests_waiting"))
            inst["kv_pct"] = round(self._get(m, "vllm:kv_cache_usage_perc") * 100, 1)
            inst["preemptions"] = int(self._get(m, "vllm:num_preemptions_total"))
            inst["wait_capacity"] = int(self._get(m, "vllm:num_requests_waiting_by_reason",
                                                   'reason="capacity"'))
            inst["wait_deferred"] = int(self._get(m, "vllm:num_requests_waiting_by_reason",
                                                   'reason="deferred"'))
            inst["prefix_hit_rate"] = round(
                self._get(m, "vllm:prefix_cache_hits_total") /
                max(1, self._get(m, "vllm:prefix_cache_queries_total")) * 100, 1)
            inst["mm_hit_rate"] = round(
                self._get(m, "vllm:mm_cache_hits_total") /
                max(1, self._get(m, "vllm:mm_cache_queries_total")) * 100, 1)

            # MTP / 投机解码
            draft = self._get(m, "vllm:spec_decode_num_draft_tokens_total")
            acc = self._get(m, "vllm:spec_decode_num_accepted_tokens_total")
            inst["mtp_accept_rate"] = round(acc / max(1, draft) * 100, 1) if draft else None
            inst["mtp_per_pos"] = {pos: int(self._get(m,
                "vllm:spec_decode_num_accepted_tokens_per_pos_total",
                f'position="{pos}"')) for pos in ("0", "1", "2", "3", "4", "5")}

            # 增量: 吞吐 + 近期均值
            key = base
            t = now_ts()
            prev = self._last.get(key, {})
            dt = (t - prev["t"]) if prev.get("t") else 0

            def delta(name):
                cur = self._get(m, name)
                p = prev.get(name)
                return (cur - p) if (p is not None and dt > 0) else 0.0

            gen_total = self._get(m, "vllm:generation_tokens_total")
            prompt_total = self._get(m, "vllm:prompt_tokens_total")
            # 累计计数器 (供 Token 统计, 按日/周/月/年聚合; 重启后归零由统计端按段处理)
            inst["prompt_total"] = int(prompt_total)
            inst["gen_total"] = int(gen_total)
            inst["requests_total"] = int(self._get(m, "vllm:request_success_total"))
            inst["gen_toks_s"] = round(delta("vllm:generation_tokens_total") / dt, 1) if dt > 0 else 0
            inst["prompt_toks_s"] = round(delta("vllm:prompt_tokens_total") / dt, 1) if dt > 0 else 0

            ttft_d = delta("vllm:time_to_first_token_seconds_sum")
            ttft_n = delta("vllm:time_to_first_token_seconds_count")
            inst["ttft_ms"] = round(ttft_d / ttft_n * 1000, 0) if ttft_n > 0 else None
            itl_d = delta("vllm:inter_token_latency_seconds_sum")
            itl_n = delta("vllm:inter_token_latency_seconds_count")
            inst["itl_ms"] = round(itl_d / itl_n * 1000, 0) if itl_n > 0 else None

            self._last[key] = {
                "t": t,
                "vllm:generation_tokens_total": gen_total,
                "vllm:prompt_tokens_total": prompt_total,
                "vllm:time_to_first_token_seconds_sum": self._get(m, "vllm:time_to_first_token_seconds_sum"),
                "vllm:time_to_first_token_seconds_count": self._get(m, "vllm:time_to_first_token_seconds_count"),
                "vllm:inter_token_latency_seconds_sum": self._get(m, "vllm:inter_token_latency_seconds_sum"),
                "vllm:inter_token_latency_seconds_count": self._get(m, "vllm:inter_token_latency_seconds_count"),
            }
            # 进程指纹: 该实例当前跑的是哪套配置 (TP/上下文/并发/MTP/显存比/GPU)
            info = inspect_vllm_proc(_url_port(base), self._proc_cache)
            if info:
                inst["proc"] = info
            instances.append(inst)
        data = {"name": "vllm", "available": True, "instances": instances,
                "online": any_ok}
        cfg = classify_config(instances)
        if cfg:
            data["cfg_name"] = cfg["name"]
        return data


# 所有内置采集器 (新增采集器在此注册即被自动发现)
BUILTIN_COLLECTORS = [GPUCollector, CPUCollector, MemCollector, DiskCollector,
                      NetCollector]


# ----------------------------------------------------------------------------
# 告警
# ----------------------------------------------------------------------------

def compute_alerts(snap, th):
    alerts = []

    def add(sev, area, msg):
        alerts.append({"sev": sev, "area": area, "msg": msg})

    gpu = snap.get("gpu", {})
    if gpu.get("available"):
        for g in gpu.get("gpus", []):
            if g.get("util_gpu", 0) >= th.get("gpu_util_pct", 95):
                add("warn", "gpu", f"GPU{g['index']} 利用率 {g['util_gpu']}% ≥ {th.get('gpu_util_pct')}%")
            if g.get("temp_c", 0) >= th.get("gpu_temp_c", 85):
                add("crit", "gpu", f"GPU{g['index']} 温度 {g['temp_c']}°C ≥ {th.get('gpu_temp_c')}°C")
            mt, mu = g.get("mem_total_mib", 0), g.get("mem_used_mib", 0)
            if mt and mu / mt * 100 >= th.get("gpu_mem_pct", 98):
                add("warn", "gpu", f"GPU{g['index']} 显存 {mu/mt*100:.0f}% ≥ {th.get('gpu_mem_pct')}%")
            if g.get("ecc_uncorrected", 0) > 0:
                add("crit", "gpu", f"GPU{g['index']} ECC 不可纠正错误 {g['ecc_uncorrected']}")

    mem = snap.get("mem", {})
    if mem.get("available") and mem.get("used_pct", 0) >= th.get("mem_pct", 92):
        add("warn", "mem", f"内存 {mem['used_pct']}% ≥ {th.get('mem_pct')}%")

    dsk = snap.get("disk", {})
    if dsk.get("available"):
        for mo in dsk.get("mounts", []):
            if mo.get("used_pct", 0) >= th.get("disk_pct", 90):
                add("warn", "disk", f"{mo['mount']} 磁盘 {mo['used_pct']}% ≥ {th.get('disk_pct')}%")

    vllm = snap.get("vllm", {})
    if vllm.get("available"):
        for inst in vllm.get("instances", []):
            if not inst.get("online"):
                add("crit", "vllm", f"vLLM 离线 {inst['url']}")
                continue
            mdl = inst.get("model", "?")
            if inst.get("waiting", 0) >= th.get("vllm_waiting", 8):
                add("warn", "vllm", f"{mdl} 排队 {inst['waiting']} ≥ {th.get('vllm_waiting')}")
            if inst.get("preemptions", 0) >= th.get("vllm_preemption", 1):
                add("warn", "vllm", f"{mdl} 累计抢占 {inst['preemptions']}")
            if inst.get("kv_pct") and inst["kv_pct"] >= th.get("vllm_kv_pct", 98):
                add("warn", "vllm", f"{mdl} KV 缓存 {inst['kv_pct']}% ≥ {th.get('vllm_kv_pct')}%")
    return alerts


# ----------------------------------------------------------------------------
# 存储
# ----------------------------------------------------------------------------

class Store:
    def __init__(self, history_seconds=1800, interval=2, db_path=None, trend_interval=15):
        self.history_seconds = history_seconds
        self.interval = interval
        self.trend_interval = trend_interval
        self.db_path = db_path
        self._buf = deque()
        self._lock = threading.Lock()
        self._last_trend = 0
        self._conn = None
        if db_path:
            try:
                self._conn = sqlite3.connect(db_path, check_same_thread=False)
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS trend(ts REAL, model TEXT, gen_toks_s REAL,"
                    " ttft_ms REAL, kv_pct REAL, gpu_util REAL, gpu_mem REAL, mem_pct REAL,"
                    " running INT, waiting INT)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_trend_ts ON trend(ts)")
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS tokens(ts REAL, prompt_total REAL,"
                    " gen_total REAL, requests_total REAL)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_ts ON tokens(ts)")
                # 每实例原始计数器 (新口径: 实例增删/重启安全的统计基础)
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS tok_inst(ts REAL, url TEXT, prompt_total REAL,"
                    " gen_total REAL, requests_total REAL)")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tok_inst_url_ts ON tok_inst(url, ts)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tok_inst_ts ON tok_inst(ts)")
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS peak_speed(key TEXT PRIMARY KEY, label TEXT,"
                    " peak_decode REAL, peak_decode_ts REAL, peak_prefill REAL,"
                    " peak_prefill_ts REAL, last_ts REAL)")
                self._conn.commit()
            except Exception as e:
                print(f"[store] sqlite init failed: {e}", file=sys.stderr)
                self._conn = None

    def add(self, snap):
        with self._lock:
            self._buf.append(snap)
            cutoff = snap["ts"] - self.history_seconds
            while self._buf and self._buf[0]["ts"] < cutoff:
                self._buf.popleft()
        self._maybe_persist(snap)

    def _maybe_persist(self, snap):
        if not self._conn or snap["ts"] - self._last_trend < self.trend_interval:
            return
        self._last_trend = snap["ts"]
        try:
            gpu = snap.get("gpu", {})
            g = (gpu.get("gpus") or [{}])[0] if gpu.get("available") else {}
            v = (snap.get("vllm", {}).get("instances") or [{}])[0]
            mem = snap.get("mem", {})
            self._conn.execute(
                "INSERT INTO trend VALUES (?,?,?,?,?,?,?,?,?,?)",
                (snap["ts"], v.get("model"), v.get("gen_toks_s"), v.get("ttft_ms"),
                 v.get("kv_pct"), g.get("util_gpu"), g.get("mem_used_mib"),
                 mem.get("used_pct"), v.get("running"), v.get("waiting")))
            # 累计 token 计数器 (多实例求和, 旧口径, 供历史数据/回退)
            insts = [x for x in (snap.get("vllm", {}).get("instances") or []) if x.get("online")]
            if insts:
                self._conn.execute(
                    "INSERT INTO tokens VALUES (?,?,?,?)",
                    (snap["ts"],
                     float(sum(x.get("prompt_total", 0) for x in insts)),
                     float(sum(x.get("gen_total", 0) for x in insts)),
                     float(sum(x.get("requests_total", 0) for x in insts))))
                # 每实例原始计数器 (新口径: 实例增删/重启安全的统计基础)
                for x in insts:
                    self._conn.execute(
                        "INSERT INTO tok_inst VALUES (?,?,?,?,?)",
                        (snap["ts"], x.get("url", "?"),
                         float(x.get("prompt_total", 0) or 0),
                         float(x.get("gen_total", 0) or 0),
                         float(x.get("requests_total", 0) or 0)))
            self._conn.commit()
        except Exception:
            pass

    def downsample(self, max_points=240):
        with self._lock:
            buf = list(self._buf)
        if len(buf) <= max_points:
            return buf
        step = len(buf) / max_points
        return [buf[int(i * step)] for i in range(max_points)]

    def trend(self, seconds=3600):
        if not self._conn:
            return []
        try:
            cur = self._conn.execute(
                "SELECT ts,model,gen_toks_s,ttft_ms,kv_pct,gpu_util,gpu_mem,mem_pct,running,waiting"
                " FROM trend WHERE ts > ? ORDER BY ts", (time.time() - seconds,))
            cols = ("ts", "model", "gen_toks_s", "ttft_ms", "kv_pct", "gpu_util",
                    "gpu_mem", "mem_pct", "running", "waiting")
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception:
            return []

    # ---------- Token 统计 (累计计数器, 按 日/周/月/年 聚合) ----------

    @staticmethod
    def _period_range(period, now):
        """返回 (周期起点 ts, 分桶级别 hour/day/month), 边界按本地时区。"""
        d = datetime.fromtimestamp(now)
        mid = d.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "day":
            return mid.timestamp(), "hour"
        if period == "week":
            return (mid - timedelta(days=d.weekday())).timestamp(), "day"
        if period == "month":
            return mid.replace(day=1).timestamp(), "day"
        return mid.replace(month=1, day=1).timestamp(), "month"

    @staticmethod
    def _seg_total(vals):
        """累加单调 counter 的增量; 负增量视为进程重启, 当前值即新段起点。"""
        tot, prev = 0.0, vals[0]
        for v in vals[1:]:
            tot += v if v < prev else (v - prev)
            prev = v
        return tot

    @classmethod
    def _bucketize(cls, rows, base, blevel):
        """rows: 升序 [(ts,p,g,r)]; base: (p,g,r)|None (首行前的锚)。
        返回 (p_tot, g_tot, r_tot, buckets{key:(p,g,r)}); 重启负跳变按 _seg_total 处理。"""
        fmt = {"hour": "%Y%m%d%H", "day": "%Y%m%d", "month": "%Y%m"}[blevel]
        if base:
            p_tot = cls._seg_total([base[0]] + [x[1] for x in rows])
            g_tot = cls._seg_total([base[1]] + [x[2] for x in rows])
            r_tot = cls._seg_total([base[2]] + [x[3] for x in rows])
        else:
            p_tot = cls._seg_total([x[1] for x in rows])
            g_tot = cls._seg_total([x[2] for x in rows])
            r_tot = cls._seg_total([x[3] for x in rows])
        buckets, last_before = {}, base
        for (ts, p, g, r) in rows:
            key = datetime.fromtimestamp(ts).strftime(fmt)
            b = buckets.setdefault(key, {"base": last_before, "p": [], "g": [], "r": []})
            b["p"].append(p); b["g"].append(g); b["r"].append(r)
            last_before = (p, g, r)
        out = {}
        for key, b in buckets.items():
            if b["base"]:
                out[key] = (cls._seg_total([b["base"][0]] + b["p"]),
                            cls._seg_total([b["base"][1]] + b["g"]),
                            cls._seg_total([b["base"][2]] + b["r"]))
            else:
                out[key] = (cls._seg_total(b["p"]), cls._seg_total(b["g"]), cls._seg_total(b["r"]))
        return p_tot, g_tot, r_tot, out

    def token_stats(self, period="day"):
        """Token 统计入口: 优先每实例计数器 (tok_inst, 实例增删/重启安全),
        周期早于新数据时回退旧全实例求和表 (tokens, 历史数据)。"""
        empty = {"period": period, "start": 0, "ts": time.time(), "estimated": True,
                 "prompt_total": 0, "gen_total": 0, "requests_total": 0, "series": []}
        if not self._conn:
            return empty
        now = time.time()
        start, blevel = self._period_range(period, now)
        r = self._token_stats_inst(period, start, blevel, now)
        if r is not None:
            return r
        return self._token_stats_legacy(period, start, blevel, now)

    def _token_stats_inst(self, period, start, blevel, now):
        """每实例 (tok_inst) 统计; 切换点 t0(新数据首样本) 落在周期内时,
        [start, t0) 用旧表(全实例求和, 用户此前看到的记录) + [t0, now] 用每实例新口径。
        - 实例中途上线: 以它首样本为锚, 不计其上线前历史累计 (防污染)
        - 实例下线: 只计其存活期间增量, 不会把其余实例计数器当"假重启"重加
        - 进程重启: 负跳变按新段计 (与 _seg_total 一致)
        - 无数据返回 None → 回退旧表
        """
        conn = self._conn
        try:
            t0 = conn.execute("SELECT MIN(ts) FROM tok_inst").fetchone()[0]
            if t0 is None:
                return None  # 无新口径数据 → 旧表
            new_start = max(start, t0)
            ds = " AND CAST(ts AS INTEGER) % 300 < 20" if now - start > 40 * 86400 else ""
            rows = conn.execute(
                "SELECT url, ts, prompt_total, gen_total, requests_total FROM tok_inst"
                " WHERE ts >= ?" + ds + " ORDER BY url, ts", (new_start,)).fetchall()
            if not rows:
                return self._token_stats_legacy(period, start, blevel, now)
            if ds:  # 降采样可能漏掉最新样本 → 每实例补真正的末行
                for u in sorted({r0[0] for r0 in rows}):
                    last_row = conn.execute(
                        "SELECT url, ts, prompt_total, gen_total, requests_total FROM tok_inst"
                        " WHERE url = ? AND ts >= ? ORDER BY ts DESC LIMIT 1", (u, new_start)).fetchone()
                    if last_row:
                        mx = max(r0[1] for r0 in rows if r0[0] == u)
                        if last_row[1] > mx:
                            rows.append(last_row)
                rows.sort(key=lambda r0: (r0[0], r0[1]))
            by_url = {}
            for (u, ts, p, g, r) in rows:
                by_url.setdefault(u, []).append((ts, p, g, r))
            buckets = {}
            totals = [0.0, 0.0, 0.0]
            estimated = False
            anchor = None
            inst_out = {}
            for u, series in by_url.items():
                b = conn.execute(
                    "SELECT prompt_total, gen_total, requests_total FROM tok_inst"
                    " WHERE url = ? AND ts < ? ORDER BY ts DESC LIMIT 1", (u, new_start)).fetchone()
                base = (b[0], b[1], b[2]) if b else None
                has_base = base is not None
                p_tot, g_tot, r_tot, ub = self._bucketize(series, base, blevel)
                if not has_base:
                    estimated = True  # 窗口起点前无该实例采样 → 首样本为锚
                    f0 = series[0][0]
                    anchor = f0 if anchor is None else min(anchor, f0)
                totals[0] += p_tot; totals[1] += g_tot; totals[2] += r_tot
                for key, (p, g, r) in ub.items():
                    bb = buckets.setdefault(key, [0.0, 0.0, 0.0])
                    bb[0] += p; bb[1] += g; bb[2] += r
                inst_out[u] = {"prompt": int(round(p_tot)), "gen": int(round(g_tot)),
                               "requests": int(round(r_tot)), "since": series[0][0],
                               "estimated": not has_base}
            # 混合: 切换点在周期内 → 补旧表 [start, t0) 部分 (用户此前的记录)
            if t0 > start:
                lrows = conn.execute(
                    "SELECT ts, prompt_total, gen_total, requests_total FROM tokens"
                    " WHERE ts >= ? AND ts < ?" + ds + " ORDER BY ts", (start, t0)).fetchall()
                if ds:
                    lr = conn.execute(
                        "SELECT ts, prompt_total, gen_total, requests_total FROM tokens"
                        " WHERE ts >= ? AND ts < ? ORDER BY ts DESC LIMIT 1", (start, t0)).fetchone()
                    if lr and (not lrows or lr[0] > lrows[-1][0]):
                        lrows.append(lr)
                lb = conn.execute(
                    "SELECT prompt_total, gen_total, requests_total FROM tokens"
                    " WHERE ts < ? ORDER BY ts DESC LIMIT 1", (start,)).fetchone()
                lbase = (lb[0], lb[1], lb[2]) if lb else None
                if lrows:
                    lp, lg, lrq, lbk = self._bucketize(lrows, lbase, blevel)
                    totals[0] += lp; totals[1] += lg; totals[2] += lrq
                    for key, (p, g, r) in lbk.items():
                        bb = buckets.setdefault(key, [0.0, 0.0, 0.0])
                        bb[0] += p; bb[1] += g; bb[2] += r
                if not lbase:
                    estimated = True
                    anchor = start if anchor is None else min(anchor, start)
            fmt = {"hour": "%Y%m%d%H", "day": "%Y%m%d", "month": "%Y%m"}[blevel]
            label = {"hour": "%H:00", "day": "%m-%d", "month": "%m月"}[blevel]
            series_out = []
            for key in sorted(buckets):
                p, g, r = buckets[key]
                series_out.append({"t": key, "label": datetime.strptime(key, fmt).strftime(label),
                                   "prompt": int(round(p)), "gen": int(round(g)),
                                   "requests": int(round(r))})
            return {"period": period, "start": start, "ts": now, "estimated": estimated,
                    "anchor_ts": anchor if estimated else None,
                    "cutover": t0 if (t0 > start) else None,
                    "prompt_total": int(round(totals[0])), "gen_total": int(round(totals[1])),
                    "requests_total": int(round(totals[2])), "series": series_out,
                    "instances": inst_out}
        except Exception:
            return None

    def _token_stats_legacy(self, period, start, blevel, now):
        """旧口径: 全实例原始计数器求和 (仅新口径引入前的历史数据)。"""
        empty = {"period": period, "start": 0, "ts": now, "estimated": True,
                 "prompt_total": 0, "gen_total": 0, "requests_total": 0, "series": []}
        try:
            sql = ("SELECT ts, prompt_total, gen_total, requests_total FROM tokens"
                   " WHERE ts >= ?")
            if now - start > 40 * 86400:  # 长周期降采样到 ~5 分钟
                sql += " AND CAST(ts AS INTEGER) % 300 < 20"
            rows = self._conn.execute(sql + " ORDER BY ts", (start,)).fetchall()
            if "% 300" in sql:  # 降采样可能漏掉最新样本 → 补上真正的末行, 保证总量贴到最新
                last_row = self._conn.execute(
                    "SELECT ts, prompt_total, gen_total, requests_total FROM tokens"
                    " WHERE ts >= ? ORDER BY ts DESC LIMIT 1", (start,)).fetchone()
                if last_row and (not rows or last_row[0] > rows[-1][0]):
                    rows.append(last_row)
            base = None
            if rows:
                b = self._conn.execute(
                    "SELECT prompt_total, gen_total, requests_total FROM tokens"
                    " WHERE ts < ? ORDER BY ts DESC LIMIT 1", (start,)).fetchone()
                base = (b[0], b[1], b[2]) if b else None
        except Exception:
            return empty
        if not rows:
            return {**empty, "start": start}
        estimated = base is None  # 周期起点前无基线采样 → 以窗口首样本为锚, "监控启动以来"
        if base:
            p_tot = self._seg_total([base[0]] + [r[1] for r in rows])
            g_tot = self._seg_total([base[1]] + [r[2] for r in rows])
            r_tot = self._seg_total([base[2]] + [r[3] for r in rows])
        else:
            # 首样本是锚点(其值不计), 之后的增量累加; 重启负跳变按新段计
            p_tot = self._seg_total([r[1] for r in rows])
            g_tot = self._seg_total([r[2] for r in rows])
            r_tot = self._seg_total([r[3] for r in rows])
        fmt = {"hour": "%Y%m%d%H", "day": "%Y%m%d", "month": "%Y%m"}[blevel]
        buckets, last_before = {}, base
        for (ts, p, g, r) in rows:
            key = datetime.fromtimestamp(ts).strftime(fmt)
            b = buckets.setdefault(key, {"base": last_before, "p": [], "g": [], "r": []})
            b["p"].append(p); b["g"].append(g); b["r"].append(r)
            last_before = (p, g, r)
        label = {"hour": "%H:00", "day": "%m-%d", "month": "%m月"}[blevel]
        series = []
        for key in sorted(buckets):
            b = buckets[key]
            if b["base"]:
                p_t = self._seg_total([b["base"][0]] + b["p"])
                g_t = self._seg_total([b["base"][1]] + b["g"])
                r_t = self._seg_total([b["base"][2]] + b["r"])
            else:  # 无基线桶: 桶内首样本为锚, 桶内增量
                p_t = self._seg_total(b["p"])
                g_t = self._seg_total(b["g"])
                r_t = self._seg_total(b["r"])
            series.append({"t": key, "label": datetime.strptime(key, fmt).strftime(label),
                           "prompt": int(round(p_t)), "gen": int(round(g_t)),
                           "requests": int(round(r_t))})
        return {"period": period, "start": start, "ts": now, "estimated": estimated,
                "prompt_total": int(round(p_tot)), "gen_total": int(round(g_tot)),
                "requests_total": int(round(r_tot)), "series": series}

    def get_peak(self, key):
        with self._lock:
            row = self._conn.execute(
                "SELECT label, peak_decode, peak_decode_ts, peak_prefill, peak_prefill_ts FROM peak_speed WHERE key=?",
                (key,)).fetchone()
        if not row:
            return None
        return {"label": row[0], "decode": row[1], "decode_ts": row[2],
                "prefill": row[3], "prefill_ts": row[4]}

    def bump_peak(self, key, label, dec, dec_ts, pre, pre_ts, ts):
        """各取 max 更新; 同配置重启延续, 换配置各记各的"""
        old = self.get_peak(key)
        with self._lock:
            if not old:
                self._conn.execute(
                    "INSERT INTO peak_speed (key, label, peak_decode, peak_decode_ts,"
                    " peak_prefill, peak_prefill_ts, last_ts) VALUES (?,?,?,?,?,?,?)",
                    (key, label, dec, dec_ts, pre, pre_ts, ts))
            else:
                nd = max(old["decode"] or 0.0, dec or 0.0)
                nds = (dec_ts if (dec or 0.0) > (old["decode"] or 0.0) else old["decode_ts"])
                np_ = max(old["prefill"] or 0.0, pre or 0.0)
                nps = (pre_ts if (pre or 0.0) > (old["prefill"] or 0.0) else old["prefill_ts"])
                self._conn.execute(
                    "UPDATE peak_speed SET label=?, last_ts=?, peak_decode=?,"
                    " peak_decode_ts=?, peak_prefill=?, peak_prefill_ts=? WHERE key=?",
                    (label, ts, nd, nds, np_, nps, key))
            self._conn.commit()

    def prune(self, keep_seconds=86400):
        if not self._conn:
            return
        try:
            self._conn.execute("DELETE FROM trend WHERE ts < ?", (time.time() - keep_seconds,))
            self._conn.execute("DELETE FROM tokens WHERE ts < ?",
                               (time.time() - 730 * 86400,))  # token 计数留 2 年
            self._conn.execute("DELETE FROM tok_inst WHERE ts < ?",
                               (time.time() - 730 * 86400,))
            self._conn.commit()
        except Exception:
            pass

    def close(self):
        if self._conn:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# ----------------------------------------------------------------------------
# 主机信息 + 自动发现
# ----------------------------------------------------------------------------

def gather_host():
    d = {}
    d["hostname"] = socket.gethostname()
    d["arch"] = platform.machine()
    d["python"] = platform.python_version()
    d["cores"] = os.cpu_count()
    d["monitor_version"] = VERSION
    d["started"] = START_TIME
    try:
        for line in read_lines("/etc/os-release") or []:
            if line.startswith("PRETTY_NAME="):
                d["os"] = line.split("=", 1)[1].strip().strip('"')
                break
    except Exception:
        pass
    try:
        d["uptime_s"] = int(float(read_text("/proc/uptime").split()[0]))
    except Exception:
        pass
    return d


def probe_service(base, timeout=2):
    """探测某地址是否为 vLLM/OpenAI 服务。返回 (ok, kind)。"""
    for path in ("/v1/models", "/metrics", "/health"):
        try:
            with urlopen(Request(base.rstrip("/") + path), timeout=timeout) as r:
                if r.status == 200:
                    return True, path
        except Exception:
            continue
    return False, None


def probe_vllm_fast(base, timeout=3.0):
    """轻量重探：仅 /v1/models 200 即视为存活 vLLM 实例（周期重探测用，避免阻塞采集）"""
    try:
        with urlopen(Request(base.rstrip("/") + "/v1/models"), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def auto_discover_vllm(configured, extra_ports=tuple(range(8000, 8011)), host="127.0.0.1"):
    """合并已配置地址 + 自动探测常见端口上的服务。返回去重后的 URL 列表。"""
    urls = list(configured or [])
    have = {u.rstrip("/") for u in urls}
    for port in extra_ports:
        base = f"http://{host}:{port}"
        if base in have:
            continue
        ok, kind = probe_service(base)
        if ok:
            urls.append(base)
            have.add(base)
    return urls


# ----------------------------------------------------------------------------
# 引擎: 采集循环 + 快照
# ----------------------------------------------------------------------------

class Monitor:
    def __init__(self, config):
        self.config = config
        self.host = gather_host()
        self.vllm_urls = auto_discover_vllm(
            config.get("vllm", {}).get("urls"),
            config.get("vllm", {}).get("probe_ports", tuple(range(8000, 8011))),
            config.get("vllm", {}).get("host", "127.0.0.1"))
        self.collectors = []
        for cls in BUILTIN_COLLECTORS:
            c = cls()
            if c.detect():
                self.collectors.append(c)
        self._vllm_cfg = config.get("vllm", {})
        self._last_rediscover = 0.0
        self.vllm_collector = VLLMCollector(self.vllm_urls)
        self.thresholds = config.get("thresholds", {})
        self.interval = config.get("interval", 2)
        self.store = Store(
            history_seconds=config.get("history_seconds", 1800),
            interval=self.interval,
            db_path=config.get("db_path"),
            trend_interval=config.get("trend_interval", 15))
        self.snapshot = None
        self.last_error = None
        self._stop = threading.Event()
        self._thread = None

    def _maybe_rediscover(self):
        """每 10s 重探 vLLM 端口，动态跟随用户起停实例（双实例/四实例等方案切换）"""
        now = time.time()
        if now - self._last_rediscover < 10:
            return
        self._last_rediscover = now
        cfg = self._vllm_cfg
        urls = list(cfg.get("urls") or [])
        have = {u.rstrip("/") for u in urls}
        for port in cfg.get("probe_ports", tuple(range(8000, 8011))):
            base = "http://%s:%d" % (cfg.get("host", "127.0.0.1"), port)
            if base in have:
                continue
            if probe_vllm_fast(base):
                urls.append(base)
                have.add(base)
        if set(urls) != set(self.vllm_urls):
            self.vllm_urls = urls
            self.vllm_collector.urls = urls
            print("[vllm] rediscovered: %s" % ", ".join(urls), file=sys.stderr)

    def collect_once(self):
        self._maybe_rediscover()
        snap = {"ts": now_ts(), "host": self.host}
        for c in self.collectors:
            try:
                snap[c.name] = c.collect()
            except Exception as e:
                snap[c.name] = {"name": c.name, "available": False, "error": str(e)}
        try:
            snap["vllm"] = self.vllm_collector.collect()
            self._update_peaks(snap)
        except Exception as e:
            snap["vllm"] = {"name": "vllm", "available": False, "error": str(e),
                            "instances": []}
        snap["alerts"] = compute_alerts(snap, self.thresholds)
        return snap

    def _peak_key(self, inst):
        p = inst.get("proc") or {}
        return "%s|%s|tp%s|%s|s%s|mtp%s" % (
            _url_port(inst["url"]),
            p.get("served") or p.get("model") or "?",
            p.get("tp") or 0,
            p.get("max_len") or 0,
            p.get("seqs") or 0,
            p.get("mtp_tokens") or 0)

    def _update_peaks(self, snap):
        v = snap.get("vllm") or {}
        ts = time.time()
        overall = None
        for inst in v.get("instances", []):
            if not inst.get("online"):
                continue
            key = self._peak_key(inst)
            dec = inst.get("gen_toks_s") or 0.0
            pre = inst.get("prompt_toks_s") or 0.0
            label = ((inst.get("proc") or {}).get("short") or "") + " @%d" % _url_port(inst["url"])
            self.store.bump_peak(key, label, dec, ts if dec > 0 else None,
                                 pre, ts if pre > 0 else None, ts)
            pk = self.store.get_peak(key)
            if pk:
                pk["key"] = key
                inst["peak"] = pk
            cand = None
            if (dec or 0.0) > 0:
                cand = (dec, ts, "decode")
            if cand and (overall is None or cand[0] > overall[0]):
                overall = cand
        if not overall:
            # 空闲无实时样本时: 用各配置已存历史峰值中的最大者, 避免横幅消失
            for inst in v.get("instances", []):
                pk = inst.get("peak") or {}
                pd = pk.get("decode") or 0.0
                pt = pk.get("decode_ts")
                if pd > 0 and pt is not None and (overall is None or pd > overall[0]):
                    overall = (pd, pt, "decode")
        if overall and overall[2] == "decode":
            v["overall_peak"] = {"val": overall[0], "ts": overall[1],
                                 "kind": "decode"}
    def _loop(self):
        while not self._stop.is_set():
            t0 = now_ts()
            try:
                snap = self.collect_once()
                self.snapshot = snap
                self.last_error = None
                self.store.add(snap)
            except Exception as e:
                self.last_error = str(e)
                print(f"[loop] {e}", file=sys.stderr)
            elapsed = now_ts() - t0
            self._stop.wait(max(0.2, self.interval - elapsed))

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def detected(self):
        """返回自动识别报告。"""
        g = (self.snapshot or {}).get("gpu", {})
        report = {
            "host": self.host["hostname"],
            "gpus": [f"GPU{gg['index']} {gg['name']} {int(gg['mem_total_mib']/1024)}GB"
                     for gg in (g.get("gpus") or [])] if g.get("available") else [],
            "vllm": self.vllm_urls,
            "collectors": [c.name for c in self.collectors] + ["vllm"],
        }
        return report


# ----------------------------------------------------------------------------
# Web 服务
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    monitor = None
    dashboard_path = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        mon = self.monitor
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html", "/dashboard"):
                html = read_text(self.dashboard_path)
                if html is None:
                    return self._send(500, "dashboard.html missing", "text/plain")
                return self._send(200, html, "text/html; charset=utf-8")
            if path == "/api/status":
                if not mon.snapshot:
                    return self._json({"error": "no snapshot yet"}, 503)
                return self._json(mon.snapshot)
            if path == "/api/history":
                buf = mon.store.downsample(240)
                return self._json({"points": buf})
            if path == "/api/trend":
                try:
                    secs = int(self.path.split("?")[1].split("s=")[1].split("&")[0])
                except Exception:
                    secs = 3600
                return self._json({"points": mon.store.trend(secs)})
            if path == "/api/tokens":
                period = "day"
                if "?" in self.path:
                    for kv in self.path.split("?", 1)[1].split("&"):
                        if kv.startswith("period="):
                            period = kv.split("=", 1)[1]
                if period not in ("day", "week", "month", "year"):
                    period = "day"
                return self._json(mon.store.token_stats(period))
            if path == "/api/detected":
                return self._json(mon.detected())
            if path == "/api/config":
                return self._json(mon.config)
            if path == "/healthz":
                return self._json({"ok": True, "ts": now_ts()})
            return self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:
            return self._json({"error": str(e)}, 500)


def run_server(mon, host, port):
    Handler.monitor = mon
    Handler.dashboard_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "dashboard.html")
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def default_config(path):
    base = os.path.dirname(os.path.abspath(__file__))
    cfg = {
        "host": "0.0.0.0",
        "port": 8501,
        "interval": 2,
        "history_seconds": 1800,
        "trend_interval": 15,
        "db_path": os.path.join(base, "monitor.db"),
        "vllm": {
            "urls": ["http://127.0.0.1:8000"],
            "host": "127.0.0.1",
            "probe_ports": [8000, 8001, 8002, 8003, 8080, 8081],
        },
        "thresholds": {
            "gpu_util_pct": 95, "gpu_temp_c": 85, "gpu_mem_pct": 98,
            "mem_pct": 92, "disk_pct": 90,
            "vllm_waiting": 8, "vllm_kv_pct": 98, "vllm_preemption": 1,
        },
    }
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                user = json.load(f)
            for k, v in user.items():
                if k == "thresholds" and isinstance(v, dict):
                    cfg["thresholds"].update(v)
                elif k == "vllm" and isinstance(v, dict):
                    cfg["vllm"].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"[config] 读取 {path} 失败, 用默认: {e}", file=sys.stderr)
    return cfg


def main():
    ap = argparse.ArgumentParser(description=f"{PROG} v{VERSION} — 本地 AI 部署资源监控")
    ap.add_argument("--config", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--interval", type=float, default=None)
    ap.add_argument("--once", action="store_true", help="只采集一次并打印 JSON 后退出")
    ap.add_argument("--detect", action="store_true", help="只打印自动识别报告后退出")
    args = ap.parse_args()

    cfg = default_config(args.config)
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port
    if args.interval:
        cfg["interval"] = args.interval

    mon = Monitor(cfg)
    mon.snapshot = mon.collect_once()  # 预热, 让快照与识别报告立即可用

    if args.detect:
        print(json.dumps(mon.detected(), ensure_ascii=False, indent=2))
        return

    if args.once:
        print(json.dumps(mon.snapshot, ensure_ascii=False, indent=2))
        return

    srv = run_server(mon, cfg["host"], cfg["port"])
    mon.start()
    det = mon.detected()
    print(f"[{PROG}] v{VERSION} 监听 http://{cfg['host']}:{cfg['port']}  "
          f"采集间隔 {cfg['interval']}s  vLLM={mon.vllm_urls}", file=sys.stderr)
    print(f"[{PROG}] 自动识别: GPU x{len(det['gpus'])}  采集器={det['collectors']}",
          file=sys.stderr)
    for g in det["gpus"]:
        print(f"[{PROG}]   {g}", file=sys.stderr)

    pidfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.pid")
    try:
        with open(pidfile, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    def _sig(s, f):
        # 抛 SystemExit 打断 serve_forever, 走 finally 清理 (避免同线程调 shutdown 死锁)
        print("\n[monitor] 收到退出信号, 正在停止...", file=sys.stderr)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    try:
        srv.serve_forever(poll_interval=0.5)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        mon.stop()
        mon.store.close()
        try:
            srv.server_close()
        except Exception:
            pass
        print(f"[monitor] 已退出", file=sys.stderr)


if __name__ == "__main__":
    main()
