# Rotor37 通用网格控制参数验证结果

## 验证对象

- 几何：`geometries/Rotor37.geomTurbo`
- AutoGrid：本机 NUMECA AutoGrid 17.1
- 活动：`runs/rotor37-control-validation/20260725_full_validation/`
- 几何 SHA-256：
  `9c567bf49f0c414e41e44e840f83117a1675464559e70c35312fd6643340e0c6`
- 执行设置：最多 32 个并发子进程，单例超时 1800 秒

本结果只代表 Rotor37、当前 AutoGrid 版本及当前质量硬阈值。完整逐项数据和
Agent 工程分析分别见：

- `runs/rotor37-control-validation/20260725_full_validation/results/automated_analysis.md`
- `runs/rotor37-control-validation/20260725_full_validation/results/agent_deep_analysis.md`

## 审计和矩阵

当前注册表包含 344 个控制键。721 个 AutoGrid setter 已全部映射或显式
排除，706 个注册绑定均存在。扩展扫描还覆盖 `set_*`、`enable_*`、
`disable_*`、非标准赋值和 compute/generate 激活动作。

通用策展集合共 156 项：

| 分组 | 数量 |
|---|---:|
| 拓扑无关通用项 | 52 |
| B2B 拓扑选择器 | 1 |
| Default 条件项 | 51 |
| HOH 条件项 | 33 |
| H&I 条件项 | 19 |

其余 188 项逐键记录排除原因，主要涉及已有 gap、fillet、snubber、bulb、
bypass、孔列、针肋、固体域、声学远场或技术效果实体，不代表 API 不存在。

## 验证判据

每个成功案例均保存 IGG、CGNS、TRB、原始 `qualityReport`、日志和
Schema 3 `run_summary.json`。判定分为：

1. 生成成功：命令返回 0，四类主要产物完整；
2. 结构有效：无负体积、无重叠，validity 正常；
3. 质量通过：满足 `quality.py` 当前硬阈值。

网格变化的主证据为 CGNS 全部结构化 block 的 I/J/K、点数/单元数、
完整 `CoordinateX/Y/Z` 坐标 SHA-256、多重网格层级和聚合指纹。质量报告、
点数和普通文件哈希只作辅助证据。

三种拓扑各执行 4 次 A/A，block、完整坐标指纹和质量向量均完全重复。
`row/low_memory_usage=false/true` 负对照的网格指纹完全相同，证明运行策略
不会被误判为网格变化。

## 最终分类

| 分类 | 数量 |
|---|---:|
| `EFFECT_PASS` | 44 |
| `EFFECT_VALID_WITH_QUALITY_WARNING` | 25 |
| `EFFECT_INVALID_MESH_ONLY` | 13 |
| `NO_MESH_EFFECT` | 21 |
| `FAIL_READBACK` | 20 |
| `BLOCKED_TOPOLOGY_BASELINE` | 33 |
| 明确排除 | 188 |

82 项控制观察到真实网格变化，其中 33 项改变拓扑、block 尺寸、点数或层级，
49 项只改变完整坐标分布。

AutoGrid 某些枚举 getter 返回整数代码。只有不同请求与生成后回读构成稳定
一一映射时才视为可靠；被重置、坍缩到同一值或返回 `None` 的控制仍归入
`FAIL_READBACK`。

## 拓扑结论

| 拓扑 | block | 点数 | 结构 | 质量 |
|---|---:|---:|---|---|
| Default | 9 | 1,464,289 | OK、无重叠 | PASS |
| H&I | 8 | 490,320 | OK、无重叠 | FAIL |
| HOH | 7 | 618,495 | NOK、重叠 | 不可用 |

- Default 是 Rotor37 当前唯一可直接推荐的拓扑。基线最小 skewness
  21.761°，最大膨胀率 1.7254，最大 aspect ratio 342.35。
- H&I 减少 66.5% 点数且结构有效，但最大膨胀率 4.7581、最小展向
  skewness 130.05°，未通过硬阈值。单参数调节虽有改善，仍无 PASS 案例。
- HOH 基线双精度检查有 30,272 个负体积。修正拓扑上下文后，10 个单参数
  和 4 个最佳组合修复仍全部重叠；最佳组合仍有 21,391 个负体积。因此
  33 个 HOH 条件控制统一门控。

## Rotor37 推荐控制

### 基础设置

- B2B 使用 `blade/b2b.topology=default`；
- Default type 使用 `streamwise`；
- 保持 `wizard/full_matching=true`；
- 保持非零 `row/optimization.steps`，本次 200 步有效；
- 分辨率优先使用 `wizard/grid_level`、`wizard/spanwise_paths`、
  `row/flow_path.number`、Default inlet/outlet/BL points。

`row/mesh_level` 和 `row/target_points` 虽能回读，但没有改变 Rotor37 网格；
需要目标级别时应控制 RowWizard，而不是依赖这两个行级 accuracy setter。

### 质量优化候选

| 控制 | Rotor37 实测建议 | 主要证据 |
|---|---|---|
| `row/optimization.skewness` | `yes` | skew 32.797°，仍 PASS |
| `row/optimization.nmb` | 约 0.75 | skew 44.538°、span skew 173.29° |
| `blade/edge_treatment.trailing_rounded` | `true` | skew 49.016°、exp 2.0694 |
| `configuration/grid_levels` | 3～4 | 4 层仍 PASS，点数增加 6.4% |
| Default boundary-layer expansion | 约 1.5 | 1.1 的展向质量 FAIL |
| Wizard/Default wall width | 约 5e-6 m 起步 | 1e-6 m 在本几何上越过部分硬阈值 |
| 停滞点 constant cells | 约 60% | 20% 的 skew 12.471° |
| 停滞点 relative distance | 约 0.75 | 0.25 的 exp 4.2659 |
| 停滞点 absolute distance | 约 5e-4 m | 1e-4 m 的 exp 6.1383 |
| 目标 expansion ratio | 约 1.1 | skew 32.86°、exp 1.6379 |

