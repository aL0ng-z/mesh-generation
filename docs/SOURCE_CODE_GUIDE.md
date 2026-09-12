# 源码指南：从零理解 geomTurbo → AutoGrid 网格生成工具

当前修复契约（2026-09-12）：控制最终核验取生成后回读，`basis=post_generation`；质量规则版本为 2；`row/target_points` 暂时停用。源码/Git 来源在启动 IGG 前采集，输入哈希使用本轮副本。平台数据库当前为 v3，修复记录见 `docs/FIXES_20260912.md`。

> **目标读者**：有 Python 基础但不了解 CFD 的开发者。本文会先解释必要的领域概念，再深入每一行源码。
>
> **阅读建议**：如果只想快速了解项目，读第 0、1 章即可。如果要修改代码，请按模块顺序读第 2～6 章。

---

## 目录

- [0. 前置概念：CFD、网格与叶轮机械](#0-前置概念cfd网格与叶轮机械)
- [1. 项目总览](#1-项目总览)
- [2. 几何解析（`geomturbo.py`）](#2-几何解析geomturbopy)
- [3. 控制项系统（`controls.py`）](#3-控制项系统controlspy)
- [4. AutoGrid 脚本执行（`autogrid.py`）](#4-autogrid-脚本执行autogridpy)
- [5. 质量评估（`quality.py`）](#5-质量评估qualitypy)
- [6. CLI 入口（`mesh.py`）](#6-cli-入口meshpy)
- [A. 附录：术语速查表](#a-附录术语速查表)

---

## 0. 前置概念：CFD、网格与叶轮机械

本章专为没有 CFD 背景的 Python 程序员准备。如果你已有叶轮机械数值仿真经验，可以跳到[第 1 章](#1-项目总览)。

### 0.1 CFD 是什么

**CFD（Computational Fluid Dynamics，计算流体力学）** 是一种用计算机模拟流体流动的方法。它的核心思想是：

1. 把连续的物理空间切分成许多小格子（**网格**）。
2. 在每个格子上求解物理方程（Navier-Stokes 方程）——这些方程描述了流体的速度、压力、温度等。
3. 把所有格子拼起来，就得到了整个流场的近似解。

> **Python 类比**：CFD ≈ 在非常大的非均匀三维数组上做迭代数值计算。网格就是数组的形状，每个数组元素存储该位置的流体状态（速度、压力等）。

### 0.2 网格是什么

**网格（Mesh / Grid）** 是一组离散的点及其连接关系。在三维 CFD 中，最常见的网格单元是六面体（hexahedron，8 个顶点），看起来像一个变形的立方体。

网格的关键属性：

| 属性 | 含义 | Python 类比 |
|---|---|---|
| **点数** | 网格中顶点的总数 | `arr.size` |
| **网格层级** | 多重网格技术中的粗化/细化等级 | 不同分辨率的图像金字塔 |
| **拓扑** | 单元的连接方式——谁和谁是邻居 | 邻接表（adjacency list） |
| **首层单元宽度** | 贴近固体壁面的第一层网格厚度 | 边界条件的空间分辨率 |
| **偏斜角（skewness）** | 网格单元偏离完美正六面体的角度 | 越接近 90° 越好 |
| **增长率（expansion ratio）** | 相邻网格单元大小的比值 | 越接近 1.0 越均匀 |
| **长宽比（aspect ratio）** | 网格单元最长边和最短边的比值 | 越接近 1.0 越好 |
| **负体积单元** | 节点顺序反转导致体积为负的畸形单元 | 相当于 `NaN`——求解器无法计算 |

> **一句话**：网格质量直接决定 CFD 计算能否收敛、计算结果是否可信。本项目的工作就是"自动生成高质量网格"。

### 0.3 网格生成（Meshing）的难点

手动生成网格需要 CFD 工程师做大量重复工作：
- 在几十条曲线上指定网格点数
- 调整靠近壁面的网格厚度（边界层）
- 处理叶片前后缘的复杂拓扑
- 优化偏斜角和增长率直到满足阈值

这些操作在 NUMECA AutoGrid 这种商业软件中是通过 GUI 手动完成的——点几百次鼠标。本项目的目标就是把这些操作全部**自动化**。

### 0.4 AutoGrid 是什么

**NUMECA AutoGrid** 是专门为叶轮机械设计的网格生成商业软件。它提供一个 Python API，允许用户通过脚本控制网格生成的每一个步骤。

本项目使用的版本是 **AutoGrid 17.1**，其 Python API 位于：

```
C:\ProgramData\NUMECA\fine171\_python\_autogrid\Autogrid.py
```

这个文件包含大约 721 个 `set_*` / `a5_set_*` 方法，分别属于 `Row`、`Blade`、`Gap`、`RowWizard`、`RSInterface` 等类。我们的 `controls.py` 模块做的事情就是**系统地管理这 721 个方法中与网格生成相关的子集**——哪些能用、怎么用、参数是什么。

### 0.5 IGG 是什么

**IGG** 是 NUMECA 的网格生成器可执行文件（`iggx86_64.exe`），它是实际执行网格计算的程序。AutoGrid 是一个"前端"——它通过 IGG 来生成网格。

本项目通过以下命令启动 AutoGrid + IGG：

```bash
iggx86_64.exe -autogrid5 -batch -script autogrid_init.py
```

- `-autogrid5`：启用 AutoGrid 模式
- `-batch`：无 GUI 批处理模式
- `-script autogrid_init.py`：执行我们生成的 Python 脚本

### 0.6 `.geomTurbo` 文件是什么

`.geomTurbo` 是 NUMECA 定义的叶轮机械几何描述文件格式。它是一个文本文件，包含：

- 全局标量参数：`VERSION`、`UNITS`（单位系统）、`UNITS-FACTOR`（米换算因子）
- 嵌套块结构：用 `NI_BEGIN` / `NI_END` 包裹的层级数据块

一个典型的 `.geomTurbo` 文件结构如下：

```text
VERSION 5.0
UNITS mm
UNITS-FACTOR 0.001

NI_BEGIN niRow
  NAME Rotor
  PERIODICITY 36
  NI_BEGIN niBlade
    NAME Main Blade
    NUMBER_OF_BLADES 36
    NI_BEGIN niShroudGap
      ...
    NI_END niShroudGap
  NI_END niBlade
NI_END niRow
```

> **Python 类比**：`.geomTurbo` ≈ 一个嵌套字典结构的序列化文本。`geomturbo.py` 的工作就是把这个文本解析成结构化的 Python 数据对象。

### 0.7 叶轮机械特有的概念速查

| 术语 | 含义 | 为什么重要 |
|---|---|---|
| **叶排（Row）** | 一圈叶片，如"压气机第 1 级转子" | 网格按叶排生成，是控制项的顶层作用域 |
| **叶片（Blade）** | 单个叶片实体，包括主叶片和分流叶片 | B2B 网格在叶片间生成 |
| **分流叶片（Splitter）** | 两个主叶片之间的短叶片 | 增加额外的几何复杂度 |
| **叶尖间隙（Tip Gap）** | 叶片尖端与机匣之间的缝隙 | 湍流的重要来源，需要专门网格 |
| **Hub / Shroud** | 轮毂 / 机匣——叶片根部 / 叶片顶部 | 网格的径向边界 |
| **B2B（Blade-to-Blade）** | 叶片到叶片的二维截面 | 网格先在 B2B 面上生成，再沿径向堆叠成 3D |
| **Flow Path** | 流道中的一条展向网格线 | 沿叶片高度方向的网格分辨率控制 |
| **边界层（Boundary Layer）** | 贴近固体壁面、速度梯度极大的一薄层流体 | 需要极高网格密度才能准确计算 |
| **RowWizard** | AutoGrid 的自动向导，根据几何自动设置默认网格参数 | 一个"自动配置"按钮，我们的脚本会在适当阶段调用它 |
| **前缘 / 尾缘（Leading / Trailing Edge）** | 叶片的进气边 / 出气边 | 曲率最大，网格最难处理的区域 |

> **一句话总结整个流程**：读取 `.geomTurbo` → 解析出有几个叶排、每个叶排有几个叶片 → 用户指定网格控制参数 → 生成 AutoGrid Python 脚本 → 交给 IGG 执行 → 收集结果和质量报告。

---

## 1. 项目总览

### 1.1 项目解决了什么问题

在 NUMECA AutoGrid 中手动生成一个叶排的 CFD 网格需要：
1. 打开 `.geomTurbo` 文件
2. 运行 RowWizard 自动向导
3. 逐个调整 B2B 拓扑类型、网格点数、边界层参数、优化参数
4. 对每个 gap、fillet、interface 重复以上步骤
5. 生成网格后检查质量报告
6. 如果质量不合格（偏斜角过大、增长率过高），回到步骤 3

对于一个三级压气机（3 个叶排，每排可能有主叶片+分流叶片+hub/shroud gap+hub/shroud fillet），手动操作可能需要 **1-2 小时**。本项目将这个流程压缩为**一条命令行**，同时保证：
- 所有参数经过类型、范围、拓扑适用性的**静态校验**（不会把错误参数传给 AutoGrid 后才报错）
- 所有 setter 调用都有 getter **回读验证**
- 完整的运行记录和质量报告自动生成

### 1.2 文件职责一览

```text
项目根目录/
├── src/             ← 扁平的网格内核源码目录
│   ├── mesh.py      ← CLI 入口：解析参数、编排全流程、写摘要和报告
│   ├── controls.py  ← 核心：344 项控制规格的静态注册表 + 选择器 + 解析 + 校验 + 审计
│   ├── geomturbo.py ← 输入：解析 .geomTurbo 文件的几何与拓扑信息
│   ├── autogrid.py  ← 输出：生成 AutoGrid Python 脚本并调用 IGG 执行
│   └── quality.py   ← 输出后：解析 .qualityReport 并判定 PASS/FAIL/UNKNOWN
├── geometries/      ← 样例：.geomTurbo 输入文件
├── tests/           ← 测试：单元测试（当前 39 个通过）
├── docs/            ← 文档：设计说明、控制目录、质量准则、开发日志
├── runs/            ← 产物：可丢弃的运行输出
├── archive/         ← 历史：v0~v6 快照（当前代码不读取）
├── README.md
└── CLAUDE.md         (即 AGENTS.md)
```

**依赖关系**（谁 import 谁）：

```text
src/mesh.py
  ├── controls.py      # 控制项解析和校验
  ├── geomturbo.py     # 几何文件解析
  ├── autogrid.py      # 脚本生成和执行
  │   └── controls.py  # 引用 CONTROL_REGISTRY 用于序列化
  └── quality.py       # 质量报告解析
```

> **注意**：项目仅依赖 Python 标准库。`re`、`json`、`dataclasses`、`pathlib`、`typing`、`subprocess`、`shutil`、`os`、`sys`、`math`——这些就是全部依赖。没有任何第三方包。

### 1.3 一次完整运行的数据流

用户执行：

```powershell
python src/mesh.py geometries/Rotor37.geomTurbo --mesh-level fine --first-cell-width 1e-5
```

下面是每一步的详细数据流：

```text
Step 1: 解析命令行参数（src/mesh.py:main）
  └→ args = argparse.Namespace(geomturbo="geometries/Rotor37.geomTurbo",
         mesh_level="fine", first_cell_width=1e-5, ...)

Step 2: 解析几何文件（src/geomturbo.py:parse_geomturbo）
  输入:  "geometries/Rotor37.geomTurbo" 文件内容（文本）
  输出:  GeomTurboSummary(
           path="...",
           units="mm",
           units_factor=0.001,
           row_count=1,
           rows=[RowInfo(name="Rotor", periodicity=36, blades=[BladeInfo(...)])]
         )

Step 3: 构建控制请求（src/mesh.py:_build_control_requests）
  输入:  args.mesh_level="fine", args.first_cell_width=1e-5
  输出:  ["row:*/mesh_level=fine", "row:*/wizard/first_cell_width=1e-5"]
         (快捷参数被转换为标准的 --set 表达式)

Step 4: 解析并校验控制（src/controls.py:parse_control_assignments）
  输入:  ["row:*/mesh_level=fine", "row:*/wizard/first_cell_width=1e-5"]
  输出:  [ControlRequest(raw="row:*/mesh_level=fine", key="row/mesh_level",
           selectors=(EntitySelector(kind="row", mode="wildcard", value="*"),),
           value="fine", spec=ControlSpec(...)), ...]

Step 5: 展开通配符、绑定实体（src/controls.py:resolve_control_requests）
  输入:  requests + GeomTurboSummary
  输出:  [ResolvedControl(control_id="C0001", key="row/mesh_level",
           target=(TargetEntity(kind="row", index=1, name="Rotor"),),
           requested_value="fine", project_value="fine",
           api_value=4, setter="set_coarse_grid_level", ...)]

Step 6: 生成 AutoGrid 脚本（src/autogrid.py:render_autogrid_script）
  输入:  GeomTurboSummary + ResolvedControl[]
  输出:  "runs/Rotor37_.../autogrid_init.py" 文件（Python 脚本文本）

Step 7: 执行 IGG（src/autogrid.py:run_autogrid_init）
  输入:  "iggx86_64.exe -autogrid5 -batch -script autogrid_init.py"
  输出:  AutoGridRun(command, returncode=0, outputs={...}, control_results=[...])

Step 8: 解析质量报告（src/quality.py:summarize_quality）
  输入:  outputs["quality_report"] = "runs/.../mesh.qualityReport"
  输出:  {metrics_source, metadata, project, entities, metrics, result}

Step 9: 写入运行摘要和报告（src/mesh.py:main）
  输出:  run_summary.json + report.md
```

### 1.4 AutoGrid 执行阶段顺序

控制项不是按用户输入顺序执行的，而是按**固定的阶段顺序**依次应用。这个顺序是 AutoGrid 的内在要求——比如必须先运行 RowWizard，才能设置拓扑和分布参数：

```text
1. configuration          ← 全局配置（多重网格层级等）
2. wizard                 ← RowWizard 参数（flow path 数、首层宽度等）
3. RowWizard.generate()   ← 自动生成默认网格拓扑
4. topology               ← B2B 拓扑类型选择（Default / HOH / H&I）
5. distribution           ← 网格点数分布（流向点、展向点、周向点……）
6. boundary_layer         ← 边界层参数（首层宽度、增长率、边界层厚度）
7. optimization           ← 网格优化参数（偏斜、正交性、光顺步数）
8. interface              ← 行间交界面设置
9. existing_effect        ← 已有技术效果的网格控制（孔、端壁、snubber 等）
10. B2B/3D 网格生成       ← a5_generate_flow_paths → a5_generate_b2b → a5_generate_3d
```

阶段常量定义在 `controls.py:12-22`（`STAGES` 和 `STAGE_ORDER`）。

### 1.5 错误码体系

| 返回码 | 含义 | 触发场景 |
|---|---|---|
| **0** | 成功 | 网格生成完成且质量可评估 |
| **1** | AutoGrid 阶段失败 | 实体不存在、拓扑不适用、setter/getter 异常、脚本 traceback、事件协议错误 |
| **2** | 静态校验失败或运行目录不可用 | 未知控制键、类型/范围错误、选择器不匹配、重复定义、`--no-row-wizard` 冲突、输出目录被占用或含非白名单既有文件 |

- 返回码 2 **不会**启动 IGG——在解析阶段或取得运行目录所有权之前就直接退出；运行目录被拒绝时保留既有文件。
- 返回码 1 会创建运行目录和 `run_summary.json`，其中 `controls.applied` 记录了具体哪个控制失败。

### 1.6 关键设计原则

在阅读后续源码详解前，先记住本项目遵循的几个核心原则：

1. **无配置文件**：不读 JSON/YAML，不读 `.trb` 模板。所有参数通过 CLI 传入。
2. **唯一真理源**：`controls.py` 中的 `CONTROL_REGISTRY` 是网格控制的唯一定义。不存在第二份手工维护的清单。
3. **严格失败**：任何不符合预期的输入都报错退出，绝不静默忽略或猜测意图。
4. **不做几何修改**：所有控制仅改变网格参数，不改变物理几何（如叶片形状、gap 尺寸等）。
5. **`.`trb` 只写不读**：`.trb` 文件仅由 AutoGrid 的 `a5_save_project()` 保存，程序不解析或修改其文本内容。
6. **运行产物可丢弃**：`runs/` 下的所有内容都是可丢弃的，`run_summary.json` 包含完整的可复现信息。

---

## 2. 几何解析（`geomturbo.py`）

文件位置：`src/geomturbo.py`（249 行）

### 2.1 `.geomTurbo` 文件格式

`.geomTurbo` 是一个嵌套的标记文本格式。它不是标准格式（如 JSON 或 INI），而是 NUMECA 自定义的"NI 标记语言"——一种使用 `NI_BEGIN` / `NI_END` 包围块的层级格式。

**语法规则**：

- 注释以 `#` 开头
- 顶层参数：`KEY  VALUE`（空格分隔）
- 嵌套块：`NI_BEGIN blockType [arg]` ... `NI_END blockType`
- 块可以嵌套，如 `niRow` 内包含 `niBlade`，`niBlade` 内包含 `niShroudGap`

**项目关心哪些块**：

| 块标签 | 含义 | 解析后产出的信息 |
|---|---|---|
| `niRow` | 一个叶排 | 叶排名称、周期数、包含的叶片 |
| `niBlade` | 一个叶片实体 | 叶片名称、叶片数、是否有 gap/fillet |
| `niTipGap` / `niShroudGap` / `niHubGap` | 间隙（tip/shroud/hub） | gap 在哪一侧（hub/shroud） |
| `niTipPartialGap` / `niShroudPartialGap` / `niHubPartialGap` | 半间隙 | partial-gap 在哪一侧 |
| `niTipFillet` / `niShroudFillet` / `niHubFillet` | 圆角 | fillet 在哪一侧 |
| `niNonAxisSurfaces tip_gap` | 非轴对称叶尖间隙面 | 叶尖间隙是否存在 |

**不关心什么**：叶片的实际几何坐标（曲线上的 `(x, y, z)` 点）、物理尺寸、曲面链接等。因为 `geomturbo.py` 只服务于**控制项的选择器匹配**——我们需要知道"有几个 row、每个 row 有几个 blade、每个 blade 在哪一侧有 gap/fillet"，以便将用户的 `--set` 表达式映射到正确的 AutoGrid 实体。

### 2.2 核心数据结构

`geomturbo.py` 定义了三个数据类，形成三层嵌套：`GeomTurboSummary` → `RowInfo` → `BladeInfo`。全部使用 `@dataclass(frozen=True)`（不可变，支持哈希）。

#### 2.2.1 `BladeInfo`

```python
@dataclass(frozen=True)
class BladeInfo:
    name: str                          # 叶片名称，如 "Main Blade"、"Splitter Blade 1"
    number_of_blades: int | None       # 该叶片的实体数量（通常=周期数）
    has_tip_gap: bool                  # 是否有叶尖间隙
    gap_sides: tuple[str, ...]         # gap 在哪些侧，如 ("hub", "shroud")
    partial_gap_sides: tuple[str, ...] # partial-gap 在哪些侧
    fillet_sides: tuple[str, ...]      # fillet 在哪些侧
```

> `gap_sides` / `partial_gap_sides` / `fillet_sides` 的值是 `"hub"` 或 `"shroud"`，用于控制项的选择器匹配。

#### 2.2.2 `RowInfo`

```python
@dataclass(frozen=True)
class RowInfo:
    name: str                  # 叶排名称，如 "Rotor"、"Stator"
    periodicity: int | None    # 周期数（一圈有多少个叶片通道）
    blades: list[BladeInfo]    # 包含的叶片实体（至少 1 个）
    has_tip_gap: bool          # 该叶排是否整体有叶尖间隙

    @property
    def main_blades(self) -> int | None:
        # 主叶片数 = 第一个叶片的 number_of_blades，降级到 periodicity

    @property
    def has_splitter(self) -> bool:
        # 是否有分流叶片 = 叶片数 > 1 或名称含 "spl"
```

`main_blades` 的逻辑（`geomturbo.py:33-38`）：优先使用第一个 `BladeInfo` 的 `number_of_blades`；如果为空，回退到叶排的 `periodicity`。这在处理只有一个叶片的简单几何时保持了合理的默认行为。

`has_splitter` 的逻辑（`geomturbo.py:41-44`）：如果有 >1 个 `BladeInfo` 实体（主叶片 + 分流叶片），或者任意叶片名称包含 `"spl"` 子串。

#### 2.2.3 `GeomTurboSummary`

```python
@dataclass(frozen=True)
class GeomTurboSummary:
    path: str                    # 输入文件路径
    version: str | None          # .geomTurbo 格式版本
    units: str | None            # 几何使用的长度单位，如 "mm"、"m"
    units_factor: float | None   # 1 项目单位 = units_factor 米
    row_count: int               # 叶排总数
    rows: list[RowInfo]          # 每个叶排的详细信息

    @property
    def multi_row(self) -> bool:    # row_count > 1
    @property
    def has_splitter(self) -> bool: # 任一叶排有分流叶片
    @property
    def has_tip_gap(self) -> bool:  # 任一叶排有叶尖间隙

    def to_dict(self) -> dict:
        # 递归转换为可 JSON 序列化的字典
```

`units_factor` 是整个单位系统的核心。见 [2.5 单位系统](#25-单位系统)。

### 2.3 `parse_geomturbo()` 解析流程

```python
def parse_geomturbo(path) -> GeomTurboSummary:
    text = path.read_text(encoding="utf-8", errors="replace")
    rows = _parse_rows(text)
    return GeomTurboSummary(
        path=str(path),
        version=_first_value(text, "VERSION"),
        units=_first_value(text, "UNITS"),
        units_factor=_first_float_value(text, "UNITS-FACTOR"),
        row_count=len(rows),
        rows=rows,
    )
```

（`geomturbo.py:89-103`）

解析过程分为两层：

1. **顶层**：用简单的正则匹配提取 `VERSION`、`UNITS`、`UNITS-FACTOR`
2. **块层**：`_parse_rows()` 逐行扫描，用栈跟踪嵌套层级

### 2.4 嵌套块解析（`_parse_rows`）

`_parse_rows`（`geomturbo.py:105-205`）是本文件最复杂的函数。它本质上是一个**基于栈的状态机**。

**核心思路**：

1. 维护一个 `stack: list[str]`，记录当前所在的块层级（如 `["nirow", "niblade", "nishroudgap"]`）
2. 遇到 `NI_BEGIN` → 入栈，创建对应的解析上下文（`current_row` / `current_blade`）
3. 遇到普通行 → 根据当前栈顶决定如何解析（名称、周期数、叶片数）
4. 遇到 `NI_END` → 出栈，完成当前实体的构建

**逐层行为**：

```
遇到 NI_BEGIN niRow:
  → 创建 current_row 字典，记录行级信息
  → 此时 current_blade 为 None

在 niRow 内遇到 NI_BEGIN niBlade:
  → 创建 current_blade 字典，记录叶片级信息

在 niBlade 内遇到 NI_BEGIN niShroudGap:
  → 将 "shroud" 添加到 current_blade["gap_sides"]
  → 标记 current_blade["has_tip_gap"] = True
  → 标记 current_row["has_tip_gap"] = True

遇到 NI_END niBlade:
  → 从 current_blade 字典构建 BladeInfo 对象
  → 添加到 current_row["blades"]

遇到 NI_END niRow:
  → 从 current_row 字典构建 RowInfo 对象
  → 添加到 rows 列表
  → 重置 current_row 和 current_blade 为 None
```

**关键代码片段**（`geomturbo.py:139-143`）：

```python
elif block in {"nitipgap", "nishroudgap", "nihubgap"}:
    if current_blade is not None:
        side = "hub" if block == "nihubgap" else "shroud"
        current_blade["gap_sides"].append(side)
        current_blade["has_tip_gap"] = side == "shroud" or current_blade["has_tip_gap"]
    if current_row is not None:
        current_row["has_tip_gap"] = True
```

这里 `"shroud"` 侧的 gap 被视为 tip gap（即 `has_tip_gap = True`），hub 侧的 gap 不标记为 tip gap 但不影响叶排级别的 `has_tip_gap`（只要**任何一个** blade 有 gap，叶排级别的 `has_tip_gap` 就为 True）。

**名称解析**（`geomturbo.py:192-203`）：

```python
if key_lower == "name":
    current_block = stack[-1] if stack else ""
    if current_blade is not None and current_block == "niblade":
        current_blade["name"] = value     # 覆盖默认名称
    elif current_blade is None and current_block == "nirow":
        current_row["name"] = value       # 覆盖默认名称
```

默认名称（`row_1`、`blade_1` 等）会在遇到用户定义的 `NAME` 时被覆盖。这保证了即使几何文件没有显式命名，解析也不会失败。

### 2.5 单位系统

`.geomTurbo` 文件中的 `UNITS` 和 `UNITS-FACTOR` 定义了长度单位：

```
UNITS mm
UNITS-FACTOR 0.001
```

这意味着 1 项目单位 = 0.001 米（即 1 毫米）。系统内部所有长度计算遵循：

```
project_value = requested_si / units_factor
```

例如用户传入 `--first-cell-width 1e-5`（即 0.01 毫米），在 `UNITS-FACTOR = 0.001` 的毫米制项目中：

```
api_value = 1e-5 / 0.001 = 0.01  (毫米)
```

这个换算发生在 `controls.py:convert_si_length()`（`controls.py:1851-1858`），被 `resolve_control_requests()` 调用。

### 2.6 辅助函数

| 函数 | 位置 | 作用 |
|---|---|---|
| `_split_key_value(line)` | `208-214` | 按首个空格拆分 `KEY VALUE` 行 |
| `_first_value(text, key)` | `217-221` | 正则匹配键值对，返回原始字符串 |
| `_first_float_value(text, key)` | `224-233` | 同上，转为 float |
| `_to_int(value)` | `236-242` | 安全转 int（先 float 再 int，容忍 `"36.0"`） |
| `_unique_tuple(values)` | `245-248` | 保持顺序的去重返回不可变元组 |

---

## 3. 控制项系统（`controls.py`）

文件位置：`src/controls.py`（2093 行，项目最大的模块）

### 3.1 设计动机：为什么需要控制项系统

AutoGrid 17.1 的 `Autogrid.py` 暴露了 721 个 `set_*` / `a5_set_*` 方法。这些方法的命名风格不一致，参数类型各式各样（有的是整数、有的是枚举字符串、有的是浮点数），而且很多方法是关于物理几何修改（如改变叶片形状）的——这些我们不应该暴露给用户。

如果在 `mesh.py` 中直接调用这些方法：
1. 每次添加新的网格控制都要写大量样板代码
2. 无法对用户输入做静态校验（类型检查、范围检查、拓扑适用性检查）
3. 无法生成控制目录文档（`--list-controls` / `--describe-control`）
4. 无法做 setter 审计（哪些 17.1 方法已被覆盖、哪些被明确排除）

`controls.py` 解决这些问题的方案是：**建立一个静态注册表作为"唯一真理源"**。

### 3.2 核心概念：从用户键入到 API 调用

整个控制系统的数据流分为四个阶段：

```
用户输入                     解析后                      展开后                       传给 AutoGrid
"row:*/mesh_level=fine"  →  ControlRequest      →      ResolvedControl         →     autogrid_init.py
                             (1 个 request，            (N 个 resolved，              (逐个调用 setter)
                              selectors 可能含          target 已绑定到
                              wildcard)                 具体实体索引)
```

每个阶段对应一个核心数据类：

| 数据类 | 含义 | 何时创建 |
|---|---|---|
| `ControlSpec` | 控制项的"规格说明书"——定义了一个控制的名称、类型、范围、对应的 API 方法 | 模块加载时（静态注册表） |
| `EntitySelector` | 用户表达式中的一个实体引用（如 `row:*`、`blade:#1`、`gap:shroud`） | 解析 `--set` 表达式时 |
| `ControlRequest` | 用户的一次请求——已解析选择器和值，但还未展开 wildcard | `parse_control_assignment()` |
| `TargetEntity` | 几何中一个具体的实体（如叶排 #1 叫 "Rotor"） | 展开 wildcard 时由几何信息填充 |
| `ResolvedControl` | 完全展开后的控制——知道要调哪个 setter、对哪个实体、传什么值 | `resolve_control_requests()` |

### 3.3 `ControlSpec`：控制规格的静态定义

```python
@dataclass(frozen=True)
class ControlSpec:
    key: str                              # 稳定语义键，如 "row/mesh_level"
    description: str                      # 中文说明
    scope: str                            # 作用域类型
    target_kind: str                      # 运行时对象类型（如 "row"、"blade"）
    hierarchy: tuple[str, ...]            # 选择器层级，如 ("row", "blade", "gap")
    value_type: str                       # "bool" | "int" | "float" | "enum" | "tuple_int" | "tuple_float"
    setter: str | None                    # AutoGrid API setter 方法名
    getter: str | None                    # AutoGrid API getter 方法名；None 表示 setter 成功即视为 applied
    priority: str                         # "P0" | "P1" | "P2"
    stage: str                            # 应用阶段（见 1.4 节）
    enum_values: tuple[str, ...]          # 枚举类型的合法值列表
    value_map: tuple[tuple[Any, Any], ...] # 值映射（如 ("coarse", 1)）
    setter_by_value: tuple[tuple[Any, str], ...]  # 按值选择 setter（用于成对接口）
    minimum: float | None                 # 数值下限（含）
    maximum: float | None                 # 数值上限（含）
    tuple_length: int | None              # 元组类型的固定长度
    si_length: bool                       # 是否为长度量（需单位换算）
    topologies: tuple[str, ...]           # 适用 B2B 拓扑列表；不匹配时严格失败
    not_applicable_when: str | None       # 不适用条件的说明
    setter_mode: str = "value"            # setter 调用模式（见 3.3.2）
```

（`controls.py:30-53`）

#### 3.3.1 字段详解

**`key`**：全局唯一的控制标识。格式为 `<scope>/<local_key>`。例如：
- `configuration/grid_levels` — 全局多重网格层级
- `row/mesh_level` — 叶排级别网格密度
- `blade/b2b.default.azimuthal_inlet_points` — Default 拓扑入口周向点数

**`scope`**：控制项所属的大类。决定了 `--list-controls P0` 时如何分类显示：
- `configuration`、`wizard`、`row`、`blade`、`gap`、`partial-gap`、`fillet`、`interface`、`endwall`、`existing-effect`

**`target_kind`**：控制项在 AutoGrid 运行时作用的目标对象类型。决定了 `autogrid_init.py` 脚本中 `_resolve_target()` 如何定位实体。如 `"row"` → 调用 `row(index)`；`"blade"` → 调用 `row_entity.blade(index)`。

**`hierarchy`**：选择器路径的层级结构。用于校验用户提供的选择器层级是否与控制项匹配。例如 `("row", "blade", "gap")` 要求选择器路径必须是 `row:xxx/blade:xxx/gap:xxx`。

**`value_type`**：支持 6 种类型：
- `"bool"` → `true`/`false`/`1`/`0`
- `"int"` → 整数
- `"float"` → 有限浮点数
- `"enum"` → 从 `enum_values` 中选择
- `"tuple_int"` → 逗号分隔的 N 个整数
- `"tuple_float"` → 逗号分隔的 N 个浮点数

**`setter` / `getter`**：映射到 `Autogrid.py` 中对应的方法名。例如 `setter="set_coarse_grid_level"` 表示脚本中会调用 `target.set_coarse_grid_level(value)`。

**`value_map`**：当用户输入的值与 API 期望的值不同时做转换。例如枚举 `"coarse"` 映射到 API 值 `1`。

**`setter_by_value`**：有些 AutoGrid 接口是"成对"的——enable 和 disable 是两个不同的方法名。例如：

```python
setter_by_value=(
    (True,  "set_b2b_hoh_topology_enable_inlet_extension"),
    (False, "set_b2b_hoh_topology_disable_inlet_extension"),
)
```

当 `setter_by_value` 非空时，脚本调用会用 `setter_mode="no_args"`（调用时不传参数，因为方法名本身已编码了值）。

**`si_length`**：`True` 表示用户输入的数值是米，需要按 `UNITS-FACTOR` 换算为项目单位。

**`topologies`**：适用的 B2B 拓扑列表（如 `("default",)`、`("hoh",)`、`("hi",)`）。当脚本运行时，`_check_topology()` 会读取实体的当前 B2B 拓扑类型，如果不在此列表中则抛出异常。这是**运行时校验**——静态解析阶段不知道 AutoGrid 实际会用什么拓扑。

#### 3.3.2 `setter_mode` 的六种模式

`setter_mode` 决定了 AutoGrid 脚本如何调用 setter 方法。这在 `autogrid.py` 的 `_invoke_control()` 中被解释：

| setter_mode | 调用方式 | 适用场景 |
|---|---|---|
| `"value"`（默认） | `method(value)` | 标准 setter，传入单个值 |
| `"no_args"` | `method()` | 成对 enable/disable 接口，值已编码在方法名中 |
| `"tuple_args"` | `method(*value)` | 需要多个参数的 setter（如流向权重 3 元组） |
| `"row_accuracy_level"` | `method(level, current_target)` | 设置网格级别时同时传入当前目标点数 |
| `"row_accuracy_target"` | `method(current_level, target)` | 设置目标点数时同时传入当前网格级别 |
| `"interface_bool"` | 方法名被替换为 `enable_xxx`/`disable_xxx` | RSInterface 的成对布尔接口 |

（`autogrid.py:349-376`）

#### 3.3.3 注册表构建：`_add()` 和 `_direct()`

注册表通过两个辅助函数构建：

**`_add()`**（`controls.py:228-276`）：完整的通用注册函数，需要显式传入所有字段。用于：
- 带 `value_map` 的枚举控制（如 `wizard/grid_level`）
- 带 `setter_by_value` 的成对接口控制
- `scope` 与 `target_kind` 不同的特殊控制
- 声学控制等带 `not_applicable_when` 条件的控制

**`_direct()`**（`controls.py:279-316`）：简化的注册函数，自动推导 `getter` 和 `scope`。用于占绝大多数的"一对一 setter 映射"控制：

```python
def _direct(key, method, description, *, target_kind, hierarchy, ...):
    # 默认规则：
    #   getter = method.replace("set_", "get_", 1)
    #   scope  = target_kind（除非 target_kind 在内置映射表中）
```

`_direct()` 负责约 80% 的控制注册，因为它大幅减少了样板代码。

**注册表自检**（`controls.py:1480-1491`）：模块加载时会自动校验：
- 所有 key 唯一
- 所有 priority ∈ {"P0", "P1", "P2"}
- 所有 stage ∈ {8 个已知阶段}
- 所有 value_type ∈ {6 种支持类型}
- 所有规格至少有一个 setter（直接或通过 `setter_by_value`）

### 3.4 控制注册表内容概览

注册表共有 **344 个控制键**：P0 10 个、P1 59 个、P2 275 个。下面按作用域逐一说明。

#### 3.4.1 全局配置（configuration）

（`controls.py:319-396`）

| 控制键 | 类型 | 范围 | 说明 |
|---|---|---|---|
| `configuration/grid_levels` | int | 1~9 | 多重网格层级数 |
| `configuration/support_curve_control_points` | int | 2~10001 | 支撑曲线控制点数（影响几何插值精度） |
| `configuration/inlet_bulb.topology` | enum | sharp/rounded/radial | 入口 bulb 拓扑形状 |
| `configuration/outlet_bulb.topology` | enum | sharp/rounded/radial | 出口 bulb 拓扑形状 |
| `configuration/inlet_bulb.*` | int | 1~10001 | 入口 bulb 的各方向点数和光顺步数（8 个子项） |
| `configuration/outlet_bulb.*` | int | 1~10001 | 出口 bulb 的各方向点数和光顺步数（8 个子项） |
| `configuration/bypass.*` | mixed | — | bypass/nozzle 拓扑的网格、边界层和分布控制（10 个子项） |

> **Bulb（球头）是什么**：叶轮机械的入口和出口需要延伸一段"球头区域"来避免边界效应。Bulb 的形状（sharp/rounded/radial）影响延伸段的网格拓扑，但不会改变叶片的几何形状。

#### 3.4.2 RowWizard 向导控制（wizard）

（`controls.py:400-460`）

| 控制键 | 类型 | 说明 |
|---|---|---|
| `wizard/grid_level` | enum(coarse/medium/fine/user) | RowWizard 启动时的网格级别 |
| `wizard/spanwise_paths` | int(3~10001) | 展向 flow path 数量（控制径向分辨率） |
| `wizard/first_cell_width` | float, SI 长度 | 贴近壁面的首层单元宽度（控制边界层分辨率） |
| `wizard/full_matching` | bool | 是否强制全匹配拓扑 |
| `wizard/blade_tip_rounded_topology` | bool | 叶尖是否用圆钝拓扑 |
| `wizard/far_field_spanwise_paths` | int(3~10001) | 声学远场的展向 flow path 数 |
| `wizard/acoustic.*` | float/int | 声学相关的单元尺寸控制（6 个子项，仅声学行适用） |

> **RowWizard 的执行位置**：wizard 阶段先设置参数 → 然后 `_generate_row_wizards()` 调用 `row_wizard.generate()` → 之后才能进行拓扑和分布控制。这个顺序是硬性要求——如果在 `generate()` 之前设置 B2B 点数，会被 `generate()` 覆盖。

#### 3.4.3 行级控制（row）

（`controls.py:464-586`）

这是控制项最密集的区域之一，覆盖 28 个控制键：

- **网格密度**：`mesh_level`、`target_points`、`flow_path.number`
- **聚集与分布**：`clustering`、`span_interpolation`、`streamwise_weight`
- **边界层**：`enforce_blade_wall_cell_width`
- **优化**：`optimization.steps`、`optimization.gap_steps`、`optimization.skewness`、`optimization.orthogonality`、`optimization.wake` 等
- **Flow path 精细控制**：`flow_path.hub_clustering`、`flow_path.constant_cells` 等
- **gap 插值**：`gap.hub_interpolation`、`gap.shroud_interpolation`

**特别说明 `mesh_level` 和 `target_points` 的联动**：

这两个控制都映射到 `set_coarse_grid_level`（一个方法），但通过不同的 `setter_mode` 区分：
- `row/mesh_level` → `setter_mode="row_accuracy_level"` → 调用 `method(level, current_target)`
- `row/target_points` → `setter_mode="row_accuracy_target"` → 调用 `method(current_level, target)`

这是 AutoGrid 的一个设计 quirks——同一个 setter 方法接受两个参数，分别用于设置级别和目标点数。我们的处理方式是保留当前未变化的那个参数。

#### 3.4.4 叶片 B2B 拓扑与点分布（blade/b2b.*）

（`controls.py:588-972`）

B2B（Blade-to-Blade）网格是 AutoGrid 的核心——先在二维叶片截面上生成网格，再沿径向堆叠为三维。项目支持三种 B2B 拓扑：

**Default 拓扑**（约 35 个控制键）：最常用的拓扑类型。控制包括：
- 基础：`topology`（类型）、`type`（走向）、`periodicity`（周期匹配）
- 点数（14 个子项）：入口/出口周向、入口/出口流向、吸力面/压力面、边界层、leading_edge_index、trailing_edge_index
- 边界层（9 个子项）：首层宽度、边界层厚度、增长率、膨胀率
- 高级（14 个子项）：喉部、出口角、尾迹、交线、弦向控制点

**HOH 拓扑**（约 30 个控制键）：H-O-H 三块拓扑，适用于有间隙的叶片。控制包括：
- 入口/出口延伸块（类型和位置）
- 各块的周向点数和流向点数
- 前/尾缘分布控制类型（none/absolute_distance/relative_distance/cell_length）
- 边界层和尾迹聚类

**H&I 拓扑**（约 18 个控制键）：H&I 拓扑，适用于更复杂的几何。控制结构与 HOH 类似但点数命名不同。

**edge_treatment**（7 个控制键）：前缘/尾缘的 blunt/sharp/rounded/blend 处理方式。

#### 3.4.5 间隙、半间隙与圆角（gap / partial-gap / fillet）

（`controls.py:974-1050`）

- **gap**（4 个控制键）：拓扑（HO/O/O2H）、展向点数、聚集、常值单元数
- **partial-gap**（7 个控制键）：前/尾缘点数、流向/展向单元宽度、聚集松弛、展向点数
- **fillet**（8 个控制键）：聚集（含前缘、尾缘、通道高度三种聚集模式）、常值单元、展向点数、蝶形拓扑

> 所有这些控制选择器的层级都是 `row:xxx/blade:xxx/gap:shroud`（或 hub）。`geomturbo.py` 解析出的 `gap_sides` / `partial_gap_sides` / `fillet_sides` 正是在这里用于匹配。

#### 3.4.6 行接口控制（interface）

（`controls.py:1054-1122`）

接口（RSInterface）控制的是两个相邻叶排之间的交界面网格。选择器为 `row:xxx/interface:inlet`（或 `outlet`、`outlet2`）。

控制项包括：流向点数、B2B 控制开关、几何固定、流向单元宽度、聚集松弛、相对位置、形状（linear/curvilinear/user/default）、参考系（relative/absolute）、Z/R 常值形状。

**特殊处理**：`b2b_control` 和 `geometry_fixed` 使用 `setter_mode="interface_bool"`。AutoGrid 的 `RSInterface` 类为这两个属性提供了成对的 enable/disable 方法（而不是 setter），所以脚本中会根据布尔值将方法名替换为 `enable_b2b_control` / `disable_b2b_control`。

#### 3.4.7 已有技术效果（existing-effect）与其它

（`controls.py:1124-1478`）

这部分控制已有实体（孔、端壁、snubber、停滞点等）的网格参数。这些实体不是由本项目创建的——它们已经在 `.geomTurbo` 或 `.trb` 模板中定义好了。我们的脚本只修改它们的**网格离散参数**，不创建、删除或修改物理几何。

涵盖的目标类型：
- `endwall`：端壁的展向点数、连接层数、优化步数（4 个控制键）
- `snubber`：凸肩的聚集、增长率、控制距离、各方向点数（10 个控制键）
- `blade-sheet`：叶片面的前/尾缘点数（2 个控制键）
- `stagnation-point`：停滞点的分布控制（6 个控制键）
- `holes-line`：已存在孔列的边界层、流向、展向点数等（14 个控制键，按 blade 层级）
- `endwall-holes-line`：端壁孔列的点数、聚集和方位控制（17 个控制键，按 endwall 层级）
- `pin-fins-line`：针肋的点数、尾迹长度和优化等（14 个控制键）
- `basin-hole`：basin hole 的优化和分布（5 个控制键）
- `lete-wizard`：已存在前缘/尾缘层的点分布和光顺（11 个控制键）
- `solid-body`：固体域的方位点数、松弛和网格保持（6 个控制键）
- `existing-effect`（ZR 顶层）：最大增长率、聚类、光顺步数等全局优化参数（14 个控制键）

### 3.5 选择器系统

#### 3.5.1 `EntitySelector`：实体引用的三种模式

```python
@dataclass(frozen=True)
class EntitySelector:
    kind: str           # 实体类型，如 "row"、"blade"、"gap"
    mode: str           # "wildcard" | "index" | "name"
    value: str | int    # 通配符为 "*"，索引为整数，名称为字符串

    @property
    def specificity(self) -> int:
        return 0 if self.mode == "wildcard" else 1

    def canonical(self) -> str:
        # 返回规范化文本，如 "row:*", "blade:#2", "gap:shroud"
```

（`controls.py:86-113`）

三种模式的区别和使用场景：

| mode | 语法 | 示例 | `specificity` | 说明 |
|---|---|---|---|---|
| `wildcard` | `kind:*` | `row:*`, `blade:*` | 0 | 匹配该类型的所有实体 |
| `index` | `kind:#N` | `row:#1`, `blade:#2` | 1 | 按从 1 开始的位置索引 |
| `name` | `kind:name` | `blade:Main Blade`, `gap:shroud` | 1 | 按区分大小写的实体名精确匹配 |

#### 3.5.2 Wildcard 展开与冲突解决

`resolve_control_requests()`（`controls.py:1861-1921`）是控制系统中最复杂的函数。它做的事：

1. **枚举候选实体**（`_candidate_targets`）：根据 `ControlSpec.hierarchy` 和 `GeomTurboSummary` 的结构，列出所有可能的目标实体组合

2. **通配符匹配**（`_target_matches`）：将每个 `ControlRequest` 的选择器与候选实体逐一比对：
   - `wildcard` 匹配任何实体
   - `index` 匹配索引相同的实体
   - `name` 匹配名称相同的实体

3. **冲突解决**：如果多个 `ControlRequest`（来自不同的 `--set` 表达式，但相同的 key 和目标实体）指向同一个控制：
   - 优先选 `specificity` 最高（即选择器最精确）的
   - 如果有多个同等精确度的选择器，检查它们请求的值是否相同
   - 值不同 → 报错（"等优先级冲突"）
   - 值相同 → 任选其一（按选择器路径字母序）

4. **排序**：按 `stage` → `target_path` → `key` 排序，确保执行顺序与阶段顺序一致

5. **构建 `ResolvedControl`**：完成 SI 长度换算、值映射、setter 选择

**关键代码**（`controls.py:1876-1885`）：

```python
for (key, target), candidates in candidates_by_identity.items():
    max_specificity = max(candidate.specificity for candidate in candidates)
    winners = [candidate for candidate in candidates
               if candidate.specificity == max_specificity]
    values = {_hashable_value(candidate.value) for candidate in winners}
    if len(values) > 1:
        raise ControlValidationError(
            f"控制 {key} 对同一实体存在等优先级冲突：{paths}"
        )
```

#### 3.5.3 `_candidate_targets()`：枚举所有可能的目标

（`controls.py:1977-2030`）

这个函数根据 `ControlSpec.hierarchy` 的具体层级，递归地枚举所有可能的目标实体组合。对于 `geomturbo.py` 无法计数的实体类型（如已有孔、snubber、停滞点），使用 `_indexed_unknown_targets()` 创建 1~99 的静态候选——这些候选在 AutoGrid 脚本运行时由正式 accessor API 做严格确认。

### 3.6 解析与校验管线

从用户输入到 `ResolvedControl` 的完整管线：

```text
parse_control_assignment(raw)           ← 解析一条 "row:*/mesh_level=fine"
  ├── 拆分 lhs / rhs（按 = 分割）
  ├── 从 lhs 提取选择器路径和局部键
  ├── parse_control_value(raw_value, spec)  ← 类型检查 + 范围校验
  ├── _parse_selector(part)                 ← 逐个解析选择器
  └── 构建 ControlRequest

parse_control_assignments(assignments)   ← 批量解析
  ├── 调用 parse_control_assignment 解析每条
  └── 检查重复定义（相同 key + 相同 selectors）

validate_wizard_compatibility(requests)  ← 检查 --no-row-wizard 是否与 wizard 控制冲突

resolve_control_requests(requests, geometry)  ← 展开 wildcard + 绑定实体
  ├── _candidate_targets(spec, geometry)      ← 枚举候选
  ├── _target_matches(selectors, target)      ← 通配符匹配
  ├── 冲突解决（最高 specificity 优先）
  ├── convert_si_length(value, units_factor)  ← SI 长度换算
  ├── spec.map_api_value(project_value)       ← 值映射
  ├── spec.setter_for_value(request.value)    ← 选择 setter
  └── 构建 ResolvedControl（按 stage 排序）
```

### 3.7 Setter 审计系统

（`controls.py:1494-1734`）

审计系统解决了一个关键问题：AutoGrid 17.1 有 721 个 setter，我们如何确信没有遗漏或错误？

**正向审计（`audit_autogrid_source`）**：读取 `Autogrid.py` 源码，解析出所有类的所有 `set_*` / `a5_set_*` 方法，对每个方法调用 `audit_setter()` 判定其状态：

```text
mapped   (361 个) → 该方法已映射到至少一个 ControlSpec
excluded (360 个) → 该方法有明确的排除理由
unaudited (0 个) → 不允许存在——发现即意味着审计失败
```

**反向审计（`audit_control_bindings`）**：从注册表出发，核验每个 ControlSpec 引用的 setter/getter 方法名确实存在于 `Autogrid.py` 中。

```text
available  (701 个) → 方法在目标类中找到
synthetic  (  2 个) → __bool_ 前缀的 interface 方法（由脚本动态处理）
missing    (  0 个) → 不允许存在
```

**排除规则**（`EXCLUDED_SETTERS` 和 `AUDIT_EXCLUSION_RULES`）：

约 360 个 setter 被排除，分为几类：
- **物理几何/实体修改**：单位（units）、周期数（periodicity）、转速（rotation_speed）、gap 物理宽度、fillet 半径等
- **实体命名**：`set_name`
- **几何变形/修复**：非轴对称端壁、stiching、data_reduction
- **创建/删除实体**：`add_`、`delete_`、`create_`、`copy_` 前缀
- **仅显示/demo**：`display`、`graphics`、`camera`、`color`、`demo`
- **重复别名**：已映射到规范键的旧接口

（`controls.py:1532-1654`）

### 3.8 查询接口

| 函数 | 位置 | 作用 |
|---|---|---|
| `list_control_specs(priority)` | `1924-1932` | 按 P0/P1/P2/ALL 列出控制规格 |
| `describe_control(key)` | `1935-1941` | 返回单个控制规格的详细信息 |

这两个函数被 `mesh.py` 的 `--list-controls` 和 `--describe-control` 使用——它们不需要几何文件。

---

## 4. AutoGrid 脚本执行（`autogrid.py`）

文件位置：`src/autogrid.py`（642 行）

### 4.1 概览

`autogrid.py` 有三个核心职责：

1. **生成脚本**（`render_autogrid_script`）：将 `ResolvedControl` 列表渲染为一个完整的、可由 IGG 直接执行的 Python 脚本文件
2. **执行脚本**（`run_autogrid_init`）：启动 IGG 进程执行脚本，捕获输出
3. **解析结果**（`parse_control_results` + `merge_control_results`）：从 IGG 输出中提取每个控制项的 setter 调用状态和 getter 回读值

### 4.2 `AutoGridRun` 数据类

```python
@dataclass(frozen=True)
class AutoGridRun:
    command: list[str]                   # 实际执行的命令
    returncode: int | None               # 退出码（None 表示 dry-run）
    run_dir: str                         # 运行目录路径
    script: str                          # 生成的脚本文件路径
    outputs: dict[str, str]              # 网格产物路径，如 {"igg": ".../mesh.igg", ...}
    control_results: list[dict]           # 每个控制的执行结果（含 status/readback/error）
    error: str | None                    # 运行错误信息（如有）
    post_control_results: list[dict]      # 3D 网格生成后的观察事件
    completion_event: dict | None         # AGMESH_COMPLETION 完成事件
    protocol_errors: list[dict]           # 事件协议错误（code + message）
    controls_verification: dict | None    # 每项控制 VERIFIED/MISMATCH/READBACK_ERROR/UNVERIFIABLE
    manifest_outputs: list[dict]          # 本次新产物清单（相对路径/大小/sha256）
```

（`autogrid.py:21-36`）

### 4.3 `render_autogrid_script()`：生成 AutoGrid 脚本

（`autogrid.py:139-469`）

这个函数是整个项目中**最长的单行函数调用**（模板字符串从第 150 行到第 469 行，共约 320 行）。它使用 Python 的 f-string 将控制计划嵌入到脚本模板中。

**生成的脚本结构**：

```python
# -*- coding: utf-8 -*-
# 由 src/mesh.py 自动生成，目标版本仅限 NUMECA AutoGrid 17.1。

import os, json

GEOMTURBO_FILE = r"..."       # 几何文件路径
OUTPUT_PREFIX = r"..."        # 输出文件前缀
USE_ROW_WIZARD = True/False   # 是否使用 RowWizard
CONTROL_PLAN = [...]          # 解析后的控制计划（JSON 嵌入）

# === 辅助函数族 ===
def _require_global(name): ...         # 验证 AutoGrid 全局函数存在
def _entity_name(entity): ...          # 安全获取实体名称
def _safe_text(value): ...             # Py2/3 兼容的 Unicode 安全转换
def _require_entity(entity, label): ... # 验证实体非空（含 IGG 怪癖 0 值）
def _part(control, kind): ...          # 从控制计划中提取目标部件
def _row_target(control): ...          # 定位并验证叶排实体
def _blade_target(control, row): ...   # 定位并验证叶片实体
def _resolve_target(control): ...      # ✦ 核心：解析控制目标实体（60+行 if/elif 链）

# === 控制应用管线 ===
def _check_topology(control, target): ...  # 拓扑适用性检查
def _control_method(control, target): ...  # 获取 setter 方法
def _invoke_control(control, target): ...  # 调用 setter（6种模式）
def _readback(control, target): ...        # 调用 getter 回读
def _emit_control(control, status, ...): ... # 向 stdout 输出控制结果 JSON 标记
def _apply_control(control): ...           # 完整应用一条控制（resolve→check→invoke→readback→emit）
def _apply_stage(stage): ...               # 按阶段批量应用
def _generate_row_wizards(): ...           # 逐排执行 RowWizard.generate()

# === 主流程 ===
a5_new_project(1)
a5_init_new_project_from_a_geomTurbo_file(GEOMTURBO_FILE)
ROW_COUNT = a5_get_row_number()

_apply_stage("configuration")
_apply_stage("wizard")
_generate_row_wizards()
_apply_stage("topology")
_apply_stage("distribution")
_apply_stage("boundary_layer")
_apply_stage("optimization")
_apply_stage("interface")
_apply_stage("existing_effect")

a5_save_project(TRB_OUT)
a5_generate_flow_paths()
a5_generate_b2b()
a5_generate_3d()
a5_save_project(TRB_OUT)
a5_save_mesh(IGG_OUT)
a5_export_CGNS_project(CGNS_OUT)
```

**关键辅助函数详解**：

**`_resolve_target(control)`**（`autogrid.py:234-321`）：根据 `control["target_kind"]` 分发到具体的实体定位逻辑。这是一个 60 行的 if/elif 链，每种 `target_kind` 有特定的实体获取方式：

```python
if kind == "configuration":
    return None                          # 全局控制，无目标实体
elif kind == "existing-effect":
    # 通过 technologicalEffectZR(index) 获取
elif kind == "row":
    return _row_target(control)
elif kind == "wizard":
    # row_entity.row_wizard()
elif kind == "interface":
    # row_entity.inlet() / row_entity.outlet() / row_entity.outlet2()
elif kind == "endwall":
    # row_entity.hub_end_wall() / row_entity.shroud_end_wall()
elif kind == "snubber":
    # row_entity.snubber(index)
elif kind == "endwall-holes-line":
    # endwall.holes_line(index)
# ... 共 17 种 target_kind
```

**`_safe_text(value)`**（`autogrid.py:178-193`）：处理 Python 2/3 兼容性。AutoGrid 17.1 内置的 Python 环境是 Python 2——这意味着 `str` 和 `unicode` 是两个不同类型，中文异常消息需要特殊处理。这个函数确保了无论异常消息是什么编码，都能被安全转换为可 JSON 序列化的格式。

**`_require_entity(entity, label)`**（`autogrid.py:196-199`）：检查 `entity is None or entity == 0`。注意 `entity == 0` 这个条件——这是 AutoGrid/IGG 的一个已知怪癖：某些 accessor 方法在实体不存在时返回整数 `0` 而不是 `None`。如果不处理这个情况，后续的 `getattr(entity, "set_xxx")` 调用会得到 `int.set_xxx`，产生令人困惑的错误。

#### 4.3.1 事件标记协议

脚本通过 stdout 输出事件。控制应用与 3D 网格生成后的观察分别使用两个前缀标记，每条事件是一个 JSON 行：

```text
AGMESH_CONTROL_RESULT:{"id":"C0001","key":"row/mesh_level",...,"run_id":"...","stage":"apply","status":"applied"}
AGMESH_CONTROL_POST_RESULT:{...,"stage":"post_generation",...}
AGMESH_COMPLETION:{"stage":"final","run_id":"...","status":"completed"}
```

所有事件统一携带 `run_id` 与 `stage`。脚本末尾无条件输出 `AGMESH_COMPLETION` 完成事件——即使没有请求任何控制，也必须收到本次脚本的最终完成事件。

宿主解析对协议错误零容忍：损坏 JSON、缺失/重复/未知 ID、key/目标/阶段/run_id 不匹配、缺失应用/观察/完成事件全部记录为 `protocol_errors`（code + message）并使运行 `FAILED`（returncode 1），不再用字典覆盖或静默跳过。

输出的 JSON 字段（`_emit_control`）：

```json
{
    "id": "C0001",
    "key": "row/mesh_level",
    "target_path": "row:#1",
    "requested": "fine",
    "project_value": "fine",
    "status": "applied",     // "applied" | "failed"
    "readback": 4,           // getter 返回值；无 getter 时为 null
    "error": null            // 失败时的异常信息
}
```

每项计划控制额外生成验证结论（`verify_control_readbacks`，写入 `run_summary.json` 的 `controls.verification`），四枚举之一：

- `VERIFIED`：按注册表规则比较回读与请求值一致——整数、布尔、枚举归一化后精确比较；浮点用 rel_tol=1e-7、abs_tol=1e-10；SI 长度按 units_factor 换算到 SI 后比较；
- `MISMATCH`：比较结果不一致；
- `READBACK_ERROR`：getter 报错；
- `UNVERIFIABLE`：setter 失败、未收到应用事件、无 getter，或回读无法按已确认规则解释（含厂商重采样与特殊枚举）。

setter 前的回读失败仅作诊断；getter 报错或回读不匹配时运行仍可 `SUCCEEDED`，验证异常单独标记，允许继续创建分支。

### 4.4 `run_autogrid_init()`：进程管理

（`autogrid.py:39-136`）

这个函数负责：

1. 通过 `acquire_run_directory()` 取得运行目录所有权，然后拷贝几何文件
2. 调用 `render_autogrid_script()` 生成脚本
3. 解析 IGG 可执行文件路径（`resolve_igg`）
4. 如果是 `dry_run`：直接返回 `AutoGridRun`（不执行 IGG）
5. 否则：通过 `subprocess.run` 启动 IGG

`acquire_run_directory()`（`autogrid.py`）在复制输入、写脚本之前以排他创建（`O_CREAT|O_EXCL`）方式写入 `.mesh_run.lock`（含 run_id 与创建时间）取得目录所有权：已存在占用标记、并发创建失败、目录已含既有文件（`worker.stdout.log`/`worker.stderr.log` 白名单除外）均抛出 `RunDirectoryError`，由 CLI 转为退出码 2 并保留既有文件。标记保留不删除，已执行或 dry-run 使用过的目录均不能再次执行；占用时快照的既有文件列表用作本次产物登记的对照。

**IGG 的调用方式**：

```python
command = [igg_executable, "-autogrid5", "-batch", "-script", script_path]
subprocess.run(command, cwd=run_path, text=False, capture_output=True, timeout=...)
```

注意 `text=False`——子进程的输出以二进制捕获，而非文本。这是因为：
- AutoGrid 内部 Python 2 的中文输出使用 GBK/GB18030 编码
- 如果让 Python 3 以默认文本模式捕获，Windows 系统编码（GBK）可能无法解码某些字符
- 改为手动按 UTF-8 → GB18030 的顺序尝试解码

**进程输出的解码**（`_completed_text`，`autogrid.py:538-550`）：

```python
if isinstance(value, bytes):
    for encoding in ("utf-8", "gb18030"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace")
```

**错误处理**：

```python
try:
    completed = subprocess.run(...)
except subprocess.TimeoutExpired:
    # 超时：从异常中提取已产生的 stdout/stderr
    process_returncode = 1
    runner_error = f"AutoGrid 超时（{timeout_seconds} 秒）"
except OSError:
    # IGG 可执行文件不存在或无法启动
    process_returncode = 1
    runner_error = f"无法启动 IGG：{exc}"
```

**返回码覆盖逻辑**（`autogrid.py:119-127`）：

```python
effective_returncode = process_returncode
if effective_returncode == 0 and (failed_control is not None or script_error is not None):
    effective_returncode = 1
```

即使 IGG 进程返回 0，如果控制应用失败或脚本有 traceback，也统一改为 1。这确保了"IGG 不报错但控制未生效"的情况不会被误判为成功。

### 4.5 输出收集与结果合并

**`collect_outputs()`**（`autogrid.py:562-581`）：扫描运行目录，收集所有已生成的网格产物文件。按后缀名分类：

| key | 后缀 | 内容 |
|---|---|---|
| `igg` | `.igg` | IGG 网格文件 |
| `cgns` | `.cgns` | CGNS 格式网格（跨软件标准格式） |
| `trb` | `.trb` | AutoGrid 项目文件 |
| `bcs` | `.bcs` | 边界条件文件 |
| `info` | `.info` | 项目信息文件 |
| `geom` | `.geom` | 几何快照 |
| `geomturbo` | `.geomTurbo` | 原始几何文件的副本 |
| `quality_report` | `.qualityReport` | AutoGrid 质量报告（文本格式） |

**`merge_control_results()` / `merge_post_control_results()`**（`autogrid.py`）：将 `ResolvedControl` 列表（计划）与从 stdout 解析出的 `events`（实际结果）合并：

- 每个 `ResolvedControl` 按 `control_id`（如 `"C0001"`）匹配
- 匹配到 event → 更新 status / readback / error，并校验 key、目标与计划一致
- 未匹配到 event → 记录协议错误（`missing_apply_event` / `missing_post_generation_event`）使运行 `FAILED`，控制状态标记为 `"not_applied"`；重复 ID、未知 ID、key/目标/阶段不匹配同样记录为协议错误

### 4.6 `resolve_igg()`：IGG 路径解析

（`autogrid.py:584-641`）

按以下优先级查找 IGG 可执行文件：

1. 直接路径（含路径分隔符）→ 验证存在
2. `PATH` 环境变量（`shutil.which`）
3. 环境变量：`NUMECA_ROOT`、`NUMECA_HOME`、`FINE_ROOT`、`FINE_HOME`、`FINE171_ROOT`
4. 常见安装目录：
   - Windows：`C:\ProgramData\NUMECA\fine171\bin64\iggx86_64.exe`
   - Linux：`/opt/numeca/bin/igg`、`/usr/local/numeca/`、`/opt/cadence/`

`_candidate_roots()` 还会在找到的根目录下自动搜索包含 "fine"、"numeca"、"fidelity"、"autogrid" 的子目录。

---

## 5. 质量评估（`quality.py`）

文件位置：`src/quality.py`（709 行）

### 5.1 概览

`quality.py` 负责解析 AutoGrid 生成的质量报告数据，并根据预设的硬性阈值判定网格质量是否合格。

### 5.2 硬门槛（`HARD_LIMITS`）

```python
HARD_LIMITS = {
    "negative_cells": 0,                  # 负体积单元 = 0（一个都不能有）
    "min_grid_levels": 3,                 # 至少 3 层多重网格
    "min_skewness_angle": 15.0,           # 最小偏斜角 ≥ 15°
    "max_expansion_ratio": 3.0,           # 最大增长率 ≤ 3
    "max_spanwise_deviation": 40.0,       # 展向角偏差 ≤ 40°（180° - min spanwise skewness）
    "max_spanwise_expansion_ratio": 2.0,  # 最大展向增长率 ≤ 2
    "max_aspect_ratio": 15000.0,          # 最大长宽比 ≤ 15000
}
```

（`quality.py:15-23`）

这些都是叶轮机械 CFD 领域的经验准则。任何一个不满足 → 质量判定为 `FAIL`。

### 5.3 六类质量指标（`CRITERIA`）

```python
CRITERIA = (
    "skewness_angle",             # 偏斜角：网格单元偏离正六面体的角度
    "spanwise_skewness_angle",    # 展向偏斜角：径向方向的偏斜
    "spanwise_expansion_ratio",   # 展向增长率：径向相邻单元大小比
    "aspect_ratio",               # 长宽比：单元最长边/最短边
    "expansion_ratio",            # 增长率：相邻单元大小比
    "wall_distance",              # 壁面距离：首层单元到壁面的距离
)
```

（`quality.py:25-32`）

每个指标的报告值中，**关注方向**不同。`CRITICAL_EXTREME` 定义了每个指标的"更差方向"：

```python
CRITICAL_EXTREME = {
    "skewness_angle": "minimum",              # 越小越差
    "spanwise_skewness_angle": "minimum",     # 越小越差
    "spanwise_expansion_ratio": "maximum",    # 越大越差
    "aspect_ratio": "maximum",                # 越大越差
    "expansion_ratio": "maximum",             # 越大越差
    "wall_distance": "maximum",               # 越大越差
}
```

（`quality.py:34-41`）

> **为什么偏斜角看最小值**：在 AutoGrid 中，偏斜角定义为 90° 减去实际偏角。所以 90° 是完美（无偏斜），0° 是最差（网格完全塌陷）。最小值越小 → 越差。

### 5.4 数据源优先级

AutoGrid 生成两种质量数据源，`summarize_quality()` 按优先级选择：

1. **`.qualityReport`**（优先）：AutoGrid 生成的文本报告，格式规范，信息完整
2. **CGNS 内嵌数据**（降级）：CGNS 文件中的 `NIGridQuality` 数据块，格式不完整且数据块标签可能已分离

（`quality.py:67-108`）

### 5.5 `.qualityReport` 解析（`parse_quality_report`）

（`quality.py:111-298`，约 190 行）

`.qualityReport` 是一个类似 INI 的文本文件。`parse_quality_report()` 使用**基于状态机的逐行扫描**来解析它。

**解析的三个阶段**：

```text
扫描开始
  │
  ├── [阶段 1] PROJECT_INFO 块内
  │   ├── "AUTOGRID version X.X"  → metadata.autogrid_version
  │   ├── "PROJECT : xxx"         → project.name
  │   ├── "ROW NAME : xxx"        → 新 project_row
  │   ├── "NUMBER OF POINTS N"    → project_row.number_of_points
  │   └── ... B2B TOPOLOGY ...    → project_row.b2b_topology
  │
  ├── [阶段 2] GRID QUALITY REPORT 之后
  │   ├── "Entire Mesh Quality"   → 新 entity (scope="entire_mesh")
  │   ├── "Row X Quality"         → 新 entity (scope="row")
  │   ├── "Minimal Skewness Angle : X"   → criterion.minimum
  │   ├── "Maximal Expansion Ratio : X"   → criterion.maximum
  │   ├── "Average Aspect Ratio : X"      → criterion.average
  │   ├── "Min Location Skewness Angle : Block I,J,K: x,y,z"  → criterion.critical_location
  │   └── ...
  │
  └── [结束] _finalize_entity_criteria + _flatten_entity_metrics
```

**状态管理**：

- `in_project_info`：是否在 `NI_BEGIN PROJECT_INFO` 内
- `in_quality_report`：是否在 `GRID QUALITY REPORT` 行之后
- `current_project_row`：当前正在构建的叶排项目信息
- `current_entity`：当前正在填充的质量实体

**关键正则**：

```python
# 版本行
re.search(r"AUTOGRID\s+version\s+([^\s*]+)", stripped, flags=re.IGNORECASE)

# 统计值行
re.match(r"(Minimal|Maximum|Maximal|Average|Minimum)\s+(.+?)\s*:\s*(NUMBER)\s*$", line)

# 最差位置行
re.match(r"(Max|Min)\s+Location\s+(.+?)\s*:\s*(.+?)\s+I\s*,\s*J\s*,\s*K\s*:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*$", line)
```

注意这几个正则处理了 AutoGrid 报告的命名不一致：`Minimal` vs `Minimum`、`Maximal` vs `Maximum`——多个别名映射到同一个统计量。

### 5.6 CGNS 内嵌质量解析（`parse_embedded_cgns_quality`）

（`quality.py:301-352`）

CGNS（CFD General Notation System）是一种跨软件的 CFD 数据交换格式。AutoGrid 在导出 CGNS 文件时，会将质量数据嵌入为二进制块。

`parse_embedded_cgns_quality()` 的解析策略：
1. 以 Latin-1 编码读取整个文件
2. 搜索 `NIGridQuality` 标记
3. 对六类指标分别提取对应的数据块
4. 从数据块中解析最小值、最大值、平均值和最差位置

CGNS 解析的约束：
- 不如 `.qualityReport` 完整——没有项目元数据（版本、日期、耗时）
- 壁面距离的单位无法直接确定（依赖附近的 `geomTurbo` 文件来推断）
- 解析改为分块流式扫描（1 MiB 块、标记尾重叠、深度跟踪 NI_BEGIN/NI_END、滚动窗口计数），正确处理跨块标记，不整文件载入内存；解析结果与旧实现逐项一致

### 5.7 质量判定（`evaluate_quality`）

（`quality.py`）

判定采用"先校验、后比较"两步顺序：

1. **必需字段检查**：8 个必需字段（`negative_cells`、`grid_levels`、偏斜角、增长率、长宽比等）任一缺失或为 `None` → 返回 `UNKNOWN`，原因含字段路径（如 `quality.metrics.grid_levels: 缺失`）。
2. **领域校验**（`_validation_problems`）：对所有已提供的统计值做合法性检查——计数必须为严格整数（点数/层级正整数、负体积单元非负、拒绝布尔与小数文本）、实数值有限、角度在 `[0, 180]`、比例为正、壁面距离非负。任何非法值 → `UNKNOWN`、`accepted = false`，原因含字段路径（如 `quality.metrics.min_skewness_angle: 非有限数值`）。
3. **硬门槛比较**：校验通过后依次检查 7 个硬门槛，阈值与等号行为不变：

```python
if metrics["negative_cells"] != 0:
    reasons.append("Negative cells detected")
if metrics["grid_levels"] < 3:
    reasons.append("Insufficient grid levels")
if metrics["min_skewness_angle"] < 15.0:
    reasons.append("Minimum skewness angle below hard limit")
if metrics["max_expansion_ratio"] > 3.0:
    reasons.append("Maximum expansion ratio above hard limit")
if 180.0 - metrics["min_spanwise_skewness_angle"] > 40.0:
    reasons.append("Spanwise angular deviation above hard limit")
if metrics["max_spanwise_expansion_ratio"] > 2.0:
    reasons.append("Spanwise expansion ratio above hard limit")
if metrics["max_aspect_ratio"] > 15000.0:
    reasons.append("Maximum aspect ratio above hard limit")

return QualityEvaluation("FAIL" if reasons else "PASS", not reasons, reasons)
```

校验先行语义同时作用于报告解析、CGNS 降级解析与直接调用三个入口。非有限值在对外结构中由 `_sanitize_non_finite()` 统一转换为 `null`，错误说明保留在 `quality_validation`、原始报告保留在 `raw`。`summarize_quality()` 额外返回 `quality_validation` 三态摘要：`VALID`（校验通过，含 PASS 与 FAIL）、`INVALID`（缺失或非法，逐项 `{field, problem}`）、`UNKNOWN`（无质量源）。

`QualityEvaluation` 是一个简单的数据类：

```python
@dataclass(frozen=True)
class QualityEvaluation:
    status: str        # "PASS" | "FAIL" | "UNKNOWN"
    accepted: bool     # True 仅当 status == "PASS"
    reasons: list[str] # 失败/未知的原因列表
```

### 5.8 内部辅助函数

| 函数 | 位置 | 作用 |
|---|---|---|
| `_parse_statistic_line(line)` | `439-454` | 解析 "Minimal Skewness Angle : 32.5" 格式的统计行 |
| `_parse_location_line(line)` | `457-477` | 解析 "Min Location ... I,J,K: 32,5,1" 格式的位置行 |
| `_criterion_name(label)` | `480-492` | 将报告标签映射为内部名称（如 "Skewness Angle" → "skewness_angle"） |
| `_finalize_entity_criteria(entity)` | `495-519` | 补齐缺失准则、设置极端方向、换算壁面距离单位 |
| `_derive_entire_mesh_locations(entities)` | `522-548` | 从各叶排统计中推导全网格级别的极值位置 |
| `_flatten_entity_metrics(entity)` | `551-574` | 将分层实体结构压平为旧版兼容的扁平指标字典 |
| `_duration_seconds(value)` | `608-615` | 将 "HH:MM:SS" 格式的生成耗时转换为秒数 |
| `_discover_project_units(source_path)` | `618-631` | 从附近的 `.geomTurbo` 文件推断长度单位 |
| `_quality_blocks(text, name)` | `634-638` | 从 CGNS 文本中提取命名质量数据块 |
| `_embedded_location(blocks, extreme)` | `641-666` | 从内嵌数据块中提取最差位置 |
| `_add_wall_uniformity(metrics)` | `690-696` | 计算壁面均匀性指标（max/min wall distance） |

---

## 6. CLI 入口（`mesh.py`）

文件位置：`src/mesh.py`（468 行）

### 6.1 `main()` 函数

（`mesh.py:26-181`，约 155 行）

`main()` 是程序的唯一入口。它的逻辑分为三个分支：

```text
main()
  │
  ├── [分支 A] --list-controls / --describe-control
  │   → 查询控制注册表，不读几何，不启动 IGG
  │   → 返回 0（成功）或 2（控制键不存在）
  │
  ├── [分支 B] 未提供 .geomTurbo 文件
  │   → 返回 2（必须提供几何文件或查询命令）
  │
  └── [分支 C] 完整的网格生成流程
      → 解析几何 → 构建控制 → 解析/校验/展开 → 执行 → 质量评估 → 写摘要和报告
```

### 6.2 命令行参数一览

（`mesh.py:29-66`）

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `geomturbo` | 位置参数 | 无 | 输入 `.geomTurbo` 文件路径（查询时可省略） |
| `--out` | str | 自动生成 | 产物目录；默认 `runs/<名称>_<时间戳>_<uuid>`，显式目录仅允许不存在、为空或仅含 Worker 预写日志 |
| `--run-id` | str | 自动生成 | 本次运行唯一标识；未传时生成 UUID4 十六进制字符串 |
| `--igg` | str | 自动查找 | IGG 可执行文件路径 |
| `--no-row-wizard` | flag | False | 跳过 RowWizard 自动向导 |
| `--dry-run` | flag | False | 仅生成脚本不执行 IGG |
| `--timeout` | int | None | AutoGrid 超时秒数 |
| `--mesh-level` | enum | None | 所有行的网格级别 |
| `--target-points` | int | None | 暂时停用，静态报错；通用 row/target_points 同样拒绝 |
| `--first-cell-width` | float | None | 所有行首层宽度（米） |
| `--spanwise-paths` | int | None | 所有行展向 flow paths 数 |
| `--gap-points` | int | None | 所有已有 gap 的展向点数 |
| `--optimization-steps` | int | None | 所有行普通优化步数 |
| `--gap-optimization-steps` | int | None | 所有行 gap 优化步数 |
| `--set` | str (可重复) | 无 | 通过注册表键设置控制 |
| `--list-controls` | P0/P1/P2/ALL | 无 | 列出控制目录 |
| `--describe-control` | str | 无 | 详细说明一个控制键 |

### 6.3 `_build_control_requests()`

（`mesh.py:184-219`）

将快捷参数（`--mesh-level`、`--first-cell-width` 等）转换为等价的 `--set` 表达式：

```python
(args.mesh_level,  f"row:*/wizard/grid_level={args.mesh_level}"),
(args.first_cell_width, f"row:*/wizard/first_cell_width={args.first_cell_width}"),
# ...
```

然后与用户直接传入的 `--set` 表达式合并，统一交给 `parse_control_assignments()` 处理。

### 6.4 运行目录命名与独占占用

（`mesh.py`）

```python
stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
return Path("runs") / f"{geomturbo_path.stem}_{stamp}_{uuid.uuid4().hex}"
```

示例：`geometries/Rotor37.geomTurbo` → `runs/Rotor37_20260723_143052_a1b2c3...`

目录所有权规则（见 4.4 节 `acquire_run_directory`）：执行前以排他创建 `.mesh_run.lock` 取得所有权；显式 `--out` 只允许不存在、为空或仅含 `worker.stdout.log`/`worker.stderr.log` 白名单文件（兼容 Worker 预先写入的两份日志）。历史产物目录与并发占用均明确拒绝，作为输入错误返回退出码 2，保留已有文件。标记保留，已执行或 dry-run 使用过的目录均不能再次执行。`run_summary.json` 以临时文件 + `os.replace` 原子写入；本次完成阶段与产物清单（相对路径、大小、SHA-256）记录在摘要的 `manifest` 中。

### 6.5 `.env` 文件解析

（`mesh.py:235-250`）

```python
def _load_env_file(path):
    # 读取 KEY=VALUE 格式文件
    # 支持引号包裹的值（"..." 或 '...'）
    # 忽略 # 注释行和空行
```

这用于在项目根目录配置 IGG 路径（不纳入版本管理）。

### 6.6 `run_summary.json`（Schema v4）

（`mesh.py`）

```json
{
    "schema_version": 4,
    "run_id": "a1b2c3...（UUID4 hex）",
    "created_at": "2026-09-11T08:00:00+00:00（UTC ISO）",
    "run_dir": "runs/Rotor37_20260723_143052_a1b2c3...",
    "geometry": { /* GeomTurboSummary.to_dict() */ },
    "controls": {
        "requested":  [ /* ControlRequest.to_dict() */ ],
        "resolved":   [ /* ResolvedControl.to_dict() */ ],
        "applied":    [ /* setter 前后回读 */ ],
        "post_generation": [ /* 3D 网格生成后回读 */ ],
        "verification": {
            "basis": "post_generation",
            "status": "COMPLETE",
            "results": [ /* 每项控制 VERIFIED/MISMATCH/READBACK_ERROR/UNVERIFIABLE */ ]
        }
    },
    "autogrid": { /* AutoGridRun.to_dict() */ },
    "mesh_fingerprint": { /* 可选：block 尺寸、完整坐标 SHA 和探针 */ },
    "quality": { /* summarize_quality() 返回值 */ },
    "quality_validation": { /* 数据校验三态摘要：VALID/INVALID/UNKNOWN */ },
    "execution_evidence": {
        "completion_event": { /* AGMESH_COMPLETION 完成事件 */ },
        "protocol_errors": [ /* 事件协议错误（code + message） */ ]
    },
    "sources": { /* 输入摘要、源码签名、Git 提交、脚本摘要、注册表签名、质量规则版本、厂商版本 */ },
    "manifest": {
        "stages_completed": ["generation", "quality"],
        "outputs": [ /* 相对路径、大小、sha256 */ ]
    }
}
```

Schema v4 在 v3 全部字段基础上增加运行身份（`run_id`、`created_at`）、执行证据（`execution_evidence`）、控制验证（`controls.verification`）、质量校验（`quality_validation`）、来源（`sources`）与产物清单（`manifest`）。历史 v3 摘要仍可读：平台侧 `_load_run_summary` 同时接受 v3/v4，缺失的新证据显示为未知，不自动认定合格。所有字段使用 `sort_keys=True` 且 `allow_nan=False` 序列化（领域校验后的序列化保护），以临时文件 + `os.replace` 原子写入。

`controls.resolved` 中的状态为 `"planned"`（dry-run 时）或实际执行后的状态。
`controls.applied` 和 `controls.post_generation` 只在真实执行时填充。指定
`--mesh-fingerprint` 时，程序从导出的 CGNS 读取每个结构化 block 的全部
Float64 坐标，记录 I/J/K、点数/单元数、逐 block SHA-256、固定坐标探针和
聚合网格指纹。

### 6.7 `report.md`（中文报告）

（`mesh.py:253-382`）

生成一份 Markdown 格式的中文网格生成报告，包含四个部分：

1. **几何输入**：来源文件、单位、叶排数、叶片信息表
2. **网格控制**：请求数、解析数、模式；每个控制的键、目标、请求值、项目单位值、阶段、状态、回读、错误
3. **AutoGrid 执行**：返回码、错误、脚本路径、命令
4. **网格质量**：数据源、判定、版本、耗时、各指标统计表（含最差位置）

### 6.8 控制目录输出

**`_print_control_catalog(priority)`**（`mesh.py:431-442`）：以 Tab 分隔的表格格式输出控制目录——键、优先级、作用域、类型、阶段、说明。

**`_print_control_description(key)`**（`mesh.py:445-463`）：输出单个控制的完整信息——包括 setter/getter 名称、拓扑限制、不适用条件等。

---

## A. 附录：术语速查表

| 缩写/术语 | 全称 | 含义 |
|---|---|---|
| **CFD** | Computational Fluid Dynamics | 计算流体力学 |
| **IGG** | — | NUMECA 的网格生成器可执行文件 |
| **B2B** | Blade-to-Blade | 叶片到叶片的二维截面 |
| **CGNS** | CFD General Notation System | 跨软件 CFD 数据交换格式 |
| **TRB** | TuRBo | AutoGrid 项目文件格式 |
| **P0 / P1 / P2** | Priority 0 / 1 / 2 | 控制项的使用频率/重要性分级 |
| **SI** | Système International | 国际单位制（米、千克、秒） |
| **DOE** | Design of Experiments | 实验设计（参数化自动调参） |
| **Hub** | — | 轮毂，叶片根部所附着的旋转面 |
| **Shroud** | — | 机匣，叶片尖端的外壳 |
| **Spanwise** | — | 展向（沿叶片高度/径向方向） |
| **Streamwise** | — | 流向（沿气流流动方向） |
| **Azimuthal** | — | 周向（绕旋转轴的方向） |
| **HOH** | H-O-H | 一种 B2B 拓扑：入口 H 块 + O 块 + 出口 H 块 |
| **H&I** | H and I | 一种 B2B 拓扑：H 块 + I 块 |
| **HARD_LIMITS** | — | 硬门槛：网格质量必须满足的最低标准 |
| **RowWizard** | — | AutoGrid 的自动网格向导 |

---

> **文档版本**：2026-07-23，基于 AutoGrid 17.1 实现
> 
> 本文档对应的代码版本是 `src/controls.py` 344 项控制注册表、`src/autogrid.py`
> 721 项 setter 审计、`run_summary.json` Schema v4，以及保持兼容的
> `src/quality.py` Schema v2 质量模型。
