# 开发日志

## 2026-09-11：批次③ 调度与资源（CR-04、CR-07~09）

### CR-04：后处理进入独立、可终止的子进程

- 新增 `platform/mesh_app/postprocess.py`（`python -m mesh_app.postprocess <run_id>`）：单运行产物散列登记（成功与失败运行均登记）与成功运行的 surface/wireframe + manifest 预览转换；子进程日志写 `data_dir/logs`，不污染 run_dir。
- Worker 解耦：`_poll_tasks` 主循环内不再同步执行后处理，成功/失败运行仅入队 `_postprocess_queue`；`_dispatch_postprocess` 固定单并发（原子占位 + `spawn_managed_process`），`_supervise_postprocess` 以 monotonic 计时轮询，超时（`MESH_POSTPROCESS_TIMEOUT_SECONDS`，默认 600）→ terminate_tree → FAILED；主循环持续处理网格轮询、超时、心跳、停止与新任务领取。
- 迁移 `0002_postprocess_status.sql`（user_version=2）：runs 表新增 `postprocess_status`（PENDING/RUNNING/COMPLETED/FAILED）与起止时间、错误字段；历史 `preview_status='PENDING'` 行保留排队，其余置 COMPLETED（不批量重新散列或转换）；网格终态不可变触发器未动。
- 资源门控：活动后处理额外计入一份现有内存预留（默认 2.5 GiB）。
- 启动恢复：`_recover_pending_postprocess` 恢复 PENDING/RUNNING 行（RUNNING 重置为 PENDING）重新排队，不同步转换；停止时终止后处理进程树并保留可恢复状态。
- 移除仅为同步后处理续写心跳的辅助线程 `_heartbeats_during_preview` 及其测试。

### CR-07：按请求规模读取和生成预览

- 切片经 h5py hyperslab 只读请求平面（I 对应 `[:, :, i]`、J 对应 `[:, j, :]`、K 对应 `[k, :, :]`，转置为 I,J,K 平面顺序），不再整块读入体坐标。
- 表面/线框只枚举边界索引；棱边去重与退化维度行为保留，与旧算法在 5 组小网格（含 (1,1,1)、(1,3,4)）逐点拓扑一致；包围盒改为 4M 元素分块归约。
- 单飞与并发：API 层 `_PreviewConversionGate`（asyncio.Semaphore(1) + 按键 inflight future 注册表 + run_in_threadpool），同键请求共享一次生成，每 API 进程同时最多一个转换，等待在异步层完成；manifest/block/slice 三路由接入。
- 预算与缓存上限：切片数组可计算大小生成前对照 `MESH_PREVIEW_MEMORY_BUDGET_MB`（默认 512）；每运行切片缓存累计对照 `MESH_PREVIEW_CACHE_LIMIT_MB`（默认 1024）；超限拒绝并返回 `PREVIEW_MEMORY_BUDGET_EXCEEDED` / `PREVIEW_CACHE_LIMIT_EXCEEDED`，已有缓存继续可读。
- `src/quality.py` CGNS 降级解析改为分块流式扫描（1 MiB 块、标记尾重叠、深度跟踪 NI_BEGIN/NI_END、滚动窗口计数），解析结果与旧实现逐项一致，未引入 h5py（保持标准库边界）。

### CR-08：卸载密码计算并限制并发

- 登录路由 scrypt 校验经 anyio 线程池执行（run_in_threadpool），配专用 `CapacityLimiter`（`MESH_SCRYPT_MAX_CONCURRENCY`，默认 2），等待在异步层排队，不占用 ASGI 事件循环；用户名不匹配时仍执行 scrypt，保持统一凭据错误与等时性。
- Cookie 增加 `Secure` 配置（`MESH_COOKIE_SECURE`，默认 False 保持本机 HTTP 开发可用）；平台 README 补充生产 HTTPS（Caddy）部署时启用说明。

### CR-09：按保存的进程组身份清理 POSIX 后代

- `windows_job.py` 保存 start_new_session 建立的 PGID，清理不再依赖对已回收 PID 的 waitpid/getpgid；terminate_tree 统一走 `_cleanup_posix_group`：SIGTERM → 宽限期 → `os.killpg(pgid, 0)` 存活检查（ESRCH/EPERM 幂等）→ 残余成员 SIGKILL → 有界确认；父进程先退出仍到达 SIGKILL 分支。
- `close()` 在 POSIX 上调用同一幂等清理路径；正常关闭、超时、停止共用，所有权边界不变（只对保存的 pgid 发信号）。
- Windows Job Object 路径保持原样并通过实机验证：真实子进程树经 terminate_tree 整树消失、Job 句柄关闭兜底清理残余孙进程均以 psutil 断言通过（本机 uses_job_object 为真）。
- 新增 `test_process_cleanup.py`：Windows 实机 2 项真实执行，POSIX 4 项场景（孙进程忽略 SIGTERM、父先退出、正常结束后残余后代、重复清理幂等）在 Windows 按预期跳过，测试 docstring 注明 Linux 验证方法。

### 配置与验证

- `config.py` 新增 5 个配置项（后处理超时、预览内存预算/缓存上限、scrypt 并发、Cookie Secure）并在 `.env.example` 补充注释说明。
- 根测试 79 项全部通过（新增 CGNS 分块解析一致性 8 项）；平台测试 100 passed、4 skipped（POSIX 专项），全部通过。
- 顺带修复 `test_run_contract.py` 并发同目录用例的既有偶发竞争（败者进程在 exists 检查与 iterdir 之间可能报"拒绝复用"而非"占用"，断言放宽为接受两种合法拒绝）。

## 2026-09-11：批次② 结果契约（CR-01~03、D-02）

### CR-01：独占运行目录与原子产物清单

- CLI 新增 `--run-id`（未传生成 UUID4 hex），平台 Worker 强制传入，平台身份与摘要身份一致。
- `src/autogrid.py` 新增 `acquire_run_directory`：复制输入、写脚本前以 O_CREAT|O_EXCL 排他创建 `.mesh_run.lock`（含 run_id 与创建时间）；显式 `--out` 仅允许不存在、为空或仅含 `worker.stdout.log`/`worker.stderr.log` 白名单文件；其余既有文件或已有标记 → `RunDirectoryError`、退出码 2、保留既有文件；dry-run 同样占用且标记保留。
- 默认目录改为 `runs/<名称>_<时间戳>_<uuid>`；`run_summary.json` 以临时文件 + `os.replace` 原子写入；`collect_outputs` 以占用时快照为对照只登记本次新产物（manifest.outputs 含相对路径/大小/sha256）。
- `.igg` 仍为最低产物要求；CGNS 缺失在摘要中单独记录（missing_mesh_outputs）。
- Worker 以 `os.mkdir` 独占创建运行目录（失败 → run FAILED，error_code=RUN_DIR_UNAVAILABLE），成功后才打开两份日志，绝不覆盖既有目录或追加历史日志。

### CR-02：事件协议校验与控制验证

- 生成脚本的控制事件带 run_id 与 stage（apply/post_generation）；脚本末尾无条件输出 `AGMESH_COMPLETION:` 完成事件（无控制也必须有）。
- 宿主解析对损坏 JSON、缺失/重复/未知 ID、key/目标/阶段/run_id 不匹配、缺失应用/观察/完成事件全部记录 protocol_errors（code+message）并使运行 FAILED（returncode 1），不再字典覆盖或跳过。
- 每项控制验证四枚举 VERIFIED/MISMATCH/READBACK_ERROR/UNVERIFIABLE：整数/布尔/枚举归一化精确比较，浮点 rel_tol=1e-7、abs_tol=1e-10，si_length 按 units_factor 换算到 SI 比较；无法解释的回读不判 VERIFIED；setter 前回读失败仅诊断；getter 报错或回读不匹配时运行仍 SUCCEEDED，验证异常单独标记。
- 新增 tests/fake_igg.py（15 种行为可配置的假 IGG 桩）与 tests/test_run_contract.py（20 项合同测试，覆盖批次②验收矩阵）。

### CR-03：质量校验先行

- 去除 `src/quality.py` 全部计数字段 `int(float(...))` 截断（改 `_strict_count`；CGNS 的 `_last_int` 改 `_last_count` 捕获完整数值记号），计数必须为严格整数：点数/层级正整数、负体积单元非负、拒绝布尔与小数文本。
- `evaluate_quality()` 在硬阈值前校验全部已提供统计值：实数有限、角度 [0,180]、比例为正、壁面距离非负；非法 → UNKNOWN/accepted=False，reasons 含字段路径；三个入口（直接调用、报告解析、CGNS 降级解析）全部生效。
- 非有限值在对外结构中转 null 并保留错误说明与原始报告；硬阈值与等号行为不变。
- 新增 tests/test_quality.py 28 项：NaN/±Inf/1e309/零负点数/小数布尔计数/非法角度比例/缺字段/等号矩阵，评审四个复现用例全部转为 UNKNOWN。

### D-02：来源记录与样本资格