这些值不能替代 `y+`、近壁模型和网格无关性分析。

### 不建议

- HOH 全族和当前 H&I 取值；
- `rounded_azimuthal` 及其方位点数族；
- sharp/blunt/blend 的非默认启用值；
- 固定 inlet/outlet angle、全局 row clustering、停滞点 parametric
  location；
- `optimization.steps=0`；
- 21 个无网格效果键和 20 个回读失败键。

## 迁移到其他几何

A/A 指纹门控、同依赖上下文比较、三层判定和生成后回读可以直接迁移。
拓扑可用性、点数、长度和优化值域不能直接迁移，必须按新几何重新建立
baseline。具有 gap、fillet、孔、针肋、snubber、bulb 或技术效果实体时，
应对本次排除的条件控制另建专用 campaign。

## 网格生成流程

以下说明基于 `mesh.py` → `autogrid.py` 的实现，展示不使用控制参数与
使用控制参数两种路径下的完整网格生成过程。

### 不使用控制参数的默认网格生成

当不提供任何 `--set` 表达式或快捷参数时，整个流程如下：

```text
┌──────────────────────────────────────────────────────────────────┐
│  1. 解析几何（mesh.py）                                          │
│     parse_geomturbo("Rotor37.geomTurbo")                        │
│     → 提取叶排名、叶片名、单位系统、已有技术效果数量等            │
├──────────────────────────────────────────────────────────────────┤
│  2. 构建控制请求（mesh.py）                                      │
│     _build_control_requests(args)                                │
│     → 无 --set、无快捷参数 → 空列表 []                           │
│     validate_wizard_compatibility([], use_row_wizard=True)       │
│     → 通过（无冲突）                                              │
│     resolve_control_requests([], geometry)                       │
│     → 空列表 []                                                  │
├──────────────────────────────────────────────────────────────────┤
│  3. 渲染 AutoGrid 脚本（autogrid.py）                             │
│     render_autogrid_script(..., controls=[])                     │
│     CONTROL_PLAN = []   ← 空控制计划                              │
│     生成的 autogrid_init.py 结构：                                │
│                                                                   │
│     ① a5_new_project(1)                                          │
│     ② a5_init_new_project_from_a_geomTurbo_file(GEOMTURBO_FILE)  │
│     ③ ROW_COUNT = a5_get_row_number()                            │
│     ④ _apply_stage("configuration")   → 无控制，空操作           │
│     ⑤ _apply_stage("wizard")          → 无控制，空操作           │
│     ⑥ _generate_row_wizards()                                    │
│        → 对每个叶排调用 row_wizard().generate()                   │
│        → RowWizard 使用 AutoGrid 内部默认值自动分配：              │
│          · 默认流向/展向/方位点数                                  │
│          · 默认首层单元宽度                                        │
│          · 默认 B2B 拓扑 = Default（streamwise）                  │
│     ⑦ _apply_stage("topology")        → 空操作                   │
│     ⑧ _apply_stage("distribution")    → 空操作                   │
│     ⑨ _apply_stage("boundary_layer")  → 空操作                   │
│     ⑩ _apply_stage("optimization")    → 空操作                   │
│     ⑪ _apply_stage("interface")       → 空操作                   │
│     ⑫ _apply_stage("existing_effect") → 空操作                   │
│     ⑬ a5_save_project(.trb)                                      │
│     ⑭ a5_generate_flow_paths()         ← 使用默认 flow path 参数  │
│     ⑮ a5_generate_b2b()                ← 使用默认 B2B 参数       │
│     ⑯ a5_generate_3d()                 ← 生成 3D 网格            │
│     ⑰ _emit_post_generation_readbacks() → 空                     │
│     ⑱ a5_save_project / a5_save_mesh / a5_export_CGNS_project    │
├──────────────────────────────────────────────────────────────────┤
│  4. 执行 IGG（autogrid.py）                                      │
│     subprocess.run(["igg", "-autogrid5", "-batch",                │
│                      "-script", "autogrid_init.py"])              │
│     → 解析 stdout 中的控制结果标记和生成后回读标记                │
│     → 收集输出产物：.igg, .cgns, .trb, .geomTurbo, .config 等   │
│     → 可选：从 CGNS 生成完整坐标 SHA-256 指纹                    │
├──────────────────────────────────────────────────────────────────┤
│  5. 质量评估（mesh.py）                                           │
│     summarize_quality(outputs, ...)                               │
│     → 解析 qualityReport（若存在）                                │
│     → 硬阈值判定：无负体积、skew≥15°、exp≤3、span skew 偏差≤40°  │
│       span exp≤2、aspect≤15,000、multigrid≥3                     │
│     → 写入 run_summary.json 和 report.md                         │
└──────────────────────────────────────────────────────────────────┘
```

在无控制参数的默认路径中，所有网格参数由 RowWizard.generate() 和后续
B2B/3D 生成步骤通过 AutoGrid 内部启发式算法自动确定。以 Rotor37 为例，
默认结果：Default 拓扑、9 个 block、1,464,289 点、skew 21.761°、
exp 1.7254、质量 PASS。

### 添加控制参数的网格生成

当通过 `--set` 表达式或快捷参数（如 `--mesh-level`、`--first-cell-width`）
提供控制参数时，流程在阶段 2～3 产生差异：

