# geomTurbo AutoGrid 初始网格工作流

本项目用于将叶轮机械 `.geomTurbo` 几何自动转换为可供后续 CFD 前处理使用的 NUMECA AutoGrid 初始网格，并同步输出几何摘要、AutoGrid 执行记录、原生质量报告和标准化运行报告。当前主实现已由历史 v6 提升至项目根目录，v0～v6 快照统一保存在 `archive/`，不参与当前运行和测试。

当前实现保持扁平结构：不使用预制 `.trb` 模板，不提供 JSON 配置入口，也不保留 v5 风格的网格参数覆盖面。唯一必需输入是 `.geomTurbo` 文件。

## 当前状态

当前已实现：

- 解析 `.geomTurbo` 基础元数据：版本、单位、行数、行名、周期数、主叶片数、splitter、tip gap。
- 生成 AutoGrid/IGG batch 脚本。
- 从 `.geomTurbo` 初始化 AutoGrid 项目。
- 可选尝试调用 AutoGrid row wizard。
- 在生成 B2B/3D 前先保存 `mesh.trb`，触发 AutoGrid 原生 `mesh.qualityReport` 输出。
- 生成 B2B 网格和 3D 网格。
- 收集 AutoGrid 输出文件。
- 从 AutoGrid quality report 或 CGNS 内嵌 `NIGridQuality` 数据提取质量指标。
- 生成统一的 `run_summary.json` 和人工阅读用的 `report.md`。
- 即使 dry run、IGG 失败或输出缺失，也写入 `run_summary.json` 和 `report.md`，方便追踪失败原因。

当前未实现：

- 不支持 `--mesh-level`、`--first-cell-width`、`--spanwise-paths` 等网格控制参数。
- 不支持任意 `.trb` 字段覆盖。
- 不支持 JSON 配置文件。
- 不做自动调参、自动修复失败网格或多套网格无关性生成。
- 不替代 CFD 求解后的收敛性、`y+`、压比、效率或流量校核。

网格控制项的调研和后续接口草案在 `docs/MESH_CONTROL_ITEMS.md`，其中的示例命令不是当前 `mesh.py` 已支持功能。

## 最近一次完整生成验证

2026-07-22 至 2026-07-23 已清理 `runs/` 并使用当前根目录实现，对 `geometries/` 下三个几何逐一执行真实 IGG/AutoGrid batch。三次执行的 IGG 返回码均为 0，均生成了 `mesh.igg`、`mesh.cgns`、`mesh.trb`、`mesh.bcs` 和原生 `mesh.qualityReport`，且负体积单元数均为 0。

| 几何 | 行数 | 网格点数 | 最小偏斜角 | 最大膨胀比 | 最小展向偏斜角 | 最大长宽比 | 判定 |
|---|---:|---:|---:|---:|---:|---:|---|
| `Rotor37.geomTurbo` | 1 | 1,464,289 | 21.761° | 1.7254 | 168.31° | 342.35 | `PASS` |
| `WP100_comp.geomTurbo` | 3 | 4,239,316 | 21.702° | 3.7167 | 119.89° | 317.09 | `FAIL` |
| `ori1.geomTurbo` | 3 | 2,744,343 | 10.155° | 2.6684 | 134.33° | 942.03 | `FAIL` |

对应运行目录：

- `runs/Rotor37_20260722_235808/`
- `runs/WP100_comp_20260722_235909/`
- `runs/ori1_20260723_000049/`

`WP100_comp` 的失败项为最大膨胀比 3.7167 超过 3.0，以及展向角度偏差 60.11° 超过 40°；`ori1` 的失败项为最小偏斜角 10.155° 低于 15°，以及展向角度偏差 45.67° 超过 40°。这里的 `PASS`/`FAIL` 只表示是否满足 `quality.py` 的当前网格硬门槛；AutoGrid 执行成功不等于质量通过，也不代表已经完成 `y+`、网格无关性或 CFD 收敛性验证。

