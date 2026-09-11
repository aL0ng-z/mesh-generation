# mesh-generation 深入 Code Review

**审查日期：2026-09-09**  
**仓库：aL0ng-z/mesh-generation**  
**固定提交：`2b325e942227ad8e6ef82a80c0e8f5733962c010`**  
**方式：通过 GitHub 连接器读取固定提交，静态调用链审查，抽取函数隔离复现。未修改远程仓库。**

## 结论

项目已经形成了有价值的“网格内核—Web 服务—持久队列—不可变运行树—质量与预览”结构。现阶段最重要的不是再增加控制项，而是让“输入、实际执行、产物、质量、专家判断”之间的关联可验证。

**不建议未经额外筛选，直接把当前 `SUCCEEDED` 或质量 `PASS` 当作 Agent 训练数据的可信标签。** 三条已复现的路径会破坏这一假设：旧产物被当成本次输出、未收到控制执行事件仍返回成功、非法质量数值仍被判为 PASS。（[S1][S1]、[S2][S2]、[S3][S3]）

本报告列出 **4 项 P1、7 项 P2**，另列 **2 项需要领域/数据契约复核的设计问题**，不把它们混同为已证实的实现错误。没有确认 P0 级问题。

P1：可能造成错误结果关联、错误成功/质量标签，或阻断核心任务监督，建议扩大经验采集前修复。P2：明确的可用性、性能、可移植性或测试缺陷，适用条件在各项中说明。这些审查等级与代码中控制项的 P0/P1/P2 常用程度分类无关。

## 1. 审查范围与证据边界

重点检查了 `src/` 五个核心模块的主执行链、参数解析/单位换算/目标解析、脚本生成与结果收集、质量判定；平台的 API、鉴权、SQLite 事务、会话/运行树、Worker、文件存储、备份、进程树管理与预览；以及控制编辑器、API 客户端、质量差值、vtk 渲染生命周期和相关测试/文档。

`controls.py` 较大，本次着重检查注册表的使用路径、解析、依赖、目标展开、单位换算与执行，而非逐项认证全部厂商绑定。没有在本次环境中逐一实测 344 个控制项、核验完整厂商 AutoGrid 17.1 API 或跑真实网格。没有宣称做过完整 pytest、npm test/build/typecheck、浏览器 E2E、Windows Job Object 或许可证环境验收。（[S4][S4]）

隔离实验运行在 Linux / Python 3.13.5，JavaScript 数值表达式运行在 Node.js v22.16.0。源码来自连接器读取后转录的函数摘录，并非完整检出；外部 IGG、渲染器边界被显式替换为测试桩。详细边界和原始结果见 `run_probes.py`、`audited_excerpts.py`、`probe-results.json`。

## 2. 问题总览

| ID | 等级 | 问题 | 证据类型 |
|---|---|---|---|
| CR-01 | P1 | 复用输出目录时，旧网格可关联到新运行 | runner + 假 IGG 隔离复现；CLI 门禁静态追踪 |
| CR-02 | P1 | 控制执行证据不完整，runner 仍返回成功 | runner + 假 IGG 隔离复现 |
| CR-03 | P1 | 非有限/物理无效质量数值仍能 PASS | 判定函数隔离复现 |
| CR-04 | P1 | 同步后处理阻塞 Worker 的任务监督循环 | 静态控制流确认 |
| CR-05 | P2 | 草稿不能解除依赖锁定，前置项与子项难以同轮配置 | 前后端调用链与既有测试交叉确认 |
| CR-06 | P2 | 整数输入静默截断科学计数法和小数 | 原生 JavaScript 表达式复现 |
| CR-07 | P2 | 切片整块加载，边界线框遍历整个体网格 | 静态复杂度/分配分析，非性能压测 |
| CR-08 | P2 | 登录同步 scrypt 阻塞 ASGI 事件循环 | 同调用形态隔离复现；官方文档核对 |
| CR-09 | P2 | POSIX 清理未覆盖父进程先退出的存活后代 | Linux 隔离进程组复现 |
| CR-10 | P2 | 根测试依赖被忽略且未提交的几何夹具 | 固定提交文件树、gitignore、测试源代码确认 |
| CR-11 | P2 | vtk 初始化/请求失败路径没有可靠释放资源 | 静态异常路径确认 |

## 3. P1：扩大使用前应修复