```text
┌──────────────────────────────────────────────────────────────────┐
│  2'. 构建并解析控制请求（mesh.py）                                │
│                                                                    │
│      命令行示例：                                                  │
│      python mesh.py Rotor37.geomTurbo                             │
│        --set "configuration/grid_levels=4"                        │
│        --set "row:*/flow_path.number=65"                          │
│        --set "row:*/optimization.skewness=yes"                    │
│        --set "blade:*/b2b.default.boundary_layer_points=33"       │
│                                                                    │
│      ① parse_control_assignments([...])                           │
│         → 解析每个 "SELECTOR/KEY=VALUE" 表达式                    │
│         → 校验：键存在、值类型匹配、枚举合法、范围合规             │
│         → 生成 ControlRequest 列表                                │
│                                                                    │
│      ② resolve_control_requests(requests, geometry)               │
│         → 对每个 ControlRequest：                                 │
│           · 匹配几何实体（wildcard * → 具体实体）                  │
│           · 检查拓扑适用性（topologies 字段）                      │
│           · 选择正确的 setter 和 getter                            │
│           · 映射枚举值（如 "yes" → 2）和 SI 单位（米 → 项目单位） │
│           · 分配阶段（configuration / wizard / topology /          │
│             distribution / boundary_layer / optimization /         │
│             interface / existing_effect）                          │
│         → 生成 ResolvedControl 列表（按阶段排序）                  │
│                                                                    │
│      ③ validate_wizard_compatibility(requests, use_row_wizard)    │
│         → 检查无冲突                                                │
├──────────────────────────────────────────────────────────────────┤
│  3'. 渲染含控制计划的 AutoGrid 脚本                                │
│                                                                    │
│      CONTROL_PLAN = [                                             │
│        {"key": "configuration/grid_levels",                        │
│         "stage": "configuration", "api_value": 4, ...},           │
│        {"key": "row:*/wizard/first_cell_width",                   │
│         "stage": "wizard", "api_value": 5e-6, ...},               │
│        {"key": "blade:*/b2b.default.boundary_layer_points",        │
│         "stage": "boundary_layer", "api_value": 33, ...},         │
│        {"key": "row:*/optimization.skewness",                     │
│         "stage": "optimization", "api_value": 2, ...},            │
│      ]                                                            │
│                                                                    │
│      生成的 autogrid_init.py 在执行时：                            │
│                                                                    │
│      ① a5_new_project / init_from_geomTurbo                       │
│      ② ROW_COUNT = a5_get_row_number()                            │
│                                                                    │
│      ③ _apply_stage("configuration")                              │
│         → _apply_control("configuration/grid_levels")              │
│           · _resolve_target → None（configuration 无 target）      │
│           · readback_before → get_grid_levels()                   │
│           · _invoke_control → set_grid_levels(4)                  │
│           · readback → get_grid_levels() → 4                      │
│           · emit: status=applied, readback_before=3, readback=4  │
│                                                                    │
│      ④ _apply_stage("wizard")                                     │
│         → _apply_control("wizard/first_cell_width")               │
│           · _resolve_target → row_wizard() for each row           │
│           · readback_before → get_first_cell_width()              │
│           · _invoke_control → set_first_cell_width(5e-6)          │
│           · readback → get_first_cell_width() → 5e-6              │
│           · emit: status=applied                                  │
│                                                                    │
│      ⑤ _generate_row_wizards()                                    │
│         → RowWizard.generate() 现在基于已设置的                   │
│           first_cell_width=5e-6、spanwise_paths=97 等运行          │
│         → 生成与默认不同的 flow path 和 B2B 初始分布              │
│                                                                    │
│      ⑥ _apply_stage("topology")                                   │
│         → 例如设置 topology=hi（切换到 H&I）                      │
│           · _check_topology → 无需预检查（拓扑选择器）            │
│           · _invoke_control → set_b2b_topology_type(3)            │
│           · 此时 project 内的拓扑已改变                            │
│                                                                    │
│      ⑦ _apply_stage("distribution")                               │
│         → 例如 streamwise_inlet_points=33                         │
│         → 在 RowWizard 已运行后精确覆盖分布参数                   │
│                                                                    │
│      ⑧ _apply_stage("boundary_layer")                             │
│         → 例如 boundary_layer_points=33                           │
│         → 控制边界层法向网格分辨率                                  │
│                                                                    │
│      ⑨ _apply_stage("optimization")                               │
│         → 例如 optimization.skewness=yes、optimization.nmb=0.75   │
│         → 设置后，后续 B2B 生成将使用这些优化参数                  │
│                                                                    │
│      ⑩ _apply_stage("interface")                                  │
│         → 例如 interface/streamwise_cell_width=5e-6               │
│                                                                    │
│      ⑪ _apply_stage("existing_effect")  → 空（Rotor37 无技术效果） │
│                                                                    │
│      ⑫ a5_generate_flow_paths()  → 使用修改后的 flow path 参数   │
│      ⑬ a5_generate_b2b()         → 使用修改后的 B2B 参数         │
│      ⑭ a5_generate_3d()          → 使用修改后的优化/接口参数     │
│                                                                    │
│      ⑮ _emit_post_generation_readbacks()                          │
│         → 对所有有 getter 的控制再次回读                           │
│         → 检测生成后是否被 AutoGrid 内部逻辑覆盖                  │
│         → 例如 boundary_layer_width 在 setter 后 = 0.0002，       │
│           但生成后回到 0.001692339 → 判为 FAIL_READBACK           │
│                                                                    │
│      ⑯ 保存产物（.trb/.igg/.cgns）                                │
└──────────────────────────────────────────────────────────────────┘
```

### 固定阶段顺序的设计原因

控制参数的阶段顺序不由命令行书写顺序决定，而由注册表中每个 `ControlSpec`
的 `stage` 字段决定：

```text
configuration        ← 全局设置（grid_levels 等）
    ↓
wizard               ← RowWizard 参数
    ↓
RowWizard.generate() ← 必须放在 wizard 之后、topology 之前
    ↓
topology             ← B2B 拓扑选择（default/hoh/hi）
    ↓
distribution         ← 点数分布参数
    ↓
boundary_layer       ← 边界层参数（点数、厚度、膨胀率）
    ↓
optimization         ← 优化参数（skewness、NMB、步数等）
    ↓
interface            ← 接口参数
    ↓
existing_effect      ← 已有技术效果参数
    ↓
generate_flow_paths  → generate_b2b → generate_3d
```