- 运行摘要升级 Schema v4：保留 v3 全部字段，新增 run_id/created_at/execution_evidence/controls.verification/quality_validation/sources/manifest；`_write_json` 与平台全部持久化 json.dumps 使用 allow_nan=False。
- sources 记录输入摘要、src 五模块执行源码签名（算法从 tests/test_campaign_runner.py 迁入 src/mesh.py，campaign runner 改为从 src 导入并保持语义）、git commit/dirty、生成脚本摘要、控制注册表确定性签名（344 键）、质量规则版本、厂商版本。
- 平台创建/重试子运行时在 run_events 记录入队来源快照（stage=ENQUEUE_SOURCE，与执行算法一致）；运行详情新增 sample_eligibility：执行证据完整、控制验证充分、质量可判定、来源完整且产物已登记才合格；质量 FAIL 仍合格（有效失败经验）；v3 历史 → 不合格「新证据缺失」；入队与执行来源漂移 → 不合格并注明。

### 验证

- 根测试 71 项全部通过（批次① 23 + 质量 28 + 合同 20）；平台测试 62 项全部通过（原 53 + 新增 9）。
- Worker `_load_run_summary` 接受 v3/v4；平台持久化 JSON 不含 NaN/Infinity 字面量。

## 2026-09-11：CR-10 可分发测试基线与合成几何夹具

### 夹具与忽略规则

- 在 `geometries/fixtures/` 新增 6 个小型合成 `.geomTurbo` 夹具（single_row / multi_row / splitter / gap / fillet / units，305 B～1.4 KB），覆盖单排、多排、splitter、gap、fillet 侧别与 UNITS/UNITS-FACTOR 单位解析；内容为虚构合成数据，不包含真实工程几何。
- `.gitignore` 由整体忽略 `geometries/` 改为 `geometries/*` 加放行 `!geometries/fixtures/`；`git check-ignore` 验证四个真实几何（Rotor37/ori1/WP100_comp/WP100_turb）仍被忽略，夹具可正常提交。

### 测试改造

- `tests/test_geomturbo.py` 普通单测全部改用合成夹具（流式解析、多排、splitter、gap、fillet、单位声明），不再直接依赖真实几何。
- 真实工程几何测试移入 `tests/test_geomturbo_integration.py`，通过环境变量 `GEOMTURBO_TEST_GEOMETRIES_DIR` 指定几何目录（默认 `geometries/`），文件缺失时 `unittest.skipUnless` 明确跳过并说明放置路径。

### 验证

- 根测试 `python -m unittest discover -s tests` 共 23 项全部通过（含集成测试实际执行 3 项）。
- 将 `GEOMTURBO_TEST_GEOMETRIES_DIR` 指向不存在目录复验：集成测试 3 项正确跳过（skipped=3），恢复后重新实际执行通过。

## 2026-09-09：清理临时产物并新增新机器部署指南

### 清理

- 确认 `C:\ProgramData\MeshExperience` 为 8 月初遗留的空数据目录（数据库 4 KB 且无任何表，三个子目录为空），已删除；真实数据在 `runs\platform-dev`。
- 删除 `runs\platform-chrome-user-test-20260809/`、`runs\platform-chrome-user-test-20260824/`（Playwright 验收遗留，共约 0.5 MB）。
- 删除 `runs\platform-dev\logs` 中 8 月的 6 个旧 API 日志，仅保留当日。
- 删除根目录、`src`、`tests` 的 `__pycache__`，根目录与 `platform` 的 `.pytest_cache`，以及空的 `.playwright-cli/`。
- 保留 `runs\platform-dev`（1 个会话、2 次成功运行、71 个产物记录）及 `platform\.venv`、`node_modules`、`ui\dist` 等运行必需目录。

### 文档

- 新增 `docs/SETUP_GUIDE.md`：新机器从 git clone 到平台可用的完整步骤——前置要求（Python ≥3.11、Node 20+、AutoGrid 17.1）、`.env` 配置与共享密码登录、后端 venv（requirements.lock 精确锁定）、前端 `npm ci` 构建、首次 `-Migrate` 启动、日常启停、验证命令（平台 53 项测试已实际运行通过）、常见问题与生产部署提示。
- 文档中平台测试命令经实际执行验证：`53 passed`。

## 2026-09-09：统一 .env / .env.example 并补充配置项注释

- 确认两文件配置值完全一致，`.env.example` 为更新版本（含共享密码登录注释段）；以注释版为基准统一两文件。
- 按 `platform/mesh_app/config.py`、`run-local.ps1`、`worker.py`、`api.py` 的实际读取逻辑，为全部配置项补充分组注释：IGG 路径解析顺序、数据目录与数据库默认值、上传上限、SQLite busy_timeout、Worker 并发/内存/磁盘/超时/心跳恢复、共享密码登录的启用条件与哈希生成命令。
- `.env` 本身在 `.gitignore` 中，不纳入版本管理；仅 `.env.example` 进入提交。

## 2026-09-09：平台共享密码登录

### 后端

- 新建 `platform/mesh_app/auth.py`：标准库 scrypt 密码编码/校验（格式 `scrypt$n$r$p$salt_hex$hash_hex`，显式放宽 OpenSSL 32MiB 内存限制）、从密码哈希派生会话签名密钥、`exp.signature` 无状态 Cookie token 的签名/校验，以及纯 ASGI 的 `AuthMiddleware`；`python -m mesh_app.auth` 交互式生成可粘贴进 `.env` 的哈希。
- `config.py` 的 `Settings` 新增 `MESH_AUTH_USERNAME` / `MESH_AUTH_PASSWORD_HASH`，两者必须同时设置或同时留空，否则启动快速失败。
- `api.py` 注册 `AuthMiddleware` 为最外层中间件（未认证请求不进入上传解析）；新增 `POST /api/auth/login`（登录成功签发 `mesh_session` Cookie，`HttpOnly`、`SameSite=Lax`、7 天）、`GET /api/auth/session`（三态：未启用/已认证/未认证）、`POST /api/auth/logout`（清除 Cookie）。`/api/health` 与非 `/api/*` 静态资源放行；登录失败统一 401 `INVALID_CREDENTIALS`，用户名与密码比较均用 `hmac.compare_digest`。
- 会话无状态：签名密钥从密码哈希派生，改密码即全员下线；未配置鉴权变量时中间件直接放行，行为与现状完全一致。

### 前端

- 新建 `features/auth/`：`AuthGate` 查询 `/api/auth/session`，加载中显示过渡态，未认证渲染登录页，已认证渲染应用；`LoginPage` 居中卡片式登录页，中文文案，复用 global.css 设计变量。
- `main.tsx` 用 `AuthGate` 包裹 `RouterProvider`；`queryClient.ts` 新增 `QueryCache.onError`——任何数据请求 401 时失效会话查询，全局回落到登录页（含会话过期）；查询重试规则同步排除 401。
- 登录成功后 `queryClient.invalidateQueries()` 全量失效，会话查询重取通过后自动进入应用；会话列表页头部增加"退出登录"按钮。

### 部署与文档

- `run-local.ps1` 加载 `.env` 后，未配置鉴权时与 IGG 未配置警告同一风格打印 `Write-Warning`。
- `.env.example` 追加注释掉的鉴权配置示例与生成命令说明；`platform/README.md` 新增"访问密码"小节（配置步骤、7 天会话、退出、明文 HTTP 风险、Caddy 升级路径）并更新开篇信任边界表述；根 README 一句带过并链接。

### 验证

- 新增 `platform/tests/test_auth.py`：未登录 401、健康端点匿名可访问、错误用户名/密码返回一致 401、登录成功 Set-Cookie 属性（HttpOnly/SameSite/Max-Age=604800/无 Secure）、篡改与过期 token 拒绝、登出后 401、会话端点三态、环境变量成对校验、未配置鉴权保持现状；平台回归 53 项全部通过（原 38 项不动）。
- 前端新增 `AuthGate.test.tsx` 6 项：未启用/已认证直接渲染、未认证渲染登录页、登录成功进入应用、登录失败提示、401 经真实 `QueryCache.onError` 路径回落登录页；`npm test` 33 项、`typecheck`、`lint`、`build` 全部通过。
- Python 依赖零新增，`requirements.lock` 不变；`src/` 网格 CLI 不受影响。

### 多智能体对抗审查修复

实现完成后运行四维（安全/正确性/回归/测试盲区）审查工作流，对发现逐项复现修复：

- **非 ASCII Cookie 签名 500（major，已复现修复）**：Cookie 头经 latin-1 解码后签名段可含高位字节，`hmac.compare_digest` 对非 ASCII str 抛 `TypeError`，未认证请求得到 500 而非 401。`verify_session` 改为按字节比较（`sig.encode("latin-1")`），畸形一律返回 False。
- **畸形密码哈希 500（自测发现，已修复）**：`verify_password` 对 `n=0`、负数、超范围参数等损坏编码串捕获 `hashlib.scrypt` 的 `ValueError/TypeError`，返回 False 而非让登录请求 500。
- **自动文档端点绕过登录（minor，已修复）**：FastAPI 默认 `/docs`、`/redoc`、`/openapi.json` 不以 `/api/` 开头，未登录可读完整 API 结构；`create_app` 显式关闭三个文档端点。
- **匿名健康快照泄露（minor，已修复）**：`/api/health` 匿名可访问但原样返回 `worker.id`（主机名）与 `igg.path`（完整安装路径）；鉴权启用时匿名请求将二者置空，已认证返回完整快照，前端横幅不受影响。
- **测试盲区（major + 3 minor，已修复）**：401 回落测试改为经真实 `QueryCache.onError` 路径（原版手工 invalidate 绕过了被测逻辑）；补登出测试机制注释更正（httpx 在 Max-Age=0 即丢弃 Cookie）、Cookie `Max-Age=604800`（7 天）断言、未认证超大上传 401 先于 413 的中间件顺序回归。

