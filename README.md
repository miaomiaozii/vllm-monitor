# vLLM Monitor — 本地 AI 部署资源监控

轻量、零依赖(纯 Python 标准库)的本地 AI 部署资源监控程序。自动识别硬件与
vLLM 服务, 提供 Web 面板 + JSON API + 告警。为"跑本地部署时能看到的东西"
而设计, 并支持硬件升级后无缝扩展。

## 部署位置
- 程序目录: `/home/firefly-ai/AI/monitor/`
- 服务: systemd `vllm-monitor.service` (开机自启 + 崩溃自动拉起)
- 面板: `http://<服务器IP>:8501`
- 日志: `journalctl -u vllm-monitor -f`

## 日常控制
```bash
cd /home/firefly-ai/AI/monitor
./monitor.sh status     # 服务状态 + 自动识别 + 面板地址
./monitor.sh start      # 启动
./monitor.sh stop       # 停止
./monitor.sh restart    # 重启
./monitor.sh log        # 跟踪日志
./monitor.sh open       # 打开面板
```
桌面快捷方式(已在桌面):
- `打开 vLLM 监控面板` — 浏览器打开面板
- `vLLM 监控管理` — status/restart/stop/打开面板
- `vLLM 监控日志` — 跟踪日志

## 监控项(自动识别)
| 类别 | 指标 |
|------|------|
| GPU (nvidia-smi) | 每张卡: 利用率 / 显存(已用/总量) / 温度 / 功率 / 核心频率 / 风扇 / ECC |
| CPU (/proc) | 总利用率 / 每核利用率 / 1/5/15m 负载 / 核数 |
| 内存 (/proc) | 已用/总量/可用 / 缓存 / Swap |
| 磁盘 | 每个挂载点 用量 / 读写速率 |
| 网络 (/proc) | 每个接口 上行/下行速率 + 累计 |
| vLLM (/metrics) | 模型名 / 在线 / 运行/排队数 / KV 缓存 / 抢占 / 生成吞吐(tok/s) / TTFT / ITL / 前缀缓存命中 / 视觉缓存命中 / MTP 接受率(按位置) |
| Strata (/metrics) | 模型名 / 运行排队数 / KV 用量 / 抢占 / 预填与解码吞吐(tok/s) / TTFT / ITL / MTP 接受率 / 前缀缓存命中 / 对话缓存 / 每卡显存与利用率 |
| Token 统计 (SQLite) | vLLM 累计计数器 每 15s 采样, 按 日/周/月/年 聚合: 输入(prompt) / 输出(generation) / 请求数 / 每请求平均; 服务重启归零自动按段累加; 数据保留 2 年 |

告警(阈值见 config.json): GPU 利用率/温度/显存/ECC、内存、磁盘、
vLLM 离线/排队/KV 满/抢占。

## 自动识别
- GPU: 从 `nvidia-smi` 动态枚举, 数量/型号变化自动适应
- vLLM: 扫描常见端口(8000/8001/8080/8081) + config.json 指定地址;
  模型名从 `/metrics` 的 `model_name` 标签动态读取(不写死)
- 引擎类型: 按 `/metrics` 响应首字符自动判别 — `{` 走 JSON(Strata), 否则走
  Prometheus 文本(vLLM), 同一端口无需手工配置
- 采集器: 每个采集器有 `detect()`, 环境不具备时自动跳过(如 Windows 无 /proc)

## 可扩展性(加新采集器)
在 `vllm_monitor.py` 中:
1. 写一个类继承 `Collector`, 实现 `name` / `detect()` / `collect() -> dict`
   - `collect()` 返回的 dict 建议含 `available: bool` 和具体指标
2. 加入 `BUILTIN_COLLECTORS` 列表
3. (可选) 在 `compute_alerts()` 加告警规则
4. (可选) 在 `dashboard.html` 的 `tick()` 加渲染函数
新采集器会自动出现在 `--detect` 报告和面板里, 无需改动采集循环。

## 文件
```
vllm_monitor.py       # 主程序(采集 + 告警 + 存储 + Web + API)
prefill_truth_probe.py # 速度口径验证工具(客户端实测 vs 面板读数)
dashboard.html        # Web 面板(内联 CSS/JS, 无 CDN 依赖)
config.json           # 配置(端口/间隔/阈值/vLLM 地址)
monitor.sh            # 控制脚本(systemd 版)
vllm-monitor.service  # systemd 单元
monitor.db            # 长期趋势数据(SQLite, 自动保留 24h)
monitor.pid / .log    # 运行时文件
```

