# 叶轮机械网格经验内网平台

本目录提供一个无登录的内网共享工作区：上传 `.geomTurbo` 后自动创建 baseline，通过不可变运行树保存控制分支、质量、事件、产物与专家经验。FastAPI 同时托管 API 和 React 静态页面，独立 Worker 从 SQLite 持久队列领取任务，Caddy 在最外层提供 HTTPS。

> 任何能访问该内网站点的人都可以查看、修改和冻结会话。专家署名只用于展示，不是权限控制。

## 目录

```text
platform/
├── mesh_app/          FastAPI、SQLite 服务、Worker 与预览转换
├── migrations/        显式应用的 SQLite 迁移
├── ui/                React + TypeScript + Vite 前端
└── deploy/            Caddy 与 Windows 启动脚本示例
```

生产数据默认位于 `C:\ProgramData\MeshExperience`，不会写入仓库。开发环境应显式设置 `MESH_DATA_DIR` 和 `MESH_DATABASE_PATH`，例如 `runs\platform-dev`。

## 首次安装

需要 Python 3.11+、Node.js 20+ 与 Caddy。以下命令均从仓库根目录执行：

```powershell
python -m venv platform\.venv
platform\.venv\Scripts\python.exe -m pip install -r platform\requirements.lock

Set-Location platform\ui
npm ci
npm run build
Set-Location ..\..

$env:PYTHONPATH = (Resolve-Path platform).Path
$env:MESH_DATA_DIR = "C:\ProgramData\MeshExperience"
$env:MESH_DATABASE_PATH = "C:\ProgramData\MeshExperience\mesh.sqlite3"
platform\.venv\Scripts\python.exe -m mesh_app.db migrate
```

迁移是显式运维动作。API 和 Worker 只检查数据库版本，不会在请求或启动时悄悄执行 DDL。升级代码后应先备份，再运行同一条 `mesh_app.db migrate` 命令。

后端锁文件固定直接与传递依赖版本；前端必须使用 `npm ci`，它严格使用已提交的 `package-lock.json`。

## 配置

可复制 `.env.example` 中的值到进程或系统环境。PowerShell 脚本不会解析 `.env` 文件，避免把部署机密或许可证配置隐式带入进程。

| 环境变量 | 默认值 | 含义 |
|---|---:|---|
| `MESH_DATA_DIR` | `C:\ProgramData\MeshExperience` | 几何、产物、预览和备份根目录 |
| `MESH_DATABASE_PATH` | `<data-dir>\mesh.sqlite3` | SQLite 文件 |
| `MESH_MAX_UPLOAD_BYTES` | `536870912` | `.geomTurbo` 文件上限；API 在 multipart 落盘前按实际请求字节提前拒绝 |
| `MESH_IGG_PATH` | 空 | 经部署确认的 IGG/AutoGrid 可执行文件 |
| `MESH_MAX_CONCURRENCY` | `20` | Worker 硬上限，不能超过 20 |
| `MESH_MEMORY_RESERVATION_GB` | `2.5` | 每个待领取任务的内存预留 |
| `MESH_MIN_FREE_MEMORY_GB` | `8` | 最低空闲内存门限 |
| `MESH_MIN_FREE_DISK_GB` | `20` | 最低空闲磁盘门限 |
| `MESH_JOB_TIMEOUT_SECONDS` | `1800` | 单任务超时 |
| `MESH_PYTHON` | 自动发现 | 部署脚本使用的 Python 路径 |

除总上传大小外，解析器还限制单条 geomTurbo 物理行、token/名称长度、嵌套深度、叶排数和每叶排叶片实体数，校验显式块配对，并对重复侧别去重；超过任一安全边界会返回 `422 INVALID_GEOMTURBO`，避免异常输入无界扩张内存状态。

并发 20 是硬上限，不保证始终运行 20 个任务。实际领取还受内存、磁盘和许可证退避控制。应在计划使用的 Windows 任务账户下确认 `MESH_IGG_PATH`、许可证与数据目录权限。

同一个 SQLite 数据库只部署一个 Worker 进程；单个 Worker 会在进程内部并发调度最多 20 个任务。当前资源预留与许可证退避按这一部署模型设计，不支持多个 Worker 共享同一数据库做横向扩展。

Windows 下 Worker 会把网格子进程加入带 `KILL_ON_JOB_CLOSE` 的 Job Object，正常退出、超时和取消时终止整棵进程树；若部署账户或宿主环境不支持 Job Object，则降级使用 `taskkill /T`。投产前应通过一次真实任务确认事件中的 `job_object` 标志为 `true`。

## 本机启动与开发

先在一个终端启动 API，再在另一个终端启动 Worker：

