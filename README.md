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

## 一条命令启动

需要 Docker（含 Compose v2）。在项目根目录执行：

```bash
docker compose up --build
```

服务监听 **http://localhost:8000**：

- `GET /health` — 健康检查
- `POST /resolve` — 解析依赖
- `GET /docs` — 交互式 OpenAPI 文档（Swagger UI）

镜像在构建时安装固定版本依赖；运行时不访问任何外部仓库。

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

## 项目结构

```
app/
  resolver.py    # PEP 440/508 解析、候选排序、回溯求解器、冲突数据结构
  serialize.py   # 冲突树 / 终态冲突的 JSON 序列化
  main.py        # FastAPI 应用与 /resolve、/health
tests/           # 端到端 API 测试与求解器单元测试（含随机暴力对照）
Dockerfile
docker-compose.yml
requirements.txt
```