## JSON API
| 端点 | 说明 |
|------|------|
| `GET /` | Web 面板 |
| `GET /api/status` | 当前完整快照 |
| `GET /api/history?s=60` | 近 N 秒历史(降采样) |
| `GET /api/trend?s=3600` | 长期趋势(SQLite, 默认 1h) |
| `GET /api/tokens?period=day` | Token 统计, period=day/week/month/year; 返回周期总量 + 分桶序列(日=小时桶/周月=日桶/年=月桶), estimated=true 表示周期起点前无基线采样(总量为累计值近似) |
| `GET /api/detected` | 自动识别报告(主机/GPU/vLLM/采集器) |
| `GET /api/config` | 当前配置 |
| `GET /healthz` | 健康检查 |

## 配置 (config.json)
```jsonc
{
  "host": "0.0.0.0", "port": 8501,     // 监听
  "interval": 2,                        // 采集间隔(秒)
  "history_seconds": 1800,              // 内存历史窗口
  "trend_interval": 15,                 // 趋势采样间隔
  "vllm": { "urls": ["http://127.0.0.1:8000"], "probe_ports": [8000,8001,8080,8081] },
  "thresholds": { "gpu_util_pct":95, "gpu_temp_c":85, "gpu_mem_pct":99,
                  "mem_pct":92, "disk_pct":90,
                  "vllm_waiting":8, "vllm_kv_pct":98, "vllm_preemption":1 }
}
```
注意: `gpu_mem_pct` 设为 99 — vLLM 会按 `gpu-memory-utilization` 预分配
KV cache 到 ~95-98% 显存, 属正常, 只在接近 OOM(99%) 时告警。

## Strata 引擎支持
Strata 的 `/metrics` 是 **JSON**, 不是 Prometheus 文本, 采集器按首字符自动分流。
字段映射(实测于 v0.1.43.1):

| 面板指标 | Strata JSON 路径 |
|---|---|
| 模型名 | `hardware_static.model_name` |
| 运行/排队 | `live.running` / `live.queued` |
| KV 用量 | `live.kv_usage` (0-1) |
| 解码 tok/s | `live.tok_s`, 空闲时回落 `requests[0].decode_tok_s` |
| 预填 tok/s | `live.prefill_tok_s_mean` (已扣前缀复用) |
| TTFT / ITL | `requests[0].ttft_ms` / `requests[0].itl_ms` |
| MTP 接受率 | 对 `requests[].draft_accept_rate` 按 draft/accepted 加权求和, 再回落 `totals.mtp_accept_rate` |
| 前缀命中 | `totals.reused_tokens / prompt_tokens` |
| 每卡显存 | `hardware_static.gpu_mem_total_mib` − `hardware.gpu_mem_used_mib[]` |

两个必须知道的坑:
1. **`totals` 只在请求完成时提交**。请求在飞时 `totals` 是上一个请求的旧值,
   所以 counter 差分对 Strata 无效 — 必须读 `live.*` / `requests[]`。
2. **`requests[].hit_rate` 是 MoE 专家缓存命中率, 不是 MTP 接受率**。两者数值
   都在 60-99% 区间, 极易混淆(曾把 61% 的专家命中率当成 MTP)。

## 速度指标的口径(重要)
朴素算法 `delta(counter) / 采集间隔` 在两种引擎上都会出错, 且都在线上出现过:

1. `vllm:prompt_tokens_total` 把**前缀缓存命中**的 token 也算成已重算。一个复用
   95% 的 10 万 token 请求会在一个轮询窗口里落下 10 万"预填"token。实测虚高峰值
   **50,107 tok/s**(两张 V100 物理上限约 14,500)。
2. `prompt_tokens_total` 与 `generation_tokens_total` **只在请求结束时累加**。用
   整个请求的 token 除以 2 秒窗口等于凭空造速率: 实测一个 11,736 token 的请求
   被读成 5,868 tok/s, 而它真实预填用了 6.53 秒(≈1,800 tok/s)。

因此改用**和之比**(分子分母都是累加量, 单请求精确, 并发下依然精确):
```
预填 = (d(prompt_tokens) − d(prefix_cache_hits)) / d(time_to_first_token_seconds_sum)
解码 =  d(generation_tokens)                     / d(inter_token_latency_seconds_sum)
```
注: TTFT 含排队时间, 所以 `prompt_toks_s` 是**用户视角的有效预填速率**, 不是纯
kernel 速率。Strata 侧同理, 预填已用 `prompt_tokens − reused` 扣除复用。

`prefill_truth_probe.py` 用客户端 `stream=true` 实测 TTFT 与 `usage` 计数, 并把
计数器差分并排打印, 用来**验证**上述公式而不是盲信面板:
```bash
python3 prefill_truth_probe.py [vllm_port] [model] [panel_port]   # 默认 8001 / <model> / 8501
```

## 卸载
```bash
sudo systemctl disable --now vllm-monitor
sudo rm /etc/systemd/system/vllm-monitor.service /etc/sudoers.d/vllm-monitor
sudo systemctl daemon-reload
rm -rf /home/firefly-ai/AI/monitor
```
