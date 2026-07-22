# NUMECA / Cadence AutoGrid5 后端 Python 自动网格生成项目开发方案

版本：v1.0  
日期：2026-07-01  
目标范围：仅后端 Python 脚本、命令行工具与本地作业管理；不包含前端、Web UI、REST API、异步服务平台。

---

## 1. 项目目标

本项目实现一个纯后端 Python 工具链，用于从 `.geomTurbo` 文件自动调用 NUMECA / Cadence AutoGrid5 生成涡轮机械结构网格，并提供生成过程参数设置与生成后质量报告查询。

目标能力：

1. 输入 `.geomTurbo` 文件。
2. 输入 YAML / JSON 参数文件，控制 AutoGrid5 网格生成过程。
3. 从命令行调用 AutoGrid5：`igg -autogrid5 -batch -script ag5_worker.py`。
4. 在 AutoGrid5 内部脚本中执行：
   - 初始化新项目；
   - 配置 row、machine type、rotor/stator、periodicity、rotation speed；
   - 配置 grid level、flow paths、cell width、gap、fillet、streamwise weight、optimization 等；
   - 生成 topology、flow paths、B2B mesh、3D mesh；
   - 保存 `.trb`、`.igg`、可选 `.cgns`、Fluent、OpenFOAM 等格式。
5. 解析 `.qualityReport`，并将质量指标保存为结构化 JSON / SQLite。
6. 提供 CLI 查询接口，例如：
   - `agmesh run ...`
   - `agmesh quality <job_dir>`
   - `agmesh parse-report ...`
   - `agmesh probe ...`
7. 适配单 row 与多 row 工况。
8. 每个 case 独立作业目录，保存完整日志、配置、生成脚本、结果和质量报告，便于复现。

非目标：

1. 不开发前端界面。
2. 不开发 REST API 服务。
3. 不实现远程队列系统。
4. 不实现图形化质量报告浏览器。
5. 不替代 AutoGrid5 内部网格生成算法，只做参数化调用、结果管理和质量查询。
6. 不要求用户预先提供 `.trb` 模板；`.trb` 由程序自动保存为中间产物和复现文件。

---

## 2. 技术依据与边界

### 2.1 可行性依据

Cadence 当前 Fidelity CFD 产品页说明，Fidelity Autogrid Mesh Generation 的结构网格能力包括：

- mesh templates；
- Python API for automatic mesh generation；
- automatic grid point distribution；
- fast smoothing algorithms；
- single stage、multistage、diffuser、bypass 等涡轮机械配置支持。

这说明“通过 Python 自动化 AutoGrid 网格生成”是产品能力范围内的路线。

公开可访问的 AutoGrid5 12.2 User Guide 镜像列出了以下关键能力：

- `igg -autogrid5 -script my_script.py` 可从命令行运行 AutoGrid5 Python 脚本；
- `igg -autogrid5 -batch -script my_script.py` 可批处理执行并避免打开 GUI；
- AutoGrid5 脚本与 IGG 脚本不可互换；
- 该手册版本中 AutoGrid5 脚本解释器为 Python 2.7；
- `a5_init_new_project_from_a_geomTurbo_file(geomTurbo_file_name, cascademode=0)` 可从 `.geomTurbo` 初始化新项目；
- `a5_initialize_topology()`、`a5_generate_flow_paths()`、`a5_generate_b2b()`、`a5_start_3d_generation()` 覆盖主要网格生成步骤；
- `a5_save_mesh(mesh_file_name)` 保存网格；
- `a5_save_project(trb_file_name)` 保存项目；
- `.qualityReport` 在 3D generation 后生成，但若 3D 生成前项目尚未保存，系统无法确定报告路径，质量报告不会保存。

CFturbo 手册说明，impeller 几何可导出为 `.geomTurbo` 文件并由 AutoGrid 加载；多叶排模型需要在 AutoGrid 中添加相应 row；半开式叶轮的 tip clearance 需要在 AutoGrid 中处理。这提示本工具必须支持多 row 与 tip gap / clearance 参数化。

### 2.2 版本边界

本方案以 AutoGrid5 v17.1 及之前版本为目标，但公开可访问手册主要是 AutoGrid5 12.2 / v8 镜像；因此开发时必须加入本机能力探测：

```bash
agmesh probe --executable igg --niversion 171
```

能力探测结果用于确认：

- `a5_init_new_project_from_a_geomTurbo_file` 是否存在；
- `a5_save_project` / `a5_save_mesh` 是否可用；
- `a5_start_3d_generation` 是否可用；
- `row(i).generation_success()` 是否存在；
- `CGNS_format()` 是否存在；
- `set_preference("numberOfThreads", n)` 是否可用；
- `a5_export_OpenFOAM()` / `a5_export_FLUENT()` 是否可用。

### 2.3 `.trb` 的定位

`.trb` 不是输入前提。

本项目输入是：

```text
.geomTurbo + config.yml / config.json
```

但建议在 3D 生成前自动保存：

```python
a5_save_project("/job/output/project.trb")
```

原因是 `.qualityReport` 的保存位置依赖项目路径；若项目未保存，报告可能不生成。因此 `.trb` 在本项目中是自动生成的中间产物，用于：

1. 质量报告落盘；
2. 作业复现；
3. 失败调试；
4. 后续可选模板化批处理。

---

## 3. 总体后端架构

推荐采用“双层 Python”架构：

