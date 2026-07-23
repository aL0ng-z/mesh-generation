# AutoGrid 17.1 网格质量解析与判定准则

本文档定义 `quality.py` 的质量数据模型和判定行为。质量判定只用于初始网格硬门槛检查，不能替代 CFD 收敛、目标 `y+` 或网格无关性验证。

## 数据源优先级

1. 优先解析 AutoGrid 原生 `.qualityReport`。
2. 缺少 `.qualityReport` 时，降级解析 CGNS 内嵌 `NIGridQuality` 数据。
3. 两者都不存在或必需字段无法恢复时，状态为 `UNKNOWN`。

CGNS 降级不会伪造报告中不存在的项目元数据或逐叶排行信息；无法恢复的字段保持 `null`。

## Schema v2 质量结构

```text
quality
  metrics_source             quality_report | embedded_cgns | null
  metadata
    autogrid_version
    generation_date
    generation_time
    generation_time_seconds
    mesh_validity
    overlapping_status
    overlapping_message
  project
    name
    template_path
    units
    units_factor
    number_of_points
    number_of_rows
    rows[]
      name
      main_blades
      splitter_blades
      number_of_points
      layers
      b2b_topology
  entities[]
    scope                    entire_mesh | row
    name
    negative_cells
    number_of_points
    grid_levels
    criteria
  metrics                    Entire Mesh 的旧扁平兼容字段
  result
    status                   PASS | FAIL | UNKNOWN
    accepted
    reasons
```

`quality.metrics` 和 `quality.result` 是兼容接口，仍可供旧调用方读取；新代码应优先使用 `quality.entities` 获得逐叶排统计和位置。

## 报告元数据与项目数据

文本状态机解析以下内容：

- AutoGrid 版本、项目名、模板路径；
- 生成日期、`HH:MM:SS` 耗时及换算后的秒数；
- `Mesh Validity`；
- overlapping 原始消息及标准化状态；
- 总点数、叶排数；
- 每个叶排的主叶片数、splitter 数、点数、layers 和 B2B topology。

`overlapping_status` 使用英文机器枚举，例如 `NO_OVERLAP`、`OVERLAP`、`UNKNOWN`。

## 六类质量指标

Entire Mesh 和每个 row 都尽可能保存六类指标的 `minimum`、`maximum` 和 `average`：

| 规范键 | 含义 | 最差极值方向 |
|---|---|---|
| `skewness_angle` | 偏斜角 | minimum |
| `spanwise_skewness_angle` | 展向偏斜角 | minimum |
| `spanwise_expansion_ratio` | 展向膨胀比 | maximum |
| `aspect_ratio` | 长宽比 | maximum |
| `expansion_ratio` | 膨胀比 | maximum |
| `wall_distance` | 壁面距离 | maximum |

每项 `criteria` 结构为：

```text
minimum
maximum
average
critical_location
  extreme
  reported_extreme
  block
  i
  j
  k
```

## `critical_location` 语义

AutoGrid 17.1 的某些报告把位置栏统一打印成 `Max Location`，即使该指标的工程最差值实际是 minimum。解析器不照搬这一歧义：

- skewness angle 和 spanwise skewness angle 的 `critical_location.extreme` 固定为 `minimum`；
- 其余四项固定为 `maximum`；
- `reported_extreme` 单独保留报告原始标签方向；
- block 以及 I/J/K 索引原样保留。

因此旧的 `max_skewness_angle_block` 不再代表“最差偏斜位置”；兼容扁平字段使用 `min_skewness_angle_block` 和 `min_skewness_angle_critical_location`。

## 壁面距离单位

`wall_distance` 的 `minimum`、`maximum`、`average` 保留 AutoGrid 项目单位原值，同时提供 SI 副本：

```text
wall_distance
  unit: Millimeters
  minimum: 0.01
  maximum: ...
  average: ...
  si
    unit: m
    minimum: 1e-5
    maximum: ...
    average: ...
```

SI 换算使用 `.geomTurbo` 的 `UNITS-FACTOR`。单位或换算因子未知时不猜测 SI 数值。

## 兼容扁平指标