这样设计是为了**避免 RowWizard 覆盖后置的拓扑、点数和优化设置**：
RowWizard.generate() 本身会计算并设置默认的拓扑和点数，如果在其之后
才应用 wizard 参数，wizard 参数会被 RowWizard 生成的默认值覆盖。
因此 wizard setter 必须在 RowWizard.generate() 之前运行；而显式的分布、
边界层、优化控制则在 generate() 之后运行，可以精确覆盖 RowWizard 的
默认分配。

### 回读三阶段验证

每个控制参数的验证分为三个阶段：

1. **setter 前回读（readback_before）**：在调用 setter 之前，通过 getter
   获取目标属性的当前值；
2. **setter 后回读（readback）**：在 setter 调用后立即通过 getter 获取值，
   验证 setter 是否成功将值设为目标值；
3. **生成后回读（post_generation）**：在 B2B/3D 网格生成完成后再次通过
   getter 回读，检测 AutoGrid 内部逻辑是否在生成过程中覆盖了 setter 设置
   的值。

三个阶段的值均写入 `run_summary.json`，完整可审计。判定逻辑：

| 情况 | 判类 |
|---|---|
| 至少两个不同请求值在三个阶段均可靠保持，且网格指纹改变 | 有效类（PASS / VALID_WITH_WARNING / INVALID_ONLY） |
| 至少两个不同请求值可靠保持，但网格指纹相同 | `NO_MESH_EFFECT` |
| 不足两个请求值在生成后可靠保持（被覆盖/坍缩/返 None） | `FAIL_READBACK` |

---

## 156 项通用候选控制参数完整分类

以下按六大验证分类逐一列出全部 156 项通用候选中每个参数的键名、
中文含义、值类型、允许的取值/范围及其在 Rotor37 上的实测表现。

### 分组说明

| 分组 | 数量 | 含义 |
|---|---:|---|
| `GENERAL_CORE` | 52 | 拓扑无关通用项，适用于全部 B2B 拓扑 |
| `GENERAL_DEFAULT` | 51 | 仅当 B2B 拓扑为 Default 时生效 |
| `GENERAL_HI` | 19 | 仅当 B2B 拓扑为 H&I 时生效 |
| `GENERAL_HOH` | 33 | 仅当 B2B 拓扑为 HOH 时生效（全部门控） |
| `GENERAL_TOPOLOGY` | 1 | B2B 拓扑类型选择器 |

---

### A. `EFFECT_PASS`（44 项）— 推荐可用集

完整网格指纹改变，且至少存在一对两端均结构有效、质量 PASS 的取值。

#### A1. 拓扑无关通用核心（26 项）

| 控制键 | 含义 | 类型 | 范围/枚举 | 实测与建议 |
|---|---:|---|---|---|
| `configuration/grid_levels` | 全局多重网格层级数 | int | 1–9 | 3→4：+6.4% 点数, 全部 PASS。推荐 3–4。 |
| `configuration/support_curve_control_points` | 支撑曲线控制点数 | int | — | 101→201：坐标改变, PASS。 |
| `interface/streamwise_cell_width` | 接口流向单元宽度 | float（SI 米） | >0 | 1e-6→5e-6 m：均 PASS, 5e-6 m 裕量更大。 |
| `row/downstream.relaxation` | 下游块流向聚集松弛 | int | — | 0→1：坐标改变, PASS。 |
| `row/downstream.untwist` | 下游块去扭曲 | bool | true/false | 坐标改变, PASS。 |
| `row/enforce_blade_wall_cell_width` | 强制叶片壁面单元宽度 | bool | true/false | 坐标改变, PASS。 |
| `row/flow_path.constant_cells` | flow path 常值单元数 | int | — | 5→9：坐标改变, PASS。 |
| `row/flow_path.control_points` | flow path 控制点数 | int | — | 17→33：坐标改变, PASS。 |
| `row/flow_path.distribution_smoothing_steps` | flow path 分布光顺步数 | int | — | 0→200：坐标改变, PASS。 |
| `row/flow_path.intermediate_points` | flow path 中间点数 | int | — | 17→33：坐标改变, PASS。 |
| `row/flow_path.number` | 叶排 flow path 数量（展向层数） | int | 3–10,001 | 65→97：+30.9% 点数（1,917,385）。推荐 65–97。 |
| `row/flow_path.smoothing_steps` | flow path 光顺步数 | int | — | 0→200：坐标改变, PASS。 |
| `row/optimization.boundary_steps` | 边界优化步数 | int | — | 0→200：坐标改变, PASS。 |
| `row/optimization.full_multigrid_steps` | 全多重网格优化步数 | int | — | 0→200：坐标改变, PASS。 |
| `row/optimization.multigrid` | 启用优化多重网格 | bool | true/false | 坐标改变, PASS。 |
| `row/optimization.nmb` | NMB（非匹配边界）优化权重 | float | 0–1 | 0.25→0.75：0.75 时 skew 44.538°, **综合最好**。推荐 0.75。 |
| `row/optimization.orthogonality` | 正交性优化权重 | float | 0–1 | 0.25→0.75：坐标改变, PASS。 |
| `row/optimization.skewness` | 偏斜优化模式 | enum | `no`, `medium`, `yes` | `no`→`yes`：skew 21.761°→32.797°。**推荐 yes**。 |
| `row/optimization.straight_boundary` | 直边界控制 | int | — | 0→1：坐标改变, PASS。 |
| `row/optimization.wake` | 尾迹正交性优化权重 | float | 0–1 | 0.25→0.75：坐标改变, PASS。 |
| `row/span_interpolation` | 展向插值间距因子 | float | 0–1 | 0.25→0.75：坐标改变, PASS。 |
| `row/upstream.relaxation` | 上游块流向聚集松弛 | int | — | 0→1：坐标改变, PASS。 |
| `row/upstream.untwist` | 上游块去扭曲 | bool | true/false | 坐标改变, PASS。 |
| `stagnation-point/distribution_type` | 停滞点分布控制类型 | enum | `absolute_distance`, `relative_distance`, `cell_length` | 三种模式均 PASS。需配合对应距离值。 |
| `wizard/grid_level` | RowWizard 网格级别 | enum | `coarse`, `medium`, `fine`, `user` | `fine`：+16.7% 点数（1,709,461）；`user`：+31.1%。**分辨率优先控制**。 |
| `wizard/spanwise_paths` | RowWizard 展向 flow paths 数 | int | 3–10,001 | 65→97：+32.3% 点数（1,937,649）。**分辨率优先控制**。 |