```text
agmesh 外层 Python 3 CLI
        │
        ├── 读取 config.yml / config.json
        ├── 参数校验与标准化
        ├── 创建 job 目录
        ├── 复制 / 记录输入 .geomTurbo
        ├── 生成 ag5_runtime_config.json
        ├── 生成 AutoGrid 内部脚本 ag5_worker.py
        ├── subprocess 调用 AutoGrid5
        ├── 收集 stdout / stderr / ag5_status.json
        ├── 定位 .qualityReport
        ├── 解析质量报告
        ├── 写出 quality.json / metrics.json / manifest.json
        └── 提供 CLI 查询

AutoGrid5 内层 Python 脚本，通常为 Python 2.7 兼容
        │
        ├── 读取 ag5_runtime_config.json
        ├── a5_init_new_project_from_a_geomTurbo_file()
        ├── configure global settings
        ├── configure rows with row_wizard()
        ├── a5_initialize_topology()
        ├── a5_generate_flow_paths()
        ├── a5_generate_b2b()
        ├── a5_save_project()
        ├── a5_start_3d_generation()
        ├── a5_save_mesh()
        ├── optional export: CGNS / Fluent / OpenFOAM
        └── 写出 ag5_status.json / metrics.json
```

关键原则：

1. 外层 Python 3 不直接调用 AutoGrid API，只通过 `subprocess` 启动 AutoGrid。
2. 内层脚本保持 Python 2.7 兼容，避免 f-string、pathlib、dataclass、typing 等语法。
3. 参数通过 JSON 文件传入，不将用户输入直接拼接成 Python 源码。
4. 每个作业独立目录运行，所有输入、输出、日志和脚本都归档。
5. 所有 AutoGrid API 调用尽量包裹 `try/except`，以兼容不同版本。
6. 作业状态由内层脚本持续写入 `ag5_status.json`，便于定位失败阶段。

---

## 4. 推荐项目结构

```text
agmesh/
  pyproject.toml
  README.md

  agmesh/
    __init__.py
    cli.py
    config_schema.py
    runner.py
    job.py
    script_renderer.py
    report_parser.py
    quality_model.py
    storage.py
    version_probe.py
    errors.py
    paths.py
    log_utils.py

    templates/
      ag5_worker.py.tpl
      probe_worker.py.tpl

  tests/
    test_config_schema.py
    test_script_renderer.py
    test_runner_dryrun.py
    test_report_parser.py
    test_quality_thresholds.py

  examples/
    centrifugal_impeller.yml
    axial_compressor_single_row.yml
    axial_stage_rotor_stator.yml
    run_single.sh

  docs/
    development_notes.md
    autogrid_api_mapping.md
```

---

## 5. 作业目录结构

每次运行生成一个独立作业目录：

```text
jobs/
  20260701-153011-9f3e2c/
    input/
      case.geomTurbo
      config.yml

    generated/
      ag5_worker.py
      ag5_runtime_config.json
      normalized_config.json

    logs/
      autogrid.stdout.log
      autogrid.stderr.log
      command.txt
      environment.json

    output/
      project.trb
      mesh.igg
      mesh.qualityReport
      mesh.cgns
      mesh.config
      mesh.bcs
      ag5_status.json
      metrics.json
      quality.json
      manifest.json
```

说明：

- `input/` 保存原始输入和配置副本；
- `generated/` 保存自动生成的内层脚本和运行时配置；
- `logs/` 保存 AutoGrid 进程日志和命令；
- `output/` 保存网格、项目文件、质量报告和结构化结果。

---

## 6. 配置文件设计

### 6.1 最小配置示例

```yaml
autogrid:
  executable: "igg"
  niversion: "171"
  batch: true
  real_batch: false
  timeout_sec: 7200
  threads: 8
  expert_mode: true

io:
  geomturbo: "/work/design001.geomTurbo"
  workdir: "/work/agmesh_jobs"
  project_name: "design001"
  mesh_filename: "mesh.igg"
  save_project_before_3d: true

global:
  units: "Meters"
  units_factor: null
  number_of_grid_levels: 3
  cgns_format: "HDF5"

generation:
  initialize_topology: true
  generate_flow_paths: true
  generate_b2b: true
  b2b_layers: [0, 50, 100]
  generate_3d: true

rows:
  - index: 1
    machine_type: "centrifugal_impeller"
    row_type: "rotor"
    periodicity: 15
    rotation_speed: 5000.0
    grid_level: 2
    flow_paths: 73
    cell_width_at_wall: 1.0e-5
    number_of_meshed_passages: 1
    full_matching_topology: true
    streamwise_weight:
      inlet: 1.0
      outlet: 1.0
      blade: 1.0
    gap_fillet:
      hub_gap: false
      tip_gap: true
      hub_fillet: false
      tip_fillet: false
      tip_gap_width_le: 0.0005
      tip_gap_width_te: 0.0005
    optimization:
      steps: 30
      skewness_control: "medium"
      orthogonality_control: 0.5
    span_interpolation: 50.0

exports:
  native_igg: true
  cgns: true
  fluent:
    enabled: false
    filename: "mesh.cas"
  openfoam:
    enabled: false
    folder: "constant/polyMesh"
    branch: "official"

quality:
  fail_on_missing_report: true
  thresholds:
    negative_cells_max: 0
    max_aspect_ratio: 5000
    min_orthogonality: 15
    max_expansion_ratio: 5.0
```

### 6.2 多 row 配置示例

```yaml
rows:
  - index: 1
    name: "rotor"
    machine_type: "axial_compressor"
    row_type: "rotor"
    periodicity: 36
    rotation_speed: 12000.0
    grid_level: 2
    flow_paths: 57
    cell_width_at_wall: 5.0e-6
    number_of_meshed_passages: 1

  - index: 2
    name: "stator"
    machine_type: "axial_compressor"
    row_type: "stator"
    periodicity: 48
    rotation_speed: 0.0
    grid_level: 2
    flow_paths: 57
    cell_width_at_wall: 5.0e-6
    number_of_meshed_passages: 1
```

---

## 7. 参数模型与校验

使用 Pydantic 或 dataclass + 自定义校验均可。推荐 Pydantic v2，因为配置较复杂。

