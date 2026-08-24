# 基于 `main` 的轻量可扩展内网 MVP

## 1. 总体架构

从干净的 `main` 开始重建，不复用 `feat/platform-mvp` 的控制面、数据库或在线数据。保留 `src/` 网格内核，通过同机 Python 服务直接调用，移除 Sites、登录、D1/R2 和远程 Runner 协议。

```text
内网浏览器
   │ HTTPS
   ▼
Caddy / Windows 防火墙
   │
   ▼
FastAPI API + React 静态页面
   ├── SQLite WAL：会话、运行树、状态、事件
   ├── 本地文件系统：几何、CGNS、TRB、报告、预览缓存
   └── 本地调度 Worker
          ├── 最多 20 个任务，受内存/磁盘门控
          ├── 独立子进程调用 src/mesh.py
          ├── Windows Job Object 管理 IGG 进程树
          └── HDF5 CGNS → vtk.js 预览资产
```

技术选择：

- 后端：Python 3.11+、FastAPI、Pydantic、Uvicorn、标准库 `sqlite3`。
- 调度：独立 Python Worker、SQLite 持久队列、`psutil` 资源监测。
- 前端：React、TypeScript、Vite、React Router、TanStack Query、vtk.js。
- 样式：模块化自定义 CSS，不引入 Tailwind 或 UI 组件库。
- 三维转换：`h5py + numpy`，不引入完整 VTK/PyVista。
- 部署：FastAPI 和 Worker 两个独立进程；Caddy 提供 HTTPS；交付 Windows 启动任务脚本，但未经额外授权不实际注册系统任务或修改防火墙。

运行语义：

- 上传几何后自动创建 baseline。
- 一个 `run` 同时代表分支节点和执行任务，不再拆分 step/run/job。
- 成功或失败节点均不可变；重试创建新节点。
- 质量 `FAIL/UNKNOWN` 不等于网格任务失败。
- 完成会话后冻结运行、控制和经验文本，但仍允许查看、比较和下载。

## 2. 文件结构与核心实现

```text
src/
├── mesh.py
├── geomturbo.py
├── autogrid.py
├── controls.py
└── quality.py

platform/
├── pyproject.toml
├── requirements.lock
├── .env.example
├── mesh_app/
│   ├── api.py                 # FastAPI 入口和 SPA 托管
│   ├── schemas.py             # API 请求/响应类型
│   ├── config.py              # 路径、并发、资源阈值
│   ├── db.py                  # SQLite 连接、事务和迁移检查
│   ├── sessions.py            # 会话、分支、冻结和乐观并发
│   ├── control_service.py     # 目录、目标、依赖和提交预检
│   ├── artifacts.py           # 安全路径、散列和下载
│   ├── worker.py              # 持久队列和并发调度
│   ├── windows_job.py         # IGG 子进程树生命周期
│   ├── preview.py             # CGNS manifest、表面和按需切片
│   └── backup.py              # SQLite 在线备份与产物清单
├── migrations/
│   └── 0001_initial.sql
├── ui/
│   ├── package.json
│   ├── package-lock.json
│   └── src/
│       ├── app/               # 路由、布局、QueryClient
│       ├── api/               # 类型化 fetch 客户端
│       └── features/
│           ├── sessions/
│           ├── runs/
│           ├── controls/
│           ├── mesh/
│           └── compare/
├── tests/
│   ├── test_api.py
│   ├── test_database.py
│   ├── test_worker.py
│   ├── test_controls.py
│   └── test_preview.py
└── deploy/
    ├── Caddyfile
    ├── run-api.ps1
    ├── run-worker.ps1
    └── install-startup-tasks.ps1
```

`src/` 网格 CLI 保持标准库运行边界；`src/controls.py` 提供一个受测试的公开目标枚举接口，供网页复用现有几何适用性逻辑。Web 依赖完全隔离在 `platform/`。

生产数据由 `MESH_DATA_DIR` 指定，默认示例为 `C:\ProgramData\MeshExperience`；数据库和产物不写入仓库。开发环境可显式指向 `runs/platform-dev/`。

## 3. 数据库与公共接口