### CR-01：旧产物会被识别为本次运行的成功产物

**位置：** `src/autogrid.py::run_autogrid_init`、`collect_outputs`；`src/mesh.py::main`、`_default_run_dir`。（[S1][S1]、[S2][S2]）

`run_autogrid_init()` 对输出目录使用 `mkdir(..., exist_ok=True)`，随后覆盖输入副本和生成脚本，但不隔离既有 `mesh.igg` / `mesh.cgns` / 质量报告。`collect_outputs()` 只检查路径存在且大小大于零，不检查产物是否由本次运行生成。CLI 的最低产物门禁只要求 `igg` 被收集。

因此，已有成功产物的 `--out` 被复用，随后 IGG 异常地返回 0、但没有产生新网格时，旧文件仍被收集。即使其他控制缺少执行证据，也会落入 CR-02 的成功路径。默认运行目录使用秒级时间戳，也不适合作为并发任务的唯一身份。

隔离实验把旧 `mesh.igg` 的 mtime 设为 1970 年，假 IGG 返回 0 且不写新文件，结果为：

```text
returncode = 0
igg_collected = true
control_status = not_applied
error = null
```

这不是“真实 IGG 已被证明会这样退出”，而是证明包装器在该输入条件下没有防止错误关联。后续 `run_summary` 会记录新几何/新控制，网格和质量却可能来自旧运行，尤其危险于科学比较与训练数据。

**适用边界：**平台为新 run 使用独立 ID 目录，降低了普通 Web 分支的目录碰撞概率；风险首先在 CLI 的显式复用输出目录及默认并发命名中成立。不能据此声称所有 Web 分支都发生污染。

**修复：**采用独占的新运行目录（UUID，必要时附时间），默认拒绝包含历史运行产物的目录；在本次临时目录完成生成后原子发布。生成带 `run_id`、输入哈希、完成阶段、预期文件及大小/哈希的 manifest，并核对最终完成标记。不要仅补一个 mtime 检查，也不要简单删除用户目录作为默认“清理”。

**回归：**旧输出 + 返回 0 + 无新输出必须失败；同秒并发不得共享目录；同一显式 `--out` 的并发执行必须明确拒绝；本次缺 CGNS 时不能补用历史 CGNS。

### CR-02：`not_applied` 和缺失回读没有进入可信成功门禁

**位置：** `src/autogrid.py:128–179` 的结果合并与返回码逻辑；`merge_control_results`、`merge_post_control_results`；生成脚本的 `_apply_control`。（[S1][S1]）

代码已经正确地把没有观察到执行标记的控制标为 `not_applied`，把缺少生成后回读的控制标为 `not_observed`。问题是最后只从原始 `parsed_events` 查找显式 `failed`，没有验证期望的控制集合是否完整；脚本最后打印的完成标记也未成为必要条件。

隔离实验使用新的非空 `.igg`，给出一项请求控制，但假 IGG 不输出任何控制事件。实际结果仍是 `returncode=0`、`control_status=not_applied`、`error=null`。与 CR-01 不同，这一缺陷不需要旧输出目录。

另外，生成脚本中 setter 后的 getter 抛错会被捕获并随 `status="applied"` 输出；生成后的 getter 失败也没有进入这个返回码判定。仅凭 `applied` 不能推断“最终网格使用了请求值”。

**修复：**核对每个预期 control ID 的执行事件、唯一性、目标和阶段；缺失/重复/不匹配事件不得作为已验证执行。建立独立的状态：执行成功、控制验证状态、产物有效性、质量评价、训练样本资格。对于声明有可靠 getter 的控制，按注册表定义进行类型/单位/枚举归一化和数值容差比较；对本就没有 getter 的控制，显式标为“已调用但不可回读验证”，而不是一律失败。不要把正常的枚举转换或厂商重采样误判为精确数值不一致。

**回归：**漏一个/全部 marker、损坏 JSON marker、重复 ID、后生成回读失败、getter 与 setter 值不一致、合法无 getter 控制等分别有明确状态；失败信息不能静默丢失。

### CR-03：异常质量数值可通过全部硬阈值

**位置：** `src/quality.py:355–389`，`evaluate_quality()`；同模块数字解析模式。（[S3][S3]）

函数只检查 required 字段存在，再执行单向阈值比较，没有验证数值有限性、计数类型和物理有效范围。点数虽然是必需字段，但完全没有正值判定。

