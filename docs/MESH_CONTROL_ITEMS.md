# AutoGrid 网格控制项调研

本文档记录当前主实现（原 v6）后续可能实现的网格控制项。当前只是调研和边界整理，不表示这些参数已经在
`mesh.py` 中开放。

## 当前主实现的实际行为

当前主实现只从 `.geomTurbo` 初始化 AutoGrid 项目，然后执行默认网格流程：

```text
geomTurbo -> AutoGrid 初始化 -> 可选 row wizard -> B2B 网格 -> 3D 网格 -> 输出 mesh.*
```

当前命令行里真正会影响网格生成的只有：

| 控制项 | 当前入口 | 影响 |
|---|---|---|
| 输入几何 | `geomturbo` 位置参数 | 决定通道、叶片、行数、周期数、tip gap 等几何基础。 |
| 是否调用 row wizard | `--no-row-wizard` | 默认尝试调用 row wizard；关闭后直接生成 B2B 和 3D 网格。 |

其余参数如 `--out`、`--igg`、`--dry-run`、`--timeout` 只控制运行环境和输出目录，不是网格参数。

## Row Wizard 是什么

Row Wizard 可以理解为 AutoGrid/Fidelity 面向单个叶轮机械 blade row 的“向导式自动网格设置器”。
它不是单个网格参数，而是一组自动化步骤：根据当前行的几何和机器类型，设置或重置一批
B2B/3D 网格参数，然后生成初始结构化网格。

在当前主实现里，`autogrid.py` 只做了很浅的一层调用：

```text
row(index) -> row_wizard/get_row_wizard/wizard -> generate/apply/compute
```

也就是说，当前主实现不会在 wizard 对话框层面设置机器类型、转速、gap、fillet、flow path 或
B2B 参数；它只是尽量调用 AutoGrid 暴露出来的默认 wizard 能力。

Row Wizard 的作用可以概括为：

- 给每个 blade row 建立一套初始网格设置。
- 自动选择或调整适合该行的拓扑和点分布。
- 处理常见叶轮机械特征，例如 rotor/stator、周期数、tip/hub gap、fillet、splitter blade。
- 生成可进一步人工或脚本调整的初始 B2B/3D 网格。

Row Wizard 的价值在于快速得到“可用初值”，不是保证最终质量最优。公开案例中常见工作流是：
导入 `.geomTurbo`，运行 row wizard 得到初始网格，再手动调整 B2B 和 3D 网格属性以改善质量。

## 资料结论

公开资料能支撑以下判断：

- Cadence 官方把 Row Wizard 描述为快速生成叶轮机械网格的入口。
- Cadence 官方说明 Fidelity/AutoGrid 支持结构化网格模板、Python API、自动网格点分布、平滑算法、
  多级/扩压器/旁路等叶轮机械配置。
- Cadence 官方说明 Fidelity CFD 的 Python API 可访问 GUI 功能，因此理论上可脚本化更多网格设置。
- 可检索的 AutoGrid5 手册片段说明，3D row mesh 是把 B2B 网格沿 hub-to-shroud flow paths
  堆叠生成；在无 hub/shroud gap 的情况下，`coarse`、`medium`、`fine` grid level 的默认
  flow paths 数量分别是 33、57、97。
- 可检索的 AutoGrid5 教程片段给出过 `Spanwise Grid Point Number = 33`、首层壁面单元宽度
  `1e-5 m`、B2B 点数建议满足 `4n + 1` 以便 FINE 多重网格使用至少 3 个 grid levels 等设置例子。
- 公开研究案例中，AutoGrid 常见关键控制量包括壁面/尾缘单元宽度、hub-to-shroud flow paths、
  tip/hub gap 尺寸、B2B 拓扑、优化步数、gap 优化步数、span interpolation 等。
- 本仓库已有运行产物 `mesh.trb` 里能看到这些参数名，例如 `FIRST_CELL_WIDTH`、
  `OPTIMIZATION`、`GAP_OPTIMIZATION_STEPS`、`HOHSkinNStrBlade`、`HOHbladeNpt*`、
  `HOH_gap_n*`、`WIDTH_AT_LEADING_EDGE`、`WIDTH_AT_TRAILING_EDGE` 等。

