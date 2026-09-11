# AutoGrid 17.1 网格控制目录与 API 审计

本文档说明当前已实现的控制模型。完整、可执行的唯一参数来源是 `src/controls.py` 中的静态 `ControlSpec` 注册表；本文不维护第二份容易失步的手工键清单。

## 实现边界

当前只开放“不改变物理几何”的网格生成控制，包括拓扑选择、点数、分布、聚集、边界层、优化、接口以及已有技术效果的网格离散控制。

明确排除：

- 周期数、转速、通道数量和物理项目类型；
- gap/partial-gap/fillet 的物理宽度、半径和几何位置；
- 入口、出口、端壁和远场的物理域位置；
- 曲面链接、输入几何替换、几何修复或变形；
- 实体创建、删除、复制和重命名；
- 仅显示、交互选择、demo、legacy 空操作和重复别名。

程序不接受任意 AutoGrid 方法名，也不提供 `.trb` 字符串覆盖入口。

## 注册表契约

每个 `ControlSpec` 都包含：

| 字段 | 含义 |
|---|---|
| `key` | 稳定语义键 |
| `scope` / `target_kind` / `hierarchy` | 作用域、运行时对象和选择器层级 |
| `value_type` | `bool`、`int`、`float`、`enum`、整数元组或浮点元组 |
| `minimum` / `maximum` / `enum_values` | 已知数值边界或合法枚举 |
| `si_length` | 是否以米作为 CLI 输入单位 |
| `priority` | P0/P1/P2 使用优先级 |
| `stage` | 固定应用阶段 |
| `setter` / `setter_by_value` | 正式 AutoGrid 17.1 API |
| `getter` | 可选回读 API；为空表示 setter 无异常即视为已应用 |
| `topologies` | 适用 B2B 拓扑；不匹配时严格失败 |
| `not_applicable_when` | 已知不适用条件说明 |

当前注册表共有 344 个控制键：P0 10 个、P1 59 个、P2 275 个。

## P0：高频基础控制

| 控制键 | 作用域 | 含义 |
|---|---|---|
| `configuration/grid_levels` | configuration | 全局多重网格层级 |
| `row/mesh_level` | row | coarse/medium/fine/user 网格级别 |
| `row/target_points` | row | user 级别目标点数 |
| `row/flow_path.number` | row | 叶排 flow path 数 |
| `row/optimization.steps` | row | 普通优化步数 |
| `row/optimization.gap_steps` | row | gap 优化步数 |
| `wizard/grid_level` | row wizard | RowWizard 网格级别 |
| `wizard/first_cell_width` | row wizard | 首层单元宽度，输入单位为米 |
| `wizard/spanwise_paths` | row wizard | RowWizard 展向 flow paths 数 |
| `gap/spanwise_points` | 已有 gap | gap 展向点数 |

其中高频项还具有 `src/mesh.py` 的显式 CLI 参数；其余控制统一通过 `--set` 使用。

## P1：常用精细控制

P1 共 59 项，覆盖：

- 流向权重和 span interpolation；
- hub/shroud 点聚集与流向分布；
- Default B2B 点数及表面流向点数；
- 边界层点数、厚度、膨胀率和增长率；
- 前缘/尾缘尺度和聚集；
- skewness、orthogonality、wake、high-stagger 优化控制；
- row-to-row matching、interface 相对位置和 matching 精度。

查询完整目录：

```powershell
python src/mesh.py --list-controls P1
python src/mesh.py --describe-control blade/b2b.default.streamwise_inlet_points
```

## P2：高级与已有实体控制

P2 共 275 项，覆盖：

- Default、HOH、H&I B2B 拓扑及其完整点数/聚集控制；
- gap 与 partial-gap 拓扑、点数和优化控制；
- 高级入口、出口、喉部、重叠、声学和远场控制；
- fillet、snubber、endwall、blade sheet 和 solid-body 的纯网格控制；
- 已存在孔、端壁孔、针肋、basin hole 和 ZR/3D 技术效果的点数、聚集与优化控制。