#### A2. Default B2B 拓扑条件项（17 项）

| 控制键 | 含义 | 类型 | 范围/枚举 | 实测与建议 |
|---|---:|---|---|---|
| `blade/b2b.default.blade_reference_angle` | 叶片参考角（网格定向） | float | -360°–360° | -5°→5°：坐标改变, PASS。 |
| `blade/b2b.default.boundary_layer_points` | 边界层法向点数 | int | — | 17→33：+16.7% 点数（1,708,401）, PASS。 |
| `blade/b2b.default.chord_control_points` | 弦向控制点数 | int | — | 17→33：坐标改变, PASS。 |
| `blade/b2b.default.free_inlet_angle` | 自由入口角（关闭固定） | bool | true/false | 坐标改变, PASS。 |
| `blade/b2b.default.free_outlet_angle` | 自由出口角（关闭固定） | bool | true/false | 坐标改变, PASS。 |
| `blade/b2b.default.high_stagger_optimization` | 高 stagger 优化 | bool | true/false | **true 优于 false**；false 时 skew 仅 16.263°。 |
| `blade/b2b.default.inlet_stagger` | 入口 stagger 类型 | enum | `normal`, `low`, `high` | `normal`/`low` PASS；`high` 时 skew 12.726° 不通过。 |
| `blade/b2b.default.intersection_control_points` | 交线控制点数 | int | — | 17→33：坐标改变, PASS。 |
| `blade/b2b.default.outlet_stagger` | 出口 stagger 类型 | enum | `normal`, `low`, `high` | `normal`/`high` PASS；`low` 时 exp 3.0252 略超限。 |
| `blade/b2b.default.periodicity` | 周期面匹配方式 | enum | `matching`, `non_matching` | `non_matching`：skew 提高到 44.723°, **需求解器支持**。 |
| `blade/b2b.default.streamwise_inlet_points` | 入口流向点数 | int | — | 17→33：+21.9% 点数（1,784,273）, PASS。 |
| `blade/b2b.default.streamwise_outlet_points` | 出口流向点数 | int | — | 17→33：+22.2% 点数（1,788,897）, PASS。 |
| `blade/b2b.default.type` | Default 拓扑走向类型 | enum | `streamwise`, `rounded_azimuthal`, `rounded_streamwise` | `streamwise` **最稳健**；`rounded_streamwise` skew 15.538° 接近阈值；`rounded_azimuthal` skew 0.007° 不可用。 |
| `blade/b2b.default.wake_control` | 尾迹控制 | bool | true/false | 坐标改变, PASS, 但 span skew 从 ~168° 降到 ~150–166°。 |
| `blade/b2b.default.wake_deviation_angle` | 尾迹偏转角 | float | — | -5°→5°：坐标改变, PASS。 |
| `blade/b2b.default.wake_prolongation` | 尾迹延伸 | bool | true/false | 坐标改变, PASS, span skew 略有降低。 |
| `blade/b2b.default.wall_width_interpolation` | 壁面宽度插值模式 | int | — | 0→1：坐标改变, PASS。 |

#### A3. 边缘处理通用项（1 项）

| 控制键 | 含义 | 类型 | 实测与建议 |
|---|---:|---|---|
| `blade/edge_treatment.trailing_rounded` | 尾缘 rounded 处理 | bool | false→true：点数 1,508,625, skew 提高到 49.016°, **效果最显著**。 |

---

### B. `EFFECT_VALID_WITH_QUALITY_WARNING`（25 项）— 有效但质量需注意

全部改变网格且结构有效，但至少一端未通过当前质量硬阈值。

#### B1. 拓扑无关通用核心（11 项）

| 控制键 | 含义 | 类型 | 范围/枚举 | 实测与建议 |
|---|---:|---|---|---|
| `blade/b2b.topology` | B2B 拓扑类型选择 | enum | `default`, `hoh`, `user`, `hi` | Default PASS；H&I 结构有效但质量 FAIL；HOH 重叠。推荐 `default`。 |
| `blade/edge_treatment.leading_sharp` | 前缘 sharp 处理 | bool | true/false | true 时结构有效但质量 FAIL。不建议开启。 |
| `blade/edge_treatment.trailing_sharp` | 尾缘 sharp 处理 | bool | true/false | true 时结构有效但质量 FAIL。不建议开启。 |
| `row/optimization.freeze_skin` | 冻结 skin 网格 | bool | true/false | true 时质量 FAIL。不建议冻结。 |
| `stagnation-point/constant_cells_percent` | 停滞点常值单元比例 | float | % | 20%→60%：20% 时 skew 12.471° FAIL。**推荐 60%**。 |
| `stagnation-point/desired_expansion_ratio` | 停滞点目标膨胀比 | float | — | 1.5→1.1：1.1 时 skew 32.86°, exp 1.6379 PASS；1.5 时 skew 4.5036° FAIL。**推荐 1.1**。 |
| `stagnation-point/distribution_absolute_distance` | 停滞点绝对控制距离 | float（SI 米） | >0 | 1e-4→5e-4 m：1e-4 时 exp 6.1383 FAIL。**推荐 5e-4 m**。 |
| `stagnation-point/distribution_cell_length` | 停滞点分布单元长度 | float（SI 米） | >0 | 1e-6/5e-6 m 均 FAIL。**当前不推荐**。 |
| `stagnation-point/distribution_relative_distance` | 停滞点相对控制距离 | float | — | 0.25→0.75：0.25 时 exp 4.2659 FAIL。**推荐 0.75**。 |
| `wizard/first_cell_width` | 首层单元宽度（RowWizard） | float（SI 米） | >0 | 1e-6→5e-6 m：1e-6 时 span skew 134.07° FAIL。**推荐 5e-6 m**。 |
| `wizard/full_matching` | 全匹配拓扑 | bool | true/false | false 时 exp 3.8585 超限。**推荐保持 true**。 |