## 可实现控制项分级

### 取值/设置示例的使用边界

下面的取值用于增强直观性，不是当前主实现已支持的命令行接口，也不是所有算例可直接照搬的推荐值。

- 单位必须跟随项目单位。`geometries/Rotor37.geomTurbo` 使用 `Meters`，运行产物里 `FIRST_CELL_WIDTH = 1e-005`；
  `geometries/WP100_comp.geomTurbo` 使用 `Millimeters`，运行产物里 `FIRST_CELL_WIDTH = 0.01`，两者都相当于
  `1e-5 m`。
- 点数参数通常受拓扑和多重网格约束。B2B 点数建议优先选 `4n + 1` 形式，例如 `17`、`33`、`57`、
  `97`。
- 多行、tandem row、splitter blade 和 matching interface 会引入行间一致性要求；不能只改单行某个
  点数而不检查接口点数。
- 如果继续使用 row wizard，网格控制值更稳妥的时机通常是 wizard 之后再设置，或者设置后重新读取
  `.trb` 验证没有被 wizard 重置。

### A. 适合优先实现的少量控制项

这些项目对用户价值高，语义相对清楚，也不需要把当前主实现变成大配置系统。

| 控制项 | 建议入口 | 作用 | 取值/设置示例 | 实现风险 |
|---|---|---|---|---|
| 网格级别预设 | `--mesh-level <level>` | 用少量预设控制整体点数、优化步数和 gap 点数。 | `level` 可取 `coarse`、`medium`、`fine`；可先分别对应 33、57、97 flow paths。该对应关系来自可检索 AutoGrid5 手册片段对无 gap blade 的 grid level 描述。 | 中 |
| 首层单元高度 | `--first-cell-width <value>` | 控制壁面第一层厚度，服务 y+ 初值。 | 以当前样例为参考：米制算例可写 `1e-5`；毫米制算例可写 `0.01`。该值应由目标 `y+`、速度、密度、黏度和参考长度估算，而不是固定常数。 | 中 |
| spanwise flow paths | `--spanwise-paths <int>` | 控制 hub-to-shroud 方向分辨率。 | `33` 用于快速检查；`57` 用于中等初筛；`97` 用于更细 spanwise 分辨率。教程案例中也出现过 `Spanwise Grid Point Number = 33`。 | 中 |
| gap 网格点数 | `--gap-points <int>` 或包含在 preset 中 | 控制 tip/hub gap 内部分辨率。 | 当前 `Rotor37` 和 `WP100` 产物里可见 `HOH_gap_n1 = 5`、`HOH_gap_n2 = 33`。若开放单一 CLI，建议先只控制最小 gap spanwise 点数，例如 `--gap-points 5`、`--gap-points 9`。 | 中 |
| 优化步数 | `--optimization-steps <int>` | 控制 B2B/3D 平滑优化强度。 | 当前产物里 `OPTIMIZATION = 200`；快速预览可考虑较低值，例如 `50` 或 `100`；正式初始网格保持 `200` 更接近现状。 | 低到中 |
| gap 优化步数 | `--gap-optimization-steps <int>` | 专门改善 gap 区域质量。 | 当前产物里 `GAP_OPTIMIZATION_STEPS = 100`；无 gap 行可忽略；有 tip/hub gap 的行可用 `50`、`100` 做 preset 档位。 | 低到中 |

建议优先做 `--mesh-level`。它比直接暴露几十个 AutoGrid 内部字段更稳，也符合当前主实现保持简化的方向。

假设后续开放这些入口，命令行可以长这样：

```powershell
python mesh.py geometries/Rotor37.geomTurbo --mesh-level coarse --dry-run
python mesh.py geometries/Rotor37.geomTurbo --mesh-level medium --first-cell-width 1e-5
python mesh.py geometries/WP100_comp.geomTurbo --mesh-level fine --spanwise-paths 97 --optimization-steps 200 --gap-optimization-steps 100
```

注意：以上命令是后续接口草案，当前 `mesh.py` 尚不支持这些参数。

