# 开发日志

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
