# 新机器部署与运行指南

从 git clone 到网格 CLI 与内网 Web 平台完整可用的全部步骤。所有命令均在 **PowerShell** 中从**仓库根目录**执行。

## 0. 前置要求

| 依赖 | 版本要求 | 用途 |
|---|---|---|
| Python | ≥ 3.11（本机验证 3.13.5） | 平台后端；网格 CLI 仅需 ≥ 3.7 |
| Node.js | 20+（本机验证 22.18.0） | 前端构建（npm 需随 Node 附带） |
| NUMECA AutoGrid/IGG | 17.1 | 真实网格生成；仅做静态校验/网页浏览时不需要 |

IGG 不需要加入 `PATH`，路径写在 `.env` 中即可。

## 1. 克隆仓库

```powershell
git clone <仓库地址>
cd mesh-generation
```

`archive/` 目录包含 v0～v6 历史快照，clone 后会存在但**完全不需要读取**，当前实现不依赖它。

## 2. 网格 CLI（可选，无第三方依赖）

`src/` 下的网格内核只用 Python 标准库，不需要任何安装步骤：

```powershell
# 静态校验 + 脚本渲染，不启动 IGG（无需 AutoGrid）
python src/mesh.py geometries/Rotor37.geomTurbo --dry-run

# 查询控制目录
python src/mesh.py --list-controls
```

真实生成网格需要先完成第 3 步的 `.env` 配置（IGG 路径）。

## 3. 配置根目录 `.env`

复制模板并按本机修改：

```powershell
Copy-Item .env.example .env
```

`.env` 是网格 CLI 与平台共用的唯一配置文件，已在 `.gitignore` 中，不会进入版本管理。最少需要确认的项：

```text
# IGG/AutoGrid 可执行文件（按实际安装路径修改）
IGG_EXE=C:\ProgramData\NUMECA\fine171\bin64\iggx86_64.exe

# 数据目录：相对路径以仓库根目录为基准
MESH_DATA_DIR=runs\platform-dev
```

各配置项的完整含义见 `.env.example` 内的分组注释。

### 3.1 共享密码登录（可选）

不配置时内网用户无需登录即可访问。需要启用时：

```powershell
# 交互式输入两次明文密码（不回显、不落盘），输出 scrypt 哈希串
platform\.venv\Scripts\python.exe -m mesh_app.auth
```

把输出的哈希串与用户名一起填入 `.env`（**两个变量必须同时设置**）：

```text
MESH_AUTH_USERNAME=<用户名>
MESH_AUTH_PASSWORD_HASH=scrypt$32768$8$1$<salt_hex>$<hash_hex>
```

注意：此命令依赖 `platform\.venv`，因此需在第 4 步完成后再执行；或临时改用任何已安装 `starlette` 的 Python 环境。

## 4. 平台后端环境

后端第三方依赖完全隔离在 `platform\.venv`，版本由 `platform\requirements.lock` 精确锁定（含全部传递依赖，锁定环境为 Python 3.13 / Windows x64）：

```powershell
python -m venv platform\.venv
platform\.venv\Scripts\python.exe -m pip install -r platform\requirements.lock
```

## 5. 前端构建

前端是 React + TypeScript + Vite，`package-lock.json` 已入库，**必须用 `npm ci`**（严格按锁文件安装，不要用 `npm install`）：

```powershell
Set-Location platform\ui
npm ci
npm run build
Set-Location ..\..
```

构建产物输出到 `platform\ui\dist`，由 FastAPI 直接托管；浏览器只包含 `/` 与 `/sessions/:id` 两个页面路由。

## 6. 首次启动（含数据库迁移）

```powershell
.\platform\deploy\run-local.ps1 -Migrate
```

`-Migrate` 是显式迁移动作：创建数据目录、初始化 SQLite、应用 `platform\migrations` 中的迁移。**仅首次安装或升级代码后使用一次**，日常启动不带该参数。

启动成功后访问 `http://127.0.0.1:8000`。

## 7. 日常启动与停止

```powershell
.\platform\deploy\run-local.ps1
```

- 脚本自动加载 `.env`、检查数据库版本、后台启动 API、前台运行 Worker
- `Ctrl+C` 停止 Worker，脚本自动清理后台 API 进程
- API 日志位于 `<MESH_DATA_DIR>\logs`（本地默认 `runs\platform-dev\logs`）
- 该脚本**不会**注册任何开机自启任务或计划任务，停止后无残留

需要分别管理进程时（如生产部署）：

```powershell
$env:MESH_PYTHON = (Resolve-Path platform\.venv\Scripts\python.exe).Path
.\platform\deploy\run-api.ps1      # 终端 1
.\platform\deploy\run-worker.ps1   # 终端 2
```

## 8. 验证安装

```powershell
# 后端测试
$env:PYTHONPATH = (Resolve-Path platform).Path
platform\.venv\Scripts\python.exe -m pytest platform -q

# 前端测试与构建检查
Set-Location platform\ui
npm test
npm run typecheck
Set-Location ..\..

# 网格内核测试（只用系统 Python，无依赖）
python -m pytest tests -q
```

在浏览器完成一次真实闭环：上传 `.geomTurbo` → baseline → 分支 → 经验文本 → 对比 → 冻结 → 刷新恢复。

## 9. 常见问题

**Q：`run-local.ps1` 报“数据库版本检查失败”？**
首次安装或拉取了新代码后执行一次 `.\platform\deploy\run-local.ps1 -Migrate`。生产环境用 `python -m mesh_app.db migrate`。

**Q：网页能打开但网格任务不执行？**
`MESH_IGG_PATH` 未配置或路径不存在（启动时会有警告）。确认 `.env` 中 `IGG_EXE` 指向真实 IGG 可执行文件，且该账户下 AutoGrid 许可证可用。

**Q：提示“未配置 MESH_AUTH_PASSWORD_HASH”？**
这是提醒而非错误——共享密码登录未启用，内网用户可直接访问。需要启用见第 3.1 节。

**Q：前端改了代码想热更新？**
`platform\ui` 下 `npm run dev`，Vite 将 `/api` 代理到 `http://127.0.0.1:8000`。

**Q：升级代码后如何更新？**

```powershell
git pull
platform\.venv\Scripts\python.exe -m pip install -r platform\requirements.lock
Set-Location platform\ui; npm ci; npm run build; Set-Location ..\..
# 先备份，再显式迁移
$env:PYTHONPATH = (Resolve-Path platform).Path
platform\.venv\Scripts\python.exe -m mesh_app.backup --output-dir <备份目录>
.\platform\deploy\run-local.ps1 -Migrate
```

## 10. 生产部署提示

生产环境与本地开发的差异（详见 `platform/README.md`）：

- 数据目录默认 `C:\ProgramData\MeshExperience`（通过系统环境变量 `MESH_DATA_DIR` 配置，不用仓库内 `.env`）
- API 与 Worker 用 `install-startup-tasks.ps1 -Apply`（管理员）注册为开机启动任务；脚本默认只显示计划
- HTTPS 由 Caddy 反向代理提供（`platform\deploy\Caddyfile`），客户端需信任其内部 CA 或替换为正式证书
- 同一个 SQLite 数据库只部署一个 Worker 进程；单 Worker 内部最多并发 20 个任务
- 在线备份：`platform\.venv\Scripts\python.exe -m mesh_app.backup --output-dir <目录>`（默认校验产物 SHA-256）