### 7.1 枚举映射

AutoGrid RowWizard 的 machine type 使用整数。外层配置使用可读字符串，运行前转换为整数。

```python
MACHINE_TYPE_MAP = {
    "wind_turbine": 1,
    "axial_turbine": 2,
    "francis_turbine": 3,
    "kaplan_turbine": 4,
    "inducer": 5,
    "axial_compressor": 6,
    "centrifugal_impeller": 7,
    "centrifugal_diffuser": 8,
    "return_channel": 9,
    "contra_rotating_fan": 10,
    "centrifugal_pump": 11,
    "axial_fan": 12,
    "marine_propeller": 13,
}

ROW_TYPE_MAP = {
    "stator": 0,
    "rotor": 1,
}
```

### 7.2 必要校验

```text
- io.geomturbo 必须存在，扩展名建议为 .geomTurbo / .geomturbo。
- autogrid.executable 必须可执行，或可由 PATH 找到。
- generation.b2b_layers 的每个值必须在 0–100。
- rows[].index 必须为正整数。
- rows[].machine_type 必须在 MACHINE_TYPE_MAP 中。
- rows[].row_type 必须是 rotor 或 stator。
- rows[].periodicity 必须 > 0。
- rows[].number_of_meshed_passages 必须 >= 1。
- rows[].flow_paths 必须 > 0。
- rows[].cell_width_at_wall 若给出，必须 > 0。
- exports.openfoam.branch 只能是 official 或 extend。
- global.cgns_format 只能是 ADF 或 HDF5。
- quality.thresholds 中阈值必须为合理数值。
```

### 7.3 归一化配置

外层生成 `normalized_config.json`，其中包含所有默认值和 AutoGrid 数字枚举：

```json
{
  "rows": [
    {
      "index": 1,
      "machine_type": "centrifugal_impeller",
      "machine_type_id": 7,
      "row_type": "rotor",
      "row_type_id": 1,
      "periodicity": 15,
      "rotation_speed": 5000.0
    }
  ]
}
```

---

## 8. AutoGrid 内层脚本模板设计

内层脚本文件：`ag5_worker.py`。该文件由外层复制模板生成，并通过环境变量读取运行时配置路径：

```bash
AGMESH_RUNTIME_CONFIG=/job/generated/ag5_runtime_config.json
```

### 8.1 内层脚本主流程

```python
# coding=utf-8
# AutoGrid5 internal script; keep Python 2.7 compatible.

import os
import json
import time
import traceback


def read_json(path):
    f = open(path, "r")
    try:
        return json.load(f)
    finally:
        f.close()


def write_json(path, obj):
    f = open(path, "w")
    try:
        json.dump(obj, f, indent=2, sort_keys=True)
    finally:
        f.close()


def bool01(value):
    return 1 if value else 0


def write_status(output_dir, status):
    write_json(os.path.join(output_dir, "ag5_status.json"), status)


def main():
    cfg_path = os.environ["AGMESH_RUNTIME_CONFIG"]
    cfg = read_json(cfg_path)
    output_dir = cfg["io"]["output_dir"]

    status = {
        "ok": False,
        "stage": "start",
        "started_at": time.time(),
        "error": None,
    }

    try:
        status["stage"] = "configure_preferences"
        write_status(output_dir, status)
        configure_preferences(cfg)

        status["stage"] = "init_from_geomturbo"
        write_status(output_dir, status)
        a5_init_new_project_from_a_geomTurbo_file(cfg["io"]["geomturbo"], int(cfg["io"].get("cascademode", 0)))

        status["stage"] = "configure_global"
        write_status(output_dir, status)
        configure_global(cfg)

        status["stage"] = "configure_rows"
        write_status(output_dir, status)
        configure_rows(cfg)

        status["stage"] = "initialize_topology"
        write_status(output_dir, status)
        select_all_rows()
        a5_initialize_topology()

        if cfg.get("generation", {}).get("generate_flow_paths", True):
            status["stage"] = "generate_flow_paths"
            write_status(output_dir, status)
            select_all_rows()
            a5_generate_flow_paths()

        if cfg.get("generation", {}).get("generate_b2b", True):
            status["stage"] = "generate_b2b"
            write_status(output_dir, status)
            select_all_rows()
            for layer in cfg.get("generation", {}).get("b2b_layers", [0, 50, 100]):
                set_active_control_layer_index(float(layer))
                a5_generate_b2b()

        status["stage"] = "save_project_before_3d"
        write_status(output_dir, status)
        a5_save_project(cfg["io"]["project_trb"])

        if cfg.get("generation", {}).get("generate_3d", True):
            status["stage"] = "generate_3d"
            write_status(output_dir, status)
            select_all()
            a5_start_3d_generation()

        status["stage"] = "save_mesh"
        write_status(output_dir, status)
        a5_save_mesh(cfg["io"]["mesh_igg"])

        status["stage"] = "exports"
        write_status(output_dir, status)
        export_optional_formats(cfg)

        status["stage"] = "collect_metrics"
        write_status(output_dir, status)
        collect_runtime_metrics(cfg)

        status["ok"] = True
        status["stage"] = "done"
        status["finished_at"] = time.time()
        write_status(output_dir, status)

    except Exception as e:
        status["ok"] = False
        status["error"] = str(e)
        status["traceback"] = traceback.format_exc()
        status["finished_at"] = time.time()
        write_status(output_dir, status)
        raise


main()
```

### 8.2 Row 配置函数