这些控制不会创建技术效果实体。选择器指向不存在的实体或不适用的拓扑时，AutoGrid 阶段会返回失败，不会静默忽略。

查询完整目录：

```powershell
python src/mesh.py --list-controls P2
python src/mesh.py --describe-control blade/b2b.hoh.wake_control
```

## 路径与选择器

控制表达式格式为：

```text
<实体选择器路径>/<局部键>=<值>
```

示例：

```text
configuration/grid_levels=3
row:*/optimization.steps=200
row:diffuser_axial/flow_path.number=89
row:#2/blade:#1/b2b.default.streamwise_inlet_points=33
row:Rotor/blade:Main Blade/gap:shroud/spanwise_points=17
```

规则：

1. `*` 表示 wildcard，`#N` 表示从 1 开始的索引，其他文本表示区分大小写的实体名。
2. 名称包含 `/` 或 `=` 时必须改用索引。
3. 精确选择器优先于 wildcard；参数顺序不影响解析结果。
4. 同一选择器、同一键重复出现直接报错。
5. 静态几何未携带可靠数量的已有技术效果，在 AutoGrid 内通过正式数量/accessor API 再做严格确认。

## 类型与单位

控制值不会执行任意 Python：

- 布尔：`true`、`false`、`1`、`0`；
- 整数和有限浮点数；
- 注册表中列出的精确枚举；
- 逗号分隔的定长整数/浮点元组。

长度参数始终按米输入。转换公式为：

```text
project_value = requested_si / units_factor
```

请求值、项目单位值、传给 API 的值，以及 setter 前、setter 后和 3D
网格生成后的 getter 回读值均进入 Schema 4 `run_summary.json`。每项控制的
验证结论（`VERIFIED` / `MISMATCH` / `READBACK_ERROR` / `UNVERIFIABLE`）记录在
`controls.verification`；脚本末尾必须输出 `AGMESH_COMPLETION` 完成事件，
即使没有请求任何控制。

## 应用阶段

控制顺序不依赖命令行书写顺序，而由注册表阶段确定：

```text
configuration
→ wizard
→ RowWizard.generate()
→ topology
→ distribution
→ boundary_layer
→ optimization
→ interface
→ existing_effect
→ B2B/3D 网格生成
```

这可避免 RowWizard 覆盖后置的拓扑、点数和优化设置。

## 启用条件与前置项

部分控制的启用条件由 `CONTROL_PREREQUISITES` 描述。值为 `">N"` 字符串时表示**启用谓词**（显式值大于 N 才满足），其余按等值判断：

- 优化子项（`freeze_skin`、`orthogonality`、`skewness`、`wake` 等）要求 `row/optimization.steps > 0`；
- 喉部相关子项（`throat_projection_type`、`throat_inlet_relaxation`、`throat_outlet_relaxation`）要求 `blade/b2b.default.throat_points > 0`，并保留拓扑等值条件（如 `blade/b2b.default.type = streamwise`）。

启用规则统一用于依赖排序、Web 可编辑性、提交校验与必要清除：前置项缺失、被清除或不满足时，子项不可编辑、提交报错并显示 `required_clears`；前置项变为零、被清除或切换到不适用拓扑时才触发子项清除。

**建议值说明**：`optimization.steps = 200` 与 `throat_points = 9` 仅为 campaign 采样矩阵的实验取值（`tests/test_campaign_runner.py` 的 `CAMPAIGN_PREREQUISITE_VALUES`），**不作为**通用启用条件；campaign 调整 `200→100/300`、`9→7/11` 不会清除仍有效的子项。CLI 不校验前置值，也不要求所有前置项必须显式传入。

## AutoGrid 17.1 setter 审计

审计针对本机正式文件：

