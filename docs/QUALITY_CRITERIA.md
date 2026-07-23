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