### SQLite Schema

| 表 | 关键字段与职责 |
|---|---|
| `sessions` | `id`、标题、可选专家署名、源文件名、几何 SHA-256/相对路径/摘要 JSON、`ACTIVE/COMPLETED`、满意运行、乐观并发版本、时间戳 |
| `runs` | 会话、父节点、重试来源、序号、幂等请求 ID、完整控制快照与增量、解析结果、状态、质量、预览状态、经验文本及版本、进度、错误、PID/Worker/心跳、时间戳 |
| `artifacts` | 会话/运行、类型、显示文件名、服务端相对路径、SHA-256、大小、MIME、时间戳 |
| `run_events` | 运行内递增序号、阶段、等级、粗粒度进度、脱敏消息、结构化数据、时间戳 |
| `workers` | Worker ID、心跳、并发上限、运行数量、资源状态 JSON |

约束：

- SQLite 启用 WAL、外键、`busy_timeout` 和 `synchronous=NORMAL`。
- 运行状态仅允许 `QUEUED → RUNNING → SUCCEEDED/FAILED`。
- `request_id` 在会话内唯一，防止重复点击创建两次任务。
- 控制、质量和 `run_summary` 使用版本化 JSON 保存，避免为 344 项控制建立大量关系表。
- 事件与产物追加写入；成功/失败运行不可覆盖。
- 会话级写操作使用 `expected_version`；经验文本使用独立 `note_version`，冲突返回 `409 VERSION_CONFLICT`。
- 迁移通过显式命令应用；API 发现数据库版本落后时拒绝启动，不在请求路径执行 DDL。

### API

所有错误统一返回：

```json
{
  "error": {
    "code": "STABLE_ERROR_CODE",
    "message": "中文可操作说明",
    "details": {}
  }
}
```

| 方法与路径 | 行为 |
|---|---|
| `GET /api/health` | 数据库、Worker 心跳、IGG 路径、队列、资源门控状态 |
| `GET /api/v1/sessions` | 游标分页列出共享工作区会话 |
| `POST /api/v1/sessions` | multipart 流式上传 `.geomTurbo`，解析后创建会话和 baseline |
| `GET /api/v1/sessions/{id}` | 返回会话及精简运行树 |
| `GET /api/v1/runs/{id}` | 返回单轮控制、质量、经验、事件摘要和产物元数据 |
| `GET /api/v1/sessions/{id}/control-state` | 根据几何和父运行返回分层控制状态 |
| `POST /api/v1/sessions/{id}/control-preview` | 预检草稿、依赖、拓扑和需要清除的子控制 |
| `POST /api/v1/sessions/{id}/runs` | 从成功父节点创建不可变子运行 |
| `POST /api/v1/runs/{id}/retry` | 为失败运行创建具有相同控制的重试节点 |
| `PUT /api/v1/runs/{id}/experience-note` | 保存或清空成功运行的经验文本 |
| `POST /api/v1/sessions/{id}/complete` | 选择成功运行并冻结会话 |
| `GET /api/v1/runs/{id}/events?after=` | 增量读取任务事件 |
| `GET /api/v1/artifacts/{id}` | 支持 Range 的受控产物下载 |
| `GET /api/v1/runs/{id}/mesh/manifest` | 返回 block、维度、范围和预览能力 |
| `GET /api/v1/runs/{id}/mesh/blocks/{block}/{mode}` | 返回真实表面或结构线框 VTP |
| `GET /api/v1/runs/{id}/mesh/slice` | 按 block、I/J/K、0 基索引生成并缓存切片 |

控制写接口只接受结构化对象：

```text
ControlChange =
  { key, selector, op: "set", value }
  | { key, selector, op: "clear" }
```

前端只产生精确 `#N` 目标；“应用到全部叶排”由服务端展开为精确目标。API 使用 `main` 的注册表、类型、范围、拓扑和前置条件进行预检，Worker 执行前再次校验。

未显式设置的 baseline 控制统一显示“由 AutoGrid 默认决定”，不重新引入全量 Getter 探测。

## 4. UI 与任务执行

UI 只包含两个路由：