## 2026-08-24：本地平台一键启动与 .env 配置

### 启动流程

- 新增 `platform/deploy/run-local.ps1`，从单个终端启动 API 和 Worker；API 作为受控子进程运行，Worker 保留在当前终端，按 `Ctrl+C` 时同步清理 API。
- 启动前显式检查数据库版本和前端构建产物；首次安装或代码升级可通过 `-Migrate` 明确执行迁移，不在普通启动中隐式修改数据库结构。
- API 启动后轮询健康接口，确认就绪再启动 Worker；API 标准输出与错误日志统一写入 `<MESH_DATA_DIR>/logs`。

### 环境配置

- 本地脚本只读取仓库根目录 `.env`，与网格 CLI 共用同一份本机配置；删除第二份 `platform/.env` 入口及 `-EnvFile` 分支。
- 未配置平台专用 IGG 路径时，兼容复用根目录 `.env` 的 `IGG_EXE` 或 `IGG_PATH`；未配置数据目录时默认使用可丢弃的 `runs/platform-dev`。
- 将配置模板从 `platform/.env.example` 移至根目录 `.env.example`，统一记录 CLI、平台数据目录和 Worker 资源参数；同步更新根 README 与平台部署说明。
- 生产 API/Worker 脚本继续只读取进程或系统环境变量，不隐式加载仓库配置。

### 验证

- 使用 Windows PowerShell 5.1 完成脚本语法解析；脚本采用带 BOM 的 UTF-8 编码，避免中文内容被系统代码页误读。
- 通过 `-Migrate -Port 8765` 完成迁移启动验证，并使用合并后的根目录 `.env` 在端口 8767 复验日常启动；数据库版本检查通过，`/api/health` 返回 `ok`，发送 `Ctrl+C` 后 Worker 退出且 API 监听端口释放。

## 2026-08-24：网格内核迁移至 src 目录

### 源码与调用路径

- 将 `mesh.py`、`controls.py`、`geomturbo.py`、`autogrid.py` 和 `quality.py` 从项目根目录迁入扁平的 `src/`；保持脚本间同级导入和仅依赖 Python 标准库的既有边界，不引入包封装或兼容入口。
- CLI 调用统一改为 `python src/mesh.py ...`；平台模块初始化时显式定位 `src/`，Worker 子进程入口同步改为仓库内绝对路径 `src/mesh.py`。
- 更新根测试、平台测试、控制验证活动、部署脚本及维护中的 Markdown/HTML 文档路径；历史开发日志不回写。

### 验证

- `python src/mesh.py --describe-control row/optimization.steps` 执行成功，确认新 CLI 入口及同级模块导入可用。
- 根测试通过：17 项测试及 5 个子测试全部通过。
- 使用既有 `platform/.venv` 完成平台回归：38 项全部通过；仅保留 FastAPI TestClient 已有的 1 条弃用警告。

## 2026-08-09：修复网页用户验收问题

### 状态呈现与可用性

- 将后端 `0..1` 归一化运行进度统一换算为整数百分比，运行树和活动事件不再把完成状态 `1.0` 显示为 `1%`，并对异常越界值做安全截断。
- 在共享会话列表和会话工作台接入 `/api/health`，每 10 秒刷新一次；Worker 离线、IGG/AutoGrid 未配置或路径不可用时，网页会展示可操作的降级告警和排队任务数量，健康请求失败时明确提示服务状态未知。
- 为 Viewer 的切片网格块与 I/J/K 轴向下拉框增加中文可访问名称，键盘和读屏用户可准确识别控件用途。

### 产物与质量对比

- 预览产物响应从既有 `previews/<run>/blocks/<block>/...` 相对路径中安全派生 `block_id`，网页以 `b0001 / surface.vtp` 形式区分同名 block 产物；不暴露存储路径，不修改数据库表结构，历史记录无需迁移即可生效。
- 质量对比表统一格式化 A、B 和差值列：真实非零极小量改用科学计数法，仅将 IEEE `-0` 归一为 `0`，避免把约 `-5.2E-9` 的差值误显示成 `-0`。

### 验证

- 新增进度换算、健康告警、预览产物 block 标识、质量数值格式和切片控件可访问名称回归；前端 Vitest 27 项全部通过，TypeScript、ESLint 与 Vite 生产构建通过。
- 平台 Python 回归 38 项全部通过；另确认产物序列化不会向 API 泄露 `relative_path`，普通非预览产物不会附带 `block_id`。

## 2026-08-06：轻量可扩展内网网格经验平台 MVP

### 分支与内核兼容

- 从最新 `main` 创建 `feat/intranet-platform-mvp`，按 `docs/PLAN.md` 从零实现内网平台；未迁移旧在线数据，也未改变根目录 CLI 的既有调用方式。
- `controls.py` 新增公开的 `enumerate_control_targets()`，网页端复用 CLI 对叶排、叶片、间隙、端壁等几何目标的适用性与精确 `#N` 选择器规则，默认排除无法从几何确认数量的静态占位目标。
- `geomturbo.py` 改为单遍逐行解析元数据和叶排拓扑，避免合法大文件的 `read_text()` 与 `splitlines()` 内存峰值；同时限制单行、token/名称、嵌套深度、叶排/叶片实体数量，校验显式块配对，并让重复 gap/fillet 侧别保持 O(1) 状态；非有限数值降级为空值，越界输入稳定返回 `INVALID_GEOMTURBO`。Rotor37、WP100_comp、ori1 的结构化结果与旧实现逐项一致。
- Web 依赖完全隔离在 `platform/`，根目录仍保持 Python >=3.7、仅标准库的运行边界；`.gitignore` 继续忽略可丢弃的 `runs/`，并取消对根目录 `tests/` 的历史忽略，使根回归、`platform/tests/` 和前端 `features/runs/` 均可随分支交付。

### 后端、持久队列与安全边界

- 新增 FastAPI API、Pydantic Schema、统一中文错误 envelope、React SPA 托管和显式 SQLite 迁移；SQLite 启用 WAL、外键、忙等待和在线备份。
- 会话、baseline 与上传几何产物在同一事务中原子创建；运行树支持幂等分支、失败重试、乐观并发经验文本和满意运行冻结。数据库触发器限制状态跃迁，禁止修改或删除终态运行。
- 独立 Worker 实现最多 20 个任务的持久调度、内存/磁盘门控、许可证失败退避、心跳、超时与僵尸修正；Windows 优先使用 Job Object 管理 IGG 进程树并保留 `taskkill /T` 降级。子进程启动后的数据库故障会立即终止进程树并回收流与任务槽。
- 网格成功与预览后处理分离：`SUCCEEDED + PENDING` 可在 Worker 重启后恢复，manifest 请求不会并发触发第二次转换，会话冻结会拒绝仍在后处理的运行。
- 上传入口在 ASGI `receive` 层按实际请求字节提前返回 413，Caddy 示例同步限制 multipart 请求体；空文件、解析失败或数据库失败会同时清理临时文件、未登记源文件和本次空会话目录。产物访问使用数据库登记与安全相对路径，支持规范的 Range/206/416，禁止目录穿越和 `/api` SPA 回退。
- 备份使用 SQLite 在线快照、唯一目录和产物清单，排除活动数据库及 WAL/SHM；并发备份不会互相覆盖。

### 真实网格预览与前端工作台

- 使用 `h5py + numpy` 直接读取 HDF5 CGNS，预生成每个结构化 block 的真实表面与线框 VTP，并按 block、I/J/K、0 基索引原子生成和复用切片缓存；ADF 或转换失败只降级 Viewer，不改变网格任务状态。
- 新增共享会话列表和专家工作台两个路由，覆盖上传、状态筛选、不可变运行树、P0/P1/P2 控制搜索与预检、质量、活动、产物、经验文本、冻结和 URL 状态恢复。
- vtk.js Viewer 支持 block 显隐、表面/线框、I/J/K 切片和双轮同步对比；质量差值在浏览器端计算。修复 Vite 8 下 `xmlbuilder2` Node 入口导致的 vtk.js 运行时异常，生产构建显式使用其浏览器 UMD 入口。
- 前端将 `SUCCEEDED + preview_status=PENDING` 视为活动后处理状态，持续轮询会话、运行与 manifest，直到 `READY/UNAVAILABLE` 终态。

### 部署与验证

