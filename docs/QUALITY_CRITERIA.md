# 基于 AutoGrid 质量输出的网格质量判断准则

本文档定义一套网格质量初筛准则。准则**只使用当前 `quality.py` 能从
AutoGrid 质量输出中解析出的指标**，不引入质量报告之外的数据。

本准则适用于当前主工作流（原 v6）生成的叶轮机械初始结构化网格。它不能替代求解器
收敛检查、`y+` 检查或网格无关性验证。

## 可用指标

当前质量报告解析器可使用以下字段：

| 指标 | 字段名 | 用途 |
|---|---|---|
| 负网格数 | `negative_cells` | 判断是否存在无效或翻转单元 |
| 网格点数 | `number_of_points` | 报告网格规模 |
| 多重网格层数 | `grid_levels` | 判断是否满足基本多重网格要求 |
| 偏斜角 | `min_skewness_angle`, `avg_skewness_angle`, `max_skewness_angle` | 判断单元角度质量 |
| 展向偏斜角 | `min_spanwise_skewness_angle`, `avg_spanwise_skewness_angle`, `max_spanwise_skewness_angle` | 判断展向角度畸变 |
| 膨胀比 | `min_expansion_ratio`, `avg_expansion_ratio`, `max_expansion_ratio` | 判断单元尺寸增长是否平滑 |
| 展向膨胀比 | `min_spanwise_expansion_ratio`, `avg_spanwise_expansion_ratio`, `max_spanwise_expansion_ratio` | 判断展向尺寸增长是否平滑 |
| 长宽比 | `min_aspect_ratio`, `avg_aspect_ratio`, `max_aspect_ratio` | 判断单元拉伸程度 |
| 壁面距离 | `min_wall_distance`, `avg_wall_distance`, `max_wall_distance` | 判断第一层几何间距 |
| 壁面距离均匀性 | `wall_distance_uniformity` | 由 `max_wall_distance / min_wall_distance` 派生 |
| 最差位置 | `*_block` 字段 | 定位最差指标所在区域 |

派生指标：

```text
spanwise_angular_deviation = 180.0 - min_spanwise_skewness_angle
```

即：

```text
展向角偏差 = 180.0 - 最小展向偏斜角
```

## 明确排除的指标

以下指标不纳入本准则，因为当前质量报告不能直接提供：

| 排除项 | 原因 |
|---|---|
| `y+` | 需要 CFD 求解后的壁面剪切信息 |
| 网格无关性 | 需要多套网格和求解结果对比 |
| 残差收敛 | 属于求解结果，不是网格质量报告字段 |
| 质量流量、压比或效率收敛 | 属于求解结果，不是网格质量报告字段 |
| Fluent 的 orthogonal quality | 当前 AutoGrid 输出不提供该同名指标 |
| OpenFOAM 的 non-orthogonality | 当前报告不按 OpenFOAM 定义输出该指标 |
| 坏单元分位数分布 | 当前解析器只有最小值、最大值、平均值和 block 名称 |
| 严格相邻体积比 | 当前输出为 expansion ratio，不是通用 neighbor volume ratio |

## 质量等级

采用三档质量等级：

| 等级 | 含义 | 建议动作 |
|---|---|---|
| `FAIL` | 至少一项硬性限值被违反 | 不建议直接用于 CFD 设置，应先修复网格 |
| `ENGINEERING_PASS` | 满足硬性限值，并达到工程初筛目标 | 可进入初步 CFD 设置 |
| `PRODUCTION_CANDIDATE` | 满足更严格的几何质量目标 | 可作为正式 CFD 的候选网格，但仍需后续求解侧验证 |

## 硬性失败限值

若任一硬性限值被违反，则网格判为 `FAIL`。

| 指标 | 硬性限值 |
|---|---:|
| `negative_cells` | `= 0` |
| `grid_levels` | `>= 3` |
| `min_skewness_angle` | `>= 20 deg` |
| `max_expansion_ratio` | `<= 3.0` |
| `spanwise_angular_deviation` | `<= 65 deg` |
| `max_spanwise_expansion_ratio` | `<= 2.0` |
| `max_aspect_ratio` | `<= 10000` |

说明：

- `number_of_points` 只报告网格规模，不作为通用硬性失败限值。可接受点数取决于
  算例规模、求解器资源和计算目标。
