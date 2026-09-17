# Local PEP 440 Dependency Resolver

一个完全本地化、确定性的依赖版本解析服务。使用 **Python + FastAPI + packaging**
实现，依据请求中自带的包版本清单（无需 PyPI、无需网络、无需账号）解析出全部
传递依赖。

## 特性

- **PEP 440 版本语义**：由 [`packaging`](https://packaging.pypa.io/) 提供版本号
  解析、排序与 `>=`、`==`、`<`、`~=`、`!=` 等说明符匹配；兼容 epoch、post、dev
  等版本形式。
- **传递依赖解析**：递归处理每个选定版本声明的依赖，支持依赖图中的环。
- **锁定优先**：`locks` 中给出的版本只要仍满足所有约束，就会被优先保留；
  锁定版本被更深层依赖证伪时，才按规则回溯到其他版本，返回结果里会说明每个锁的
  处置（`applied` / `backtracked` / `unsatisfiable` / `missing-version` /
  `not-found` / `unused`）。
- **确定性回溯**：其余候选严格按版本**从高到低**尝试；候选顺序、依赖处理顺序、
  根需求顺序均为确定顺序，相同输入永远得到相同输出（含尝试次数）。
- **默认排除预发布版**：`a/b/rc/dev` 等预发布默认不参与；可通过
  `allow_prereleases=true` 全局放开，或由显式预发布钉版（如 `==2.0rc1`）、
  对该包的锁自动放开。
- **可操作的冲突报告**：无解时返回 409，并给出：
  - 冲突包名与原因（`unsatisfiable` / `not-found` / `no-versions`）；
  - 互相矛盾的约束（说明符、提出者及其版本）；
  - 每条约束完整的**依赖路径**（从根一路到提出约束的包）；
  - 候选版本清单与已尝试版本，以及整棵失败分支树，而不是一句笼统错误。
- **PEP 508**：依赖可写成 `name[extra] spec; marker` 字符串；环境标记在固定的
  默认环境下求值（可用 `marker_env` 覆盖），保证结果可复现。
- **同步与异步两种用法**：`POST /resolve` 同步返回解析结果；`POST /jobs` 把同样的
  请求体持久化为作业，由后台 worker 领取执行，`GET /jobs/{id}` 轮询状态与最终响应，
  `DELETE /jobs/{id}` 可取消作业。
- **SQLite 持久化与崩溃恢复**：作业（请求、状态、最终响应、取消标记）保存在
  SQLite 中，Docker Compose 通过命名卷持久化；进程重启后，上次遗留的 `running`
  作业会自动退回 `queued` 重新执行。

## 一条命令启动

需要 Docker（含 Compose v2）。在项目根目录执行：

```bash
docker compose up --build
```

服务监听 **http://localhost:8000**：

- `GET /health` — 健康检查
- `POST /resolve` — 同步解析依赖
- `POST /jobs` — 创建异步解析作业（202 Accepted）
- `GET /jobs/{id}` — 查询作业状态与最终响应
- `DELETE /jobs/{id}` — 取消作业
- `GET /docs` — 交互式 OpenAPI 文档（Swagger UI）

镜像在构建时安装固定版本依赖；运行时不访问任何外部仓库。

### 数据持久化

异步作业保存在容器内 `/data/resolver.db`（SQLite，WAL 模式）。Compose 已把命名卷
`resolver-data` 挂载到 `/data`，因此 `docker compose down` 乃至
`docker compose up --build` 重建容器后，作业历史与结果仍然保留；如需清空，执行
`docker compose down -v`。

## 请求格式

```json
{
  "packages": {
    "<包名>": {
      "<PEP440版本>": {
        "dependencies": { "<依赖包名>": "<版本说明符>" }
      }
    }
  },
  "root": { "<包名>": "<版本说明符>" },
  "locks": { "<包名>": "<锁定版本>" },
  "allow_prereleases": false,
  "marker_env": { "python_version": "3.11" }
}
```

- `packages`：本地包索引，包名 → 版本 → 条目。`dependencies` 可以是
  `{包名: 说明符}` 映射，也可以是 PEP 508 字符串数组，例如
  `["requests>=2.0", "tomli; python_version < '3.11'"]`。
- `root`：根依赖，映射或 PEP 508 字符串数组均可。
- `locks`：可选锁定版本。
- `allow_prereleases`：可选，默认 `false`。
- `marker_env`：可选，覆盖标记求值环境。
- `max_attempts`：可选，候选求值上限（默认 100000）。

### 成功响应（200）

```bash
curl -s -X POST localhost:8000/resolve -H 'Content-Type: application/json' -d '{
  "packages": {
    "app": {"1.0": {"dependencies": {"web": ">=2.0", "db": ""}}},
    "web": {"2.0": {"dependencies": {"common": "<2.0"}},
            "3.0": {"dependencies": {"common": ">=2.0"}}},
    "db":  {"1.0": {"dependencies": {"common": "==1.5"}}},
    "common": {"1.5": {}, "2.0": {}}
  },
  "root": {"app": ""}
}'
```

`web 3.0` 要求 `common>=2.0`，与 `db` 的 `common==1.5` 冲突，于是回溯到
`web 2.0`：

```json
{
  "status": "ok",
  "attempts": 5,
  "resolved": {"app": "1.0", "common": "1.5", "db": "1.0", "web": "2.0"},
  "dependency_graph": [ ... ],
  "locks": {}
}
```

### 锁定优先

```json
{ "packages": {"x": {"1.0": {}, "2.0": {}}},
  "root": {"x": ">=1.0"},
  "locks": {"x": "1.0"} }
```

只要锁仍满足约束，`x` 解析为 `1.0`（而非最高的 `2.0`），`locks.x = "applied"`。

### 冲突响应（409）

```json
{
  "packages": {
    "service-a": {"1.0": {"dependencies": {"libfoo": ">=2.0"}}},
    "service-b": {"1.0": {"dependencies": {"libfoo": "<2.0"}}},
    "libfoo": {"1.0": {}, "2.0": {}, "3.0": {}}
  },
  "root": ["service-a", "service-b"]
}
```

返回（节选）：

```json
{
  "status": "conflict",
  "conflict": {
    "terminal_conflicts": [
      {
        "package": "libfoo",
        "reason": "unsatisfiable",
        "available_versions": ["3.0", "2.0", "1.0"],
        "active_constraints": [
          {
            "requirement": "libfoo>=2.0",
            "required_by": "service-a",
            "required_by_version": "1.0",
            "path": [{"name": "<root>"}, {"name": "service-a", "version": "1.0"}]
          },
          {
            "requirement": "libfoo<2.0",
            "required_by": "service-b",
            "required_by_version": "1.0",
            "path": [{"name": "<root>"}, {"name": "service-b", "version": "1.0"}]
          }
        ]
      }
    ],
    "tree": { "...": "完整的失败分支树（含每步尝试的版本）..." }
  }
}
```

错误请求（非 PEP 440 版本、非法说明符、多余字段等）返回 `400/422` 并附带原因。

## 异步解析作业 API

`POST /jobs` 的请求体与 `POST /resolve` **完全一致**；服务只做 JSON 解析并把请求
原样持久化，立即返回 `202 Accepted`：

```bash
curl -s -X POST localhost:8000/jobs -H 'Content-Type: application/json' -d '{
  "packages": {
    "app": {"1.0": {"dependencies": {"web": ">=2.0", "db": ""}}},
    "web": {"2.0": {"dependencies": {"common": "<2.0"}},
            "3.0": {"dependencies": {"common": ">=2.0"}}},
    "db":  {"1.0": {"dependencies": {"common": "==1.5"}}},
    "common": {"1.5": {}, "2.0": {}}
  },
  "root": {"app": ""}
}'
```

```json
{
  "id": "1",
  "status": "queued",
  "request": { "...": "提交的原始请求..." },
  "cancel_requested": false,
  "created_at": "2026-09-17T10:00:00+00:00",
  "started_at": null,
  "finished_at": null
}
```

### 查询作业：`GET /jobs/{id}`

状态机为：

```
queued ──领取──▶ running ──成功──▶ succeeded
   │                ├──冲突/限制/请求非法──▶ failed
   │                └──取消──▶ cancelled
   └────取消──────▶ cancelled
```

- `queued` / `running`：作业等待中或执行中，不含 `response` 字段。
- `succeeded`：解析成功，`response_status` 为 `200`，`response` 即 `/resolve`
  成功时的完整响应体。
- `failed`：解析完成但结果是 `409 conflict` / `409 limit`，或请求校验失败
  （`400`）；HTTP 状态码保存在 `response_status`，响应体保存在 `response`。
  异步接口本身始终可以查询，因此不会把 409 直接作为 `GET /jobs/{id}` 的状态码。
- `cancelled`：已取消，无 `response`。
- 作业不存在返回 `404`。

```json
{
  "id": "1",
  "status": "succeeded",
  "request": { "...": "..." },
  "cancel_requested": false,
  "created_at": "...",
  "started_at": "...",
  "finished_at": "...",
  "response_status": 200,
  "response": { "status": "ok", "resolved": {"...": "..." }, "...": "..." }
}
```

### 取消作业：`DELETE /jobs/{id}`

- 取消 **queued** 作业：在一个 `BEGIN IMMEDIATE` 事务内原子地立即转为
  `cancelled`，返回 `200`。
- 取消 **running** 作业：置上持久化的取消标记并返回 `202 Accepted`；求解器在
  **候选求值边界**（每尝试一个版本之前）协作式检查该标记，尽快停止，作业最终变为
  `cancelled`。停止发生在两个候选之间，因此不会留下半成品解析结果。
- **终态不可覆盖**：`succeeded` / `failed` / `cancelled` 作业的取消请求返回
  `409 Conflict`，其 `response` 与状态保持不变；worker 完成作业时的写回也带有
  `WHERE status = 'running'` 守卫，与取消竞争时若取消先落库则结果被丢弃。
- 作业不存在返回 `404`。

### 领取顺序与并发安全

后台 worker 以 FIFO（最早入队优先，按自增 id）领取作业。领取在单个
`BEGIN IMMEDIATE` 事务中完成「取最早 queued 行 + 置 running」，SQLite 的写锁保证
多个 worker（甚至多个共享同一数据库文件的进程）不会重复领取同一作业。

### 重启恢复

worker 领取作业时立即把状态持久化为 `running`。进程崩溃或容器重启后，启动流程会
把所有遗留的 `running` 作业退回 `queued` 并清空开始时间，随后被重新领取执行。
若取消标记在崩溃前已经落库，退回后领取该作业时会直接终态化为 `cancelled`
（取消粘性）。

### 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RESOLVER_DB_PATH` | `data/resolver.db`（容器内 `/data/resolver.db`） | SQLite 数据库路径 |
| `JOB_WORKERS` | `1` | 并发 worker 数 |
| `JOB_POLL_INTERVAL` | `0.1` | 无作业时的轮询/唤醒间隔（秒） |

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
pip install pytest httpx
pytest -q
```

## 解析策略说明

- 每个包维护当前所有生效约束的交集；候选必须同时满足全部约束。
- 搜索采用带精确撤销日志的深度优先回溯：选定一个版本后施加其依赖并递归；
  更深层失败时，按逆序撤销该候选引入的全部约束、边与发现标记，再尝试下一候选。
- 求解器仅做“按时间顺序回溯”，保证完备：有解时一定能找到；无解时一定报告。
- 为防止病态输入无限膨胀，`max_attempts` 触顶会返回 `status: "limit"`（仍带
  结构化冲突信息）。
- 异步作业的取消是**协作式**的：求解器在每次候选求值前调用边界检查回调，收到取消
  信号后在两个候选之间安全退出（不会留下部分施加的约束）。

## 项目结构

```
app/
  resolver.py    # PEP 440/508 解析、候选排序、回溯求解器、冲突数据结构、协作取消边界
  serialize.py   # 冲突树 / 终态冲突的 JSON 序列化
  service.py     # 同步 / 异步共用的请求校验与解析流水线
  store.py       # SQLite 作业存储（BEGIN IMMEDIATE 领取、终态守卫、崩溃恢复）
  worker.py      # 后台 worker 循环与取消信号
  main.py        # FastAPI 应用、生命周期与 /resolve、/jobs、/health
tests/           # 端到端 API 测试与求解器单元测试（含随机暴力对照）
data/            # 本地开发默认 SQLite 数据目录（已加入 .gitignore）
Dockerfile
docker-compose.yml
requirements.txt
```