- 提供锁定的 Python/Node 依赖、`.env.example`、Caddy 2.10+ HTTPS 示例、API/Worker 启动脚本和默认只预览的 Windows 启动任务安装脚本；同一数据库明确只部署一个 Worker。
- Python 回归：根目录 17 项、平台 37 项全部通过；`pip check` 与 Python 编译检查通过。
- 前端回归：Vitest 15 项、TypeScript、ESLint、Vite 生产构建全部通过。
- 真实 baseline：Rotor37 为 9 block、1,464,289 点、质量 PASS；WP100_comp 为 4,239,316 点、质量 FAIL；ori1 为 2,744,343 点、质量 FAIL。三次网格命令均成功退出，验证质量 FAIL 与任务 FAILED 保持独立。
- 使用 Playwright CLI 完成 Rotor37 浏览器闭环：上传、baseline、将 `row/optimization.steps` 设为 40 的控制分支、经验文本 v1、表面/线框/IJK 切片、双轮 Viewer 与 22 项质量差值、冻结及刷新恢复；所有网格预览请求返回 200，浏览器控制台 0 错误。
- 未实际注册 Windows 启动任务、修改防火墙或启动 Caddy；这些外部系统变更继续由部署运维在确认主机、证书、许可证和账户权限后执行。

## 2026-07-25：网格控制验证结果文档完善

- 将 156 项通用候选控制参数的完整分类写入
  `docs/MESH_CONTROL_VALIDATION_RESULTS.md`：按 `EFFECT_PASS`（44 项）、
  `EFFECT_VALID_WITH_QUALITY_WARNING`（25 项）、
  `EFFECT_INVALID_MESH_ONLY`（13 项）、`NO_MESH_EFFECT`（21 项）、
  `FAIL_READBACK`（20 项）、`BLOCKED_TOPOLOGY_BASELINE`（33 项）
  逐项列出键名、中文含义、值类型、取值范围/枚举及 Rotor37 实测表现。
- 新增"网格生成流程"章节：详细说明不使用控制参数（默认路径）与使用
  控制参数两种模式下的完整网格生成过程，包括 parse_geomTurbo →
  解析控制请求 → 渲染脚本 → 按阶段应用控制 → RowWizard.generate() →
  B2B/3D 生成 → 质量评估的完整链路。
- 说明固定阶段顺序的设计原因（configuration → wizard →
  RowWizard.generate() → topology → distribution → boundary_layer →
  optimization → interface → existing_effect），以及回读三阶段验证
  （setter 前 / setter 后 / 生成后）的判定逻辑。

## 2026-07-25：Rotor37 通用网格控制全量指纹验证

### 控制策展与 API 审计

- 在不改变完整注册表和既有拓扑依赖行为的前提下，为 `controls.py` 增加
  显式通用策展集合：拓扑无关 52 项、拓扑选择器 1 项、Default 51 项、
  HOH 33 项、H&I 19 项，共 156 项；其余 188 项逐键记录排除原因。
- 建立 `CONTROL_PREREQUISITES` 和依赖深度/排序校验，显式覆盖 user 网格
  级别、untwist、优化权重、Default 模式、HOH extension/control type、
  H&I relaxation 和停滞点分布等前置条件，不改变普通 CLI 的隐式行为。
- 重新审计 344 个注册键、721 个 setter 和 706 个绑定：setter 为
  361 映射/360 排除，绑定为 704 正式/2 合成；扩展 API 扫描为
  364 映射、425 排除和 19 个激活动作。
- 显式记录 AutoGrid 17.1 厂商绑定差异：HOH leading edge cell length
  使用 SI float；trailing edge 同名 C 绑定只接受 int 且 getter 返回
  `None`。

### Schema 3 与完整网格指纹

- `mesh.py` 新增可选 `--mesh-fingerprint`；`run_summary.json` 升级为
  Schema 3，保留原字段并增加 setter 前、setter 后、3D 生成后回读，
  block I/J/K、点数/单元数、固定坐标探针、多重网格层级和聚合指纹。
- `autogrid.py` 在 CGNS 导出后由主进程通过 AutoGrid 随附的
  `hdf5dll.dll` 读取每个结构化 block 的全部连续 Float64
  `CoordinateX/Y/Z` 数据，流式计算逐 block 和聚合 SHA-256。
- 实机确认 batch 环境中的 `Block.save_coords(...,0,0,0)` 会静默返回但
  不创建文件，因此删除未调用的 IGG 侧死代码；正式指纹以完整 CGNS 坐标
  为准，仍只使用 Python 标准库。

### Campaign runner 与分析器

- 重写 `tests/test_campaign_runner.py`，支持 audit、pilot、baseline、
  cases、analyze、all、断点续跑、`--list-matrix`、默认 32 并发和
  1800 秒单例超时。
- 每个案例保存变量控制、依赖控制、上下文签名、实际参数列表、日志和
  结果；同一依赖上下文只生成一次 baseline，无效果数值控制自动追加更宽
  的安全第三值。
- 实现 Default/H&I/HOH 各 4 次 A/A、低内存负对照、HOH 最多 10 个
  单参数和 4 个组合有限修复，以及 outlet interface/trailing stagnation
  point 目标解析 smoke。
- 重写 `tests/test_analyze_results.py`，输出 344 项支持/排除表、逐参数和
  逐取值结果、完整指纹、质量指标、临界 block/I/J/K、失败簇以及
  CSV/JSON/Markdown 报告。
- 修正回读判定：AutoGrid 枚举/布尔 getter 的稳定整数一一映射视为可靠；
  只有一个默认值回读成功、其余值被重置时仍判为 `FAIL_READBACK`。
- 修复 HOH rescue 中跨族 `row/flow_path.number` 被自身 Default 依赖
  覆盖的问题，新增强制拓扑依赖函数和回归测试；修正后重新执行全部
  10 个单参数和 4 个组合案例。

### Rotor37 实机结果

- 活动目录：
  `runs/rotor37-control-validation/20260725_full_validation/`。
- 实际完成 8 个 pilot、12 个 A/A、40 个上下文基线、328 个主案例、
  36 个自适应案例和 14 个修正后的 HOH 修复案例，共 438 次成功命令；
  所有案例网格和日志均保留。
- Default 四次均为 9 block、1,464,289 点、结构有效且质量 PASS；
  H&I 四次均为 8 block、490,320 点、结构有效但质量 FAIL；HOH 四次均为
  7 block、618,495 点且重叠。
- 修正后的 HOH 14 个修复案例全部仍重叠，33 个 HOH 条件控制最终统一为
  `BLOCKED_TOPOLOGY_BASELINE`。
- 156 项最终分类：44 `EFFECT_PASS`、25
  `EFFECT_VALID_WITH_QUALITY_WARNING`、13
  `EFFECT_INVALID_MESH_ONLY`、21 `NO_MESH_EFFECT`、20
  `FAIL_READBACK`、33 `BLOCKED_TOPOLOGY_BASELINE`。
- 82 项观察到真实网格变化，其中 33 项改变拓扑/尺寸/点数/层级，49 项
  仅改变完整坐标分布；`row/low_memory_usage` 负对照指纹完全相同。

### 文档与验证

- 新增 `docs/MESH_CONTROL_VALIDATION_RESULTS.md`，同步稳定结论、推荐值域
  和跨几何迁移边界。
- 新增活动级 `results/agent_deep_analysis.md`；自动结果同时包含
  `automated_analysis.md/json`、`control_results.csv/json`、
  `case_results.csv`、`mesh_fingerprints.csv`、`quality_metrics.csv`
  和 `failure_clusters.md`。
- 更新 `README.md`、`docs/MESH_CONTROL_ITEMS.md` 和
  `docs/SOURCE_CODE_GUIDE.md` 的 344 项注册表与 Schema 3 说明。
- 全量单元测试：`69 tests passed`；核心脚本 `py_compile` 通过。
- 本次未读取、搜索、修改或执行 `archive/`，未引入第三方依赖、JSON 配置
  或包级目录。

## 2026-07-25：测试脚本重命名与归档 + low_memory_usage 修复 + 全量指标比对

### 测试脚本归档

- `campaign_runner.py` → `tests/test_campaign_runner.py`
- `analyze_results.py` → `tests/test_analyze_results.py`
- 两者均为纯测试/验证基础设施，不影响项目主要功能。移入 `tests/` 后遵循 `test_*.py` 命名约定。
- `test_campaign_runner.py` 的 `PROJECT_ROOT` 修正为 `parent.parent`（适配新目录层级）。输出目录 `runs/` 不变。
- 用法更新为：`python tests/test_campaign_runner.py`、`python tests/test_analyze_results.py <campaign_dir>`

### row/low_memory_usage 修复

- **根因**：AutoGrid 17.1 `enable_low_memory_usage()` 内部调用 `set_row_properties_(impl, "memory_use", 1)`，传入 int 但底层 C 函数期望字符串。
- **修复**（`controls.py`）：绕过 broken enable/disable 方法，改为直接调用全局 `set_row_properties_` 并传入 `"1"`/`"0"` 字符串值。
  - `setter="set_row_properties_"`、`value_map=((True, "1"), (False, "0"))`、`setter_mode="row_property_memory_use"`
- **修复**（`autogrid.py`）：`_invoke_control()` 新增 `row_property_memory_use` 分支，通过 `_require_global("set_row_properties_")` 调用并传入 `target.impl`。
- **修复**（`controls.py` 审计）：新增 `_RUNTIME_GLOBALS` 集合，`audit_control_bindings()` 增加模块级函数回退查找。`set_row_properties_` 是 C 扩展暴露的全局 helper，不在源码中以 `def` 定义。