```text
C:\ProgramData\NUMECA\fine171\_python\_autogrid\Autogrid.py
```

`audit_autogrid_source()` 按“所属类 + 方法名”逐个检查所有 `set_*` 和 `a5_set_*` 定义。当前结果：

| 状态 | 数量 | 含义 |
|---|---:|---|
| `mapped` | 361 | 映射到至少一个规范控制键 |
| `excluded` | 360 | 有明确静态理由或受控规则排除 |
| `unaudited` | 0 | 不允许存在 |
| 合计 | 721 | AutoGrid 17.1 setter 定义数 |

`audit_control_bindings()` 反向验证注册表引用的方法确实存在于正确的 17.1 对象上。当前 703 个绑定中，701 个是正式 API 方法，2 个是对 `RSInterface` enable/disable 成对接口的受控布尔适配；缺失绑定为 0。

审计可复现：

```powershell
$env:PYTHONPATH = "src"
python -c "from collections import Counter; from controls import audit_autogrid_source; p=r'C:\ProgramData\NUMECA\fine171\_python\_autogrid\Autogrid.py'; print(Counter(x['status'] for x in audit_autogrid_source(p)))"
```

精确排除理由保存在 `EXCLUDED_SETTERS` 和 `AUDIT_EXCLUSION_RULES`；测试要求每个 17.1 setter 必须是 `mapped` 或 `excluded`，并要求注册表不存在缺失 setter/getter。

## 严格失败策略

以下情况返回静态错误码 2，且不启动 IGG：

- 未知控制键；
- 类型、枚举或范围错误；
- 选择器层级错误或几何实体未匹配；
- 重复定义或等优先级冲突；
- `--no-row-wizard` 与 wizard 控制同时出现；
- 长度控制缺少有效 `UNITS-FACTOR`。

以下情况在 AutoGrid 阶段返回错误码 1，并记录失败控制：

- 实体在 AutoGrid 项目中不存在；
- 控制不适用于当前拓扑；
- 17.1 setter/getter 或实体 accessor 缺失；
- setter、getter 或网格生成抛出异常。

dry-run 只在 `controls.resolved` 中记录 `planned`，不会把控制写成 `applied`。

## 参数设置途径

控制参数支持三种设置方式，覆盖从快速原型到批量脚本的不同场景。

### 途径 1：独立 CLI 参数（7 个 P0 高频项）

以下 P0 控制项在 `src/mesh.py` 中有专用命令行参数，无需书写 `--set` 表达式：

| CLI 参数 | 对应控制键 | 值类型 | 说明 |
|---|---|---|---|
| `--mesh-level` | `row/mesh_level` | enum | `coarse`/`medium`/`fine`/`user`，作用于所有行 |
| `--target-points` | `row/target_points` | int | user 级别的目标点数，作用于所有行 |
| `--first-cell-width` | `wizard/first_cell_width` | float（米） | 所有行首层单元宽度 |
| `--spanwise-paths` | `wizard/spanwise_paths` | int | 所有行 RowWizard 展向 flow paths 数 |
| `--gap-points` | `gap/spanwise_points` | int | 所有已有 gap 的展向点数 |
| `--optimization-steps` | `row/optimization.steps` | int | 所有行普通优化步数 |
| `--gap-optimization-steps` | `row/optimization.gap_steps` | int | 所有行 gap 优化步数 |

快捷参数均采用 `row:*` wildcard，作用于几何中所有匹配实体。需要逐行差异化时改用 `--set`。

### 途径 2：通用 `--set` 表达式（全部 344 个控制键）

```bash
python src/mesh.py input.geomTurbo --set "row:*/mesh_level=medium"
python src/mesh.py input.geomTurbo --set "row:Rotor/blade:#1/b2b.default.streamwise_inlet_points=33"
python src/mesh.py input.geomTurbo --set "configuration/grid_levels=3"
```

`--set` 可在一条命令中重复多次，每次定义一个控制项。