#### B2. Default B2B 拓扑条件项（8 项）

| 控制键 | 含义 | 类型 | 实测与建议 |
|---|---:|---|---|
| `blade/b2b.default.azimuthal_inlet_points` | 入口方位点数 | int | 17→33：改变坐标，质量 WARNING。注意：方位点数族整体不推荐。 |
| `blade/b2b.default.azimuthal_inlet_up_points` | 入口上方方位点数 | int | 17→33：getter 修正影响（17 回读 37）。不推荐。 |
| `blade/b2b.default.azimuthal_outlet_down_points` | 出口下方方位点数 | int | 17→33：getter 修正影响。不推荐。 |
| `blade/b2b.default.azimuthal_outlet_points` | 出口方位点数 | int | 17→33：质量 WARNING。不推荐。 |
| `blade/b2b.default.boundary_layer_expansion` | 边界层增长率 | float | 1.1→1.5：1.1 时 span skew 109.26° FAIL。**推荐 1.5**。 |
| `blade/b2b.default.cell_width_at_wall` | 壁面首层宽度 | float（SI 米） | 1e-6→5e-6 m：1e-6 时 exp 3.0492 FAIL。**推荐 5e-6 m**。 |
| `blade/b2b.default.throat_points` | 喉部点数 | int | 0→9：启用时 exp 约 3.77 FAIL。不推荐。 |
| `blade/b2b.default.throat_projection_type` | 喉部投影类型 | int | 0→1：exp 约 3.86 FAIL。不推荐。 |

#### B3. H&I B2B 拓扑条件项（6 项）

| 控制键 | 含义 | 类型 | 实测与建议 |
|---|---:|---|---|
| `blade/b2b.hi.automatic_clustering_relaxation` | 自动聚集松弛 | bool | true 时 skew 提高到 45.872°, exp 降到 4.6739, 展向仍 FAIL。 |
| `blade/b2b.hi.clustering_relaxation` | 手动聚集松弛因子 | float | 0.25 时 span skew 171.26°, 但 exp 仍 4.7584。 |
| `blade/b2b.hi.h_full` | 完整 H 块 | bool | true 时 341,944 点, exp 改善到 4.2546, 仍 FAIL。 |
| `blade/b2b.hi.h_inlet` | 入口 H 块 | bool | true 时 540,096 点, exp 接近 3, 仍 FAIL。 |
| `blade/b2b.hi.h_outlet` | 出口 H 块 | bool | true 时 512,488 点, exp 3.0548, span skew 119.68°。 |
| `blade/b2b.hi.skin_block` | skin 块 | bool | false 时仅 5 block/237,813 点, skew 10.649°/exp 17.81, 明显不可取。 |

---

### C. `EFFECT_INVALID_MESH_ONLY`（13 项）— 仅无效网格上观察到变化

改变网格，但所有比较端中至少一端有负体积、重叠或 validity 异常。

| 控制键 | 含义 | 类型 | 实测表现 |
|---|---:|---|---|
| `blade/b2b.default.azimuthal_inlet_down_points` | Default 入口下方方位点数 | int | 17 结构有效但质量 FAIL, 33 重叠 |
| `blade/b2b.default.azimuthal_outlet_up_points` | Default 出口上方方位点数 | int | 17 结构有效但质量很差, 33 重叠 |
| `blade/b2b.default.inlet_angle` | 固定入口网格角 | float | ±5° 均重叠 |
| `blade/b2b.default.outlet_angle` | 固定出口网格角 | float | ±5° 均重叠 |
| `blade/b2b.default.streamwise_pressure_points` | Default 压力面流向点数 | int | 17 重叠, 33 结构有效但质量 FAIL |
| `blade/b2b.default.streamwise_suction_points` | Default 吸力面流向点数 | int | 17 重叠, 33 结构有效但质量 FAIL |
| `blade/edge_treatment.leading_blend` | 前缘 blend 权重 | int | 0/1 均重叠 |
| `blade/edge_treatment.leading_blunt` | 前缘 blunt 处理 | bool | true 重叠 |
| `blade/edge_treatment.trailing_blend` | 尾缘 blend 权重 | int | 0/1 均重叠 |
| `blade/edge_treatment.trailing_blunt` | 尾缘 blunt 处理 | bool | true 重叠 |
| `row/clustering` | 行级展向聚集系数 | float | 0.25/0.75 均重叠 |
| `row/optimization.steps` | 普通优化步数 | int | **0 重叠**；200 有效且 PASS。唯一推荐的非零端。 |
| `stagnation-point/parametric_location` | 停滞点参数位置 | float | 0.25/0.75 均重叠 |

> **特别注意：** `row/optimization.steps` 的非零端（200）完全有效，推荐保持 200 或以上。
> 零步数在 Rotor37 上直接导致重叠，不能作为"低成本"设置。

---

### D. `NO_MESH_EFFECT`（21 项）— 网格完全相同

Setter 成功、getter 按请求变化，但完整坐标 SHA-256 证明网格完全相同。