### 分类器重写：从单指标 → 全量指标比对

- **旧行为**：`classify_control_result()` 只检查 `total_points` 一项是否与基线不同 → 可能漏掉只改变质量指标但不改变点数的参数。
- **新行为**（`tests/test_campaign_runner.py`）：
  - `collect_quality_metrics()` 自动抽取 `.qualityReport` metrics 中**全部 22 个标量数值字段**（`number_of_points`、`min/max/avg_*`、`negative_cells`、`wall_distance_uniformity` 等）。
  - `classify_control_result()` 遍历全部指标逐一与基线比对，**任意一项不同 → EFFECT_OK**。
  - 质量判定（`status`/`accepted`）变化 → 升级为 QUALITY_SENSITIVE。
  - CALL_ONLY 现在意味着 22+ 项指标全部与基线严格一致。

## 2026-07-25：第三轮验证活动 — 405/406 (99.8%) + Python 2 unicode 修复 + 测试值生成完善

### 第三轮验证结果

全面修复后运行 `python campaign_runner.py`：
- **405/406 成功（99.8%）**，仅 1 个残余失败
- 修复项：unicode 编码 ×2、测试值生成 ×11、far_field_constant_cells_percent float→int

### 本轮修复

| 修复项 | 文件 | 说明 |
|---|---|---|
| Python 2 unicode→str 编码 | `autogrid.py` | 新增 `_ensure_str()` 辅助函数，将 JSON 解码的 unicode API 值编码为 bytes（`str`），避免 Python 2 C 扩展报 `TypeError: Expecting string` |
| multigrid value_map 恢复 | `controls.py` | 从 `((True, 1), (False, 0))` 恢复为 `((True, "yes"), (False, "no"))`——unicode 编码修复后字符串可正常工作 |
| low_memory_usage getter 禁用 | `controls.py` | `getter=None`——`get_low_memory_usage()` 在 Python 2 下返回类型与 JSON 序列化冲突 |
| far_field_constant_cells_percent | `controls.py` | `value_type` float→int（float→int 修复遗漏项） |
| int 测试值范围修复 | `campaign_runner.py` | `generate_test_values()` int 分支重写：窄范围 [0,1] 取边界值、target_points "points"→专用大值分支、通用 int 取 v1 和 v1×5 |
| relaxation/blend/interpolation | `campaign_runner.py` | 测试值从 `[5, 25]`（越界）→ `[0, 1]`（合法） |

### 残余问题

- **`row/low_memory_usage`**（1 例）：`enable_low_memory_usage()` / `disable_low_memory_usage()` 在 AutoGrid 17.1 Row 对象上调用时报 `TypeError: Expecting string`——方法可能期望字符串参数而非无参调用。需查阅 AutoGrid 17.1 API 文档确认正确调用签名。

### 进展总结

| 指标 | 初始 | 最终 | 改进 |
|---|---|---|---|
| 成功率 | 332/406 (81.8%) | 405/406 (99.8%) | +73 例 |
| 失败根因分类 | 5 类 | 1 类 | 81% 类别消除 |
| 已修复 API 类型不匹配 | 0 | 17 | 全覆盖 |
| 已修复脚本级缺陷 | 0 | 3 | getter 容错 + unicode 编码 + SI int 转换 |

### 第二轮验证结果

修复后运行 `python campaign_runner.py`：
- **392/406 成功（96.6%）**，相比上轮 332/406（81.8%）提升 **14.8 个百分点**
- 74 个失败 → 14 个失败（**减少 81%**）

### 已确认修复的类别

| 类别 | 上轮失败 | 本轮失败 | 状态 |
|---|---|---|---|
| wizard 路径前缀缺失 | 7 | 0 | ✅ 已确认修复 |
| gap/stagnation 拓扑误注入 | 12 | 0 | ✅ 已确认修复 |
| float→int 类型不匹配 | 11 | 0 | ✅ 已确认修复 |
| enum/bool 类型不匹配 | 4 | 0 | ✅ 已确认修复 |
| getter 签名不匹配 | 2 | 0 | ✅ 已确认修复 |
| 值超出合法范围 | 1 | 1 | ⚠️ 仍存在（target_points 测试值 < 100） |

### 剩余 14 个失败的根因与修复

| 子类别 | 案例数 | 根因 | 修复状态 |
|---|---|---|---|
| int 值超出 [0,1] 范围 | 9 | campaign_runner 测试值生成未限制到合法范围 | ✅ 已修复（`generate_test_values()` 新增 `hi <= 2` 分支） |
| wizard/far_field_constant_cells_percent | 2 | float→int 修复遗漏项 | ✅ 已修复（`controls.py` value_type） |
| Python 2 str/unicode 残余 | 2 | multigrid / low_memory_usage setter 内部期望 `str`（bytes），但 JSON 解码后为 `unicode` | 待修复（需在 autogrid_init.py 脚本模板中对 unicode→bytes 编码） |
| 测试值超限 | 1 | target_points 测试值 33 < 100 | 待调整测试值 |

### 修改文件

- `controls.py`：far_field_constant_cells_percent float→int
- `campaign_runner.py`：`generate_test_values()` int 分支增加 `hi <= 2` 边界处理

### 预期下轮结果

修复 11 个剩余案例后：**403/406 ≈ 99.3%**（仅剩 3 个需脚本级修复）

## 2026-07-25：修复 17 个 API 类型不匹配 + getter 容错 + SI int 转换

### 修复 11 个 float→int 类型不匹配（controls.py）

AutoGrid 17.1 的以下 setter 实际期望 `int`，但 ControlSpec 中声明为 `float`：

| 控制键 | 修改 |
|---|---|
| `row/upstream.relaxation` | float → int，范围 [0, 1] |
| `row/downstream.relaxation` | float → int，范围 [0, 1] |
| `row/downstream.before_nozzle_relaxation` | float → int，范围 [0, 1] |
| `row/optimization.straight_boundary` | float → int，范围 [0, 1] |
| `blade/edge_treatment.leading_blend` | float → int，范围 [0, 1] |
| `blade/edge_treatment.trailing_blend` | float → int，范围 [0, 1] |
| `blade/b2b.default.wall_width_interpolation` | float → int，范围 [0, 1]（从循环中独立为单独 `_direct()` 调用） |
| `blade/b2b.default.throat_inlet_relaxation` | float → int，范围 [0, 1] |
| `blade/b2b.default.throat_outlet_relaxation` | float → int，范围 [0, 1] |
| `blade/b2b.default.intersection_quality` | float → int，范围 [0, ] |
| `blade/b2b.hoh.{leading,trailing}_edge_cell_length` | float → int，通过循环内条件判断区分 cell_length |

### 修复 2 个 enum + 1 个 bool 类型不匹配（controls.py）

- **`row/optimization.skewness`** / **`row/optimization.gap_skewness`**：添加 `value_map=(("no", 0), ("medium", 1), ("yes", 2))`。原无 value_map，Python 2 下 JSON 解码的 unicode 字符串传入 API 导致 "TypeError: Expecting string"。改用 int 值绕过 str/unicode 歧义。
- **`row/optimization.multigrid`**：`value_map` 从 `((True, "yes"), (False, "no"))` 改为 `((True, 1), (False, 0))`。同理避免 Python 2 str/unicode 问题。
- **`row/low_memory_usage`**：暂未修复（含 1 次失败），需进一步分析 getter 行为。

### 修复 getter 容错（autogrid.py）

- **`_apply_control()` 分离 getter 失败**：原逻辑中 `_readback()` 失败会抛出异常，导致整个控制（含已成功的 setter）标记为 failed。修改后，getter 异常被独立捕获，setter 成功时状态仍为 "applied"，错误信息记录在单独字段中。
- **HOH extension_location getter 禁用**：`blade/b2b.hoh.{inlet,outlet}_extension_location` 的 getter 需要额外参数（如 `cst_cells`），当前读回约定不支持。将其从循环中分离，显式设置 `getter=None`。setter 仍正常执行，网格变化检测不受影响。

### 修复 SI 长度转换后 int 保持（controls.py）

- `resolve_control_requests()` 中：`convert_si_length()` 总是返回 float，但对于 `value_type="int"`（如 HOH cell_length），需要 int 值传给 API。修改后在 SI 转换后对 int/tuple_int 类型额外执行 `int()` 转换。

### 测试

- 全量 26 测试通过（0.20s），所有修改后的 ControlSpec 类型和 value_map 经代码级验证。

### 进行中

- 第二轮验证活动（`campaign_runner.py`）正在后台执行，预计恢复 17 个 API 类失败 + 2 个 getter 类失败 = 共约 36 个案例。

## 2026-07-24：Rotor37 拓扑控制验证活动执行与事后分析

### 活动执行

- 运行 `python campaign_runner.py`，执行 406 个 OFAT 测试案例 + 12 个基线。
- **结果**：332/406 成功（81.8%），74 失败（18.2%），0 超时。

### 基线发现