### 途径 3：Python API（程序化调用）

从仓库根运行自定义脚本前，将 `src/` 加入模块搜索路径：

```powershell
$env:PYTHONPATH = "src"
```

```python
from controls import parse_control_assignments, resolve_control_requests
from geomturbo import parse_geomturbo

geometry = parse_geomturbo("input.geomTurbo")
requests = parse_control_assignments([
    "row:*/mesh_level=fine",
    "configuration/grid_levels=3",
])
resolved = resolve_control_requests(requests, geometry)
# resolved 可直接传入 run_autogrid_init(..., controls=resolved)
```

## 值类型与校验规则

| 值类型 | CLI 输入示例 | 校验规则 |
|---|---|---|
| `bool` | `true`、`false`、`1`、`0` | 仅接受这四个字面量，大小写敏感（小写） |
| `int` | `73`、`-1` | 正则 `[+-]?\d+`，受 `minimum`/`maximum` 约束 |
| `float` | `0.001`、`1.5e-5` | 必须是有限浮点数（拒绝 `inf`/`nan`），受 min/max 约束 |
| `enum` | `coarse`、`medium` | 必须精确匹配注册表中的 `enum_values` 列表 |
| `tuple_int` | `0,5,3` | 逗号分隔定长整数元组，每个元素受 min/max 约束 |
| `tuple_float` | `0.3,0.4,0.3` | 逗号分隔定长浮点元组，每个元素受 min/max 约束 |
| **SI 长度** | 以**米**输入 | 自动除以 `UNITS-FACTOR` 转换为项目单位值；缺少有效换算因子时报错 |

长度参数（`si_length=True`）的转换公式：

```text
project_value = requested_si / units_factor
```

请求值、项目单位值、传给 API 的值和三阶段 getter 回读均进入
Schema 4 `run_summary.json`，每项控制带四枚举验证结论（`VERIFIED` /
`MISMATCH` / `READBACK_ERROR` / `UNVERIFIABLE`；浮点按 rel_tol=1e-7、
abs_tol=1e-10 比较，长度换算到 SI 后比较），完整可审计。需要验证网格是否
真正改变时可追加 `--mesh-fingerprint`，生成完整 block 坐标 SHA-256、I/J/K
和固定坐标探针。

## 按作用域分类总览

以下按 `target_kind` 列出全部 344 个控制键的分布。完整、可执行的权威来源仍是 `src/controls.py` 中的 `CONTROL_REGISTRY`；此表用于快速定位。

### configuration（全局配置）— 20 项

| 子类 | 控制键（局部） | 数量 | 类型 | 阶段 |
|---|---|---|---|---|
| 多重网格 | `grid_levels` | 1 | int (1–9) | configuration |
| 支撑曲线 | `support_curve_control_points` | 1 | int (2–10001) | configuration |
| inlet bulb | `inlet_bulb.topology`、`.streamwise_points`、`.h_streamwise_points`、`.spanwise_points`、`.c_block_points`、`.radial_points`、`.singular_line`、`.smoothing_steps`、`.butterfly_smoothing_steps` | 9 | enum + 6 int + 2 int | topology / distribution / optimization |
| outlet bulb | `outlet_bulb.topology`、`.streamwise_points`、`.h_streamwise_points`、`.spanwise_points`、`.c_block_points`、`.radial_points`、`.singular_line`、`.smoothing_steps`、`.butterfly_smoothing_steps` | 9 | enum + 6 int + 2 int | topology / distribution / optimization |

### wizard（RowWizard 与声学）— 13 项

