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
| Token 统计 (SQLite) | vLLM 累计计数器 每 15s 采样, 按 日/周/月/年 聚合: 输入(prompt) / 输出(generation) / 请求数 / 每请求平均; 服务重启归零自动按段累加; 数据保留 2 年 |

告警(阈值见 config.json): GPU 利用率/温度/显存/ECC、内存、磁盘、
vLLM 离线/排队/KV 满/抢占。

## 自动识别
- GPU: 从 `nvidia-smi` 动态枚举, 数量/型号变化自动适应
- vLLM: 扫描常见端口(8000/8001/8080/8081) + config.json 指定地址;
  模型名从 `/metrics` 的 `model_name` 标签动态读取(不写死)
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

## 卸载
```bash
sudo systemctl disable --now vllm-monitor
sudo rm /etc/systemd/system/vllm-monitor.service /etc/sudoers.d/vllm-monitor
sudo systemctl daemon-reload
rm -rf /home/firefly-ai/AI/monitor
```