| 拓扑 | 点数 | 负体积 | 质量 | 关键指标 |
|---|---|---|---|---|
| **Default** | 1,464,289 | 0 | PASS | 偏斜角 21.8°, 增长率 1.73, 长宽比 342 |
| **HOH** | 618,495 | **29,944** | UNKNOWN | 偏斜角 ~0°, 增长率 100, 长宽比 21,893,000（负体积副作用） |
| **H&I** | 490,320 | 0 | FAIL | 偏斜角 42.4°, 增长率 4.76, 长宽比 884 |
| 重复性 | 完全确定 | — | 完全确定 | 三种拓扑 4×A/A 均一致 |

关键发现：**HOH 拓扑对 Rotor37 产生无效网格**（29,944 个负体积单元跨 4 个 block），不能直接用于 CFD 计算。H&I 网格虽无负体积但质量明显劣于 Default。

### 74 个失败案例的根因分析

| 类别 | 数量 | 根因 | 状态 |
|---|---|---|---|
| wizard 路径前缀缺失 | 7 | campaign_runner 生成 `row:#1/grid_level` 而非 `row:#1/wizard/grid_level` | ✅ 已修复 |
| gap/stagnation 拓扑误注入 | 12 | b2b.topology 注入到 gap/stagnation 实体路径而非 blade 路径 | ✅ 已修复 |
| float 参数实际需要 int | 11 | ControlSpec value_type 与 AutoGrid API 签名不符 | 待修复 |
| enum/bool 类型不匹配 | 4 | value_map/setter_by_value 映射类型与 API 约定不一致 | 待修复 |
| getter 签名不匹配 | 2 | getter 需要额外参数，当前 _readback() 只传 1 个 | 待修复 |
| 值超出合法范围 | 1 | `row/target_points` 测试值 < 100 | 待调整测试值 |

### 控制参数有效性

| 分类 | 总数 | EFFECT_OK | CALL_ONLY | FAILED_ALL | 有效率 |
|---|---|---|---|---|---|
| COMMON_CORE | 97 | 7 | 61 | 29 | 7.2% |
| COMMON_TOPOLOGY | 1 | 1 | 0 | 0 | 100% |
| CONDITIONAL_DEFAULT | 52 | 15 | 33 | 4 | 28.8% |
| CONDITIONAL_HOH | 41 | 18 | 20 | 3 | 43.9% |
| CONDITIONAL_HI | 19 | 3 | 16 | 0 | 15.8% |
| **合计** | **210** | **44** | **130** | **36** | **21.0%** |

- 仅 21% 的参数产生可检测的网格点数变化。
- 61.9% 为 CALL_ONLY（setter/getter 成功但网格不变）。
- H&I 拓扑最突出：19 项条件参数中 16 项（84%）为 CALL_ONLY。

### campaign_runner bug 修复

- **Bug 1（wizard 路径）**：`spec.target_kind == "wizard"` 时 entity_path 从 `row:#1` 纠正为 `row:#1/wizard`。
- **Bug 2（gap/stagnation 拓扑注入）**：新增 `topo_entity_path` 参数，将拓扑上下文与参数路径分离。gap/stagnation 控制的 `--set` 从 `{entity}/b2b.topology=X` 改为 `{blade_path}/b2b.topology=X --set {entity}/param=Y`。

### 新增分析工具

- **`analyze_results.py`**（~450 行）：独立的深度分析脚本，从 campaign 活动目录读取全部 run_summary.json，生成包含根因诊断、逐参数点变化、质量影响和修复建议的综合中文 Markdown 报告。
- 产物：`analysis_report.md`（综合报告）+ `analysis_data.json`（结构化分析数据）。

### 测试

- 全量 54 测试通过。campaign_runner 修复后通过 dry-run 验证。

## 2026-07-24：campaign_runner.py — Rotor37 拓扑控制验证活动执行器

### 新增文件

- **`campaign_runner.py`**（~500 行）：按 PLAN.md 方案编排完整验证活动的独立脚本。

### 功能概览

| 阶段 | 说明 |
|---|---|
| Phase 1 | 生成 3 拓扑 × 4 重复 = 12 个 A/A 基线 |
| Phase 2 | 构建测试矩阵（基线 + 拓扑切换 + OFAT），并发执行 |
| Phase 3 | 收集结果、分类（EFFECT_OK / QUALITY_SENSITIVE / CALL_ONLY / FAILED_ALL）、生成 Markdown 报告 |

### 测试矩阵规模

- **基线**：12 例（default/hoh/hi 各 4 次）
- **拓扑切换**：3 例（每种拓扑值单独运行）
- **OFAT 参数测试**：403 例，覆盖 210 个 Rotor37 适用控制参数
  - `core`（拓扑无关，固定 default 上下文）：155 例
  - `conditional_default`：129 例
  - `conditional_hoh`：83 例
  - `conditional_hi`：33 例
  - `topology`（选择器枚举值）：3 例
- **总计**：418 例

### Rotor37 适用性过滤

- `_is_rotor37_applicable()`：自动排除 holes-line、basin-hole、pin-fins-line、endwall、snubber、blade-sheet、solid-body、lete-wizard、partial-gap、fillet 等 Rotor37 不存在的几何实体控制。
- 排除 `not_applicable_when` 不为空的控制（如 bypass、acoustic 专用控制）。
- `ROTOR37_APPLICABLE_TARGETS` 显式允许：configuration、wizard、row、blade、gap、interface、stagnation-point。

### 执行特性

- 基于 `concurrent.futures.ThreadPoolExecutor`（经验证无许可证串行限制，4 workers 加速比 3.3-3.7×）。
- 默认并发 32 workers（可配 `--workers N`）。
- 每例超时 1800s，失败案例可单独复跑。
- 增量保存 `campaign_state.json`，支持 `--resume` 从中断处恢复。
- `--dry-run` 模式：只生成 mesh.py 调用验证命令正确性，不启动 IGG。
- `--list-matrix`：打印完整测试矩阵概况。

### 用法

```powershell
# 查看测试矩阵
python campaign_runner.py --list-matrix

# 全部执行（基线 + 测试 + 报告）
python campaign_runner.py

# 分阶段执行
python campaign_runner.py --phase 1          # 只生成基线
python campaign_runner.py --phase 2          # 只执行测试矩阵
python campaign_runner.py --phase 3          # 只生成报告

# 断点续跑
python campaign_runner.py --resume runs/rotor37-control-validation/20260724_120000

# 减少并发
python campaign_runner.py --workers 16
```

### 产物结构

```
runs/rotor37-control-validation/<timestamp>/
├─ baselines/{default,hoh,hi}/A01..A04/   # 基线 run_summary.json
├─ topology/{default,hoh,hi}/              # 拓扑切换结果
├─ cases/{core,default,hoh,hi}/            # OFAT 测试结果
├─ campaign_state.json                     # 恢复点
├─ baselines_results.json                  # 基线汇总
├─ test_results.json                       # 测试汇总
├─ classification.json                     # 参数分类
└─ validation_report.md                    # 中文 Markdown 报告
```

## 2026-07-24：B2B 拓扑依赖排序、自动注入与冲突检测（PLAN.md 实施）

### 新增控制项注册（3 项）

- **`row/low_memory_usage`**（P2, wizard）：Row 级低内存占用模式，通过 `enable_low_memory_usage` / `disable_low_memory_usage` 切换。
- **`stagnation-point/distribution_from_expansion_ratio`**（P2, existing_effect）：停滞点膨胀比分布模式开关。
- **`stagnation-point/desired_expansion_ratio`**（P2, existing_effect）：停滞点目标膨胀比，setter 为非标准命名 `desired_expansion_ratio(value)`。
- **非标准 setter 支持**：新增 `_ALL_KNOWN_SETTERS` 全局集合，收集所有 ControlSpec 中引用的 setter（含非 `set_*` 命名），`autogrid.py` 的 `_serialize_control_plan()` 校验同步放宽，允许 `desired_expansion_ratio` 等非标准方法通过。

### 修复 C-02：B2B 拓扑依赖排序

- **根因**：`resolve_control_requests()` 在同一阶段内按目标路径和键名（字典序）排序，`blade/b2b.hoh.*`（'h' 开头）排在 `blade/b2b.topology`（'t' 开头）之前，导致条件参数在拓扑选择器之前执行。
- **修复**：新增 `_topology_sort_key()`——`blade/b2b.topology` 在同阶段同目标内强制优先（排序权重 0），其他控制为 1。
- **验证**：`test_topology_selector_before_conditional_within_same_stage`、`test_topology_selector_before_hoh_conditional`、`test_topology_selector_before_hi_conditional` 三个测试覆盖 Default/HOH/H&I 三种拓扑。

### B2B 拓扑自动注入

- **`_ensure_topology_selectors()`**：在 wildcard 展开后、精确选择器覆盖前运行。
  - 若使用了 `b2b.default.*` / `b2b.hoh.*` / `b2b.hi.*` 条件参数但未显式设置 `b2b.topology`，自动注入对应拓扑选择器（source=`auto-inject`）。
  - 若显式拓扑已存在且兼容，不覆盖。
  - 通过 `CONDITIONAL_KEY_TOPOLOGY` 反向映射确定所需拓扑值。

### 跨拓扑冲突检测

- **同一叶片跨拓扑族**：同时使用 HOH 和 H&I 条件参数 → `ControlValidationError("跨拓扑冲突")`。
- **显式拓扑与条件参数不匹配**：设置 `b2b.topology=default` 但使用 `b2b.hoh.*` → `ControlValidationError("拓扑冲突")`。
- **同一 control_id 无需额外校验**：跨拓扑冲突在 auto-inject 阶段提前检测，不依赖 IGG 运行时 `_check_topology()`。

