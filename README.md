# geomTurbo → AutoGrid 17.1 网格生成工具

本项目从 `.geomTurbo` 文件直接建立 NUMECA AutoGrid 17.1 项目，应用经过类型校验的纯网格控制，生成 B2B/3D 网格，并把原生 `.qualityReport` 标准化为 Schema v3 运行摘要。

当前实现保持根目录扁平：不依赖 `.trb` 模板，不读取 JSON/YAML 配置，不对 `.trb` 做字符串修改。所有运行产物写入 `runs/`。

## 已实现能力

- 解析几何单位、叶排、主叶片、splitter、gap、partial-gap 和 fillet 等选择器信息。
- 通过 `controls.py` 中唯一的静态 `ControlSpec` 注册表开放 AutoGrid 17.1 纯网格控制。
- 提供 P0/P1/P2 共 344 个控制键，以及 721 个官方 setter 的映射或明确排除审计。
- 支持全局、wizard、row、blade、gap、partial-gap、fillet、interface、endwall 和已有技术效果等作用域。
- 严格校验选择器、类型、枚举、范围、拓扑适用性和 AutoGrid API 能力。
- 长度控制始终按米输入，并按 `.geomTurbo` 的 `UNITS-FACTOR` 换算为项目单位。
- 按固定阶段调用正式 AutoGrid 17.1 Python API，并通过 getter 回读可回读参数。
- 完整解析项目、逐叶排质量统计以及最差 block/I/J/K 位置。
- 输出 Schema v3 `run_summary.json` 和中文 `report.md`，同时保留旧版字段。
- 可选 `--mesh-fingerprint`，记录完整 CGNS block 坐标 SHA-256、I/J/K 尺寸、固定坐标探针和聚合网格指纹。
- 在缺少 `.qualityReport` 时降级读取 CGNS 内嵌 `NIGridQuality` 数据。

本阶段不包含自动调参、DOE、优化循环、CFD 求解、`y+` 计算或网格无关性分析。

## 项目结构

```text
mesh.py                 命令行入口、Schema v3 摘要、网格指纹和中文报告
controls.py             类型化控制注册表、选择器、校验、SI 换算和 17.1 API 审计
geomturbo.py            .geomTurbo 元数据与实体选择信息解析
autogrid.py             AutoGrid 17.1 脚本渲染、执行、回读和结果标记解析
quality.py              .qualityReport/CGNS 质量解析与 PASS/FAIL/UNKNOWN 判定
geometries/             .geomTurbo 输入样例
tests/                  根目录实现的单元测试
docs/                   设计、控制目录、质量准则和开发日志
runs/                   可丢弃的运行产物
archive/                v0～v6 历史快照；当前实现完全不读取
```

## 运行环境

项目仅依赖 Python 标准库，无需安装第三方包，使用系统任意 Python ≥3.7 即可运行。

真实网格生成需要 NUMECA AutoGrid/IGG 17.1。IGG 路径按以下顺序解析：

1. `--igg` 显式路径；
2. 当前目录 `.env` 中的 `IGG_EXE` 或 `IGG_PATH`；
3. `PATH` 和常见 NUMECA 安装环境。

本地 `.env` 示例：

```text
IGG_EXE=C:\ProgramData\NUMECA\fine171\bin64\iggx86_64.exe
```

`.env` 不纳入版本管理。

## 快速使用

默认生成网格：

```powershell
python mesh.py geometries/Rotor37.geomTurbo
```

只完成静态校验、脚本渲染和计划记录，不启动 IGG：

```powershell
python mesh.py geometries/Rotor37.geomTurbo --dry-run
```

指定输出目录和超时：

```powershell
python mesh.py geometries/WP100_comp.geomTurbo --out runs/wp100 --timeout 1200
```

## 控制目录查询

查询不需要提供几何文件：

```powershell
python mesh.py --list-controls
python mesh.py --list-controls P0
python mesh.py --list-controls P1
python mesh.py --describe-control row/optimization.steps
```

`--describe-control` 会显示类型、范围、单位、优先级、阶段、适用拓扑、setter/getter 和回读能力。完整控制目录以 `controls.py` 的注册表及上述命令输出为准。

## 高频控制

```powershell
python mesh.py geometries/Rotor37.geomTurbo `
  --mesh-level fine `
  --first-cell-width 1e-5 `
  --spanwise-paths 97 `
  --gap-points 17 `
  --optimization-steps 200 `
  --gap-optimization-steps 100
```

显式参数包括：

| 参数 | 含义 |
|---|---|
| `--mesh-level coarse\|medium\|fine\|user` | 所有叶排网格级别 |
| `--target-points N` | 所有叶排 user 级别目标点数 |
| `--first-cell-width M` | 所有叶排首层宽度；固定以米输入 |
| `--spanwise-paths N` | 所有叶排 RowWizard 展向 flow paths 数 |
| `--gap-points N` | 所有已存在 gap 的展向点数 |
| `--optimization-steps N` | 所有叶排普通优化步数 |
| `--gap-optimization-steps N` | 所有叶排 gap 优化步数 |