| 控制键 | 含义 | 类型 | 无效果原因 |
|---|---:|---|---|
| `blade/b2b.default.fix_inlet_angle` | 固定入口角 | bool | 单排 Rotor37 自动生成不依赖此开关 |
| `blade/b2b.default.fix_inlet_mesh` | 固定入口网格 | bool | 同上 |
| `blade/b2b.default.fix_outlet_angle` | 固定出口角 | bool | 同上 |
| `blade/b2b.default.fix_outlet_mesh` | 固定出口网格 | bool | 同上 |
| `blade/b2b.default.high_stagger_detection` | 高 stagger 自动检测 | bool | 当前几何不触发此限制 |
| `blade/b2b.default.intersection_precision_ratio` | 交线精度检查比率 | float | 1.1/1.5/3.0 均相同坐标；当前几何不触发 |
| `blade/b2b.default.intersection_quality` | 交线质量控制级别 | int | 5/9/18 均相同坐标 |
| `blade/b2b.default.leading_edge_cell_width` | 前缘单元宽度 | float | 1e-5/1e-6/5e-6 m 均相同坐标 |
| `blade/b2b.default.skin_max_expansion` | skin 块最大增长率 | float | 1.1/1.5/3.0 均相同坐标 |
| `blade/b2b.default.throat_inlet_relaxation` | 喉部入口松弛 | int | 两个变体坐标相同, 上下文质量 FAIL |
| `blade/b2b.default.throat_outlet_relaxation` | 喉部出口松弛 | int | 同上 |
| `blade/b2b.default.trailing_edge_cell_width` | 尾缘单元宽度 | float | 1e-5/1e-6/5e-6 m 均相同坐标 |
| `blade/b2b.hi.leading_edge_index` | H&I 前缘索引 | int | 无有效 getter；三值均得同一坐标 |
| `blade/b2b.hi.trailing_edge_index` | H&I 尾缘索引 | int | 同上 |
| `row/downstream.untwist_location` | 下游去扭曲流向位置 | float | 0.25/0.75/1.0 生成相同坐标 |
| `row/flow_path.hub_clustering` | hub 端 flow path 聚集 | float | 0.25/0.75/1.5：flow path 构造只响应模式不响应数值 |
| `row/flow_path.shroud_clustering` | shroud 端 flow path 聚集 | float | 同上 |
| `row/mesh_level` | 行网格级别（coarse/medium/fine/user） | enum | 4 个值均可回读，网格完全相同；RowWizard 已生成离散不受此行级 accuracy setter 影响 |
| `row/streamwise_weight` | 入口/叶片/出口流向权重 | tuple_float | 无 getter；三个元组均无坐标效果 |
| `row/target_points` | user 级别目标点数 | int | 50 万/100 万/200 万均可回读，网格完全相同 |
| `row/upstream.untwist_location` | 上游去扭曲流向位置 | float | 0.25/0.75/1.0 生成相同坐标 |

---

### E. `FAIL_READBACK`（20 项）— 回读失败

Setter 后或生成后无法保持至少两个不同请求值。

#### E1. Default B2B 回读失败（8 项）

| 控制键 | 含义 | 类型 | 失败原因 |
|---|---:|---|---|
| `blade/b2b.default.boundary_layer_width` | 边界层厚度 | float | setter 后 = 请求值, **生成后全部回到 0.001692339 m** |
| `blade/b2b.default.hub_cell_width_at_wall` | hub 端首层宽度 | float | 生成后固定回到 1e-5 m |
| `blade/b2b.default.intersection_law` | 交线分布律 | int | 生成后固定回 1（不论请求 5/9/18） |
| `blade/b2b.default.leading_edge_index` | Default 前缘索引 | int | 固定回到 17, 不论请求 0/9/21 |
| `blade/b2b.default.leading_edge_zcst` | 前缘 Z 常值线 | bool | true → 生成后回到 0 |
| `blade/b2b.default.shroud_cell_width_at_wall` | shroud 端首层宽度 | float | 生成后固定回到 1e-5 m |
| `blade/b2b.default.trailing_edge_index` | Default 尾缘索引 | int | 固定回到 105, 不论请求 0/9/21 |
| `blade/b2b.default.trailing_edge_zcst` | 尾缘 Z 常值线 | bool | true → 生成后回到 0 |

#### E2. H&I B2B 回读失败（11 项）

| 控制键 | 含义 | 类型 | 失败原因 |
|---|---:|---|---|
| `blade/b2b.hi.azimuthal_inlet_points` | H&I 入口方位点数 | int | 生成后固定为 17 |
| `blade/b2b.hi.azimuthal_inlet_up_points` | H&I 入口上方方位点数 | int | 同上 |
| `blade/b2b.hi.azimuthal_outlet_points` | H&I 出口方位点数 | int | 同上 |
| `blade/b2b.hi.azimuthal_outlet_up_points` | H&I 出口上方方位点数 | int | 同上 |
| `blade/b2b.hi.streamwise_blade_inlet_pressure_points` | H&I 入口压力面流向点数 | int | 拓扑重新计算为 13 |
| `blade/b2b.hi.streamwise_blade_inlet_suction_points` | H&I 入口吸力面流向点数 | int | 同上 |
| `blade/b2b.hi.streamwise_blade_outlet_pressure_points` | H&I 出口压力面流向点数 | int | 同上 |
| `blade/b2b.hi.streamwise_blade_outlet_suction_points` | H&I 出口吸力面流向点数 | int | 同上 |
| `blade/b2b.hi.streamwise_blade_pressure_points` | H&I 叶片压力面流向点数 | int | 拓扑重新计算为 37 |
| `blade/b2b.hi.streamwise_blade_suction_points` | H&I 叶片吸力面流向点数 | int | 同上 |
| `blade/b2b.hi.streamwise_previous_blade_suction_points` | H&I 前一叶片吸力面流向点数 | int | 同被拓扑重算为 37 |

#### E3. 拓扑无关回读失败（1 项）

| 控制键 | 含义 | 类型 | 失败原因 |
|---|---:|---|---|
| `stagnation-point/distribution_from_expansion_ratio` | 停滞点膨胀比分布模式 | bool | getter 实际复用 distribution type；false/true 均回读 2 |

---

### F. `BLOCKED_TOPOLOGY_BASELINE`（33 项）— HOH 全部门控