```powershell
$env:MESH_PYTHON = (Resolve-Path platform\.venv\Scripts\python.exe).Path
.\platform\deploy\run-api.ps1
.\platform\deploy\run-worker.ps1
```

脚本会解析仓库绝对路径，从仓库根运行，并把 `PYTHONPATH` 指向 `platform`。因此 `mesh_app` 与根目录 `mesh.py`、`controls.py` 均可导入，不依赖调用者当前目录。

前端热更新开发：

```powershell
Set-Location platform\ui
npm run dev
```

Vite 将 `/api` 代理到 `http://127.0.0.1:8000`。生产构建由 FastAPI 从 `platform/ui/dist` 托管；浏览器只含 `/` 与 `/sessions/:id` 两个页面路由。

## HTTPS 与启动任务

编辑 [`deploy/Caddyfile`](deploy/Caddyfile) 或设置 `MESH_HOST`，再让 Caddy 反向代理 `127.0.0.1:8000`。示例需要 Caddy 2.10+，使用 Caddy 内部 CA，客户端需要信任其根证书；也可以按组织规范替换为正式内网证书。Caddy 的上传入口默认限制为 `540MB`；若调整 `MESH_MAX_UPLOAD_BYTES`，应同步设置 Caddy 进程环境变量 `MESH_MAX_REQUEST_BODY`，并为 multipart 开销保留余量。

启动任务脚本默认只显示计划，不修改系统：

```powershell
.\platform\deploy\install-startup-tasks.ps1
```

确认绝对路径、`SYSTEM` 账户下的 IGG/许可证环境与目录权限后，管理员才能显式安装：

```powershell
.\platform\deploy\install-startup-tasks.ps1 -Apply
```

脚本仅注册 API 与 Worker 两个启动任务，不注册 Caddy、不修改防火墙。Caddy 服务、内网 DNS、端口 443 入站规则和证书信任必须由运维按组织策略单独完成。

## 备份与恢复准备

SQLite 使用 WAL。运行在线备份入口可得到一致数据库副本及产物清单：

```powershell
$env:PYTHONPATH = (Resolve-Path platform).Path
platform\.venv\Scripts\python.exe -m mesh_app.backup --output-dir D:\MeshBackups
```

默认会校验产物 SHA-256；大数据集的受控窗口可加 `--skip-file-hashes`。恢复前应停 API 与 Worker，保留原数据目录，先在隔离目录核对数据库、清单和文件散列，再由运维执行替换。本 MVP 不提供危险的网页批量删除或恢复按钮。

## 前端验证

```powershell
Set-Location platform\ui
npm test
npm run typecheck
npm run lint
npm run build
```

Vitest 覆盖不可变运行树、控制草稿、活动轮询、质量差值、URL 状态与冻结判定。三维 Viewer 从公开 manifest 驱动 block 显隐、表面/线框和 I/J/K 0 基切片，并在进入 Viewer/对比时才动态加载 vtk.js。最终验收仍需在真实服务与许可环境中用 Playwright 完成“上传 → baseline → 分支 → 经验文本 → 双轮对比 → 冻结 → 刷新恢复”的浏览器闭环。

## 已知安全说明

当前 `npm audit` 报告 2 个 high，均来自 `react-router-dom → react-router` 的同一条 [GHSA-qwww-vcr4-c8h2](https://github.com/advisories/GHSA-qwww-vcr4-c8h2) 依赖链。公告影响的是不稳定 RSC Action 接口；本前端是纯 BrowserRouter SPA，不启用 RSC、Server Actions 或 React Router 服务端请求处理，因此该攻击面不适用。暂不通过降级或 `npm audit fix --force` 引入其他已知漏洞或不兼容；待 `react-router-dom` 发布与当前 Node/Vite 环境兼容的已修复版本后升级并消除告警。

## API 交互约定

- 所有错误按 `{ "error": { "code", "message", "details" } }` 展示；`409 VERSION_CONFLICT` 需要刷新后重新操作。
- 会话级写操作发送 `expected_version`，经验文本单独发送 `expected_note_version`。
- 控制预检是服务端规则的唯一来源；前端仅维护 `set/clear` 草稿和精确 `#N` selector。
- 当预检返回 `required_clears` 时，前端确认后以 `confirm_required_clears=true` 创建新运行。
- `QUEUED/RUNNING` 节点以及 `SUCCEEDED + preview_status=PENDING` 的后处理窗口每 3 秒轮询；页签、选中节点和对比节点写入 URL。
- 质量 `FAIL/UNKNOWN` 与运行 `FAILED` 是不同概念；预览失败也不改变成功网格状态。