`quality.metrics` 对应 Entire Mesh，保留下列常用字段：

- `negative_cells`、`number_of_points`、`grid_levels`；
- 六类指标的 `min_*`、`max_*`、`avg_*`；
- 最差位置的 `*_critical_location` 和 `*_block`；
- `wall_distance_uniformity = max_wall_distance / min_wall_distance`，前提是最小值已知且非零。

## 固定硬门槛

当前 `HARD_LIMITS` 与既有判定逻辑保持不变：

| 指标 | 通过条件 |
|---|---:|
| `negative_cells` | `= 0` |
| `grid_levels` | `>= 3` |
| `min_skewness_angle` | `>= 15°` |
| `max_expansion_ratio` | `<= 3.0` |
| `180° - min_spanwise_skewness_angle` | `<= 40°` |
| `max_spanwise_expansion_ratio` | `<= 2.0` |
| `max_aspect_ratio` | `<= 15000` |

`number_of_points` 是必需的完整性字段，但不设置统一点数上限或下限。

## 状态规则

- `PASS`：所有必需字段存在，且全部硬门槛满足；`accepted = true`。
- `FAIL`：所有必需字段足以判定，但至少一个硬门槛违反；`accepted = false`。
- `UNKNOWN`：任一必需字段缺失，或没有可用质量源；`accepted = false`。

状态枚举和机器字段保持英文。`report.md` 会将判定原因转换成中文供人工阅读。

不会额外引入 `WARNING`、`ENGINEERING_PASS` 或 `PRODUCTION_CANDIDATE`，也不会改变当前门槛。

## 三算例默认回归

当前 AutoGrid 17.1 报告解析结果：

| 几何 | 点数 | 最小偏斜角 | 最大膨胀比 | 最小展向偏斜角 | 最大长宽比 | 状态 |
|---|---:|---:|---:|---:|---:|---|
| `Rotor37.geomTurbo` | 1,464,289 | 21.761° | 1.7254 | 168.31° | 342.35 | `PASS` |
| `WP100_comp.geomTurbo` | 4,239,316 | 21.702° | 3.7167 | 119.89° | 317.09 | `FAIL` |
| `ori1.geomTurbo` | 2,744,343 | 10.155° | 2.6684 | 134.33° | 942.03 | `FAIL` |

WP100 的失败原因是最大膨胀比和展向角偏差；ori1 的失败原因是最小偏斜角和展向角偏差。该回归用于证明解析增强没有改变原判定。

## 使用示例

```python
from quality import summarize_quality

summary = summarize_quality(
    {"quality_report": r"runs\case\mesh.qualityReport"},
    units="Millimeters",
    units_factor=0.001,
)

print(summary["result"]["status"])
for entity in summary["entities"]:
    print(entity["scope"], entity["name"], entity["criteria"])
```

## 完整指标字段清单

以下列出 `summarize_quality()` 返回字典中所有可提取字段。字段名与 `run_summary.json` 中 `quality` 节点完全一致。

### 1. 全局计数（3 个）

| 字段路径 | 类型 | 含义 | 硬门槛 |
|---|---|---|---|
| `metrics.negative_cells` | int | 负体积单元数 | = 0 |
| `metrics.number_of_points` | int | 全网格总点数 | 完整性字段，不设限 |
| `metrics.grid_levels` | int | 多重网格层级数 | ≥ 3 |

### 2. 六类质量准则统计值（18 个）

每类准则提供 min / max / avg 三个统计值。以下为 `quality.metrics` 中的扁平字段名：

| 准则 | min 字段 | max 字段 | avg 字段 |
|---|---|---|---|
| 偏斜角 | `min_skewness_angle` | `max_skewness_angle` | `avg_skewness_angle` |
| 展向偏斜角 | `min_spanwise_skewness_angle` | `max_spanwise_skewness_angle` | `avg_spanwise_skewness_angle` |
| 展向膨胀比 | `min_spanwise_expansion_ratio` | `max_spanwise_expansion_ratio` | `avg_spanwise_expansion_ratio` |
| 长宽比 | `min_aspect_ratio` | `max_aspect_ratio` | `avg_aspect_ratio` |
| 膨胀比 | `min_expansion_ratio` | `max_expansion_ratio` | `avg_expansion_ratio` |
| 壁面距离 | `min_wall_distance` | `max_wall_distance` | `avg_wall_distance` |

