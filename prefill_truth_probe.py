#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ground-truth prefill/decode rate probe — verify the panel's speed numbers.

Why this exists
---------------
The monitor derives vLLM speeds from `/metrics` counters. Two independent traps
make a naive derivation wrong, and both were observed in production:

1. `vllm:prompt_tokens_total` counts tokens served from the prefix cache as if
   they had been recomputed. A request that reused 95% of a 100K prompt therefore
   lands ~100K "prefill" tokens in one poll window. Observed bogus peak:
   50,107 tok/s on two V100s (physical ceiling is ~14,500).

2. `prompt_tokens_total` and `generation_tokens_total` advance only when a
   request *finishes*. Dividing a whole request's tokens by the 2 s poll window
   invents a rate. Observed: an 11,736-token prompt read as 5,868 tok/s when its
   real prefill took 6.53 s (~1,800 tok/s).

The monitor therefore uses ratio-of-sums:
    prefill = (d(prompt_tokens) - d(prefix_cache_hits)) / d(time_to_first_token_seconds_sum)
    decode  =  d(generation_tokens)                    / d(inter_token_latency_seconds_sum)
Both numerator and denominator are summed counters, so this is exact for one
request and still exact when summed across concurrent ones.

This probe measures the same thing client-side (real TTFT with `stream=true`,
plus `usage` counts) and prints the counter deltas side by side, so the
derivation can be checked rather than trusted.

Usage: python3 prefill_truth_probe.py [vllm_port] [model] [panel_port]
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request

UNIT = "供应链与库存周转的案例分析，包含季度数据与结论。"

COUNTERS = (
    "vllm:prompt_tokens_total",
    "vllm:prefix_cache_hits_total",
    "vllm:time_to_first_token_seconds_sum",
    "vllm:time_to_first_token_seconds_count",
    "vllm:generation_tokens_total",
    "vllm:inter_token_latency_seconds_sum",
    "vllm:inter_token_latency_seconds_count",
)


def metrics(port):
    txt = subprocess.run(["curl", "-s", "--max-time", "5",
                          "http://127.0.0.1:%d/metrics" % port],
                         capture_output=True, text=True).stdout
    out = {}
    for line in txt.splitlines():
        for key in COUNTERS:
            if line.startswith(key + "{"):
                out[key] = float(line.split()[-1])
                break
    return out


def panel(panel_port):
    """What the monitor currently believes, for the same instance."""
    try:
        d = json.load(urllib.request.urlopen(
            "http://127.0.0.1:%d/api/status" % panel_port, timeout=5))
        for i in (d.get("vllm") or {}).get("instances") or []:
            if str(i.get("url") or "").endswith(":%d" % port):
                return i.get("prompt_toks_s"), i.get("gen_toks_s")
    except Exception as exc:
        return "err:%s" % exc, None
    return None, None


def run(port, model, panel_port, reps):
    prompt = "背景资料：" + UNIT * reps + "\n\n请用三句话总结上面的内容。"
    body = {"model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 40, "temperature": 1.0, "top_p": 0.95, "top_k": 20,
            "stream": True, "stream_options": {"include_usage": True}}
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % port,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})

    before = metrics(port)
    t0 = time.time()
    ttft = None
    usage = {}
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            choices = obj.get("choices") or [{}]
            if ttft is None and choices[0].get("delta", {}).get("content") is not None:
                ttft = time.time() - t0

    after = metrics(port)
    pt = usage.get("prompt_tokens", 0)
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    new = pt - cached
    gen = usage.get("completion_tokens", 0)

    d_pt = after.get("vllm:prompt_tokens_total", 0) - before.get("vllm:prompt_tokens_total", 0)
    d_hit = after.get("vllm:prefix_cache_hits_total", 0) - before.get("vllm:prefix_cache_hits_total", 0)
    d_ttft = after.get("vllm:time_to_first_token_seconds_sum", 0) - before.get("vllm:time_to_first_token_seconds_sum", 0)
    d_gen = after.get("vllm:generation_tokens_total", 0) - before.get("vllm:generation_tokens_total", 0)
    d_itl = after.get("vllm:inter_token_latency_seconds_sum", 0) - before.get("vllm:inter_token_latency_seconds_sum", 0)

    print("reps=%d  prompt_tokens=%d  cached=%d  new=%d  completion=%d"
          % (reps, pt, cached, new, gen))
    print("  GROUND TRUTH  TTFT=%.3fs -> prefill %.0f tok/s" % (
        ttft or 0, new / ttft if ttft else 0))
    print("  MONITOR FORMULA  (d(prompt)-d(hits))/d(ttft_sum) = (%.0f-%.0f)/%.3f = %.0f tok/s" % (
        d_pt, d_hit, d_ttft, (d_pt - d_hit) / d_ttft if d_ttft else 0))
    print("  WRONG FORMULA (delta/poll-window) would read ~%.0f tok/s over a 2 s window" % (
        (d_pt - d_hit) / 2.0,))
    print("  decode: d(gen)/d(itl_sum) = %.0f/%.3f = %.0f tok/s" % (
        d_gen, d_itl, d_gen / d_itl if d_itl else 0))
    print("  panel live reading: prompt_toks_s=%s gen_toks_s=%s" % panel(panel_port))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("port", type=int, nargs="?", default=8001)
    ap.add_argument("model", nargs="?", default="qwen3.8-27b-quasar-nvfp4")
    ap.add_argument("panel_port", type=int, nargs="?", default=8501)
    a = ap.parse_args()
    port, model, panel_port = a.port, a.model, a.panel_port
    for reps in (300, 900):
        run(port, model, panel_port, reps)
        time.sleep(6)