## 核心流程

```text
.geomTurbo
  -> 解析几何元数据
  -> 生成 AutoGrid/IGG batch 脚本
  -> 从 geomTurbo 初始化 AutoGrid 项目
  -> 可选执行 AutoGrid row wizard
  -> 预保存 mesh.trb，确定 AutoGrid quality report 文件名
  -> 生成 B2B 与 3D 网格
  -> 收集 mesh.* 输出
  -> 解析 quality report 或 CGNS 内嵌质量数据
  -> 写入 runs/<case>_<timestamp>/ 运行目录
```

## 项目结构

```text
mesh.py                 CLI 入口，组织一次完整运行并生成 run_summary.json/report.md
geomturbo.py            轻量级 .geomTurbo 元数据解析器
autogrid.py             AutoGrid 脚本生成、IGG 路径解析、batch 执行和输出收集
quality.py              质量指标解析和 PASS/FAIL/UNKNOWN 判定
AGENTS.md               当前项目范围、目录边界和协作约定
geometries/             当前主实现使用的 .geomTurbo 输入样例
docs/                   项目设计、质量准则、网格控制项调研和历史说明文档
  autogrid_backend_py_dev_plan.md  AutoGrid Python 后端开发计划
  QUALITY_CRITERIA.md   网格质量指标和更完整的分级准则说明
  MESH_CONTROL_ITEMS.md 后续可实现网格控制项调研；不是当前 CLI 文档
  NUMECA_AutoGrid_*.docx          AutoGrid 说明与 TRB 学习资料
archive/                v0～v6 历史快照；当前开发默认忽略
tests/                  单元测试
runs/                   运行产物目录，可丢弃
```

示例输入文件：

```text
geometries/Rotor37.geomTurbo       米制，单行 rotor，含 tip gap
geometries/WP100_comp.geomTurbo    毫米制，多行压气机，含 splitter 和 tip gap
geometries/ori1.geomTurbo          米制，三行压气机，无 splitter，含 tip gap
```

## 运行环境

- Python 3.10 或更新版本。
- 真实网格生成需要 NUMECA AutoGrid/IGG。
- `--dry-run` 不启动 IGG，只生成运行脚本、几何摘要、执行摘要和报告，适合检查流程。

IGG 可执行文件解析优先级如下：

1. 命令行显式传入的 `--igg`。
2. 启动 `python mesh.py ...` 时当前工作目录 `.env` 中的 `IGG_EXE` 或 `IGG_PATH`。
3. `PATH` 中的 `igg`/`iggx86_64`。
4. 常见 NUMECA/FINE 环境变量和安装目录。

本机固定路径建议写入当前工作目录的 `.env`，该文件已被 `.gitignore` 忽略：

```text
IGG_EXE=C:\ProgramData\NUMECA\fine171\bin64\iggx86_64.exe
```

## 使用方法

先做 dry run 检查：

```powershell
python mesh.py geometries/Rotor37.geomTurbo --dry-run
```

生成初始网格：

```powershell
python mesh.py geometries/Rotor37.geomTurbo
```

指定输出目录：

```powershell
python mesh.py geometries/WP100_comp.geomTurbo --out runs/wp100
```

指定 IGG 可执行文件：

```powershell
python mesh.py geometries/Rotor37.geomTurbo --igg C:\ProgramData\NUMECA\fine171\bin64\iggx86_64.exe
```

若当前工作目录 `.env` 已配置 `IGG_EXE`，通常不需要再传 `--igg`：

```powershell
python mesh.py geometries/Rotor37.geomTurbo
```

跳过 row wizard：

```powershell
python mesh.py geometries/Rotor37.geomTurbo --no-row-wizard
```

设置 AutoGrid 超时时间：

```powershell
python mesh.py geometries/Rotor37.geomTurbo --timeout 600
```

依次重新生成 `geometries/` 下全部几何：