### B. 可实现但需要更谨慎的控制项

这些控制项有明确价值，但一旦直接暴露，会变成 v5 风格的参数覆盖面。更适合在内部 preset 中使用，
或只给高级用户开放少数稳定参数。

| 控制项 | 可能对应的 AutoGrid/.trb 参数 | 说明 | 取值/设置示例 |
|---|---|---|---|
| B2B 点数分布 | `HOHbladeNpt1` 到 `HOHbladeNpt8`、`HINBlade*` | 控制叶片前缘、叶身、尾缘、上下游方向点数。 | 当前产物常见组合是 `33, 17, 33, 17, 17, 17, 17, 17`。若手工加密，优先保持 `4n + 1`，例如把局部 `17` 提到 `33`，或把关键方向 `33` 提到 `57`。 |
| 叶片表面点数 | `HOHSkinNStrBlade*` | 控制 blade skin 方向分辨率。 | `Rotor37` 产物为 `89`；`WP100` 多数行为 `81`，其中一处 `HOHSkinNStrBladeDown = 137`。可作为 preset 内部随 `mesh-level` 增减的候选字段。 |
| blade-to-blade 聚集 | `HOHBladeToBladeclusteringRatio` | 控制 B2B 方向聚集强度。 | 当前产物为 `0.0125`。建议先只在 preset 内保持或小幅调整，避免用户直接输入无量纲聚集强度。 |
| streamwise 聚集 | `HOHStreamwiseClusteringRatio` | 控制流向聚集强度。 | 当前产物为 `0.2`。适合跟 B2B 点数一起作为 preset 内部细节。 |
| 边界层数量/增长 | `HOHSkinNBndLayerGap`、`HOHSkinExpansionRatio` | 控制壁面层和 gap 边界层。 | 当前产物里 `HOHSkinNBndLayerGap = 17`；`HOHSkinExpansionRatio` 约在 `1.05` 到 `1.27`。通常应结合首层高度和总边界层厚度一起调整。 |
| wake/orthogonality 控制 | `OPTIMIZATION_WAKECONTROL_LEVEL`、`OPTIMIZATION_ORTHOGONALITY_LEVEL` | 控制平滑优化目标权重。 | 当前产物为 `0.5`、`0.5`，gap 内正交性权重也是 `0.5`。更适合作为质量修复策略的一部分，而不是普通 CLI。 |
| tip/hub 控制宽度 | `TIP_CONTROL_WIDTH_*`、`HUB_CONTROL_WIDTH_*` | 控制端壁附近影响区。 | 单位跟随算例：`Rotor37` tip 控制宽度为 `0.000356 m`；`WP100` tip 控制宽度可见 `0.2 mm` 和 `0.605 mm`，hub 控制宽度可见 `1.347 mm`。不建议跨算例复用。 |
| gap 前后缘宽度 | `WIDTH_AT_LEADING_EDGE`、`WIDTH_AT_TRAILING_EDGE` | 来自几何或 gap 定义，修改会改变物理间隙。 | `Rotor37` 为 `0.000356 m`；`WP100` 部分行为 `0.2 mm`。这更像几何/gap 定义，不应作为单纯加密参数。 |

注意：`WIDTH_AT_LEADING_EDGE` 和 `WIDTH_AT_TRAILING_EDGE` 更像几何/gap 定义，不只是网格密度。
如果开放修改，可能改变算例物理含义，应默认从 `.geomTurbo` 读取，不建议作为普通网格参数。

### C. 不建议在当前主实现直接开放的控制项

| 控制项 | 原因 |
|---|---|
| 任意 `.trb` 字段覆盖 | 会重新引入大配置面，违背当前主实现的简化目标。 |
| 每个 block 的任意点数覆盖 | 易产生拓扑不一致、奇偶点数错误或连接失败。 |
| 机器类型、转速、rotor/stator 手工覆盖 | 这些更接近 row wizard 的物理/工况定义，应优先从几何或项目语义获得。 |
| 任意拓扑类型选择 | H&I、O4H、出口 H-topology 等对几何敏感，错误选择会直接破坏网格质量。 |
| 自动调参闭环 | 需要失败诊断、重试策略和质量目标，不应混入当前初始网格脚本。 |