### 控制项过滤器

新增按拓扑依赖关系分组的过滤器常量（`controls.py`）：

| 常量 | 说明 | 数量 |
|---|---|---|
| `COMMON_CORE_KEYS` | 拓扑无关通用控制 | ~300 项 |
| `COMMON_TOPOLOGY_KEYS` | 拓扑选择器本身 | 1 项 |
| `TOPOLOGY_DEFAULT_KEYS` | `topologies=("default",)` | ~30 项 |
| `TOPOLOGY_HOH_KEYS` | `topologies=("hoh",)` | ~24 项 |
| `TOPOLOGY_HI_KEYS` | `topologies=("hi",)` | ~13 项 |
| `COMMON_KEYS` | 以上全部并集 | ~370 项 |

另有 `TOPOLOGY_KEY_MAP`（拓扑值→键集合）和 `CONDITIONAL_KEY_TOPOLOGY`（条件键→所需拓扑值）两个辅助映射。

### 测试

- 新增 `TopologyDependencyTests` 测试类，15 个测试覆盖：
  - 拓扑排序（3 个）
  - 自动注入（4 个，含 Default/HOH/H&I 各自验证及显式不覆盖验证）
  - 冲突检测（2 个：跨拓扑混用 + 显式/条件不匹配）
  - 通用控制不触发注入（1 个）
  - 注册表完整性（2 个：3 新控制 + 非标准 setter）
  - 过滤器非空（1 个）
  - user 拓扑枚举（1 个）
- 全量 **54 测试通过**（39 旧 + 15 新）。
- 未修改 `archive/`、`geomturbo.py`、`quality.py`、`mesh.py`。

## 2026-07-24：并行许可证测试 + 修复 RowWizard 覆盖 mesh_level/target_points

- **并行许可证测试**：在 Rotor37 上使用 4 组控制参数（coarse/medium/fine/user+800k），以串行、线程并行、进程并行三种方式各执行 1 次（共 12 次 IGG 运行），验证 NUMECA AutoGrid 17.1 许可证并发能力。
  - **结论：无串行限制，完全并发**。4 workers 的加速比：线程 3.3×（95s→29s），进程 3.7×（95s→26s）。
  - 大批量并发（20-50 个 mesh）推荐 `ThreadPoolExecutor`：启动快（微秒 vs 秒级 spawn）、内存省（共享进程 vs 每 worker 80-200MB）、可靠性等同（IGG 本身就是独立 OS 进程）。
  - 详见 `tests/tmp_parallel_test/`（可随时清理）。

- **修复 C-03：RowWizard 覆盖 `row/mesh_level` 和 `row/target_points`**。
  - **根因**：`--mesh-level`（以及 `--target-points`）映射到 `row/mesh_level`（stage="distribution"），运行在 `RowWizard.generate()` **之后**，wizard 内部用自己的默认 `grid_level`（=medium）生成网格，覆盖了行级设定。
  - **修复**：
    - `controls.py`：`row/mesh_level` 和 `row/target_points` 的 stage 从 `"distribution"` 改为 `"wizard"`，使其在 `RowWizard.generate()` 之前执行。
    - `mesh.py`：`--mesh-level` 映射从 `row:*/mesh_level=X` 改为 `row:*/wizard/grid_level=X`（直接控制 wizard 的 grid_level）。`--target-points` 额外追加 `row:*/wizard/grid_level=user` 以确保 wizard 进入 user 模式。
  - **验证**：Rotor37 三组实测——
    | 参数 | 修复前点数 | 修复后点数 | 变化 |
    |---|---|---|---|
    | coarse | 1,464,289 | 1,504,585 | +2.8% |
    | medium | 1,464,289 | 1,643,505 | +12.2% |
    | user+500k | 1,464,289 | 1,919,853 | +31.1% |
    - 三个不同 level 现在产生**显著不同**的网格，证明参数已生效。

## 2026-07-24：网格控制参数验证执行

- 按照 `docs/MESH_CONTROL_AUDIT_AND_TEST_PLAN.md` 的规范执行了 Phase A（扩展静态 API 审计）、Phase B（A/A 基线）和 Phase C（P0 冒烟验证）。
- **Phase A**：对 `Autogrid.py` 全部 1,990 个方法完成分类审计，发现 21 个未纳入注册表的纯网格控制、36 个 enable/18 个 disable/11 个 compute/8 个 generate 操作缺口。产出 `runs/control-validation/results/source-audit.csv` 和 `unaccounted-mesh-controls.md`。
- **Phase B**：在 Rotor37、WP100_comp、ori1 上建立 A/A 基线，确认 AutoGrid 17.1 生成完全确定（两次相同运行所有质量指标一致，TRB 字节相同）。
- **Phase C**：完成全部 10 个 P0 参数的真实网格验证，8 个到达 L4（EFFECT_OK）或 L5（QUALITY_SENSITIVE）。发现关键问题：RowWizard 默认覆盖 `row/mesh_level` 和 `row/target_points`。
- **缺陷复现**：确认 C-01（审计盲区）、C-02（拓扑顺序缺陷）、C-03（readback 假阳性）和 C-04（wildcard 过度展开）四个决定性问题。
- **新增文件**：
  - `runs/control-validation/campaign_001/environment.md` — 验证环境文档
  - `runs/control-validation/campaign_001/phase_a_audit.py` — Phase A 扩展审计脚本
  - `runs/control-validation/campaign_001/probe_tools.py` — 网格指纹探针工具
  - `runs/control-validation/campaign_001/p0_validator.py` — P0 增强验证器
  - `runs/control-validation/results/source-audit.csv` — 完整 API 审计 CSV
  - `runs/control-validation/results/unaccounted-mesh-controls.md` — 未计入控制列表
  - `runs/control-validation/campaign_001/results/control-results.csv` — P0 验证结果矩阵
  - `docs/MESH_CONTROL_VALIDATION_RESULTS.md` — 完整验证报告（中文）
- **未修改**：`mesh.py`、`controls.py`、`autogrid.py`、`geomturbo.py`、`quality.py` 均未触碰。

## 2026-07-23：重写 AutoGrid17 速查表 — 全量 341 项 + 质量报告字段全量拆解

- **新增** `docs/generate_cheatsheet.py`：从 `controls.py` 注册表自动提取全部 341 项 `ControlSpec`，生成完整速查表 HTML。运行方式：`python docs/generate_cheatsheet.py`。
- 控制参数：全部 341 项按 10 个作用域分组，每项展示控制键、中文含义、类型/范围、应用阶段、备注（SI 长度/拓扑限制/setter 模式）。
- **质量部分重写**：不再混杂准则名和字段名，改为严格追溯 `parse_quality_report()` 解析逻辑，分层拆解为 7 个子章节：
  - 一、报告元数据 metadata（7 字段，含解析来源）
  - 二、项目信息 project（6 顶层 + rows[] 逐行 6 字段）
  - 三、实体统计 entities[]（3 全局 + 6 准则 × {min/max/avg/location}，含 wall_distance.si 换算）
  - 四、扁平化指标 metrics（34 字段完整清单，6 准则各 5 字段：3 统计 + 1 位置 + 1 block + 1 衍生 wall_distance_uniformity）
  - 五、质量判定 result（8 必需字段 + 7 硬门槛逐条解释 + PASS/FAIL/UNKNOWN 逻辑）
  - 六、数据源优先级（.qualityReport vs CGNS 降级）
  - 七、三算例基线
  - 最差位置对象结构 critical_location（8 字段，含 derived_from 语义）
- A4 横版单列，含目录导航，PDF 14 页（~1.2MB）。

## 2026-07-23：编写源码指南

- 将已废止的 `autogrid_backend_py_dev_plan.md` 重写为 `SOURCE_CODE_GUIDE.md`。
- 新文档面向无 CFD 背景的 Python 开发者，从零开始解释 CFD 概念、网格生成原理和叶轮机械术语，然后逐模块深入源码——`geomturbo.py`、`controls.py`、`autogrid.py`、`quality.py` 和 `mesh.py`。
- 对每个数据类的每个字段、每个关键函数的调用链、选择器语法、setter 审计机制、阶段执行顺序、质量判定逻辑均做了详细的中文说明。
- 删除旧文件 `docs/autogrid_backend_py_dev_plan.md`。

## 2026-07-23：移除 conda 环境依赖

- 确认项目所有 `.py` 文件仅使用 Python 标准库（`re`、`json`、`dataclasses`、`pathlib`、`typing`、`subprocess` 等），无任何第三方包依赖。
- 将 `CLAUDE.md`（即 `AGENTS.md`）、`README.md` 和 `docs/MESH_CONTROL_ITEMS.md` 中的 `conda activate LLM` 指令替换为"依赖 Python 标准库，Python ≥3.7 即可运行"的说明。

## 2026-07-23：AutoGrid 17.1 全量网格控制与质量解析 Schema v2

### 控制注册表

