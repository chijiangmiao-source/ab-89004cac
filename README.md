# 隔离控制协议迁移裁决系统

在隔离控制协议升级时，对“正在执行的会话能否安全迁入新规程”作出持久化裁决。
纯 Python 标准库实现（无第三方运行时依赖），含 Web 页面、HTTP API、持久化裁决库，
以及 Compose 内一次性 `verify` 验收服务。

## 它保证什么

1. **最大双向匹配（greatest bisimulation），而非只比起始状态**
   对每个仍匹配的旧/新状态对：
   - 可见输出必须相同；
   - 允许动作集合必须相同（同名动作两侧同时存在，缺一即动作缺失）；
   - 对每个同名动作，后继对仍须匹配。

   从每个冻结会话的 `(旧当前状态, 新目标状态)` 种子对出发，在可达状态对空间内
   划分细化到不动点。种子落在关系内才判可迁入，并给出新规程目标状态；
   否则给出**首个阻断路径**（沿动作下钻到第一个输出/动作签名冲突点）。

2. **全部冻结会话可映射才能发布** —— 一个会话阻断，候选整体禁止发布。

3. **同一持久化裁决绑定**：冻结时把旧/新规程摘要（含 SHA-256）、会话版本与快照、
   映射证据、阶段 `FROZEN` 写入 SQLite 同一行；发布只是在同一事务把该行
   `FROZEN → PUBLISHED`，不存在“半发布”。

4. **竞争/重传不混用快照**：同一 `migration_id` 绑定一个把标识、旧新规程、会话
   快照全部纳入的 SHA-256；同标识换载荷重传一律 `409 snapshot_mismatch`，
   幂等重传（同哈希）返回同一结论。

5. **中断恢复**：进程启动时把所有残留 `FROZEN` 回滚为未发布；`PUBLISHED` 作为
   唯一已发布结论原样保留——冻结后或切换前崩溃、重开，结论唯一且确定。

6. **可配置宿主端口与健康状态**：页面经 `http://宿主:${APP_PORT:-8080}/` 访问，
   页头每 5 秒轮询 `/health` 并显示冻结/已发布计数。

## 目录

```
app/core/models.py        规程/会话模型与校验（确定性、≤12 会话）
app/core/bisimulation.py  最大双向匹配 + 首个阻断路径取证
app/core/store.py         SQLite 两阶段裁决、竞争冲突、启动恢复
app/core/service.py       摘要/快照哈希/冻结发布编排
app/server.py             标准库 HTTP 服务（HOST/PORT/DB 可配）
app/web/index.html        录入与裁决页面（健康状态、目标状态/阻断路径）
tests/                    34 项：规则、非仅起始比较、陈旧快照、竞争、恢复、HTTP
verify/run_verify.py      Compose 内一次性验收（退出码报告）
Dockerfile / docker-compose.yml
```

## 本地运行

```bash
# 无 Docker 时可直接运行（仅需 Python 3.11）
mkdir -p data
PORT=8080 DB=data/decisions.db python3 -m app.server
# 浏览器打开 http://localhost:8080/
```

页面上可直接“填充示例：可迁移 / 动作缺失”，选择稳定迁移标识后
**发起裁决（冻结）**，可发布时再 **确认发布切换**；也可按标识重查。

## Compose 部署与验收

```bash
# 可配置宿主端口
APP_PORT=9090 docker compose up --build
# 只运行一次性验收（完成一次后以退出码报告，容器退出不重启）
docker compose build
docker compose run --rm verify
```

`verify` 服务等待 app 健康后依次执行：构建检查（compileall）→
规则测试（unittest 全套）→ 对运行 app 的 API/HTTP 冒烟
（可迁移 / 动作缺失 / 并发陈旧快照）→ 真实子进程重开的中断恢复演练，
**全部通过退出 0，任一失败退出 1**。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 健康状态与裁决计数 |
| GET  | `/` | 裁决页面 |
| POST | `/api/decisions/freeze` | 冻结裁决（见下） |
| POST | `/api/decisions/{id}/publish` | 发布，需带冻结时的 `snapshot_hash` |
| GET  | `/api/decisions/{id}` | 查询唯一结论 |
| GET  | `/api/decisions` | 裁决列表 |

```jsonc
// POST /api/decisions/freeze
{
  "migration_id": "cutover-2026-10-07-a",
  "old_spec": { "name":"isol", "version":"v1", "start":"LOCKED",
    "states": { "LOCKED": {"output":"红灯", "actions":{"unlock":"OPEN"}} } },
  "new_spec": { "name":"isol", "version":"v2", "start":"LOCKED", "states": { } },
  "sessions": [
    { "session_id":"S1", "spec_version":"v1", "state":"LOCKED",
      "target_state":"LOCKED",          // 可省略，默认同名
      "observed_output":"红灯" }         // 冻结时现场可见输出
  ]
}
```

错误码：`400 bad_request`（含陈旧快照/快照版本不符）、
`404 not_found`、`409 snapshot_mismatch | not_frozen | not_publishable`。

## 关键语义说明

- **为什么可达性也要查**：即使某会话当前所在状态两侧签名一致，只要它能沿某个
  允许动作到达不匹配后继，下一条动作就可能放大设备权限——因此该会话仍判阻断。
- **为什么默认同名映射**：会话迁入新规程时落到与其当前状态对应的状态；状态改名
  时可在会话里显式给 `target_state`。
- **陈旧快照如何识别**：冻结瞬间比对会话报告的 `observed_output` 与旧规程该状态
  可见输出、`spec_version` 与旧规程版本，不一致直接拒绝（400）。