## 推荐的实现路线

如果后续要开发，建议分三步推进：

1. 保持当前无配置文件风格，只增加少量 CLI 参数。
2. 优先实现 `--mesh-level coarse|medium|fine`，内部映射到少数稳定字段。
3. 在确认 AutoGrid Python API 的正式 setter 之前，避免依赖脆弱的字符串替换。

可能的 preset 方向：

| preset | 目标 | 示例映射草案 |
|---|---|---|
| `coarse` | 快速生成，低点数，用于检查几何和流程。 | `spanwise-paths = 33`；B2B 点数尽量保留当前默认；`OPTIMIZATION` 可用 `50` 到 `100`。 |
| `medium` | 默认工程初筛，保持当前 row wizard 生成水平附近。 | `spanwise-paths = 57`；`FIRST_CELL_WIDTH` 默认由 row wizard 或 y+ 估算值给出；`OPTIMIZATION = 200`、`GAP_OPTIMIZATION_STEPS = 100` 可作为当前样例基准。 |
| `fine` | 增加 spanwise/B2B 点数和优化步数，用于质量改善或网格无关性第一步。 | `spanwise-paths = 97`；局部 B2B 点数从 `33` 增到 `57` 或从 `57` 增到 `97`；优化步数是否继续增加需实机验证。 |

## 实现前需要确认的问题

- AutoGrid 当前版本是否提供稳定 Python API 来设置 B2B/3D 参数，而不是只能保存 `.trb` 后修改。
- 参数应该在 row wizard 前设置，还是 row wizard 后设置。Row wizard 可能会重置 expert mode 参数。
- 多行算例中参数是全局统一，还是允许按 row 名称设置。
- splitter blade 是否和 main blade 共用控制项。
- gap 参数是来自 `.geomTurbo` 的几何定义，还是由 AutoGrid 后处理配置生成。
- 质量失败时是否只报告，还是进入自动重试。

## 参考资料

- [Cadence: Fidelity Turbomachinery Meshing with the Row Wizard](https://resources.system-analysis.cadence.com/computational-fluid-dynamics/fidelity-turbomachinery-meshing-with-the-row-wizard)
- [Cadence: Fidelity CFD Platform](https://www.cadence.com/en_US/home/tools/system-analysis/computational-fluid-dynamics/fidelity.html)
- [Cadence: Fidelity CFD Pre-Processing and Meshing](https://www.cadence.com/en_US/home/tools/system-analysis/computational-fluid-dynamics/pre-processing-meshing.html)
- [Cadence: Fidelity Fine Turbo Datasheet](https://www.cadence.com/en_US/home/resources/datasheets/fidelity-fine-turbo-accelerates-turbomachinery-designs-ds.html)
- [Cadence: Compute Grid Spacing for a Given Y+](https://www.cadence.com/en_US/home/tools/system-analysis/computational-fluid-dynamics/y-plus.html)
- [AutoGrid5 v8 user manual excerpt: flow paths and grid level](https://www.scribd.com/document/638134105/Untitled)
- [AutoGrid5 tutorial excerpt: first wall cell width and spanwise points](https://www.scribd.com/document/837559122/Tutorial-3-%E7%A6%BB%E5%BF%83%E6%B3%B5)
- [AutoGrid5 advanced tutorial excerpt: B2B grid point rule](https://www.scribd.com/document/89020372/Tutorial-Guide-AutoGrid-82-1-Advanced-Acrov5)
- [Diener, Development of a Mixed-Flow Compressor Impeller for Micro Gas Turbine Application](https://cfturbo.com/fileadmin/content/down/publications/students/2016-03-Diener-Mixed-Flow-Compressor.pdf)
- [Effect of Hub Gap on Expanding Stability of Centrifugal Compressor](https://www.researchgate.net/publication/351076552_Effect_of_Hub_Gap_on_Expanding_Stability_of_Centrifugal_Compressor/fulltext/609b977845851525ed871bb7/Effect-of-Hub-Gap-on-Expanding-Stability-of-Centrifugal-Compressor.pdf)