固定其他指标为满足当前阈值的基线，每次只替换一个字段，隔离实验得到：

| 输入替换 | 当前结果 | 问题 |
|---|---|---|
| `number_of_points=0` | PASS | 空网格计数没有被拒绝 |
| `max_expansion_ratio=NaN` | PASS | NaN 的比较没有触发失败 |
| `min_skewness_angle=float("1e309")` | PASS | 正无穷没有触发最小角下限失败 |
| `max_expansion_ratio=-2` | PASS | 负比值满足仅有的上限比较 |

其中 NaN 是直接函数输入的反例，不把它说成当前正则一定能从报告解析出的文字。更直接的解析入口是允许指数格式的数字文本：`1e309` 可以转成正无穷。

**修复：**在阈值判定之前增加 schema 校验：计数必须为合法整数且满足范围，所有实值必须有限，角度/比值/体积等按厂商指标定义校验物理域。非法数值进入 `UNKNOWN` 或独立 `INVALID_METRICS`，不得 PASS；记录原因。输出 JSON 使用严格有限数值约束，`allow_nan=False` 是最后一道序列化保护，不是主要的领域校验。

**边界：**这不要求把所有质量 FAIL 都映射成运行 FAILED；“产出了网格，但质量不合格”应继续是可分析的有效实验记录。质量 PASS 也仍不等于网格无关性、边界层分辨率或 CFD 解可信。

**回归：**NaN、±Inf、指数溢出、0 点、负点数、浮点/布尔计数、非法角度/比值、缺字段、边界等号都进入明确且不矛盾的状态。

### CR-04：后处理占用唯一调度循环，心跳续写不能代替监督

**位置：** `platform/mesh_app/worker.py::_poll_tasks` → `_complete_success` → `_postprocess_succeeded_run`，其中后两者在约 648–769 行；`_heartbeats_during_preview`。（[S5][S5]）

Worker 在轮询任务时，遇到成功运行就同步登记/散列产物并执行完整 `prepare_preview()`。代码自己也注明这些操作可能持续数分钟。虽然启动了轻量线程为其他 RUNNING 任务续写心跳，但主调度调用并未返回。

后果是后处理期间无法正常完成下一轮进程轮询、外层超时判断、终态登记和新任务领取；停止响应也受这一调用链影响。出现后处理卡住时，数据库可能仍表现为“Worker 在线、其他任务有心跳”，却没有完成实际监督。

**重要限定：**CLI 对 IGG 自身的 `subprocess.run(timeout=...)` 仍然存在，不应说所有超时保护都失效。这里失效或延迟的是 Worker 外层监督、任务状态推进和整个调度循环。

**修复：**把后处理放进独立、受限的执行通道，保留单并发也可以；对 CPU/内存较重且需要可终止的转换，优先使用专用子进程。主线程继续轮询/超时/停止，单独记录后处理任务状态与超时。资源预算应计入后处理内存，不能因为生成任务已 SUCCEEDED 就把实际仍占用的资源忽略。

**回归：**令 A 的预览阻塞、B 到达外层超时，断言 B 仍在规定容差内被清理；A 后处理挂起时停止信号仍被处理；后处理失败只降级预览，不回写网格终态。

## 4. P2：具体的功能、性能与维护问题

### CR-05：前置控制在草稿中已设置，子控制仍被锁定

**位置：** `platform/ui/src/features/controls/ControlEditor.tsx` 的 `ControlRow` / `controlQuery` / `previewQuery`；`control_service.py::get_control_state`、`_availability`。（[S6][S6]、[S7][S7]）

`disabled = frozen || item.availability !== 'EDITABLE'` 完全依赖从父运行快照算出的 availability。草稿预检虽然调用服务端，但返回结果没有更新控制目录的草稿态可编辑性。

例如 baseline 上 `row/target_points` 被锁定；用户把 `row/mesh_level=user` 加进当前草稿后，目标点数输入仍锁定，难以一次提交两项设置。服务端测试明确证明两项同时传入的 preview 可以通过，因此这里是 UI 无法表达后端已支持的操作，而不是服务端不支持。（[S8][S8]）

**修复：**让预检响应返回基于“父快照 + 当前草稿”的 effective availability，或增加显式草稿态目录接口；保持服务端规则为唯一来源，不在 React 复制一套依赖图。冻结/运行状态锁定仍单独生效。