| 子类 | 控制键（局部） | 数量 | P0/P1/P2 |
|---|---|---|---|
| 基础向导 | `grid_level`、`spanwise_paths`、`first_cell_width`（3 个 P0）；`far_field_spanwise_paths`、`far_field_constant_cells_percent`、`full_matching`、`blade_tip_rounded_topology`（4 个 P1） | 7 | P0×3 + P1×4 |
| 声学向导 | `acoustic.max_span_cell_size`、`acoustic.max_far_field_span_cell_size`、`acoustic.max_b2b_cell_size`、`acoustic.max_stream_cell_size`、`acoustic.max_bulb_stream_cell_size`（5 个 SI 长度）；`acoustic.far_field_reference_layer`（1 个 int） | 6 | 全部 P2 |

### row（叶排行级）— 39 项

| 子类 | 控制键（局部） | 数量 | P0/P1/P2 |
|---|---|---|---|
| 网格密度 | `mesh_level`、`target_points` | 2 | P0 |
| 流向权重 | `streamwise_weight`（tuple_float×3） | 1 | P1 |
| 上下游 | `upstream.relaxation`、`upstream.untwist`、`upstream.untwist_location`、`downstream.relaxation`、`downstream.before_nozzle_relaxation`、`downstream.untwist`、`downstream.untwist_location` | 7 | P2×7 |
| gap 插值 | `gap.hub_interpolation`、`gap.shroud_interpolation`、`gap.hub_interpolation_location`、`gap.shroud_interpolation_location` | 4 | P1 |
| 展向/聚集/其他 | `span_interpolation`、`clustering`、`enforce_blade_wall_cell_width`、`bladeless_mesh` | 4 | P1×3 + P2×1 |
| 优化 | `optimization.steps`、`.gap_steps`（2 个 P0）；`.full_multigrid_steps`、`.boundary_steps`、`.straight_boundary`、`.freeze_skin`、`.orthogonality`、`.gap_orthogonality`、`.wake`、`.nmb`、`.skewness`、`.gap_skewness`、`.multigrid`（11 个 P1） | 13 | P0×2 + P1×11 |
| flow path | `flow_path.number`（P0）；`.hub_clustering`、`.shroud_clustering`、`.constant_cells`、`.control_points`、`.intermediate_points`、`.smoothing_steps`、`.distribution_smoothing_steps`（7 个 P1） | 8 | P0×1 + P1×7 |

### blade（叶片 B2B 与边缘处理）— 121 项

| 子类 | 范围 | 数量 | 优先级 |
|---|---|---|---|
| 拓扑选择 | `blade/b2b.topology` | 1 | P2 |
| Default 拓扑枚举 | `b2b.default.type`、`.periodicity`、`.inlet_stagger`、`.outlet_stagger` | 4 | P2 |
| Default 布尔开关 | `b2b.default.high_stagger_optimization` 等 12 项（含 wake_control/wake_prolongation 为 P1） | 12 | P1×2 + P2×10 |
| Default 点数 | `b2b.default.azimuthal_inlet_points` 等 14 项 | 14 | P1 |
| Default 边界层 | `b2b.default.cell_width_at_wall` 等 9 项 | 9 | P1 |
| Default 分布 | `b2b.default.throat_points` 等 13 项 | 13 | P2 |
| HOH | `b2b.hoh.inlet_extension` 等 41 项（含延伸块、点数 16 项、gap 匹配与尺寸比、edge 控制、边界层） | 41 | P2 |
| H&I | `b2b.hi.h_full` 等 20 项（含 H 块开关 4 项、点数 14 项、聚集松弛 2 项） | 20 | P2 |
| 边缘处理 | `edge_treatment.leading_blunt` 等 7 项 | 7 | P2 |

### 其他目标实体 — 148 项