## 完整 `--set` 接口

`--set` 可重复使用，每次只接收一条已注册控制：

```powershell
python mesh.py geometries/WP100_comp.geomTurbo `
  --set "configuration/grid_levels=3" `
  --set "row:*/optimization.steps=200" `
  --set "row:diffuser_axial/flow_path.number=89" `
  --set "row:#2/blade:#1/b2b.default.streamwise_inlet_points=33"
```

blade/gap 示例：

```powershell
python mesh.py geometries/Rotor37.geomTurbo `
  --set "row:row 1/blade:Main Blade/gap:shroud/spanwise_points=17"
```

选择器规则：

- `*` 匹配该层全部实体；`#N` 使用从 1 开始的索引；普通文本按区分大小写的实体名匹配。
- 实体名包含 `/` 或 `=` 时必须使用 `#N`。
- 精确选择器覆盖 wildcard，覆盖结果与参数顺序无关。
- 同一选择器和控制键重复定义会直接报错。
- 值只按注册表解析为 `bool`、`int`、`float`、`enum` 或逗号分隔元组，不执行 Python 表达式。
- 未知键、类型/范围错误、实体不存在、拓扑不适用、setter/getter 缺失都严格失败。
- `--no-row-wizard` 与任何 wizard 控制不能同时使用。

### 长度单位

所有标记为长度的控制都以米输入：

```text
project_value = requested_si / units_factor
```

例如 `1e-5 m` 在米制项目中传入 `1e-5`，在 `UNITS-FACTOR = 0.001` 的毫米制项目中传入 AutoGrid 的值为 `0.01`。请求值、项目单位值和 getter 回读值都会写入运行摘要。

## AutoGrid 执行顺序

```text
新建项目并导入 geomTurbo
  → a5_get_row_number() 获取真实叶排数
  → configuration 控制
  → wizard 控制
  → RowWizard.generate()
  → topology
  → distribution
  → boundary_layer
  → optimization
  → interface
  → existing_effect
  → 保存 mesh.trb
  → 生成 flow paths、B2B 和 3D 网格
  → 保存项目并导出 CGNS
```

`.trb` 仅由 AutoGrid API 保存，用于复现和人工核对，程序不会读取后再修改其文本。

## 运行产物与 Schema v3

典型运行目录：

```text
runs/<case>_<timestamp>/
  input.geomTurbo
  autogrid_init.py
  stdout.log
  stderr.log
  mesh.igg
  mesh.cgns
  mesh.trb
  mesh.bcs
  mesh.qualityReport
  mesh_fingerprint.json
  run_summary.json
  report.md
```

`run_summary.json` 的稳定顶层结构为：

```text
schema_version: 3
geometry
controls
  requested
  resolved
  applied
  post_generation
autogrid
mesh_fingerprint
quality
  metrics_source
  metadata
  project
  entities
  metrics
  result
```

- `controls.resolved` 记录静态解析结果；dry-run 中状态为 `planned`。
- `controls.applied` 记录 setter 前后回读；`controls.post_generation` 记录 3D 网格生成后的 getter 回读。
- `mesh_fingerprint` 记录每个 block 的 I/J/K、点数/单元数、完整坐标 SHA-256、固定位置探针和聚合指纹；只有指定 `--mesh-fingerprint` 时生成。
- `quality.entities` 包含 Entire Mesh 和每个 row 的完整统计。
- `quality.metrics` 与 `quality.result` 保留兼容接口。

静态 CLI/控制校验失败返回码为 2，且不会创建运行目录或启动 IGG。AutoGrid 内部实体、拓扑、API 或生成失败返回码为 1，并记录失败控制。成功返回 0。

## 质量判定

状态保持为 `PASS`、`FAIL`、`UNKNOWN`：

| 指标 | 通过条件 |
|---|---:|
| 负体积单元 | `= 0` |
| grid levels | `>= 3` |
| 最小 skewness angle | `>= 15°` |
| 最大 expansion ratio | `<= 3` |
| 展向角偏差 `180° - min spanwise skewness` | `<= 40°` |
| 最大 spanwise expansion ratio | `<= 2` |
| 最大 aspect ratio | `<= 15000` |

任一必需字段缺失时为 `UNKNOWN`。详细字段、极值位置语义和 CGNS 降级约束见 `docs/QUALITY_CRITERIA.md`。

## 当前三算例基线

现有 AutoGrid 17.1 质量报告的默认判定保持不变：

| 几何 | 叶排数 | 点数 | 判定 |
|---|---:|---:|---|
| `Rotor37.geomTurbo` | 1 | 1,464,289 | `PASS` |
| `WP100_comp.geomTurbo` | 3 | 4,239,316 | `FAIL` |
| `ori1.geomTurbo` | 3 | 2,744,343 | `FAIL` |

`PASS` 只表示满足上述网格硬门槛，不代表 CFD 已收敛，也不代表满足目标 `y+` 或网格无关性要求。

## 测试

只运行当前根目录测试：

```powershell
python -m pytest tests -q
```

测试不会读取或执行 `archive/` 中的历史实现。