**回归：**选 user 后同一草稿可输入 target_points；前置设置撤销后子项状态/required_clears 正确；整个过程不应要求先消耗一次真实网格运行。

### CR-06：整数输入发生静默数值截断

**位置：** `ControlEditor.tsx:29–34`，`parseInputValue()`。（[S6][S6]）

整数分支使用 `Number.parseInt(raw, 10)`。Node.js 实测：`"1e3" → 1`，`"1.9" → 1`，空串 → NaN。后端严格验证收到的整数也无济于事，因为原始用户输入已经被前端改成了另一个整数。最终是否入库仍取决于该控制的范围，但例如允许 1 的整型控制会直接接受被截断后的数值。

**修复：**编辑阶段保存字符串，提交/预检前严格解析。明确决定科学计数法是否支持；支持时使用 `Number` 并检查 `Number.isSafeInteger`，不支持时用完整整数格式校验并报错。空串必须是“未完成编辑”而不是强行变成 0/NaN 或自动 clear。

**回归：**`1e3`、`1.9`、`+10`、空串、超安全整数、粘贴输入及输入法中间态。

### CR-07：预览成本随体网格增长，而不只是所请求的面/切片

**位置：** `platform/mesh_app/preview.py::get_or_create_slice`、`_read_points`、`_wireframe_geometry`；API 的 mesh slice 路由。（[S9][S9]、[S10][S10]）

单个 I/J/K 切片请求先通过 `_read_points` 把 X/Y/Z 全部体坐标读入，转置并 stack 成完整 `(I,J,K,3)` 数组，最后 `np.take` 取平面。Float64 情况下，仅三个坐标源数组和叠加数组就约为 `48N` 字节，尚未计算有限性掩码、Python 拓扑对象、输出缓存等。这是静态内存模型，不是实测峰值。

边界线框函数又用三组完整三重循环遍历体网格，只在内部筛选边界线段，实际为 O(IJK) 的 Python 遍历，而输出只涉及表面。API 线程池上的多个不同切片请求会叠加这种资源消耗。另一个相似热点是质量的 CGNS fallback 对整文件 `read_bytes()` 后再解码，应纳入运行内存预算。（[S3][S3]）

**修复：**切片用 HDF5 hyperslab 直接选取平面，并维护 K/J/I 到 I/J/K 的正确索引映射；线框只枚举边界索引。对同一未生成资产做单飞去重，并限制并发、内存和缓存体积。h5py 官方文档明确支持直接将切片转换为 hyperslab 选择，无需先读全体数据。（[E3][E3]）

**回归：**大 block 单切片读取元素量随平面而非体积增长；相同切片并发只生成一次；线框点/线拓扑与旧实现小网格基准一致；进行真实内存/延迟基准后再调整 Worker 预留值。

### CR-08：登录路由在 async 上下文直接执行 scrypt

**位置：** `platform/mesh_app/api.py` 的 `login` 路由；`auth.py:46–76` 的 `verify_password()`。（[S10][S10]、[S11][S11]）

异步路由直接调用同步 scrypt 校验，未像多数其他服务调用一样通过 `run_in_threadpool` 处理。FastAPI 不会自动把在 async 函数中手动调用的普通工具函数移到线程池。（[E1][E1]）

同调用形态的隔离实验中，一次校验约 0.25 秒；预约的 5ms 事件循环回调直到校验结束并让出控制后才执行。该数值只是本次环境测量，不是生产性能承诺，也不是一次 HTTP DoS 压测。

**修复：**通过有界线程/工作队列卸载校验，对并发设置上限。登录限速虽被 MVP 文档明确排除，仍应在扩大网络访问范围时重新评估；本条确认的实现问题是事件循环阻塞，不把“没有实现文档明确不做的功能”单独算缺陷。（[S12][S12]）

**回归：**并发登录时 health/普通 API 的事件循环延迟可控，且哈希并发不会无界占用内存。HTTPS 部署另建议支持 Secure Cookie 配置，并保留本机 HTTP 开发路径。

### CR-09：POSIX 父进程退出后，后代进程可能没有被清理

**位置：** `platform/mesh_app/windows_job.py::ManagedProcess.terminate_tree`、`_terminate_posix_group`、`close`。（[S13][S13]）