```python
def configure_rows(cfg):
    expected_rows = cfg.get("rows", [])
    actual_nrows = a5_get_row_number()

    if len(expected_rows) != actual_nrows:
        raise RuntimeError("Row count mismatch: config=%d, geomTurbo=%d" % (len(expected_rows), actual_nrows))

    unselect_all_rows()

    for rcfg in expected_rows:
        i = int(rcfg["index"])
        r = row(i)
        r.select()

        wiz = r.row_wizard()
        wiz.initialize(
            int(rcfg["machine_type_id"]),
            int(rcfg["row_type_id"]),
            float(rcfg.get("rotation_speed", 0.0)),
            int(rcfg["periodicity"]),
        )

        if rcfg.get("grid_level", None) is not None:
            wiz.set_grid_level(int(rcfg["grid_level"]))

        if rcfg.get("flow_paths", None) is not None:
            wiz.set_flow_path_number(int(rcfg["flow_paths"]))

        if rcfg.get("cell_width_at_wall", None) is not None:
            wiz.set_row_cell_width_at_wall(float(rcfg["cell_width_at_wall"]))

        if rcfg.get("full_matching_topology", None) is not None:
            wiz.set_full_matching_topology(bool01(rcfg["full_matching_topology"]))

        gf = rcfg.get("gap_fillet", {})
        wiz.hub_gap_is_asked(bool01(gf.get("hub_gap", False)))
        wiz.tip_gap_is_asked(bool01(gf.get("tip_gap", False)))
        wiz.hub_fillet_is_asked(bool01(gf.get("hub_fillet", False)))
        wiz.tip_fillet_is_asked(bool01(gf.get("tip_fillet", False)))

        if gf.get("hub_gap_width_le", None) is not None:
            wiz.set_hub_gap_width_at_leading_edge(float(gf["hub_gap_width_le"]))
        if gf.get("hub_gap_width_te", None) is not None:
            wiz.set_hub_gap_width_at_trailing_edge(float(gf["hub_gap_width_te"]))
        if gf.get("tip_gap_width_le", None) is not None:
            wiz.set_tip_gap_width_at_leading_edge(float(gf["tip_gap_width_le"]))
        if gf.get("tip_gap_width_te", None) is not None:
            wiz.set_tip_gap_width_at_trailing_edge(float(gf["tip_gap_width_te"]))

        # Apply Row Wizard automatic expert settings.
        wiz.generate()

        r.set_periodicity(int(rcfg["periodicity"]))
        r.set_rotation_speed(float(rcfg.get("rotation_speed", 0.0)))

        if rcfg.get("number_of_meshed_passages", None) is not None:
            r.set_number_of_meshed_passage(int(rcfg["number_of_meshed_passages"]))

        sw = rcfg.get("streamwise_weight", None)
        if sw:
            r.set_streamwise_weight(float(sw["inlet"]), float(sw["outlet"]), float(sw["blade"]))

        cg = rcfg.get("coarse_grid", None)
        if cg:
            r.set_coarse_grid_level(int(cg.get("level", 2)), int(cg.get("target_points", 250000)))

        opt = rcfg.get("optimization", {})
        if opt.get("steps", None) is not None:
            safe_call(lambda: r.set_row_optimization_steps(int(opt["steps"])))
        if opt.get("skewness_control", None) is not None:
            safe_call(lambda: r.set_row_optimization_skewness_control(opt["skewness_control"]))
        if opt.get("orthogonality_control", None) is not None:
            safe_call(lambda: r.set_row_optimization_orthogonality_control(float(opt["orthogonality_control"])))

        if rcfg.get("span_interpolation", None) is not None:
            safe_call(lambda: r.set_span_interpolation(float(rcfg["span_interpolation"])))

        r.unselect()
```

### 8.3 可选导出函数

```python
def export_optional_formats(cfg):
    exports = cfg.get("exports", {})

    fluent = exports.get("fluent", {})
    if fluent.get("enabled", False):
        a5_export_FLUENT(fluent["filename"])

    openfoam = exports.get("openfoam", {})
    if openfoam.get("enabled", False):
        a5_export_OpenFOAM(openfoam["folder"], openfoam.get("branch", "official"))
```

### 8.4 运行时指标收集

```python
def collect_runtime_metrics(cfg):
    output_dir = cfg["io"]["output_dir"]
    metrics = {
        "rows": [],
        "quality_functions": {},
    }

    try:
        nrows = a5_get_row_number()
        metrics["row_count"] = nrows
    except Exception:
        nrows = 0
        metrics["row_count"] = None

    for i in range(1, nrows + 1):
        item = {"index": i}
        try:
            item["name"] = row(i).get_name()
        except Exception:
            pass
        try:
            item["generation_success"] = bool(row(i).generation_success())
            item["generation_error_message"] = row(i).generation_error_message()
        except Exception:
            item["generation_success"] = None
            item["generation_error_message"] = None
        metrics["rows"].append(item)

    try:
        row_list = range(1, nrows + 1)
        metrics["quality_functions"]["meridional_expansion_ratio_hist"] = calc_row_meridional_mesh_quality(
            "Expansion ratio", row_list, 0.0, 10.0, 20, 0, 0, 1
        )
    except Exception as e:
        metrics["quality_functions"]["meridional_expansion_ratio_error"] = str(e)

    try:
        row_list = range(1, nrows + 1)
        metrics["quality_functions"]["b2b_orthogonality_hist"] = calc_row_2D_mesh_quality(
            "Orthogonality", row_list, 0.0, 90.0, 18, 0, 0, 1
        )
    except Exception as e:
        metrics["quality_functions"]["b2b_orthogonality_error"] = str(e)

    write_json(os.path.join(output_dir, "metrics.json"), metrics)
```

---

## 9. 外层 Python 3 模块设计

### 9.1 `config_schema.py`

职责：

- 读取 YAML / JSON；
- 校验字段；
- 应用默认值；
- 生成 `normalized_config.json`；
- 生成 AutoGrid 数字枚举字段。

核心类：