HOH 拓扑在 Rotor37 上基线即含 30,272 个负体积。经过 10 个单参数修复和
4 个组合修复后，最佳组合（`hoh.boundary_layer_points=33` + `flow_path.number=65`）
仍有 21,391 个负体积。因此全部 HOH 条件控制统一门控，不进入有效参数判定。

| 控制键 | 含义 | 类型 |
|---|---:|---|
| `blade/b2b.hoh.around_boundary_layer_points` | 边界层周围点数 | int |
| `blade/b2b.hoh.blade_distribution_smoothing_steps` | 叶片分布光顺步数 | int |
| `blade/b2b.hoh.blade_side_points` | 吸力/压力面点数 | int |
| `blade/b2b.hoh.boundary_layer_cell_width` | 边界层单元宽度 | float |
| `blade/b2b.hoh.boundary_layer_factor` | 边界层因子 | float |
| `blade/b2b.hoh.boundary_layer_points` | 边界层点数 | int |
| `blade/b2b.hoh.h_inlet_azimuthal_points_1` | 入口 H1 方位点数 | int |
| `blade/b2b.hoh.h_inlet_azimuthal_points_2` | 入口 H2 方位点数 | int |
| `blade/b2b.hoh.h_inlet_azimuthal_points_3` | 入口 H3 方位点数 | int |
| `blade/b2b.hoh.h_outlet_azimuthal_points_1` | 出口 H1 方位点数 | int |
| `blade/b2b.hoh.h_outlet_azimuthal_points_2` | 出口 H2 方位点数 | int |
| `blade/b2b.hoh.h_outlet_azimuthal_points_3` | 出口 H3 方位点数 | int |
| `blade/b2b.hoh.i_inlet_azimuthal_points` | 入口 I 方位点数 | int |
| `blade/b2b.hoh.i_inlet_periodic_points` | 入口 I 周期点数 | int |
| `blade/b2b.hoh.i_outlet_azimuthal_points` | 出口 I 方位点数 | int |
| `blade/b2b.hoh.i_outlet_periodic_points` | 出口 I 周期点数 | int |
| `blade/b2b.hoh.inlet_extension` | inlet 延伸块 | bool |
| `blade/b2b.hoh.inlet_extension_location` | inlet 延伸块位置 | float |
| `blade/b2b.hoh.inlet_extension_streamwise_points` | inlet 延伸块流向点数 | int |
| `blade/b2b.hoh.inlet_extension_type` | inlet 延伸块类型 | enum |
| `blade/b2b.hoh.leading_edge_absolute_distance` | 前缘绝对控制距离 | float |
| `blade/b2b.hoh.leading_edge_cell_length` | 前缘单元长度 | float |
| `blade/b2b.hoh.leading_edge_control_type` | 前缘分布控制类型 | enum |
| `blade/b2b.hoh.leading_edge_relative_distance` | 前缘相对控制距离 | float |
| `blade/b2b.hoh.outlet_extension` | outlet 延伸块 | bool |
| `blade/b2b.hoh.outlet_extension_location` | outlet 延伸块位置 | float |
| `blade/b2b.hoh.outlet_extension_streamwise_points` | outlet 延伸块流向点数 | int |
| `blade/b2b.hoh.outlet_extension_type` | outlet 延伸块类型 | enum |
| `blade/b2b.hoh.trailing_edge_absolute_distance` | 尾缘绝对控制距离 | float |
| `blade/b2b.hoh.trailing_edge_cell_length` | 尾缘单元长度 | int |
| `blade/b2b.hoh.trailing_edge_control_type` | 尾缘分布控制类型 | enum |
| `blade/b2b.hoh.trailing_edge_relative_distance` | 尾缘相对控制距离 | float |
| `blade/b2b.hoh.wake_clustering` | 尾迹聚集 | float |

---

## 按作用域汇总

| 作用域 | `EFFECT_PASS` | `EFFECT_VALID_WARNING` | `EFFECT_INVALID_ONLY` | `NO_MESH_EFFECT` | `FAIL_READBACK` | `BLOCKED` | 小计 |
|---|---:|---:|---:|---:|---:|---:|---:|
| configuration | 2 | 0 | 0 | 0 | 0 | 0 | 2 |
| wizard | 2 | 2 | 0 | 0 | 0 | 0 | 4 |
| row（含 flow_path） | 16 | 1 | 2 | 7 | 0 | 0 | 26 |
| blade (default) | 17 | 8 | 6 | 10 | 8 | 0 | 49 |
| blade (H&I) | 0 | 6 | 0 | 2 | 10 | 0 | 18 |
| blade (HOH) | 0 | 0 | 0 | 0 | 0 | 33 | 33 |
| blade (topology) | 0 | 1 | 0 | 0 | 0 | 0 | 1 |
| blade (edge treatment) | 1 | 2 | 4 | 0 | 0 | 0 | 7 |
| interface | 1 | 0 | 0 | 0 | 0 | 0 | 1 |
| stagnation-point | 1 | 5 | 1 | 0 | 1 | 0 | 8 |
| **合计** | **40**¹ | **25** | **13** | **21** | **20** | **33** | **156**² |

> ¹ 合计列中 EFFECT_PASS 的 40 来自按注册 scope 分类的逐行统计；A1（26）+ A2（17）+ A3（1）
> 实际 = 44 项 EFFECT_PASS，差异在于 `blade/b2b.topology` 和 `blade/edge_treatment.*`
> 等按 target_kind 归入 blade 行，但其 EFFECT_PASS 项在分组统计时落在 blade (topology) /
> blade (edge treatment) 行中（该行 EFFECT_VALID_WARNING 和 EFFECT_INVALID_ONLY 的
> 项不属于 EFFECT_PASS 统计）。
>
> ² 52 (GENERAL_CORE) + 51 (GENERAL_DEFAULT) + 19 (GENERAL_HI)
> + 33 (GENERAL_HOH) + 1 (GENERAL_TOPOLOGY) = 156。