当前逻辑对进程组发送 SIGTERM 后，只等待直接父进程。父进程先结束就不会再进入 SIGKILL 分支；`close()` 只对 Windows Job 句柄做清理，在 POSIX 不终止残余进程组。若调用前父进程已经结束，函数还会直接 close 并返回。

隔离测试让孙进程忽略 SIGTERM：`terminate_tree()` 返回后父进程已结束，但孙进程仍存活。本次复现的独立进程组在 finally 中已被 SIGKILL 清理。

**适用边界：**当前平台生产模型明确以 Windows 为主，本项不证明 Windows Job Object 实现同样有问题，也不作为 Windows 上最高优先级阻塞项。它违反的是此模块明确提供的 POSIX 等价终止行为。CLI 直接使用 `subprocess.run(timeout=...)` 与平台进程树管理也不是同一种保证；Python 的该超时约定针对启动的子进程，不应据此推断整棵树都被处理。（[E2][E2]）

**修复：**启动时保存进程组身份，父进程退出后仍检查/处理残余组成员；宽限期后终止存活组，避免依赖一个已回收 PID 再查 pgid。保留所有权边界，不能以宽泛进程名杀进程。

**回归：**父先退出、孙忽略 SIGTERM、重复清理、正常完成后残余后代；Windows Job 真机验收单独进行。

### CR-10：根测试不是自包含的干净检出测试

**位置：** `.gitignore` 的 `geometries/`；`tests/test_geomturbo.py`。（[S14][S14]、[S15][S15]）

固定提交的文件树不包含 `geometries/`，并且它被显式忽略。以下四个测试没有跳过/自动提供夹具的分支，却直接读取该目录：`test_parser_streams_file_without_read_text`、`test_rotor37_summary`、`test_wp100_summary`、`test_ori1_existing_fillet_side_is_discovered`。

所以，没有另外放入文件的干净检出会在这些测试执行时发生 FileNotFoundError。**这是静态确定的失败路径，不是本次实际运行完整 pytest 的统计，也不是测试收集/import 失败。** `test_campaign_runner.py` 本身就是验证器，不能把它误判成“引用了删除掉的辅助脚本”。

**修复：**给普通单元测试提交小型、可分发的合成几何夹具；真实工程几何放进明确的集成测试组，缺少时 skip 并说明配置路径。fixture 不需要包含商业几何或大数据。

**回归：**干净 checkout 不安装 NUMECA、不放私人几何也能跑单元测试；真实几何/许可证测试通过明确 marker 或独立入口开启。

### CR-11：vtk 对象只有完全初始化成功后才获得有效 disposer

**位置：** `platform/ui/src/features/mesh/MeshCanvas.tsx` 的 effect 初始化和 catch 分支。（[S16][S16]）

代码先创建 `GenericRenderWindow`，接着 fetch、解析 VTP 和创建 reader/mapper/actor，直到全部成功后才赋值实际 `dispose`。如果资源请求失败或解析抛错，catch 只更新 UI 状态；effect cleanup 仍可能是初始空函数，已创建的 vtk 对象没有可靠释放。部分 reader 也可能在进入追踪数组前抛错。

这是静态可确认的生命周期遗漏；本次没有声称测量了 GPU 泄漏大小或真实浏览器崩溃次数。

**修复：**创建第一项资源时就安装幂等清理函数，每创建一项立即登记所有权；错误和取消路径调用同一清理函数。请求使用 AbortController，避免切换运行/离开页面后仍读入无用资产。

**回归：**fetch 401/500、单个 VTP 损坏、挂起请求中卸载、连续切换 block/run 时，已创建对象都被释放，监听器数量不持续增长。

## 5. 需要先澄清领域/数据契约的问题

### D-01：实验取值 200 / 9 是否被误用为通用前置条件

`CONTROL_PREREQUISITES` 同时被 campaign 用来构造测试上下文、被 Web 平台用来做严格的领域前置校验。`_missing_prerequisites` 使用等值判断，导致 skewness 等优化项要求 `optimization.steps == 200`；部分 throat 子项要求点数恰好等于 9。隔离验证证实 steps=100/300 被判缺依赖，200 通过。（[S7][S7]、[S17][S17]）