```python
class AutoGridConfig(BaseModel):
    executable: str = "igg"
    niversion: str | None = None
    batch: bool = True
    real_batch: bool = False
    timeout_sec: int = 7200
    threads: int | None = None
    expert_mode: bool = True

class IOConfig(BaseModel):
    geomturbo: Path
    workdir: Path
    project_name: str
    mesh_filename: str = "mesh.igg"
    save_project_before_3d: bool = True

class RowConfig(BaseModel):
    index: int
    machine_type: str
    row_type: Literal["rotor", "stator"]
    periodicity: int
    rotation_speed: float = 0.0
    grid_level: int | None = 2
    flow_paths: int | None = 57
    cell_width_at_wall: float | None = None
    number_of_meshed_passages: int = 1
```

### 9.2 `job.py`

职责：

- 生成 job id；
- 创建目录；
- 复制输入；
- 计算 hash；
- 记录 manifest。

关键字段：

```python
@dataclass
class MeshJob:
    job_id: str
    root: Path
    input_dir: Path
    generated_dir: Path
    logs_dir: Path
    output_dir: Path
    geomturbo_path: Path
    config_path: Path
    normalized_config_path: Path
    runtime_config_path: Path
    worker_script_path: Path
    stdout_log: Path
    stderr_log: Path
    mesh_path: Path
    project_trb_path: Path
```

### 9.3 `script_renderer.py`

职责：

- 从模板复制 `ag5_worker.py.tpl`；
- 写入 generated 目录；
- 不把用户配置直接拼入脚本；
- 只通过 `AGMESH_RUNTIME_CONFIG` 读取配置。

### 9.4 `runner.py`

职责：

- 组装 AutoGrid 命令；
- 设置环境变量；
- 调用 `subprocess.run()`；
- 记录 stdout / stderr；
- 处理 timeout；
- 返回 `RunResult`。

命令生成逻辑：

```python
def build_command(cfg, worker_script):
    exe = cfg.autogrid.executable
    cmd = [exe]

    if cfg.autogrid.niversion:
        cmd += ["-niversion", str(cfg.autogrid.niversion)]

    if cfg.autogrid.real_batch:
        # 注意：公开手册中提示 real-batch 下 Python 脚本生成可能失败；默认不启用。
        cmd += ["-real-batch"]

    cmd += ["-autogrid5"]

    if cfg.autogrid.batch:
        cmd += ["-batch"]

    cmd += ["-script", str(worker_script)]
    return cmd
```

### 9.5 `report_parser.py`

职责：

- 定位 `.qualityReport`；
- 读取文本；
- 用版本相关 parser 或通用 regex parser 提取指标；
- 输出统一 `quality.json`；
- 进行阈值判定。

解析策略：

```text
1. 优先识别行/列结构。
2. 若格式不匹配，则使用关键词 regex：
   - negative cell(s)
   - expansion ratio
   - aspect ratio
   - orthogonality / skewness
   - Nb levels / number of levels
3. 若仍不能解析，则保留 raw text，并将 parser 标记为 RawOnlyParser。
4. 若 fail_on_missing_report=true 且无 qualityReport，则作业质量阶段失败。
```

### 9.6 `quality_model.py`

统一质量模型：

```python
@dataclass
class MetricRange:
    min: float | None = None
    max: float | None = None

@dataclass
class EntityQuality:
    name: str
    entity_type: str | None = None
    negative_cells: int | None = None
    expansion_ratio: MetricRange = field(default_factory=MetricRange)
    aspect_ratio: MetricRange = field(default_factory=MetricRange)
    orthogonality: MetricRange = field(default_factory=MetricRange)
    skewness: MetricRange = field(default_factory=MetricRange)
    nb_levels: int | None = None

@dataclass
class QualityReport:
    ok: bool
    source: Path
    parser: str
    summary: EntityQuality
    entities: list[EntityQuality]
    thresholds: dict
    raw_text_path: Path | None = None
```

### 9.7 `storage.py`

MVP 可不用数据库，仅使用文件系统。第二阶段加入 SQLite。

建议 SQLite 表：

```sql
CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  created_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  status TEXT,
  geomturbo_path TEXT,
  config_path TEXT,
  workdir TEXT,
  autogrid_command TEXT,
  exit_code INTEGER,
  error TEXT
);

CREATE TABLE quality_reports (
  job_id TEXT PRIMARY KEY,
  parser TEXT,
  ok INTEGER,
  raw_path TEXT,
  json_path TEXT,
  negative_cells INTEGER,
  expansion_ratio_min REAL,
  expansion_ratio_max REAL,
  aspect_ratio_min REAL,
  aspect_ratio_max REAL,
  orthogonality_min REAL,
  orthogonality_max REAL,
  nb_levels INTEGER,
  passed INTEGER
);
```

---

## 10. CLI 设计

### 10.1 `agmesh run`

执行完整网格生成。

```bash
agmesh run \
  --geomTurbo /work/design001.geomTurbo \
  --config /work/config.yml \
  --out /work/agmesh_jobs
```

可选参数：

```bash
agmesh run \
  --geomTurbo case.geomTurbo \
  --config config.yml \
  --set rows.0.flow_paths=89 \
  --set rows.0.cell_width_at_wall=5e-6 \
  --set autogrid.threads=16
```

输出：

```text
Job ID: 20260701-153011-9f3e2c
Status: DONE
Mesh: /work/agmesh_jobs/20260701-153011-9f3e2c/output/mesh.igg
Project: /work/agmesh_jobs/20260701-153011-9f3e2c/output/project.trb
Quality: /work/agmesh_jobs/20260701-153011-9f3e2c/output/quality.json
```

### 10.2 `agmesh quality`

查询作业质量结果。

```bash
agmesh quality /work/agmesh_jobs/20260701-153011-9f3e2c
```