### 3. 最差位置字段（12 个）

每类准则提供一个最差极值所在位置，含 block 名称和 I/J/K 索引。注意：偏斜角和展向偏斜角的工程最差值为 minimum，因此使用 `min_*` 前缀。

| 准则 | critical_location 字段 | block 字段 |
|---|---|---|
| 偏斜角 | `min_skewness_angle_critical_location` | `min_skewness_angle_block` |
| 展向偏斜角 | `min_spanwise_skewness_angle_critical_location` | `min_spanwise_skewness_angle_block` |
| 展向膨胀比 | `max_spanwise_expansion_ratio_critical_location` | `max_spanwise_expansion_ratio_block` |
| 长宽比 | `max_aspect_ratio_critical_location` | `max_aspect_ratio_block` |
| 膨胀比 | `max_expansion_ratio_critical_location` | `max_expansion_ratio_block` |
| 壁面距离 | `max_wall_distance_critical_location` | `max_wall_distance_block` |

每个 `critical_location` 对象结构：

```text
{
  "extreme": "minimum" | "maximum",     // 工程最差值方向
  "reported_extreme": "minimum" | "maximum",  // 报告原始标签
  "block": "row_1_flux_1_Main_Blade_skin",    // 所在块名称
  "i": 0, "j": 58, "k": 15                    // I/J/K 网格索引
}
```

### 4. 壁面距离 SI 换算（4 个）

`wall_distance` 准则在 `entities[].criteria.wall_distance` 中额外提供 SI 副本：

| 字段路径 | 含义 |
|---|---|
| `wall_distance.si.unit` | 固定为 `"m"` |
| `wall_distance.si.minimum` | min_wall_distance × units_factor |
| `wall_distance.si.maximum` | max_wall_distance × units_factor |
| `wall_distance.si.average` | avg_wall_distance × units_factor |

### 5. 衍生指标（2 个）

| 字段路径 | 计算方式 | 前提条件 |
|---|---|---|
| `metrics.wall_distance_uniformity` | `max_wall_distance / min_wall_distance` | min_wall_distance 已知且非零 |
| `metadata.generation_time_seconds` | 从 `HH:MM:SS` 文本解析 | 报告含 `Generation Time` 行 |

### 6. 元数据（7 个）

| 字段路径 | 类型 | 来源 |
|---|---|---|
| `metadata.autogrid_version` | str | 报告头 `AUTOGRID version 17.1` |
| `metadata.generation_date` | str | `Generation Date : 2026-07-22 23:58:28` |
| `metadata.generation_time` | str | `Generation Time : 00:00:13` |
| `metadata.generation_time_seconds` | int | 上述时间的秒数换算 |
| `metadata.mesh_validity` | str | `Mesh Validity : OK`，失败时含错误描述 |
| `metadata.overlapping_status` | enum | `NO_OVERLAP` / `OVERLAP` / `UNKNOWN` |
| `metadata.overlapping_message` | str | 原始重叠消息文本 |

### 7. 项目信息（6+ 个，逐行展开后更多）

| 字段路径 | 类型 | 来源 |
|---|---|---|
| `project.name` | str | `PROJECT : mesh` |
| `project.template_path` | str | `TEMPLATE FILE : ...` |
| `project.units` | str | 从 `input.geomTurbo` 发现（如 `Millimeters`） |
| `project.units_factor` | float | 从 `input.geomTurbo` 发现（如 `0.001`） |
| `project.number_of_points` | int | 全局总点数 |
| `project.number_of_rows` | int | 叶排数 |
| `project.rows[]` | list | 每行含以下字段 |

每个 `project.rows[i]` 包含：

| 字段 | 类型 | 含义 |
|---|---|---|
| `name` | str | 叶排名称 |
| `main_blades` | int | 主叶片数 |
| `splitter_blades` | int | 分流叶片数 |
| `number_of_points` | int | 该行网格点数 |
| `layers` | int | 展向网格层数 |
| `b2b_topology` | str | B2B 拓扑类型（如 `Default`） |