- 边界层内较高的 `max_aspect_ratio` 有时可以接受，但极端值仍应触发人工复查。

## 工程通过目标

若网格没有违反硬性限值，并且满足下表全部目标，则判为 `ENGINEERING_PASS`。

| 指标 | 目标 |
|---|---:|
| `negative_cells` | `= 0` |
| `grid_levels` | `>= 3` |
| `min_skewness_angle` | `>= 20 deg` |
| `avg_skewness_angle` | `>= 75 deg` |
| `max_expansion_ratio` | `<= 2.5` |
| `avg_expansion_ratio` | `<= 1.20` |
| `spanwise_angular_deviation` | `<= 45 deg` |
| `max_spanwise_expansion_ratio` | `<= 1.8` |
| `max_aspect_ratio` | `<= 5000` |
| `avg_aspect_ratio` | `<= 50` |
| `wall_distance_uniformity` | `<= 2.5` |

## 生产候选目标

若网格没有违反硬性限值，并且满足下表全部目标，则判为
`PRODUCTION_CANDIDATE`。

| 指标 | 目标 |
|---|---:|
| `negative_cells` | `= 0` |
| `grid_levels` | `>= 3` |
| `min_skewness_angle` | `>= 25 deg` |
| `avg_skewness_angle` | `>= 80 deg` |
| `max_expansion_ratio` | `<= 1.8` |
| `avg_expansion_ratio` | `<= 1.15` |
| `spanwise_angular_deviation` | `<= 30 deg` |
| `max_spanwise_expansion_ratio` | `<= 1.5` |
| `max_aspect_ratio` | `<= 2000` |
| `avg_aspect_ratio` | `<= 30` |
| `wall_distance_uniformity` | `<= 2.0` |

`PRODUCTION_CANDIDATE` 只表示几何质量报告层面较强，足以进入正式 CFD 前的
进一步检查；它不等于最终生产可用。

## 判定顺序

按以下顺序评估网格：

1. 若缺少必要指标，返回 `UNKNOWN`。
2. 若任一硬性限值被违反，返回 `FAIL`。
3. 若满足全部生产候选目标，返回 `PRODUCTION_CANDIDATE`。
4. 若满足全部工程通过目标，返回 `ENGINEERING_PASS`。
5. 否则返回 `WARNING`。

`WARNING` 表示网格未触发硬性失败，但至少有一项工程通过目标未满足。

## 报告模板

每份网格质量报告建议包含：

```text
状态:
质量指标来源:
网格点数:
负网格数:
多重网格层数:
最小 / 平均偏斜角:
最大 / 平均膨胀比:
最大膨胀比所在 block:
最小展向偏斜角:
展向角偏差:
最大展向膨胀比:
最大长宽比:
壁面距离均匀性:
未满足准则:
最差区域:
```

## 参考资料

- [Ansys Meshing skewness 文档](https://ansyshelp.ansys.com/public/Views/Secured/corp/v252/en/wb_msh/msh_skewness.html)：
  skewness 的取值范围为 0 到 1，0 最好，1 最差；高度偏斜的单元不可接受，因为控制方程离散通常假设单元相对接近等角。
- [Ansys Meshing orthogonal quality 文档](https://ansyshelp.ansys.com/public/views/secured/corp/v251/en/wb_msh/msh_orthogonal_quality.html)：
  orthogonal quality 的取值范围为 0 到 1，0 最差，1 最好。
- [OpenFOAM meshQualityControls 示例](https://github.com/OpenFOAM/OpenFOAM-2.0.x/blob/master/applications/utilities/mesh/generation/snappyHexMesh/snappyHexMeshDict)：
  常见网格质量控制包括最大非正交性、边界/内部 skewness、最小 determinant、face weight 和 volume ratio。
- [SimScale 网格质量文档](https://www.simscale.com/docs/simulation-setup/meshing/mesh-quality/)：
  网格质量指标用于判断网格对稳定性、收敛性和精度的适用性，但必须结合求解器和物理问题解释。
- [Cadence 网格质量讨论](https://resources.system-analysis.cadence.com/blog/fidelity-pointwise-and-isimq-mesh-adaptation-for-accurate-aerovehicle-drag-prediction)：
  理想 CFD 网格应接近正交、体积膨胀比接近 1、长宽比较低；实际工程网格需要在质量、规模和计算成本之间折中。
