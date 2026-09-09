# 平台共享密码登录设计（PLAN-AUTH）

## 需求与决策

目标场景：一台机器部署启动平台（`run-local.ps1`，直连路由器），同路由器下的其他用户打开网页，输入账号密码后才能使用。

已确认的决策：

- **单一共享账号**：只配置一组用户名+密码，所有用户共用；平台内部仍是共享工作区，不引入按用户的权限模型（与 `docs/PLAN.md` 的共享哲学一致，仅改变入口信任边界）。
- **应用层登录**：登录逻辑做进 FastAPI（中间件 + Cookie），不依赖 Caddy（本机未安装 Caddy，且 Caddy basic_auth 无法保护裸露的 8000 端口）。
- **7 天免登录**：登录态保持 7 天，到期或退出后需重新登录。

## 总体设计

```text
浏览器 ──HTTP(内网)──> FastAPI
   ├── AuthMiddleware（纯 ASGI，复用 _error_response 错误结构）
   │     放行：/api/health（run-local 就绪探测、Caddy 探活需要）
   │           /api/auth/*（登录/会话/登出）
   │           非 /api/*（静态资源与 SPA index.html，不含数据）
   │     其余 /api/*：校验 mesh_session Cookie → 失败返回 401 UNAUTHORIZED
   ├── POST /api/auth/login   校验用户名+scrypt 密码 → 签发 HMAC 签名的无状态 Cookie（7 天）
   ├── GET  /api/auth/session 返回 {enabled, authenticated, username}
   └── POST /api/auth/logout  清除 Cookie
```

- **无状态会话**：Cookie 值为 `exp.signature`（HMAC-SHA256 签名的时间戳），不落库、重启不失效；签名密钥从密码哈希派生，**改密码即全员下线**。
- **未配置即不启用**：`MESH_AUTH_PASSWORD_HASH` 未设置时鉴权关闭，行为与现状完全一致（本地开发与既有测试零影响）；`run-local.ps1` 在此情况下打印警告。

## 凭证配置

- `.env` 新增两个变量（生产脚本走进程/系统环境变量，与现有 `MESH_*` 约定一致）：
  - `MESH_AUTH_USERNAME`：共享用户名
  - `MESH_AUTH_PASSWORD_HASH`：`python -m mesh_app.auth` 交互式生成的 scrypt 编码串（格式 `scrypt$n$r$p$salt_hex$hash_hex`，标准库 `hashlib.scrypt`，不新增依赖）
- 两者必须同时设置，否则启动时报错（快速失败）。
- 明文密码不落任何文件；生成命令用 `getpass` 两次输入确认，输出可直接粘贴进 `.env` 的两行。

## 后端改动

| 文件 | 改动 |
|---|---|
| `platform/mesh_app/auth.py`（新建，约 150 行） | scrypt 编码/校验、会话密钥派生、Cookie 签名/校验、`AuthMiddleware`、`__main__` 生成哈希 |
| `platform/mesh_app/config.py` | `Settings` 增加 `auth_username` / `auth_password_hash` 字段与成对校验 |
| `platform/mesh_app/api.py` | 注册 `AuthMiddleware`（在 `_UploadBodyLimitMiddleware` 之后添加，使其成为最外层，未认证请求不进入上传解析）；新增 login/logout/session 三个路由 |
| `platform/mesh_app/schemas.py` | 新增 `LoginRequest`（username/password） |

- Cookie：`mesh_session`，`HttpOnly`、`SameSite=Lax`、`Path=/`、`Max-Age=604800`；不设 `Secure`（部署为内网明文 HTTP）。
- 登录失败统一返回 401 `INVALID_CREDENTIALS`（不区分用户名/密码错误）；比较均用 `hmac.compare_digest`。
- `/api/health` 保持匿名可访问（只暴露状态计数，无数据）。Worker 走 SQLite 队列不经 HTTP，不受影响。

## 前端改动

| 文件 | 改动 |
|---|---|
| `platform/ui/src/features/auth/AuthGate.tsx`（新建） | 包在 `RouterProvider` 外：查询 `/api/auth/session`，加载中显示过渡态，未认证渲染登录页，已认证渲染应用 |
| `platform/ui/src/features/auth/LoginPage.tsx` + `.module.css`（新建） | 居中卡片式登录页，用户名/密码/提交/错误提示，中文文案，复用 `global.css` 设计变量 |
| `platform/ui/src/api/client.ts` | 新增 `login` / `logout` / `getSession`；Cookie 由浏览器自动携带，JS 不接触 |
| `platform/ui/src/api/types.ts` | 新增 `AuthSession` 类型 |
| `platform/ui/src/app/queryClient.ts` | `QueryCache.onError`：任何请求 401 时失效会话查询，触发全局回落到登录页（含会话过期） |
| `platform/ui/src/main.tsx` | 用 `AuthGate` 包裹 `RouterProvider` |
| `platform/ui/src/features/sessions/SessionListPage.tsx` | 头部增加“退出登录”按钮 |

- 登录成功后 `queryClient.invalidateQueries()` 全量失效，会话查询重取通过后自动进入应用。

## 部署脚本与文档

- `platform/deploy/run-local.ps1`：加载 `.env` 后，若未配置鉴权则 `Write-Warning`（与 IGG 未配置警告同一风格）。
- `.env.example`：追加注释掉的鉴权配置示例与生成命令说明。
- `platform/README.md`：新增"访问密码"小节（配置步骤、7 天会话、退出、明文 HTTP 下载密码的风险说明、可选 Caddy 升级路径）；更新开篇"任何能访问该内网站点的人…"的表述为"通过共享密码登录后…"。
- 根 `README.md`：一句带过并链接 platform README。
- `docs/DevLog.md`：按项目规范用中文记录本次改动。

## 测试

- 新建 `platform/tests/test_auth.py`：
  - 未登录访问数据接口 → 401 `UNAUTHORIZED`；`/api/health` 匿名可访问；
  - 错误用户名/密码 → 401 `INVALID_CREDENTIALS`（两者返回一致）；
  - 登录成功 → `Set-Cookie` 存在，携 Cookie 访问数据接口 200；
  - 篡改 Cookie、过期 token（用 auth 模块签名函数构造）→ 401；
  - 登出清 Cookie 后 → 401；会话端点三态（未启用/已认证/未认证）。
- 既有测试不动：`make_app` 未设鉴权变量 → 鉴权关闭，`platform/tests` 与根 `tests` 应原样通过（回归验证）。
- 前端：新增 `AuthGate.test.tsx`（未认证渲染登录页、登录后进入应用、401 回落）；跑 `npm test`、`npm run typecheck`、`npm run lint`、`npm run build`。

## 明确不做

- 按用户账号、角色权限、注册/改密页面（保持单一共享账号）。
- 登录限速/锁定（可信内网 + scrypt 本身有计算成本，遵循项目“避免过度防御"原则）。
- 服务端可吊销的会话表（无状态设计，改密码即全员下线已覆盖主要吊销需求）。
- HTTPS（本次部署为路由器内网明文 HTTP；如需加密沿用可选 Caddy 路径，已在文档说明）。

## 兼容性

- 鉴权未配置时所有行为与现状一致；`src/` 网格 CLI 完全不受影响。
- 平台 Python 依赖零新增（scrypt/hmac 均为标准库），`requirements.lock` 不变。