**这一行为已由 `platform/tests/test_controls.py` 明确测试。** 不应建议简单“补测试”或在没有领域确认时直接删掉测试。与此同时，注册表对普通优化步数的范围是 0–100000；现有 CLI 并不以同样方式强制这些配方等值。（[S8][S8]、[S17][S17]）

需要核对的不是“当前实现有没有等于 200”，而是“厂商接口或产品定义是否真的只允许该特定值”。本次没有实机证据证明它是 AutoGrid 的必要条件。

建议分开表达 `activation_predicates`（实际启用条件）、`recommended_context`（建议使用场景）和 `campaign_test_values`（验证采样值）。例如确认优化要求确实仅为正步数后，才把等于 200 改为相应谓词。切换步数时是否应清除其他优化设置，也应由真正失效条件决定。

### D-02：运行树、重放语义和训练样本资格应显式化

平台的新运行将完整显式快照转换为 CLI 参数，再从 geomTurbo 初始化新项目；不是在父运行已经生成的网格上 warm-start 继续修改。因此 `parent_run_id` 主要表达决策/比较关系，不等价于厂商求解器的增量状态继承。（[S1][S1]、[S5][S5]、[S18][S18]）

现有 requested/resolved/applied/post_generation、不可变分支、几何哈希、产物哈希、版本锁都是好基础。但要形成可重现训练集，建议补充：实际执行代码提交/工作树摘要、控制注册表版本、厂商 API/构建版本、脚本哈希、质量判定规则版本、单位契约、控制验证状态、网格身份，以及人工偏好/满意标记的独立含义。

排队期间升级代码可能改变同一快照的解释；应固定任务执行版本或至少清楚记录入队与执行版本差异。最终样本不应只携带“requested 参数 + PASS”，而应包含可观察到的最终状态和证据完整性。

项目的 campaign 已有 `_source_signature()`，不能把项目描述为“完全没有来源记录”；更适合复用这一思想补到平台运行契约中。（[S19][S19]）

建议把样本资格与任务状态分离：网格运行成功但控制验证不足的记录可保留诊断用途；质量 FAIL 的有效实验可作为失败经验；专家满意是偏好证据，不自动升级为数值可信标签。

## 6. 已有设计中应保留的部分

SQLite 的 WAL、外键、短事务和原子领取，与文档限定的单 Worker、多子进程模型相称；不需要为了“企业级”而先换成复杂分布式任务系统。会话/version 与 note_version 分离，分支不可变、重试建立新节点、冻结前检查运行与预览窗口，体现了对多人协作一致性的考虑。（[S5][S5]、[S18][S18]、[S20][S20]）

上传大小在 multipart 落盘前限制，几何解析又限制单行、标识符、深度和实体数量；存储使用受控根目录、生成式文件名、临时文件原子发布、散列登记和按 artifact ID 下载。这些是已实现的保护，不应泛泛写成“没有上传校验/路径校验”。（[S10][S10]、[S21][S21]、[S22][S22]）

参数白名单、类型/范围校验、有限输入浮点数检查、SI 长度换算、几何目标展开与分阶段应用是应继续保持的控制架构。宿主 Python 与厂商脚本运行环境的边界也不应在重构中随意抹平。（[S1][S1]、[S17][S17]）

质量状态、生成状态、预览状态分离，以及 ADF/依赖缺失时的预览降级，是合理设计；质量 FAIL 仍可保存为研究数据。（[S3][S3]、[S9][S9]、[S12][S12]）

共享账号/专家署名不构成多租户权限，是文档明确选择的共享内网模型，不把“没有逐用户 RBAC”列为漏洞；多 Worker 横向扩展也明确不支持，所以本次不把仅启动时恢复任务当作当前部署模型的确定缺陷。（[S12][S12]）

## 7. 依赖与运维文档核对

`platform/ui/package-lock.json` 中 react-router 与 react-router-dom 已锁定为 7.18.2。核对官方 GHSA-qwww-vcr4-c8h2：7.x 的影响范围为 >=7.12.0 且 <7.18.2，修复版包含 7.18.2，且公告限定不稳定 RSC API。（[S23][S23]、[E4][E4]）

因此 README 中“当前仍有这条链的 2 个 high、等待修复版”的说明已不符合该锁定版本与公告。应更新记录并重新运行 npm audit，而不是继续把这一条当成当前已知未修复漏洞。**本次未跑完整依赖审计，不由此推断全部依赖均无漏洞。**（[S12][S12]）