示例输出：

```text
Job: 20260701-153011-9f3e2c
Quality report: output/mesh.qualityReport
Parser: AutoGridTextParser
Thresholds: PASSED

Summary:
  negative_cells: 0
  expansion_ratio_max: 3.21
  aspect_ratio_max: 3840.0
  orthogonality_min: 18.7
  nb_levels: 3
```

### 10.3 `agmesh parse-report`

只解析已有 `.qualityReport`。

```bash
agmesh parse-report output/mesh.qualityReport --json
```

### 10.4 `agmesh probe`

检测 AutoGrid 版本和 API 能力。

```bash
agmesh probe --executable igg --niversion 171 --out probe_report.json
```

### 10.5 `agmesh render-script`

只生成内层脚本和 runtime config，不运行 AutoGrid。

```bash
agmesh render-script \
  --geomTurbo case.geomTurbo \
  --config config.yml \
  --out dryrun/
```

用于调试配置和审查脚本。

---

## 11. 网格生成流程状态机

```text
CREATED
  ↓
VALIDATING_CONFIG
  ↓
PREPARING_JOB_DIR
  ↓
WRITING_RUNTIME_CONFIG
  ↓
RENDERING_AG5_WORKER
  ↓
RUNNING_AUTOGRID
  ├── configure_preferences
  ├── init_from_geomturbo
  ├── configure_global
  ├── configure_rows
  ├── initialize_topology
  ├── generate_flow_paths
  ├── generate_b2b
  ├── save_project_before_3d
  ├── generate_3d
  ├── save_mesh
  ├── exports
  └── collect_metrics
  ↓
PARSING_QUALITY_REPORT
  ↓
CHECKING_THRESHOLDS
  ↓
WRITING_MANIFEST
  ↓
DONE / FAILED / DONE_WITH_QUALITY_FAILURE
```

状态文件：`output/ag5_status.json`

```json
{
  "ok": false,
  "stage": "generate_3d",
  "error": "cannot compute intersection on all layers",
  "traceback": "...",
  "started_at": 1782939011.0,
  "finished_at": 1782939320.0
}
```

---

## 12. 质量报告查询设计

### 12.1 质量数据来源

质量查询使用两个来源：

1. `.qualityReport` 文件：AutoGrid 生成后的正式质量报告；
2. `metrics.json`：内层脚本通过 `generation_success()`、`calc_row_meridional_mesh_quality()`、`calc_row_2D_mesh_quality()` 等函数写出的辅助结构化指标。

`.qualityReport` 是最终判断来源；`metrics.json` 用于补充诊断。

### 12.2 质量指标模型

```json
{
  "ok": true,
  "source": "output/mesh.qualityReport",
  "parser": "AutoGridGenericTextParser",
  "summary": {
    "negative_cells": 0,
    "expansion_ratio": {"min": 1.0, "max": 3.2},
    "aspect_ratio": {"min": 1.0, "max": 3840.0},
    "orthogonality": {"min": 18.7, "max": 89.9},
    "nb_levels": 3
  },
  "entities": [
    {
      "name": "ROW_1",
      "type": "row",
      "negative_cells": 0,
      "expansion_ratio": {"min": 1.0, "max": 3.2},
      "aspect_ratio": {"min": 1.0, "max": 3840.0},
      "orthogonality": {"min": 18.7, "max": 89.9},
      "nb_levels": 3
    }
  ],
  "thresholds": {
    "passed": true,
    "violations": []
  }
}
```

### 12.3 阈值判定

```yaml
quality:
  thresholds:
    negative_cells_max: 0
    max_aspect_ratio: 5000
    min_orthogonality: 15
    max_expansion_ratio: 5.0
```

判定结果示例：

```json
{
  "passed": false,
  "violations": [
    {
      "scope": "ROW_1",
      "metric": "negative_cells",
      "actual": 12,
      "expected": "<= 0"
    },
    {
      "scope": "ROW_1",
      "metric": "orthogonality_min",
      "actual": 8.5,
      "expected": ">= 15"
    }
  ]
}
```

### 12.4 `.qualityReport` 解析器开发策略

由于不同 AutoGrid 版本的 `.qualityReport` 文本格式可能变化，解析器采用多级策略：

```text
1. AutoGrid17TextParser：针对 v17.1 样本格式。
2. AutoGrid12TextParser：针对公开 12.2 手册描述和样本格式。
3. GenericRegexParser：关键词和数字提取。
4. RawOnlyParser：保留原始文件，结构化指标为空。
```

第一阶段实现 `GenericRegexParser`，拿到本机 v17.1 的真实 `.qualityReport` 样本后再固化 `AutoGrid17TextParser`。

### 12.5 CLI 查询选项

```bash
# 简洁摘要
agmesh quality jobs/<job_id>

# 输出 JSON
agmesh quality jobs/<job_id> --json

# 只看某个 row
agmesh quality jobs/<job_id> --entity ROW_1

# 查看阈值违规
agmesh quality jobs/<job_id> --violations

# 查看原始 report
agmesh quality jobs/<job_id> --raw
```

---

## 13. 错误处理策略