```powershell
Get-ChildItem geometries -Filter *.geomTurbo |
  Sort-Object Name |
  ForEach-Object { python mesh.py $_.FullName --timeout 1200 }
```

每个输入会写入独立的时间戳目录；上述命令为串行执行，可避免多个 IGG 进程同时争用许可证和计算资源。

## CLI 参数

当前 `mesh.py --help` 暴露的参数如下：

| 参数 | 说明 |
|---|---|
| `geomturbo` | 必填，输入 `.geomTurbo` 文件。 |
| `--out` | 输出目录；默认写入 `runs/<case>_<timestamp>/`。 |
| `--igg` | IGG 可执行文件名或完整路径；显式传入时覆盖 `.env`。 |
| `--no-row-wizard` | 跳过 AutoGrid row wizard 尝试。 |
| `--dry-run` | 只创建脚本和报告，不执行 IGG。 |
| `--timeout` | AutoGrid batch 执行超时时间，单位为秒。 |

## 运行产物

每次运行会创建一个独立目录。典型内容如下：

```text
runs/<case>_<timestamp>/
  # 输入快照与复现信息
  input.geomTurbo
  autogrid_init.py
  stdout.log
  stderr.log

  # 主要网格与质量产物
  mesh.igg
  mesh.cgns
  mesh.trb
  mesh.bcs
  mesh.qualityReport
  run_summary.json
  report.md

  # AutoGrid 自动写出的 sidecar，通常只用于诊断或重新打开项目
  mesh.info
  mesh.geom
  mesh.geomTurbo
  mesh.config
  mesh.x_t
```

其中 `.trb` 是运行后生成的产物，不是输入模板。不同 AutoGrid/IGG 版本、脚本 API 可用性和导出结果会影响具体文件是否存在。
`input.geomTurbo` 是原始输入的快照；`mesh.geomTurbo` 是 AutoGrid 保存项目/网格时重新导出的几何文件，可能被
AutoGrid 规范化或改写版本号，不应当视为原始输入的替代品。
当前实现会在网格生成前先保存 `mesh.trb`，AutoGrid 会据此直接写出同名前缀的 `mesh.qualityReport`。这个文件是原生
AutoGrid 质量报告，不是 `run_summary.json` 或 `report.md` 的替代品；`run_summary.json` 中的 `quality` 字段只是
对它的标准化解析结果。若某个 AutoGrid 版本未写出 `.qualityReport`，代码才会回退解析 CGNS 内嵌
`NIGridQuality` 数据。

`run_summary.json` 只记录当前代码收集到的根目录 `mesh.*` 输出。部分 AutoGrid 版本还可能额外写出
`mesh_SubProject_Fluid/`、`mesh.bak.*` 或其他 sidecar 文件，它们可用于诊断，但当前不会作为机器接口读取。

`runs/` 下的目录均按可重新生成的运行产物处理，不承诺长期保留。当前保留的三个目录来自上文所列最近一次完整生成验证；机器判定应以各目录内的 `run_summary.json` 为准，人工复核可查看 `report.md` 和 `mesh.qualityReport`。

## 模块摘要

`mesh.py` 负责命令行参数解析、默认运行目录创建、`.env` 读取、AutoGrid 调用、统一运行摘要写入和最终
`report.md` 渲染。若 IGG 返回非零，或 IGG 返回 0 但没有收集到任何网格输出，CLI 返回失败状态。

`geomturbo.py` 解析 `.geomTurbo` 中的轻量元数据，不做完整几何重建。

`autogrid.py` 生成 AutoGrid 初始化脚本。脚本只从 `.geomTurbo` 初始化项目，随后尝试调用 row wizard。生成网格前会先保存 `mesh.trb`，这是 AutoGrid batch 模式确定 `mesh.qualityReport` 文件名的关键步骤；之后执行 B2B 网格生成、3D 网格生成、项目保存和 CGNS 导出。