- 新增根目录 `controls.py`，建立 341 项静态 `ControlSpec` 注册表，作为 CLI、校验、脚本渲染和文档查询的唯一参数来源。
- 按 P0/P1/P2 划分 10/59/272 项控制，覆盖 configuration、wizard、row、blade、gap、partial-gap、fillet、interface、endwall 和已有技术效果等作用域。
- 实现 wildcard、区分大小写名称和 1 基 `#N` 索引选择器；精确选择器稳定覆盖 wildcard，同一作用域重复定义严格报错。
- 实现 bool、int、float、enum、定长元组的安全解析、范围校验和拓扑适用性声明，不执行任意 Python。
- 实现长度 SI 输入和项目单位换算；请求值、项目单位值、API 值和回读值进入运行摘要。
- 对正式 AutoGrid 17.1 `Autogrid.py` 的 721 个 `set_*`/`a5_set_*` 完成所属类级审计：361 个映射、360 个明确排除、0 个未审计。
- 增加反向 API 绑定审计：701 个正式 setter/getter 可用，2 个 interface 布尔适配，0 个缺失绑定。

### AutoGrid 执行

- 扩展 `autogrid.py`，接收已解析控制列表，并以稳定 stdout 标记回传 requested、project value、applied、readback 和 error。
- 使用 `a5_get_row_number()` 获取真实叶排数，移除 1～99 试探式行数识别。
- 固定执行阶段为 configuration、wizard、RowWizard.generate、topology、distribution、boundary layer、optimization、interface、existing effect，然后生成 flow paths、B2B 和 3D 网格。
- 对实体 accessor、setter、getter 和拓扑适用性执行严格检查；IGG 即使错误地返回 0，只要脚本 traceback 或控制失败也统一改为返回码 1。
- dry-run 只记录 `planned`，不声称控制已应用。
- `.trb` 仅通过 AutoGrid 保存，不读取或字符串修改。
- 修正 AutoGrid 内部 Python 2 脚本编码声明、Blade solid-body 控制对象，以及 H&I edge index 和 endwall holes line 的 17.1 API 映射。

### 几何实体解析

- 扩展 `geomturbo.py`，在 blade 级记录 hub/shroud gap、partial-gap 和 fillet，用于控制选择器的静态匹配。

### 质量模型

- 重写 `quality.py` 为按章节工作的文本状态机，解析版本、项目、模板、日期、耗时、有效性、重叠状态、总点数和逐叶排项目信息。
- 对 Entire Mesh 和每个 row 解析六类指标的 minimum、maximum、average 以及 block/I/J/K 最差位置。
- 将位置统一为 `critical_location`；skewness 和 spanwise skewness 按 minimum 语义映射，其余按 maximum 语义映射，并保留报告原始方向。
- wall distance 同时保存项目单位原值和米制 SI 数据。
- 保留 CGNS 内嵌质量降级解析，无法恢复的字段保持 `null`。
- 保持原有 `PASS`/`FAIL`/`UNKNOWN` 与 `HARD_LIMITS` 判定不变。

### CLI、摘要与报告

- 增加 `--mesh-level`、`--target-points`、`--first-cell-width`、`--spanwise-paths`、`--gap-points`、`--optimization-steps` 和 `--gap-optimization-steps`。
- 增加可重复 `--set`、`--list-controls [P0|P1|P2]` 和 `--describe-control KEY`。
- 静态控制错误返回 2 且不启动 IGG；AutoGrid 内部控制或生成错误返回 1。
- `run_summary.json` 升级到 Schema v2，新增 `controls.requested/resolved/applied` 和完整 `quality.metadata/project/entities`，保留旧 `quality.metrics/result`。
- 扩充中文 `report.md`，展示实际控制、项目单位值、回读、逐叶排质量指标和最差位置。
- AutoGrid 脚本内部的实体、拓扑和 API 失败原因改为中文，并在 `report.md` 控制表和执行摘要中显示。
- CLI 输出运行摘要时关闭非 ASCII 转义，使中文失败原因可直接阅读。
- 修复 Windows 下 AutoGrid 中文异常输出被系统 GBK 解码器中断的问题：子进程改为二进制捕获，并按 UTF-8/GB18030 容错解码。
- 兼容 AutoGrid 17.1 内置 Python 2：控制异常在写入 JSON stdout 标记前安全转换为 Unicode，避免中文错误被 ASCII 二次解码异常覆盖。

### 文档

- 重写 `README.md`、`MESH_CONTROL_ITEMS.md` 和 `QUALITY_CRITERIA.md`，同步当前扁平、无配置、无模板实现。
- 将 `autogrid_backend_py_dev_plan.md` 标记为已被当前实现取代。
- 未读取、搜索、修改或测试 `archive/`。

### 验证记录

- 根目录测试：`39 passed`。
- Rotor37 默认真实 AutoGrid 17.1 运行成功，Schema v2 判定为 `PASS`。
- Rotor37 P0 实机控制验证成功：全局 grid levels、`1e-5 m` 首层宽度、73 个 spanwise paths、17 个 gap 点和 20 个优化步数均完成 setter 调用及 getter 回读。
- WP100 单行覆盖实机验证成功：`row:diffuser_axial/optimization.steps=20` 只解析到 `row:#3` 并回读 20；三行均解析出六类指标和最差位置，判定保持 `FAIL`。
- ori1 P1/P2 实机控制验证成功：Rotor 主叶片 wake control、shroud gap clustering 和 hub fillet clustering 均成功应用并回读。
- ori1 不适用拓扑实机验证成功：Default 拓扑请求 HOH wake clustering 时严格返回 1，以中文记录失败控制且不生成网格。
- 三个默认质量基线保持 Rotor37 `PASS`、WP100 `FAIL`、ori1 `FAIL`。

## 2026-07-23：补充中文 docstring

- 为 `mesh.py`、`geomturbo.py`、`autogrid.py`、`quality.py` 和 `controls.py` 补充中文模块级 docstring。
- 为上述脚本中的类、属性、公开函数和内部辅助函数补充简洁的中文用途说明。
- 本次变更仅完善源码文档，不修改网格生成、控制解析或质量判定逻辑。

## 2026-07-23：补充 MESH_CONTROL_ITEMS 和 QUALITY_CRITERIA 文档

### MESH_CONTROL_ITEMS.md 补充内容

- 新增"参数设置途径"章节，详细说明独立 CLI 参数（7 个 P0 高频项及映射表）、通用 `--set` 表达式和 Python API 三种设置方式。
- 新增"值类型与校验规则"表格，涵盖 bool / int / float / enum / tuple_int / tuple_float / SI 长度七种类型的 CLI 输入格式和校验规则，以及 SI 换算公式。
- 新增"按作用域分类总览"章节，按 target_kind 分组列出全部 341 个控制键的分布：configuration 20 项、wizard 13 项、row 39 项、blade 121 项、gap/partial-gap/fillet 19 项、interface 12 项、endwall/snubber/blade-sheet/stagnation-point 22 项、holes-line/endwall-holes-line/pin-fins-line/basin-hole 49 项、existing-effect/solid-body/lete-wizard 37 项，末尾附汇总表。
- 新增"P0 控制项 CLI 映射速查"表，列出 10 个 P0 控制键的 CLI 参数、值类型和范围/枚举。
- 新增"P1 控制项完整列表"，按 wizard / row / blade Default / gap / interface 分组列出全部 59 个 P1 键。

### QUALITY_CRITERIA.md 补充内容

- 新增"完整指标字段清单"章节（共 10 个子节），逐类列出所有可提取字段：
  - 全局计数（3 个）：negative_cells / number_of_points / grid_levels
  - 六类准则统计值（18 个）：每类 min/max/avg 字段名
  - 最差位置字段（12 个）：每类 critical_location + block 字段名
  - 壁面距离 SI 换算（4 个）：si.unit + si.min/max/avg
  - 衍生指标（2 个）：wall_distance_uniformity / generation_time_seconds
  - 元数据（7 个）：版本/日期/耗时/有效性/重叠状态等
  - 项目信息（6+ 个）：含逐行字段详解
  - 逐实体指标：entities[] 结构说明
  - 质量判定（3 个）：status / accepted / reasons
  - 辅助字段（2 个）：metrics_source / metrics
- 新增"指标总数汇总"表，按类别统计独立字段数（基础约 55 个，含逐行约 100+）。
- 新增"评估必需字段"列表，说明 evaluate_quality() 检查的 8 个必需字段。
- 新增"CGNS 降级差异"说明，列出 CGNS 内嵌解析无法恢复的字段类别。

## 2026-07-23：生成网格控制与质量速查 PDF

- 创建 `docs/AutoGrid17_CheatSheet.html`：A4 横排三栏排版的速查页，涵盖 341 个控制参数总览、P0 CLI 映射、三种设置途径、值类型校验规则、应用阶段顺序、按 18 个作用域分类分布、P1 完整列表、六类质量准则与硬门槛、最差位置字段、完整可提取字段清单（约 55 基础字段）、质量评估规则、CGNS 降级差异、三算例基线和常用命令。
- 通过 Edge 无头模式将 HTML 转为 `docs/AutoGrid17_CheatSheet.pdf`（413 KB），排版包含彩色标签（P0 绿/P1 蓝/P2 灰/SI 黄）、等宽代码字体、斑马纹表格和跨栏标题。