| 失败类型 | 检测方式 | 状态 | 处理建议 |
|---|---|---|---|
| AutoGrid executable 不存在 | `shutil.which()` / `Path.exists()` | `CONFIG_ERROR` | 提示路径错误 |
| license 不可用 | stderr 关键字 + 非零返回码 | `LICENSE_ERROR` | 提示检查 license server |
| `.geomTurbo` 不存在 | 外层校验 | `CONFIG_ERROR` | 不启动 AutoGrid |
| `.geomTurbo` 无法初始化 | 内层 `init_from_geomturbo` 异常 | `GEOMETRY_ERROR` | 保存 stdout/stderr 和 traceback |
| row 数不匹配 | `a5_get_row_number()` vs config | `CONFIG_ERROR` | 提示修改 rows 配置 |
| RowWizard 失败 | `configure_rows` 异常 | `MESH_SETUP_ERROR` | 提示 row index 和参数 |
| flow paths 失败 | `generate_flow_paths` 异常 | `MESH_GENERATION_ERROR` | 保留 project.trb |
| B2B 失败 | `generate_b2b` 异常 | `MESH_GENERATION_ERROR` | 保存 status 和日志 |
| 3D 失败 | `a5_start_3d_generation()` 异常 | `MESH_GENERATION_ERROR` | 读取 `generation_error_message()` |
| mesh 未保存 | 缺少 `.igg` | `OUTPUT_ERROR` | 标记运行失败 |
| qualityReport 缺失 | 文件检查 | `QUALITY_REPORT_MISSING` | 默认失败，可配置降级 |
| 阈值不通过 | `quality.json` 判定 | `DONE_WITH_QUALITY_FAILURE` | 网格生成完成但质量失败 |
| parser 失败 | 异常 | `QUALITY_PARSE_WARNING` | 使用 RawOnlyParser |

---

## 14. 日志与可复现性

每个 job 必须保存：

```text
logs/command.txt
logs/autogrid.stdout.log
logs/autogrid.stderr.log
logs/environment.json
generated/ag5_worker.py
generated/ag5_runtime_config.json
generated/normalized_config.json
output/ag5_status.json
output/metrics.json
output/quality.json
output/manifest.json
```

`manifest.json` 示例：

```json
{
  "job_id": "20260701-153011-9f3e2c",
  "created_at": "2026-07-01T15:30:11-07:00",
  "input_geomturbo_sha256": "...",
  "config_sha256": "...",
  "autogrid_command": "igg -niversion 171 -autogrid5 -batch -script ...",
  "exit_code": 0,
  "artifacts": {
    "mesh_igg": "output/mesh.igg",
    "project_trb": "output/project.trb",
    "quality_report": "output/mesh.qualityReport",
    "quality_json": "output/quality.json"
  }
}
```

---

## 15. 测试计划

### 15.1 单元测试

```text
- YAML/JSON 配置解析
- 枚举映射
- b2b_layers 范围校验
- OpenFOAM branch 校验
- job 目录创建
- script_renderer 输出稳定性
- runner 命令构造
- quality parser 对样本文本的解析
- threshold checker
```

### 15.2 集成测试

需要 AutoGrid5 运行环境和 license。

```text
- 最小单 row .geomTurbo → .igg
- 离心叶轮 tip gap off
- 离心叶轮 tip gap on
- 轴流压气机 rotor
- rotor + stator 双 row
- 不同 flow_paths 参数对结果文件影响
- 3D generation 失败场景
- qualityReport 缺失场景
```

### 15.3 回归样本集

建议维护：

```text
samples/
  impeller_simple/
    case.geomTurbo
    config.yml
    expected_quality_keys.json
  axial_rotor/
    case.geomTurbo
    config.yml
  axial_stage/
    case.geomTurbo
    config.yml
```

不建议把商用或涉密几何纳入公开测试仓库。

---

## 16. 部署与运行环境

### 16.1 Python 环境

外层：

```text
Python 3.10+
PyYAML
Pydantic v2
click 或 typer
rich，可选
pytest
```

内层：

```text
AutoGrid5 自带 Python 环境，按公开手册旧版本为 Python 2.7；v17.1 以本机安装为准。
```

### 16.2 AutoGrid 环境

需要：

```text
- NUMECA / Cadence AutoGrid5 已安装。
- igg / igg171 / iggx86_64.exe 命令可运行。
- license 可用。
- 工作目录具有读写权限。
- Linux batch 环境建议具备显示环境；real-batch 默认关闭，因为用户手册提示 real-batch 下 Python script 生成可能失败。
```

---

## 17. 开发里程碑

### M0：环境与能力探测

交付物：

```text
agmesh probe
agmesh doctor
probe_report.json
```

功能：

```text
- 检查 AutoGrid executable。
- 检查 batch script 是否能启动。
- 检查关键 API 是否存在。
- 检查 license 是否可用。
```

验收：

```bash
agmesh doctor --executable igg --niversion 171
```

输出 OK，并生成 `probe_report.json`。

### M1：CLI MVP

交付物：

```text
agmesh run
job 目录创建
runtime_config.json
ag5_worker.py
AutoGrid subprocess 调用
stdout/stderr 保存
```

验收：

```bash
agmesh run --geomTurbo sample.geomTurbo --config sample.yml --out jobs/
```

能完成启动、配置和日志保存；若 AutoGrid 失败，也能明确定位失败阶段。

### M2：完整 `.geomTurbo → .igg` 自动生成

交付物：

```text
- 从 .geomTurbo 初始化项目
- RowWizard 参数化配置
- topology / flow paths / B2B / 3D 生成
- 自动保存 .trb 和 .igg
```

验收：

```text
output/project.trb 存在
output/mesh.igg 存在
output/ag5_status.json 中 ok=true
```

### M3：质量报告解析与查询

交付物：

```text
agmesh parse-report
agmesh quality
quality.json
threshold checker
```

验收：

```bash
agmesh quality jobs/<job_id>
```

能输出 negative cells、expansion ratio、aspect ratio、orthogonality/skewness、Nb levels 等信息；若解析失败，保留 raw report 并给出 warning。

### M4：多 row 与参数覆盖

交付物：

```text
- 多 row 配置
- 每个 row 不同 machine_type / row_type / periodicity / rotation_speed
- gap / fillet 参数
- streamwise_weight
- optimization 参数
```

验收：

```text
rotor + stator 样例可生成 .igg；各 row 参数正确写入日志和 status。
```