- `/`：共享会话列表、状态筛选和“新建会话”上传对话框。
- `/sessions/:id`：完整专家工作台。

工作台布局：

- 左侧：不可变运行树，显示 baseline、子分支、失败和重试节点。
- 中间：真实网格 Viewer、质量、事件和产物四个页签。
- 右侧：控制编辑器和当前运行的经验文本。
- 顶部：运行状态、刷新、双轮对比、选择满意网格并冻结。
- 对比模式：两个成功运行并排显示，同步相机，质量差值在浏览器端计算。

控制编辑器：

- P0 常用项默认展示，P1/P2 折叠但可全量搜索。
- 按几何实体、目标、阶段和拓扑筛选。
- 显示 `EDITABLE / LOCKED / NOT_APPLICABLE` 及稳定原因。
- 草稿变化防抖调用 `control-preview`，不在 TypeScript 中复制 Python 规则。
- 父控制变化需要清除子覆盖时，提交前明确确认。
- 未保存草稿离开页面时提示。

三维查看：

- 运行完成后预生成每个 block 的真实边界表面和结构线框。
- I/J/K 切片第一次请求时从 HDF5 CGNS 生成 VTP，原子写入缓存；后续直接复用。
- block 显隐、表面/线框切换和 0 基切片范围由 manifest 驱动。
- ADF CGNS 或转换失败不改变网格成功状态；质量和原始产物仍可用，Viewer 显示明确原因。
- vtk.js 动态加载，避免首页承担三维依赖体积。

前端服务器状态由 TanStack Query 管理，仅活动运行每 3 秒轮询；当前页签、选中节点和对比运行写入 URL，刷新可恢复。草稿保持组件本地状态，不增加 Redux/Zustand。

Worker 配置默认值：

```text
MESH_MAX_CONCURRENCY=20
MESH_MEMORY_RESERVATION_GB=2.5
MESH_MIN_FREE_MEMORY_GB=8
MESH_MIN_FREE_DISK_GB=20
MESH_JOB_TIMEOUT_SECONDS=1800
```

20 是硬上限，不保证始终启动 20 个任务。Worker 在每次领取前预留内存并检查磁盘；许可证容量由部署环境确认。若连续检测到许可证类失败，调度进入短暂退避，避免错误风暴。

## 5. 测试、验收与交付

自动验证：

- 根目录现有 Python 回归测试全部通过。
- API 覆盖上传、非法文件、路径穿越、控制预检、幂等、分支、失败重试、经验文本、版本冲突和冻结写保护。
- SQLite 测试覆盖 WAL 并发、原子领取、20 槽上限、资源门控和 Worker 崩溃后的终态修正。
- 预览测试覆盖合成 HDF5、block 边界、表面/线框、I/J/K 首尾索引、缓存和 ADF 降级。
- 前端使用 Vitest 验证运行树、控制草稿、轮询、质量差值和冻结状态；执行 TypeScript、ESLint 和生产构建。
- 使用 Playwright CLI 跑真实浏览器闭环：上传 → baseline → 分支 → 经验文本 → 双轮对比 → 冻结 → 刷新重载。
- 最终使用 Rotor37、WP100_comp、ori1 完成真实 baseline；Rotor37 额外完成一次控制分支、网格查看、对比和冻结。

交付内容包括完整源代码、SQLite 迁移、锁定依赖、Windows 启动脚本、Caddy 示例、备份命令、中文 README 和 `docs/DevLog.md`。同时更新 `.gitignore`，跟踪测试与迁移，但忽略虚拟环境、Node 依赖、构建产物、数据库和运行数据。

明确假设：

- 应用不做登录；允许网段内的任何访问者查看、修改和冻结共享会话。专家署名只用于展示，不构成权限。
- 从 `main` 全新开始，不迁移旧 Sites/D1/R2 数据，也不沿用旧站点部署。
- 不提供管理员、删除、自动调参、推荐系统、CFD 求解、`y+` 或网格无关性功能。
- 数据默认永久保留；清理通过运维手段完成，MVP 不开放危险的批量删除 API。
- 根目录网格 CLI 的既有调用方式与 Schema v3 保持向后兼容。