### 8. 逐实体指标（entities[]）

`quality.entities` 数组包含 1 个 `entire_mesh` 实体和 N 个 `row` 实体。每个实体包含：

| 字段 | 类型 | 含义 |
|---|---|---|
| `scope` | str | `entire_mesh` 或 `row` |
| `name` | str | 实体名（如 `Entire Mesh`、`row 1`） |
| `negative_cells` | int | 该范围内负体积单元数 |
| `number_of_points` | int | 该范围内网格点数 |
| `grid_levels` | int | 该范围内多重网格层级 |
| `criteria` | dict | 六类质量准则（每类含 min/max/avg + critical_location） |

对于 `scope == "row"` 的实体，`criteria` 中的 `critical_location` 为报告直接给出的位置；对于 `entire_mesh`，若报告未直接提供位置，解析器从匹配的 row 中自动推导，并在 location 中附加 `derived_from_scope` 和 `derived_from_name` 字段。

### 9. 质量判定（3 个）

| 字段路径 | 类型 | 含义 |
|---|---|---|
| `result.status` | enum | `PASS` / `FAIL` / `UNKNOWN` |
| `result.accepted` | bool | `true` 仅当 status 为 PASS |
| `result.reasons` | list[str] | 失败或无法判定时的具体原因 |

### 10. 辅助字段（2 个）

| 字段路径 | 类型 | 含义 |
|---|---|---|
| `metrics_source` | str | 数据来源：`quality_report` / `embedded_cgns` / `null` |
| `metrics` | dict | Entire Mesh 的旧扁平兼容字段集合（含以上全部 min/max/avg 及位置字段） |

### 指标总数汇总

按"每个独立可提取数值/字符串字段"计算（不含 entities 按 row 展开）：

| 类别 | 独立字段数 |
|---|---:|
| 全局计数（negative_cells / number_of_points / grid_levels） | 3 |
| 六类准则 min / max / avg（6 × 3） | 18 |
| 最差位置 critical_location × 6 + block × 6 | 12 |
| 壁面距离 SI 值（min/max/avg） | 3 |
| 衍生指标（wall_distance_uniformity / generation_time_seconds） | 2 |
| 元数据（autogrid_version / generation_date / generation_time / generation_time_seconds / mesh_validity / overlapping_status / overlapping_message） | 7 |
| 项目基础信息（name / template_path / units / units_factor / number_of_points / number_of_rows） | 6 |
| 逐行项目信息（每行 6 字段 × N_rows） | 6 × N |
| 质量判定（status / accepted / reasons） | 3 |
| 辅助（metrics_source） | 1 |
| **基础合计（不含逐行）** | **~55** |
| **含 1 行完整统计** | **~100+**（55 基础 + 1 个 entire_mesh entity 的 ~25 准则字段 + 1 个 row entity 的 ~25 准则字段） |

其中 `quality.entities` 是信息最丰富的结构——每个 entity 包含独立的三项全局计数和六类准则完整 min/max/avg/位置。对于 N 行几何，entities 共 N+1 个（1 个 entire_mesh + N 个 row）。

### 评估必需字段

`evaluate_quality()` 判定前会检查以下 8 个字段是否全部存在且非空：

```text
negative_cells
number_of_points
grid_levels
min_skewness_angle
max_expansion_ratio
min_spanwise_skewness_angle
max_spanwise_expansion_ratio
max_aspect_ratio
```

任一字段缺失 → 状态 `UNKNOWN`。全部存在 → 逐项检查硬门槛。

### CGNS 降级差异

从 CGNS 内嵌 `NIGridQuality` 提取时，以下字段无法恢复，保持 `null`：

- `metadata` 全部字段（autogrid_version / generation_date / generation_time / mesh_validity / overlapping_*）
- `project` 全部字段（name / template_path / rows[] 等）
- `entities` 仅含一个 `entire_mesh` 实体，无逐行统计

CGNS 降级不伪造报告元数据，代码调用方应做好空值防御。