### M5：导出格式与工程化

交付物：

```text
- CGNS_format
- Fluent export
- OpenFOAM export
- SQLite 作业索引，可选
- 更完善的 report parser
```

验收：

```text
启用 exports 后，对应输出文件/目录存在；quality 查询不依赖前端。
```

---

## 18. 第一版建议实现范围

第一版聚焦稳定的后端命令行：

```text
输入：
  - .geomTurbo
  - config.yml

支持参数：
  - units
  - number_of_grid_levels
  - machine_type
  - row_type
  - periodicity
  - rotation_speed
  - grid_level
  - flow_paths
  - cell_width_at_wall
  - number_of_meshed_passages
  - hub/tip gap on/off
  - hub/tip gap width at LE/TE
  - hub/tip fillet on/off
  - b2b_layers
  - quality thresholds

输出：
  - project.trb
  - mesh.igg
  - mesh.qualityReport
  - ag5_status.json
  - metrics.json
  - quality.json
  - manifest.json
  - stdout/stderr logs

CLI：
  - agmesh run
  - agmesh quality
  - agmesh parse-report
  - agmesh probe
  - agmesh render-script
```

第二版再加入：

```text
- streamwise weights
- optimization controls
- span interpolation
- CGNS / Fluent / OpenFOAM export
- SQLite 索引
- 更多 technological effects
- 更细的 block/entity 质量解析
```

---

## 19. 推荐验收标准

### 功能验收

```text
1. 给定单 row .geomTurbo 和 config.yml，能够生成 .igg。
2. 3D 生成前自动保存 .trb。
3. 生成后能找到 .qualityReport。
4. 能输出结构化 quality.json。
5. 能执行质量阈值判定。
6. AutoGrid 失败时能给出失败阶段和日志路径。
7. 不需要用户预先准备 .trb。
```

### 可复现验收

```text
1. job 目录包含原始输入、归一化配置、AutoGrid 脚本、日志和输出。
2. manifest 中包含命令行、hash、输出路径。
3. 重复运行同一输入和同一配置，目录结构一致，关键输出存在。
```

### 工程验收

```text
1. Python 单元测试覆盖配置、脚本渲染、命令构造、质量解析。
2. 支持本地 dry-run，不启动 AutoGrid 也能审查 generated/ag5_worker.py。
3. 支持 probe 检测 AutoGrid API 能力。
4. 所有路径处理兼容 Linux；Windows 路径在传给 AutoGrid 内层脚本时转换为 / 风格。
```

---

## 20. 风险与应对

| 风险 | 说明 | 应对 |
|---|---|---|
| AutoGrid v17.1 API 与 12.2 手册差异 | 部分函数可能改名或行为变化 | 实现 `agmesh probe`，关键调用 `try/except`，以本机 `_python/_autogrid/` 文档为最终准据 |
| `.qualityReport` 格式差异 | 不同版本报告文本格式可能不同 | 先做 GenericRegexParser，再用 v17.1 样本固化专用 parser |
| real-batch 下 Python 脚本不稳定 | 用户手册提示 real-batch 下 Python mesh generation 可能失败 | 默认 `real_batch=false`，必要时单独验证 |
| `.geomTurbo` row 语义不足 | 多 row、gap、tip clearance 需显式配置 | 配置文件强制写 row 参数，row 数 mismatch 直接失败 |
| 质量阈值行业差异 | 不同求解器/工况对指标要求不同 | 阈值完全配置化，不在代码中硬编码 |
| 商业 license 限制并发 | AutoGrid 可能受 license token 限制 | 第一版不做并发；后续如需要，再加本地进程锁 |
| Windows 路径问题 | AutoGrid 脚本手册要求文件路径用 `/` | 外层统一转换传入内层 JSON 的路径 |

---

## 21. 资料来源

1. Cadence, “Fidelity CFD Platform” — Fidelity Autogrid Mesh Generation 相关产品说明：结构网格、mesh templates、Python API、自动点分布、smoothing、多级配置等。  
   https://www.cadence.com/en_US/home/tools/system-analysis/computational-fluid-dynamics/fidelity.html

2. Cadence, “Fidelity CFD Pre-Processing and Meshing” — 自动化网格、高质量网格、skewness、non-orthogonality、adjacent volume ratio 等质量目标说明。  
   https://www.cadence.com/en_US/home/tools/system-analysis/computational-fluid-dynamics/pre-processing-meshing.html

3. AutoGrid5 User Guide, public mirror of IGG™/AutoGrid5™ 12.2 User Guide — Python script、batch script、`.geomTurbo` 初始化、网格生成 API、`.qualityReport` 等章节。  
   https://www.scribd.com/document/430288512/AutoGrid5-User-Guide

4. CFturbo Manual, “Fidelity AutoGrid” — `.geomTurbo` 文件可由 AutoGrid 加载，多叶排模型与 tip clearance 处理说明。  
   https://manual.cfturbo.com/en/numeca_autogrid5.html

5. Cadence Community, “Autogrid Mesh to openFoam” — 指向 Cadence support 中 AUTOGRID manual 的官方支持路径，并涉及 OpenFOAM 导出/质量相关讨论。  
   https://community.cadence.com/cadence_technology_forums/computational-fluid-dynamics/f/flow/62631/autogrid-mesh-to-openfoam

---

## 22. 一句话结论

本项目应实现为 **纯后端 Python CLI 工具 `agmesh`**：外层 Python 3 管理配置、作业、日志和质量查询；内层 AutoGrid5 Python 脚本从 `.geomTurbo` 初始化项目、按配置生成结构网格、保存 `.trb`/`.igg` 并产出 `.qualityReport`。这样既不依赖预先存在的 `.trb` 模板，也能保留 AutoGrid 官方脚本能力、质量报告机制和工程可复现性。
