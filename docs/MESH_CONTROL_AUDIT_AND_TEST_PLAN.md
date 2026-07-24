# 网格生成控制参数静态核对与验证测试计划

## 1. 文档目的

本文回答两个问题：

1. 当前从 `.geomTurbo` 自动生成 AutoGrid 17.1 网格的项目，控制参数是否覆盖全面；
2. 如何验证每个控制参数不仅“能被调用”，而且确实作用于最终网格，并对网格质量或其直接控制目标产生可观测影响。

本次工作只进行了代码、API 源码、既有运行产物和公开资料的静态核对，没有启动 IGG、没有生成新网格、没有执行项目测试。

核对日期：2026-07-24。

## 2. 核对范围与证据

### 2.1 项目文件

已静态阅读：

- `mesh.py`
- `controls.py`
- `autogrid.py`
- `geomturbo.py`
- `quality.py`
- `tests/test_controls.py`
- `tests/test_autogrid.py`
- `tests/test_mesh.py`
- `tests/test_quality.py`
- `docs/MESH_CONTROL_ITEMS.md`
- `docs/QUALITY_CRITERIA.md`
- `docs/SOURCE_CODE_GUIDE.md`
- `README.md`

按照项目约束，未读取、未搜索、未修改 `archive/`。

### 2.2 本机 AutoGrid 17.1 API

已静态阅读：

```text
C:\ProgramData\NUMECA\fine171\_python\_autogrid\Autogrid.py
C:\ProgramData\NUMECA\fine171\_python\_igg\PYTHON.py
```

其中 `Autogrid.py` 是当前项目所针对版本的直接 API 证据；`PYTHON.py` 提供生成后 block 数量、名称、I/J/K 尺寸和网格点坐标等探针能力。

### 2.3 既有运行产物

仅静态读取 `runs/` 中已有的 11 份 `run_summary.json` 及相应质量报告，没有重新运行任何算例。

现有成功回读记录共 9 条，覆盖 8 个不同控制键：

| 控制键 | 已有成功记录 |
|---|---:|
| `configuration/grid_levels` | 1 |
| `wizard/first_cell_width` | 1 |
| `wizard/spanwise_paths` | 1 |
| `gap/spanwise_points` | 1 |
| `row/optimization.steps` | 2 |
| `blade/b2b.default.wake_control` | 1 |
| `gap/clustering` | 1 |
| `fillet/clustering` | 1 |

这相当于 341 个注册键中的约 2.35%。这些记录能证明部分调用链曾经成功，但不能证明其余控制有效，也不能证明每一项对最终网格的独立影响。

### 2.4 外部权威资料