备份入口实际提供 SQLite 在线副本和产物清单，并不复制全部网格文件。文档和操作规程应明确：恢复演练必须同时具备实际产物文件及其核验结果，manifest 不是文件备份本身。其使用 SQLite Online Backup API 而非裸拷贝 WAL 数据库的方式应保留。（[S24][S24]）

## 8. 建议的修复顺序与验收

| 顺序 | 最小交付 | 可验证的完成标准 |
|---|---|---|
| A | CR-01/02/03：可信结果边界 | 非本次产物不入本次 manifest；控制证据缺失不作已验证成功；非法指标不 PASS |
| B | CR-04：监督与后处理解耦 | 预览阻塞不延迟其他任务外层超时和停止；内存预算覆盖后处理 |
| C | CR-05/06/11：可表达且稳定的编辑/查看 | 同草稿可配置前置项及子项；整数不静默变值；异常 vtk 路径可释放 |
| D | CR-07/08/09/10：资源与测试基线 | 平面读取规模、鉴权并发、进程组清理和干净检出单测都有回归 |
| E | D-01/02：领域语义和采集契约 | 激活条件与采样值拆分；样本具有足够运行身份、实际控制和质量来源证据 |

建议新增或强化独立的 core unit、fake-IGG contract、platform transaction/worker、frontend interaction 四层测试，再把 AutoGrid 17.1 和浏览器真实闭环放到有许可证/几何的受控环境。没有覆盖率报告时不要以文件名数量或文档中的历史测试通过数代替本次验收结果。

最有价值的首批回归不是再增加一组 happy path，而是本报告中的旧输出复用、缺控制事件、无效质量值、后处理挂起、草稿依赖解锁、数值输入中间态和残余进程树。

## 9. 本次隔离实验摘要

`probe-results.json` 包含原始输出。

- 合法质量基线 PASS；4 个非法单字段变体均错误地 PASS。
- 旧产物不重新生成仍被 runner 收集，返回 0。
- 新产物但没有任何控制事件，控制记为 not_applied 而 runner 仍返回 0。
- 特定依赖检查 steps=100/300 失败、200 通过；这是当前限制的复现，不是厂商领域规则有效性的证明。
- 同步密码校验阻塞预约的事件循环回调，yield 后回调恢复；未做 HTTP 压测。
- POSIX 直接父进程退出但忽略 SIGTERM 的孙进程存活，探针 finally 清理独立进程组。
- JavaScript parseInt 将 `1e3` 和 `1.9` 均变成 1。

使用实际仓库模块复跑的方法见同目录 README。探针产生的“错误行为已复现”不是一组安全性回归测试全部通过；修复后应将相应输出转换为拒绝、错误状态或被正确清理的期望断言。

## 参考源

下列仓库链接均固定到审查提交。各发现已经给出函数或相关行段，避免用会随 main 移动的链接作为唯一依据。

[S1]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/src/autogrid.py
[S2]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/src/mesh.py
[S3]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/src/quality.py
[S4]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/README.md
[S5]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/mesh_app/worker.py
[S6]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/ui/src/features/controls/ControlEditor.tsx
[S7]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/mesh_app/control_service.py
[S8]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/tests/test_controls.py
[S9]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/mesh_app/preview.py
[S10]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/mesh_app/api.py
[S11]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/mesh_app/auth.py
[S12]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/README.md
[S13]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/mesh_app/windows_job.py
[S14]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/.gitignore
[S15]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/tests/test_geomturbo.py
[S16]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/ui/src/features/mesh/MeshCanvas.tsx
[S17]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/src/controls.py
[S18]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/mesh_app/sessions.py
[S19]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/tests/test_campaign_runner.py
[S20]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/mesh_app/db.py
[S21]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/mesh_app/artifacts.py
[S22]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/src/geomturbo.py
[S23]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/ui/package-lock.json
[S24]: https://github.com/aL0ng-z/mesh-generation/blob/2b325e942227ad8e6ef82a80c0e8f5733962c010/platform/mesh_app/backup.py
[E1]: https://fastapi.tiangolo.com/async/
[E2]: https://docs.python.org/3/library/subprocess.html
[E3]: https://docs.h5py.org/en/stable/high/dataset.html
[E4]: https://github.com/advisories/GHSA-qwww-vcr4-c8h2