| target_kind | 控制键示例 | 数量 | 优先级 |
|---|---|---|---|
| `gap` | `topology`（枚举 HO/O/O2H）、`clustering`（P1）、`spanwise_points`（P0）、`constant_cells` | 4 | P0×1 + P1×1 + P2×2 |
| `partial-gap` | `leading_edge_points`、`trailing_edge_points`、`streamwise_cell_width`（SI）、`spanwise_cell_width`（SI）、`clustering_relaxation`、`constant_cells`、`spanwise_points` | 7 | P2 |
| `fillet` | `clustering`、`leading_edge_clustering`、`trailing_edge_clustering`、`spanwise_height_clustering`、`constant_cells`、`spanwise_points`、`butterfly_topology`、`butterfly_radial_points` | 8 | P2 |
| `interface` | `streamwise_points`（P1）、`b2b_control`（P1）、`streamwise_cell_width`（P1/SI）、`streamwise_index`、`geometry_fixed`、`clustering_relaxation_factor`、`relative_location`、`z_cst`、`r_cst`（SI）、`clustering_relaxation_location`、`shape`、`reference_frame` | 12 | P1×3 + P2×9 |
| `endwall` | `spanwise_points`、`connected_layers`、`optimization_steps`、`generation_type` | 4 | P2 |
| `snubber` | `clustering`、`skin_expansion`、`leading_edge_relative_control`、`trailing_edge_relative_control`、`spanwise_index`、`skin_points`、`upstream_points`、`downstream_points`、`spanwise_points`、`fillet_butterfly_radial_points` | 10 | P2 |
| `blade-sheet` | `leading_edge_points`、`trailing_edge_points` | 2 | P2 |
| `stagnation-point` | `distribution_type`（枚举）、`distribution_cell_length`（SI）、`distribution_absolute_distance`（SI）、`distribution_relative_distance`、`constant_cells_percent`、`parametric_location` | 6 | P2 |
| `holes-line` | `boundary_layer_points`、`streamwise_points`、`spanwise_points`、`streamwise_left_points`、`streamwise_right_points`、`spanwise_up_points`、`spanwise_down_points`、`inside_optimization_steps`、`around_optimization_steps`、`upstream_wake_length`、`downstream_wake_length`、`preserved_lower_layers`、`preserved_upper_layers`、`intersection_tolerance` | 14 | P2 |
| `endwall-holes-line` | 继承 holes-line 13 项（不含 spanwise_points）+ 额外 `up_clustering`、`down_clustering`、`azimuthal_points` | 16 | P2 |
| `pin-fins-line` | 同 holes-line 14 项 | 14 | P2 |
| `basin-hole` | `optimization_steps`、`streamwise_resolution`、`boundary_optimization_steps`、`hole_side_points`、`boundary_layer_points` | 5 | P2 |
| `existing-effect` | `maximum_expansion`、`general_maximum_expansion`、`boundary_layer_maximum_expansion`、`boundary_maximum_expansion`、`clustering_relaxation_angle`、`solid_wall_clustering`、`smoothing_steps`、`constant_cells_percent`、`radial_expansion`、`far_field_smoothing_steps`、`theta_deviation_propagation`、`azimuthal_points`、`periodic_fnmb_row_connection`、`periodic_fnmb_rs_connection`、`periodic_rs_connection`、`matching_rs_connection`、`h_topology_corners`、`h_topology_thin_films`、`special_boundary_layer_distribution` | 19 | P2 |
| `solid-body` | `streamwise_distribution`（枚举）、`azimuthal_points`、`b2b_relaxation`、`keep_blade_mesh`、`keep_mesh_around_skin`、`keep_cooling_channel_mesh`、`keep_skin_mesh` | 7 | P2 |
| `lete-wizard` | `blade_type`（枚举）、`hub_clustering`、`shroud_clustering`、`layers`、`control_points`、`constant_cells`、`hub_expansion`、`shroud_expansion`、`leading_chord_tolerance`、`trailing_chord_tolerance`、`iteration_steps` | 11 | P2 |
| `configuration`（bypass） | `bypass.topology`、`bypass.boundary_layer_relative_width`、`bypass.nozzle_index`、`bypass.clustering`、`bypass.spanwise_points`、`bypass.streamwise_points`、`bypass.relative_control_distance`、`bypass.upstream_points`、`bypass.downstream_points`、`bypass.inlet_distribution_relaxation` | 10 | P2 |