`quality.py` 优先解析 AutoGrid 原生 `mesh.qualityReport`；没有 quality report 时，尝试从 CGNS 内嵌的 `NIGridQuality` 数据中提取指标。它只返回标准化质量数据，最终由 `mesh.py` 写入 `run_summary.json`。

## 运行摘要

`run_summary.json` 是唯一机器可读汇总文件，结构如下：

```text
run_summary.json
  schema_version
  run_dir
  geometry
  autogrid
  quality
```

其中：

- `geometry` 来自 `.geomTurbo` 轻量解析。
- `autogrid` 记录 batch 命令、返回码、脚本路径和收集到的输出文件。
- `quality` 记录质量指标来源、解析后的指标、`PASS/FAIL/UNKNOWN` 判定和原因；dry run 或 IGG 失败时为 `null`，IGG 成功但缺少网格输出或质量数据时为 `UNKNOWN`。

`report.md` 是从同一批数据生成的人读摘要。它可以保留以便快速检查，但不作为机器接口。

## 质量判定

当前代码实现的结果只有三类：

| 状态 | 含义 |
|---|---|
| `PASS` | 必要指标齐全，且全部硬性阈值通过。 |
| `FAIL` | 必要指标齐全，但至少一项硬性阈值失败。 |
| `UNKNOWN` | 缺少网格输出、缺少质量数据源或缺少必要指标。 |

当前硬性阈值定义在 `quality.py` 的 `HARD_LIMITS` 中：

| 指标 | 当前阈值 |
|---|---:|
| `negative_cells` | `= 0` |
| `grid_levels` | `>= 3` |
| `min_skewness_angle` | `>= 15.0` |
| `max_expansion_ratio` | `<= 3.0` |
| `180.0 - min_spanwise_skewness_angle` | `<= 40.0` |
| `max_spanwise_expansion_ratio` | `<= 2.0` |
| `max_aspect_ratio` | `<= 15000.0` |

`docs/QUALITY_CRITERIA.md` 记录了更完整的工程质量分级设想，例如 `ENGINEERING_PASS` 和 `PRODUCTION_CANDIDATE`，但这些等级当前尚未在 `quality.py` 中实现。

## 网格控制项调研

`docs/MESH_CONTROL_ITEMS.md` 记录后续可能开放的网格控制项，包括：

- `--mesh-level <level>`
- `--first-cell-width <value>`
- `--spanwise-paths <int>`
- `--gap-points <int>`
- `--optimization-steps <int>`
- `--gap-optimization-steps <int>`

这些参数目前只是调研和接口草案。若后续实现，应继续保持当前简化方向：优先少量 CLI 参数或内部 preset，不重新引入 JSON 配置、大规模 `.trb` 覆盖或原 v5 风格参数面。

## 测试

运行单元测试：

```powershell
python -m unittest discover -s tests -v
```

测试覆盖内容包括：

- `geometries/Rotor37.geomTurbo` 和 `geometries/WP100_comp.geomTurbo` 的几何摘要解析。
- AutoGrid batch 脚本是否从 geomTurbo 初始化，且不打开 `.trb` 模板。
- 假 IGG 执行时的输出收集。
- `.env` 中的 `IGG_EXE` 是否生效，以及命令行 `--igg` 是否覆盖 `.env`。
- IGG 返回 0 但没有网格输出时，CLI 是否返回失败并写入 `UNKNOWN` 原因。
- CGNS 内嵌质量指标解析和硬性质量判定。
- dry run 只写入 `run_summary.json`，不再生成旧的三个分散 JSON。

## 维护边界

- 保持目录扁平：`mesh.py`、`geomturbo.py`、`autogrid.py`、`quality.py`、`tests/`。
- 生成文件只写入 `runs/`。
- `archive/` 仅保存历史版本，当前开发、搜索和测试默认忽略。
- 新增功能前先确认 AutoGrid Python API 的稳定 setter，避免依赖脆弱的 `.trb` 字符串替换。
- 文档中的调研性参数必须标注为未实现，避免与当前 CLI 混淆。