- [Cadence Fidelity Fine Turbo 产品资料](https://www.cadence.com/en_US/home/resources/datasheets/fidelity-fine-turbo-accelerates-turbomachinery-designs-ds.html)：说明 AutoGrid 是面向叶轮机械的结构网格技术，并强调网格与计算速度、精度的关系。
- [Cadence CFD 网格与前处理说明](https://www.cadence.com/en_US/home/tools/system-analysis/computational-fluid-dynamics/pre-processing-meshing.html)：将 skewness、non-orthogonality、相邻体积比和边界层质量列为重要网格准则。
- [NUMECA FINE/Turbo 17.1 发布说明](https://www.numeca.de/news/fine-turbo-version-17-1/)：说明 17.1 新增了行间展向节点分布匹配等版本相关功能。
- [CGNS SIDS：Zone 结构](https://cgns.org/standard/SIDS/hierarchy.html)：定义 `Zone_t`、`VertexSize`、`CellSize` 和 `GridCoordinates_t`，可作为最终网格结构和坐标变化的标准化证据。
- [NASA：Examining Spatial (Grid) Convergence](https://www.grc.nasa.gov/www/wind/valid/tutorial/spatconv.html)：要求使用两个或更多逐级加密网格；三层网格更适合估计收敛阶和 GCI。
- [Cadence：Y+ Calculator](https://community.cadence.com/cadence_blogs_8/b/cfd/posts/the-handiest-cfd-app---the-y-calculator)：说明首层网格高度应由目标 `y+` 和流动条件反推，不能仅凭几何质量报告断定适用性。

## 3. 总体结论

### 3.1 简要结论

当前控制系统在“项目人为限定的纯网格控制范围”内覆盖较广，但不能认定为“覆盖全面”，更不能认定 341 个控制都已真实可用并会影响最终网格质量。

更准确的评价是：

| 维度 | 结论 |
|---|---|
| 参数目录广度 | 较广：341 个控制键，P0 10 个、P1 59 个、P2 272 个 |
| `set_*` API 名称审计 | 较完整：现有审计覆盖 721 个 `set_*` / `a5_set_*` 定义 |
| AutoGrid 全部状态改变接口 | 不完整：审计没有系统覆盖 `enable_*`、`disable_*`、`unset_*`、非标准命名 setter 和必要的 `compute/generate` 激活动作 |
| 调用签名与参数语义 | 未证明：当前反向审计只检查方法名和所属类是否存在 |
| 调用顺序与依赖 | 存在确定性缺陷和多项未验证依赖 |
| 真实 IGG 集成证据 | 很少：既有成功产物仅涉及 8 个不同控制键 |
| 单参数网格影响 | 未建立：既有记录大多是组合设置，没有 OFAT 对照 |
| 网格质量影响 | 未建立：getter 回读不等于网格发生变化，也不等于质量指标变化 |
| CFD 精度影响 | 未验证：当前项目没有求解、`y+` 或网格无关性环节 |

因此，项目文档中“全部 setter 已审计”的表述可保留为“全部符合当前正则范围的 `set_*` 方法已分类”，但不应把它等同于“全部网格控制已覆盖”或“全部参数已验证有效”。

### 3.2 当前覆盖的优点

现有实现已经具备较好的控制系统骨架：

- 统一的 `ControlSpec` 注册表；
- 明确的类型、范围、SI 长度换算和实体选择器；
- configuration、wizard、row、blade、gap、partial-gap、fillet、interface 和多类技术效果作用域；
- setter 白名单和脚本注入防护；
- AutoGrid 执行结果标记与 getter 回读；
- `.qualityReport` 的全局、逐行质量解析；
- 米制与毫米制 `.geomTurbo` 样例；
- dry-run、静态错误和运行时错误分层。

这些能力适合继续建设系统化验证，而不需要重新设计项目结构。

## 4. 静态核对发现

### 4.1 已确认问题

#### C-01：721 项审计不是完整 API 审计

`controls.py:1674-1690` 只匹配：

```text
set_*
a5_set_*
```

本机 `Autogrid.py` 还定义了：

| 方法类别 | 静态数量 |
|---|---:|
| `enable_*` | 39 |
| `disable_*` | 21 |
| `unset_*` | 13 |
| `compute_*` | 11 |
| `generate*` | 14 |

这些方法不全是控制参数，但当前审计没有逐项判定其应映射、应排除，还是属于某个控制的必要激活动作。

至少以下 9 个明确属于网格分布或优化的逻辑控制没有进入 341 项注册表：

| 建议语义控制 | AutoGrid 17.1 API | 说明 |
|---|---|---|
| stagnation-point expansion-ratio 模式 | `enable_distribution_from_expansion_ratio` / `disable_distribution_from_expansion_ratio` | 停滞点分布模式 |
| stagnation-point 目标增长率 | `desired_expansion_ratio(value)` | 非 `set_*` 命名 setter |
| endwall 多重网格优化 | `enable_multigrid_optimization` / `disable_multigrid_optimization` | 端壁优化控制 |
| holes-line 孔内 skewness 控制 | `enable/disable_skewness_control_inside_holes` | 明确位于 mesh control 段 |
| holes-line 孔周 skewness 控制 | `enable/disable_skewness_control_arround_holes` | 明确位于 mesh control 段 |
| endwall-holes-line 孔内 skewness 控制 | 同名 enable/disable | 端壁孔网格优化 |
| endwall-holes-line 孔周 skewness 控制 | 同名 enable/disable | 端壁孔网格优化 |
| pin-fins-line 内部 skewness 控制 | 同名 enable/disable | 针肋网格优化 |
| pin-fins-line 周围 skewness 控制 | 同名 enable/disable | 针肋网格优化 |

另外，`Row.enable_low_memory_usage`、`Row.enable_full_mesh_generation`、`Row.enable_acoustic_source` 以及 gap/partial-gap/fillet 的 defined-shape 开关等接口也应进入完整审计；它们是否对外开放可另行决策，但不能继续处于审计盲区。

结论：当前“341 项”不是完整 AutoGrid 状态控制全集。

#### C-02：B2B 拓扑切换与拓扑专属参数存在确定性顺序问题

`resolve_control_requests()` 在同一阶段、同一实体内按控制键字符串排序（`controls.py:1887-1892`）。所有 B2B 拓扑选择和若干拓扑专属开关均处于 `topology` 阶段。

例如同一次请求包含：

```text
blade/b2b.topology=hoh
blade/b2b.hoh.inlet_extension=true
```

字符串排序会先处理 `blade/b2b.hoh.*`，再处理 `blade/b2b.topology`。而 `autogrid.py:403-419` 在每个控制调用前先执行当前拓扑检查。因此，当初始拓扑不是 HOH 时，专属控制会先失败，拓扑切换永远没有机会执行。

同类风险存在于：

- 从非 default 拓扑切回 default 并同时设置 `b2b.default.*`；
- 切换到 H&I 并同时设置 `b2b.hi.*`；
- 所有“模式/类型选择器 + 依赖值”处于同一阶段但缺少显式子顺序的组合。

这是静态可确定的执行顺序缺陷，不需要运行即可成立。

#### C-03：`applied` 不代表回读一致

`autogrid.py:403-409` 的实际逻辑是：

1. setter 不抛异常；
2. 调用 getter；
3. 无论 getter 返回什么，都标记为 `applied`。

当前没有比较：

```text
readback == api_value
```

也没有处理 AutoGrid 自动取整、钳位、奇偶修正、拓扑回退或单位错误。因此可能出现“请求值被 AutoGrid 改写，但状态仍为 applied”的假阳性。

对于没有 getter 的控制，当前状态只说明 setter 没有抛异常，证据更弱。

#### C-04：未知数量实体的 wildcard 会展开到 99 个虚拟目标

`controls.py:2033-2038` 对无法从 `.geomTurbo` 静态计数的实体生成 `#1..#99` 候选。wildcard 会匹配全部 99 个候选。

实际执行时，`autogrid.py` 会在超过真实实体数量时失败。因此：

- `holes-line:*`
- `snubber:*`
- `pin-fins-line:*`
- `existing-effect:*`
- 其他未知数量效果

不能可靠表示“仅对实际存在的全部实体应用”。它通常会在真实实体之后的第一个虚拟索引失败。

这与文档中 wildcard 的一般语义不完全一致，应通过运行时真实计数展开，而不是固定生成 99 个目标。

#### C-05：pin-fins 目标解析硬编码第一个 channel

`autogrid.py:316-320` 固定使用：

```text
cooling_channel()
pinFinsChannel(1)
```

当前选择器没有 cooling-channel / pin-fins-channel 层级，无法准确控制第二个及后续 channel。即使注册的 setter 本身存在，也不能宣称对所有合法实体全面可用。

#### C-06：`not_applicable_when` 只是说明文字

`not_applicable_when` 被保存和显示，但没有参与 `resolve_control_requests()` 或生成脚本中的静态判定。

例如：

- acoustic 控制作用于非声学行；
- bypass 控制作用于非 bypass 项目；

都只能等 AutoGrid 阶段报错，或者在 API 静默接受时产生假阳性。

### 4.2 高风险、必须通过真实网格试验确认的问题

#### R-01：部分 setter 之后可能缺少必要的激活动作

本机 API 明确提供：

- `RowAcousticWizard.compute_number_of_points()`
- `WizardLETE.generate()`
- `EndWall.generate()`
- `TechnologicalEffectZR.compute_default_mesh()`
- `TechnologicalEffectZR.compute_auto_blocking()`

当前主脚本只统一执行 `RowWizard.generate()`，没有针对上述对象执行相应 compute/generate。

这不必然说明所有相关控制无效，因为某些值可能在最终 `a5_generate_*` 时自动消费；但仅凭 setter 和 getter 无法证明。必须通过“有/无激活动作”的成对试验确认。

#### R-02：范围和离散约束没有充分依据

多数 `minimum` / `maximum` 是注册表给出的宽范围，本机包装 API 本身没有公开这些约束。结构网格点数还可能受到：

- 奇偶性；
- 多重网格可整除性；
- 最小 block 尺寸；
- matching 接口一致性；
- 相邻 block 拓扑关系；

等隐含约束。

一个明确的静态不一致是：

- `controls.py` 对 `wizard/first_cell_width` 允许 `0`；
- `docs/MESH_CONTROL_ITEMS.md` 将其描述为 `> 0`。

因此现有静态范围只能视为候选输入范围，不能视为已验证 API 合法域。

#### R-03：单位语义仍有疑点

长度控制大多标记了 `si_length=True`，但还需验证：

- `interface/z_cst` 与 `interface/r_cst` 是否都属于项目长度；
- AutoGrid getter 返回的是项目单位、米，还是归一化值；
- mm 与 m 两类项目经过换算后是否产生物理等价网格；
- relative、normalized 和 absolute distance 是否被正确区分。

#### R-04：正则排除可能掩盖有效网格控制

`AUDIT_EXCLUSION_RULES` 按方法名中的 `angle`、`width`、`location`、`geometry` 等词批量排除。这样的规则适合初筛，不足以作为最终语义证明。

项目自身已经映射了多个合法的 `*_location`、`*_angle` 和 `*_width` 网格控制，说明同名模式不能单独决定其是否属于物理几何。

后续审计必须逐方法确认 broad-regex 排除项，尤其是：

- WizardLETE 层位置；
- BasicCurve discretisation；
- RowWizard inlet/outlet/expansion 参数；
- 几何数据简化、缝合容差；
- interface 与 flow-path 位置控制。

最终可以继续排除，但每项需要具体理由，不能只记录“命中正则”。

#### R-05：多个控制存在覆盖和交互

典型组合包括：

- `wizard/grid_level` 与 `row/mesh_level`；
- `wizard/spanwise_paths` 与 `row/flow_path.number`；
- `wizard/first_cell_width` 与 `blade/b2b.default.cell_width_at_wall`；
- `row/target_points` 与 `row/mesh_level=user`；
- topology、distribution type、shape 等模式控制与其依赖值；
- 全局 optimization 权重、步数和局部 gap/holes 优化。

这些组合不能只做单参数测试，还要验证明确的优先级和交互规则。

### 4.3 当前算例覆盖缺口

现有三份 `.geomTurbo` 的静态覆盖如下：

| 几何 | 主要能力 |
|---|---|
| `Rotor37.geomTurbo` | 单叶排轴流转子、shroud tip gap、米制 |
| `WP100_comp.geomTurbo` | 三叶排、离心叶轮、splitter、多个 shroud gap、毫米制 |
| `ori1.geomTurbo` | IGV/转子/静子三叶排、shroud gap、hub fillet、米制 |

当前没有明确覆盖以下实体或场景：

- partial-gap；
- bypass/nozzle；
- inlet/outlet bulb 与声学 far field；
- acoustic row；
- snubber；
- blade sheet；
- endwall effect 与 endwall holes；
- blade holes / basin hole；
- cooling channel / pin fins；
- solid body；
- ZR/3D technological effect；
- 多个 cooling / pin-fins channel；
- 可确认的 HOH、H&I、user B2B 基线；
- 多通道/full-annulus 生成。

缺少适用几何时，相关控制只能标记为 `BLOCKED_NO_FIXTURE`，不能记为 PASS，也不能用不存在实体的失败来判定参数无效。

## 5. “真实可用”和“影响质量”的分层定义

不能用单一布尔值描述控制有效性。每个控制至少需要以下证据层：

| 层级 | 状态 | 判据 |
|---|---|---|
| L0 | `STATIC_OK` | 正确对象存在 setter；签名、值类型、单位、依赖和阶段已核对 |
| L1 | `CALL_OK` | 在适用实体上真实调用，无异常、无静默错误 |
| L2 | `READBACK_OK` | getter 回读与归一化后的期望值一致；无 getter 时明确记录 |
| L3 | `PERSIST_OK` | 保存 `.trb` 后重新打开，控制值仍一致 |
| L4 | `EFFECT_OK` | 最终网格的结构、点数、坐标分布或目标局部量发生符合预期的变化 |
| L5 | `QUALITY_SENSITIVE` | 至少一个预先指定的质量指标变化超过 A/A 噪声和报告分辨率 |
| L6 | `QUALITY_IMPROVABLE` | 三水平试验显示存在可重复的改善方向或最优区间 |
| L7 | `CFD_VERIFIED` | 使用一致物理设置完成网格收敛/独立性与目标 `y+` 验证 |

建议定义：

- “真实可用”：至少达到 L4；
- “会影响最终网格质量”：至少达到 L5；
- “可用于提高网格质量”：至少达到 L6；
- “适合生产 CFD”：还需要 L7。

setter 无异常、getter 有返回值，只能达到 L1 或 L2，不能直接称为有效网格控制。

## 6. 测试总体策略

### 6.1 基本原则

1. 使用真实 AutoGrid 17.1/IGG，不用 mock 或 dry-run 代替集成证据。
2. 一次只改变一个待测控制；必要依赖项单独列入 `dependency_controls`。
3. 每个变体与完全相同环境下的 matched baseline 比较。
4. 先做 A/A 重复性，再定义有效变化阈值。
5. 不能只比较文件是否存在或文件大小。
6. getter、持久化、最终网格和质量报告四类证据必须分开记录。
7. “网格发生变化”“质量指标发生变化”“质量变好”是三个不同结论。
8. 不以全局极值的单次微小波动判定参数有效。
9. 缺少适用实体时记录阻塞，不伪造 PASS/FAIL。
10. 不修改生产代码来迎合测试；先保留失败证据和最小复现。

### 6.2 参数取值方法

先通过 getter 取得基线值，再确定测试值：

| 类型 | 建议变体 |
|---|---|
| bool | 使用与基线相反的值；必要时再切回，验证可逆性 |
| enum | 至少测试一个非默认值；拓扑和模式枚举最终应覆盖所有合法值 |
| int 点数 | 在满足拓扑/多重网格约束的前提下，取约 `0.75×` 和 `1.25×` 基线的明显差异值 |
| int 步数 | 测试 `0` 与一个足够大的非零值；避免只差 1 步 |
| float 比例/权重 | 基线两侧取明显但安全的水平，如 `0.7×`、`1.3×`，并按已知范围截断 |
| 绝对长度 | 基线的 `0.5×` 和 `2×`；同时做 m/mm 物理等价测试 |
| tuple | 每次只改变一个分量，验证参数次序，随后再做组合值 |
| topology/mode | 先切换模式，再应用依赖参数；不能依靠字符串排序 |

如 getter 基线位于边界、为 0、为空或不可信，必须根据 API/GUI 手工确认安全值，并在结果中记录取值依据。

## 7. 分阶段测试计划

### 阶段 A：完整静态 API 审计

目标：建立真正的 API 控制全集，不生成网格。

步骤：

1. 枚举 `Autogrid.py` 中所有类和全局方法。
2. 至少覆盖：
   - `set_*`
   - `a5_set_*`
   - `enable_*`
   - `disable_*`
   - `unset_*`
   - 接受值但不符合上述命名的公共方法；
   - `compute_*` / `generate*` 激活动作。
3. 每项记录：
   - owner；
   - 方法名和签名；
   - getter；
   - 语义类别；
   - mapped / excluded / activation / unknown；
   - 逐项排除理由；
   - 适用实体和依赖。
4. broad regex 只能作为候选标签，不能作为最终排除证据。
5. 反向检查每个 `ControlSpec` 的：
   - setter 签名；
   - getter 签名；
   - `setter_mode`；
   - enum 映射；
   - SI 单位；
   - stage；
   - topology/mode 依赖。

验收：

- `unknown = 0`；
- 所有非标准命名网格 setter 已映射或明确排除；
- 所有必要 activation 已与控制组关联；
- 不再以“721 个 `set_*` 已分类”代表完整 API 覆盖。

### 阶段 B：测试探针与基线

目标：建立可比较、可复现的最终网格证据。

每个真实运行除现有产物外，还应输出 test-only mesh probe：

```text
AutoGrid 版本
API 源码 SHA-256
项目提交/工作树状态
输入 geomTurbo SHA-256
完整命令和控制计划
block 数量
每个 block 的名称
每个 block 的 I/J/K 点数
总节点数和总单元数
固定规则抽样的网格点坐标
接口/连接摘要（API 可取得时）
qualityReport 全局与逐实体指标
生成耗时、峰值内存（可取得时）
```

生成后探针可使用本机 `_igg/PYTHON.py` 中的正式接口：

```text
num_of_blocks()
block(i).get_name()
block(i).get_size()
block(i).grid_point(i, j, k)
```

建议对每个 block 采样：

- 8 个角点；
- block 中心；
- 每个方向的 1/4、1/2、3/4 位置组合；
- 首末两层和壁面法向相邻点；
- AutoGrid 质量报告最差 I/J/K 附近的局部点。

CGNS 的 `Zone_t/VertexSize/CellSize/GridCoordinates_t` 是结构、尺寸和坐标的标准化依据。若 A/A 证明 CGNS 字节完全稳定，可以把 SHA-256 作为辅助证据；在此之前不能仅以二进制 hash 判定网格变化。

对每个代表几何至少运行两次完全相同的 A/A 基线。若结果不完全一致，建议增加到 3 次，并为每个质量指标建立基线噪声：

```text
T_metric = max(
    5 × A/A 标准差,
    2 × 报告打印分辨率,
    预定义工程最小变化量
)
```

### 阶段 C：P0 冒烟与因果测试

优先逐项验证 10 个 P0 控制：

1. `configuration/grid_levels`
2. `row/mesh_level`
3. `row/target_points`
4. `row/flow_path.number`
5. `row/optimization.steps`
6. `row/optimization.gap_steps`
7. `wizard/grid_level`
8. `wizard/first_cell_width`
9. `wizard/spanwise_paths`
10. `gap/spanwise_points`

每项至少包含：

- matched baseline；
- 一个明显非默认值；
- getter 回读；
- `.trb` 重开回读；
- 最终 block/点数/坐标探针；
- `.qualityReport` 差异；
- 预期方向检查。

特殊依赖：

- `row/target_points` 必须与 `row/mesh_level=user` 配套，但只能把 `row/mesh_level=user` 标记为依赖；
- gap 控制只在真实 gap 上执行；
- first-cell-width 同时检查 wall distance 与单位；
- optimization steps 重点检查 skewness、spanwise skewness、expansion ratio，不要求点数变化。

阶段门：

- P0 的每个适用控制均达到 L4；
- 任何 `applied` 但 readback 不一致的情况均作为失败；
- 任何变体与基线完全相同且无合理原因的控制进入 `NO_EFFECT` 调查；
- 未通过 P0 前不进行大规模 P1/P2。

### 阶段 D：P1 全量验证

P1 共 59 项，按控制族分批：

1. wizard；
2. row distribution / flow path；
3. row optimization；
4. default B2B 点数；
5. default B2B boundary layer；
6. gap；
7. interface。

每批先做一个代表控制的 pilot，再执行该批其余控制。

主要预期：

| 控制族 | 首要效果证据 | 质量证据 |
|---|---|---|
| 点数、flow paths | block I/J/K、总点数变化 | skewness、增长率、长宽比 |
| clustering / distribution | 坐标抽样和局部间距变化 | 增长率、展向增长率、skewness |
| boundary layer | 首层壁距、层内点数/增长 | wall distance、aspect ratio、expansion ratio |
| optimization | 坐标变化可有可无 | skewness、正交性相关指标、增长率 |
| wake / interface | 相关 block 与连接变化 | 局部 skewness、增长率、matching 稳定性 |

### 阶段 E：P2 按适用性全量验证

P2 不应一次性盲跑。先生成 applicability matrix：

```text
control_key × fixture × target × topology × prerequisites
```

每个控制只能落入以下状态之一：

- `READY`
- `NOT_APPLICABLE`
- `BLOCKED_NO_FIXTURE`
- `BLOCKED_API_DEPENDENCY`

对 `READY` 控制按阶段 C/D 的协议执行。对缺少实体的控制，形成所需 `.geomTurbo` fixture 清单，不允许用不存在目标的失败代替验证。

拓扑控制必须覆盖：

- topology-only；
- dependent-control-only；
- topology + dependent control 同次请求；
- topology + dependent control 分两次保存/重开；
- 切回原拓扑；
- 每个合法 enum。

已有技术效果还必须验证：

- 运行时真实实体计数；
- `#N` 精确选择；
- wildcard 仅展开真实实体；
- 多实体独立控制；
- activation 动作是否必要；
- 保存、重开后是否持久。

### 阶段 F：交互与优先级

完成单参数测试后，至少执行下列成对测试：

| 组合 | 目的 |
|---|---|
| `wizard/grid_level` × `row/mesh_level` | 明确最终密度控制优先级 |
| `wizard/spanwise_paths` × `row/flow_path.number` | 明确展向点数优先级 |
| `wizard/first_cell_width` × blade wall width | 明确边界层控制优先级 |
| optimization steps × optimization weight/mode | 验证步数为 0 时权重是否无效 |
| topology × topology-specific control | 验证模式先于依赖参数 |
| interface shape × shape-specific value | 验证 shape 依赖 |
| wildcard × exact selector | 验证覆盖与实体展开 |

结果中必须记录最终生效值，而不只记录请求顺序。

### 阶段 G：网格收敛和 CFD 适用性

几何质量变化不等于 CFD 结果更准确。对于准备进入生产使用的控制组合，还应：

1. 建立粗/中/细三层相似网格；
2. 尽量保持拓扑、边界层策略、局部分布比例和物理边界一致；
3. 有效加密比建议不小于 1.1；
4. 所有 CFD 工况、模型、收敛准则保持一致；
5. 比较质量流量、压比/膨胀比、效率、扭矩、损失等目标量；
6. 计算观察收敛阶、Richardson 外推和 GCI；
7. 结合实际求解后的 `y+` 反校首层高度。

只有完成该阶段，才能把“几何质量合格”提升为“网格分辨率对目标 CFD 结果已验证”。

## 8. 判定规则

### 8.1 readback

- bool / int：归一化后精确一致；
- enum：把 API 整数或字符串统一映射到公共枚举后比较；
- float / SI 长度：

```text
abs(readback - expected_project_value)
<= max(abs_tol, rel_tol × max(1, abs(expected_project_value)))
```

容差必须按 API 精度确定，不能用过宽容差掩盖单位错误。

### 8.2 最终网格变化

满足以下任一预先声明的主判据，可记为 `EFFECT_OK`：

- block 数量或名称集合变化；
- 对应 block 的 I/J/K 尺寸变化；
- 总节点/单元数变化；
- 坐标探针变化超过 A/A 容差；
- wall distance 等直接目标量变化；
- 接口/连接结构变化；
- 目标局部质量指标变化。

不能以文件时间戳、文件大小、setter 日志或任意 getter 变化单独判定 `EFFECT_OK`。

### 8.3 质量敏感性

当前可直接使用的质量指标：

- mesh validity；
- overlapping status；
- negative cells；
- skewness angle：最小值越大通常越好；
- spanwise skewness angle：最小值越接近 180°通常越好；
- expansion ratio：最大值越接近 1 越好；
- spanwise expansion ratio：最大值越接近 1 越好；
- aspect ratio：需结合边界层用途解释，不能机械追求最小；
- wall distance：用于目标符合度，不直接代表越小越好。

`QUALITY_SENSITIVE` 要求至少一个预先指定指标变化超过 `T_metric`。  
`QUALITY_IMPROVABLE` 还要求多水平结果显示可重复的改善方向或最优区间，并且不能以制造负体积、overlap 或严重恶化其他硬门槛为代价。

### 8.4 最终状态

建议每个控制输出一个状态：

| 状态 | 含义 |
|---|---|
| `PASS_EFFECT_AND_QUALITY` | 达到 L5 或更高 |
| `PASS_EFFECT_ONLY` | 网格确有变化，但当前质量指标未检出显著变化 |
| `PASS_CALL_ONLY` | 只证明调用/回读，未证明最终网格变化 |
| `FAIL_READBACK` | 回读不一致或被钳位/回退 |
| `FAIL_GENERATION` | 参数导致真实生成失败 |
| `NO_EFFECT` | matched baseline 与变体在主观测量上无可检出差异 |
| `BLOCKED_NO_FIXTURE` | 缺少适用几何 |
| `BLOCKED_ENVIRONMENT` | 许可证、IGG 或环境问题 |
| `NOT_APPLICABLE` | 经证据确认不适用，不是失败 |

## 9. 必须优先执行的缺陷复现

后续 AGENT 在大规模参数测试前，应先复现：

1. 同次设置 `blade/b2b.topology=hoh` 与一个 `blade/b2b.hoh.*` 控制；
2. 人为选择一个可能被 AutoGrid 修正的点数，验证 `applied` 是否仍会在 readback 不同时出现；
3. 对真实只有 1 个效果的未知数量实体使用 wildcard，验证 `#2` 是否导致失败；
4. 对 meter 和 millimeter 项目施加物理等价长度，比较 project value 与实际网格；
5. acoustic setter 有/无 `compute_number_of_points()`；
6. LETE / endwall setter 有/无相应 `generate()`；
7. holes/pin-fins optimization steps 有/无 skewness enable；
8. 保存 `.trb` 后重开回读。

这些复现决定测试框架是否可信，不能跳过。

## 10. 结果产物

所有可丢弃运行产物放在：

```text
runs/control-validation/<campaign_id>/
```

建议结构：

```text
environment.md
source-audit.csv
applicability.csv
baselines/
cases/
results/
  control-results.csv
  quality-deltas.csv
  mesh-fingerprints.csv
  blocked-controls.csv
  failures.md
campaign-summary.md
```

`control-results.csv` 至少包含：

```text
campaign_id
control_key
priority
fixture
target_path
topology
variant
dependency_controls
requested_value
project_value
api_value
readback_before_generation
readback_after_generation
readback_after_reopen
setter_status
generation_status
baseline_id
block_signature_changed
point_count_delta
coordinate_probe_delta
quality_metric
quality_baseline
quality_variant
quality_delta
quality_threshold
evidence_level
final_status
artifact_dir
notes
```

最终持久化结论写入：

```text
docs/MESH_CONTROL_VALIDATION_RESULTS.md
```

其中必须给出：

- 341 个现有键逐项状态；
- 静态审计新增/排除候选；
- 实测 PASS/FAIL/BLOCKED 数量；
- 每个失败的最小复现；
- 缺失 fixture 清单；
- 不得继续宣称有效的参数；
- 建议修复优先级；
- 测试环境和 AutoGrid 版本指纹。

## 11. 资源控制

完整验证不是 341 次运行即可完成：

- bool 至少需要 baseline + 非默认值；
- numeric 通常需要 baseline + 低/高两个水平；
- enum 最终应覆盖所有合法值；
- topology/effect 需要不同 fixture 和依赖；
- A/A、持久化和交互测试还会增加运行数。

因此实际规模预计为数百次真实网格生成。静态阶段不能可靠估算总耗时和磁盘占用。后续 AGENT 必须先完成：

1. 3 个代表几何的 A/A；
2. 10 个 P0 pilot；
3. 统计单次耗时、产物大小和许可证行为；
4. 给出全量 campaign 资源估算；
5. 再按 P0 → P1 → P2 分批推进。

真实 IGG 建议串行执行，避免许可证冲突、磁盘争用和并发导致的不可重复性。

## 12. 本次核对的最终回答

1. **是否覆盖全面：否。** 现有 341 项在狭义纯网格范围内覆盖较广，但审计只完整覆盖 `set_*` 命名族；至少 9 个明确纯网格逻辑控制遗漏，且还存在实体寻址、拓扑顺序和激活动作风险。
2. **是否已证明真实可用：否。** 当前单元测试主要验证解析、脚本文本、mock 运行和 API 名称存在；既有真实成功产物仅覆盖 8 个不同控制键。
3. **是否已证明会影响最终网格质量：否。** getter 回读没有与期望值比较，既有试验也没有逐项 matched baseline、A/A 阈值和 OFAT 因果设计。
4. **下一步：** 按本文 L0-L7 证据链分阶段验证，先修正测试框架的判定能力，再执行 P0/P1/P2 实网格 campaign；没有适用 fixture 的控制必须明确标记为阻塞。