### 汇总

| 作用域大类 | 控制键数 |
|---|---:|
| configuration（含 bypass） | 30 |
| wizard（含 acoustic） | 13 |
| row | 40 |
| blade（B2B + edge treatment） | 120 |
| gap / partial-gap / fillet | 19 |
| interface | 12 |
| endwall / snubber / blade-sheet / stagnation-point | 24 |
| holes-line / endwall-holes-line / pin-fins-line / basin-hole | 49 |
| existing-effect / solid-body / lete-wizard | 37 |
| **合计** | **344**（P0: 10, P1: 59, P2: 275） |

## P0 控制项 CLI 映射速查

| 控制键 | CLI 快捷参数 | 值类型 | 范围/枚举 |
|---|---|---|---|
| `configuration/grid_levels` | 仅 `--set` | int | 1–9 |
| `row/mesh_level` | `--mesh-level` | enum | coarse / medium / fine / user |
| `row/target_points` | `--target-points` | int | 100–2,000,000,000 |
| `row/flow_path.number` | 仅 `--set` | int | 3–10001 |
| `row/optimization.steps` | `--optimization-steps` | int | 0–100000 |
| `row/optimization.gap_steps` | `--gap-optimization-steps` | int | 0–100000 |
| `wizard/grid_level` | 仅 `--set` | enum | coarse / medium / fine / user |
| `wizard/first_cell_width` | `--first-cell-width` | float（米） | > 0 |
| `wizard/spanwise_paths` | `--spanwise-paths` | int | 3–10001 |
| `gap/spanwise_points` | `--gap-points` | int | 2–10001 |

## P1 控制项完整列表

查询实时目录：`python src/mesh.py --list-controls P1`。以下为 P1 全部 59 项的速览：

**wizard（4 项）：** `wizard/far_field_spanwise_paths`、`wizard/far_field_constant_cells_percent`、`wizard/full_matching`、`wizard/blade_tip_rounded_topology`

**row（27 项）：** `row/streamwise_weight`、`row/gap.hub_interpolation`、`row/gap.shroud_interpolation`、`row/gap.hub_interpolation_location`、`row/gap.shroud_interpolation_location`、`row/enforce_blade_wall_cell_width`、`row/span_interpolation`、`row/clustering`、`row/optimization.full_multigrid_steps`、`row/optimization.boundary_steps`、`row/optimization.straight_boundary`、`row/optimization.freeze_skin`、`row/optimization.orthogonality`、`row/optimization.gap_orthogonality`、`row/optimization.wake`、`row/optimization.nmb`、`row/optimization.skewness`、`row/optimization.gap_skewness`、`row/optimization.multigrid`、`row/flow_path.hub_clustering`、`row/flow_path.shroud_clustering`、`row/flow_path.constant_cells`、`row/flow_path.control_points`、`row/flow_path.intermediate_points`、`row/flow_path.smoothing_steps`、`row/flow_path.distribution_smoothing_steps`

**blade Default（25 项）：** `blade/b2b.default.wake_control`（P1）、`blade/b2b.default.wake_prolongation`（P1）、`blade/b2b.default.azimuthal_inlet_points` 等 14 项点数、`blade/b2b.default.cell_width_at_wall` 等 9 项边界层/单元宽度

**gap（1 项）：** `gap/clustering`

**interface（3 项）：** `interface/streamwise_points`、`interface/b2b_control`、`interface/streamwise_cell_width`

> **注意：** 除以上标注外，`row/mesh_level`、`row/target_points`、`row/flow_path.number`、`row/optimization.steps`、`row/optimization.gap_steps`、`wizard/grid_level`、`wizard/first_cell_width`、`wizard/spanwise_paths`、`gap/spanwise_points`、`configuration/grid_levels` 属于 P0，不在 P1 列表中。
