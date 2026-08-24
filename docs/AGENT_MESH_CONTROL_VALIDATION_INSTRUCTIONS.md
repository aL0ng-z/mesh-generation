# 交给后续 AGENT 的完整网格控制验证指令

下面的指令可直接交给后续 AGENT 执行。它授权进行测试和生成测试产物，不授权修复生产实现。

---

## 完整指令

你是一名精通压气机、涡轮数值仿真和 NUMECA AutoGrid 17.1 的资深网格工程师。你的任务是在当前仓库中，对从 `.geomTurbo` 自动生成 CFD 网格的全部控制参数执行真实、可审计的有效性和质量敏感性验证。

### 一、最终目标

对当前 `src/controls.py` 注册的 341 个控制键逐项回答：

1. API 映射、对象、调用签名、类型、单位、阶段和依赖是否正确；
2. 在适用几何和实体上，真实 AutoGrid 调用是否成功；
3. getter 回读是否与预期一致；
4. 保存并重开 `.trb` 后设置是否持久；
5. 最终网格的 block、点数、坐标分布或直接目标量是否发生预期变化；
6. AutoGrid 网格质量指标是否出现超过基线噪声的变化；
7. 该参数属于有效、无效果、失败、不适用，还是因缺少 fixture/环境而阻塞。

最终不得只报告“setter 存在”或“命令成功”。必须建立：

```text
API/签名
→ 真实调用
→ 回读
→ TRB 持久化
→ 最终网格变化
→ 质量指标变化
```

证据链。

### 二、先读文件

开始前完整阅读：

```text
AGENTS.md
docs/MESH_CONTROL_AUDIT_AND_TEST_PLAN.md
src/controls.py
src/autogrid.py
src/mesh.py
src/geomturbo.py
src/quality.py
tests/test_controls.py
tests/test_autogrid.py
tests/test_mesh.py
tests/test_quality.py
docs/MESH_CONTROL_ITEMS.md
docs/QUALITY_CRITERIA.md
```

同时静态核对：

```text
C:\ProgramData\NUMECA\fine171\_python\_autogrid\Autogrid.py
C:\ProgramData\NUMECA\fine171\_python\_igg\PYTHON.py
```

### 三、硬性约束

1. 完全忽略 `archive/`：不读取、不搜索、不修改、不运行。
2. 不使用 Git worktree，不新建分支，不提交，除非用户另行明确要求。
3. 先执行 `git status --short`，保留所有用户已有改动；不得覆盖、还原或顺手整理无关文件。
4. 生产实现保持不变：
   - 不修改 `src/mesh.py`
   - 不修改 `src/controls.py`
   - 不修改 `src/autogrid.py`
   - 不修改 `src/geomturbo.py`
   - 不修改 `src/quality.py`
5. 如需自动化，可新增最小化 test-only 脚本或测试文件，但不得引入包级子目录、JSON/YAML 配置或第三方依赖。
6. 项目和测试工具只使用 Python 标准库、本机 NUMECA/IGG API 和系统已有命令。
7. 如新增或修改任何代码，必须用中文更新 `docs/DevLog.md`。
8. 所有说明、日志摘要、报告和面向用户输出使用中文。
9. 所有可丢弃产物写入新的：

   ```text
   runs/control-validation/<campaign_id>/
   ```

   不覆盖现有 `runs/` 目录。
10. 真实 IGG 测试串行执行；不要并发占用许可证。
11. dry-run 和 mock 只能用于预检，不能作为控制有效的最终证据。
12. 不为让测试通过而改生产代码。发现缺陷时保留最小复现、日志和产物，并记录建议；没有用户授权不得修复。

### 四、已知静态风险，必须先验证

在全量 campaign 前优先处理以下审计和复现：

1. 当前 API 审计只覆盖 721 个 `set_*` / `a5_set_*`，没有完整覆盖本机 API 中的 39 个 `enable_*`、21 个 `disable_*`、13 个 `unset_*`、11 个 `compute_*` 和 14 个 `generate*`。
2. 至少复核以下遗漏的纯网格逻辑控制：
   - stagnation-point expansion-ratio 模式；
   - stagnation-point desired expansion ratio；
   - endwall multigrid optimization；
   - holes-line / endwall-holes-line / pin-fins-line 的孔内和孔周 skewness 开关。
3. 复现同次设置：

   ```text
   blade/b2b.topology=hoh
   blade/b2b.hoh.<任一控制>=...
   ```

   确认 topology-specific 控制是否因排序先于 topology setter 而失败。
4. 验证当前 `applied` 是否会在 getter 回读与请求值不一致时仍被标记成功。
5. 对未知数量实体测试 wildcard；确认它是否错误展开 `#1..#99` 并在超过真实数量后失败。
6. 验证 pin-fins 解析硬编码 `pinFinsChannel(1)` 的覆盖限制。
7. 对 acoustic、LETE、endwall、technological effect 验证 setter 后是否必须显式调用相应 compute/generate。
8. 验证 `not_applicable_when` 当前是否只有说明作用。

上述结果先写入：

```text
runs/control-validation/<campaign_id>/preflight-findings.md
```

### 五、建立完整静态 API 清单

不要直接调用现有 `audit_autogrid_source()` 并把结果当成完整结论。建立扩展静态审计，至少枚举：

```text
set_*
a5_set_*
enable_*
disable_*
unset_*
所有接受值但不符合上述命名的公共方法
compute_*
generate*
```

逐方法输出 `source-audit.csv`，字段至少包括：

```text
owner
method
signature
kind
getter
mapped_control_keys
classification
reason
activation_for
applicable_entity
dependency
source_line
```

`classification` 只能是：

```text
mapped
excluded
activation
unknown
```

要求：

- `unknown` 最终为 0，或逐项写明无法判断的确切阻塞；
- 正则只能初筛，不能直接作为最终排除理由；
- 检查注册表 setter/getter 的所属类和签名，而不只是名称存在；
- 检查 `setter_mode`、enum 映射、SI 单位、stage、topology 和 mode 依赖；
- 找出 getter 需要参数、setter 需要多个参数但当前只传一个值的情况；
- 找出非标准命名 setter，例如 `desired_expansion_ratio(value)`。

### 六、建立 applicability matrix

先读取并静态解析：

```text
geometries/Rotor37.geomTurbo
geometries/WP100_comp.geomTurbo
geometries/ori1.geomTurbo
```

然后在真实 AutoGrid 项目中只做实体清点，记录每个 fixture 实际拥有的：

```text
row
blade/splitter
gap
partial-gap
fillet
interface
endwall
snubber
blade-sheet
stagnation-point
holes-line
endwall-holes-line
basin-hole
cooling channel
pin-fins-line
solid-body
LETE wizard
acoustic wizard
ZR/3D technological effect
bypass/bulb/far-field
B2B topology
```

生成：

```text
runs/control-validation/<campaign_id>/applicability.csv
```

每个控制键对每个 fixture 标记：

```text
READY
NOT_APPLICABLE
BLOCKED_NO_FIXTURE
BLOCKED_API_DEPENDENCY
```

缺少实体时不得伪造几何，不得把“目标不存在”记作控制 FAIL。形成精确的缺失 fixture 需求清单。

### 七、实现 test-only 最终网格探针

不要以输出文件存在、大小或时间戳作为网格变化证据。

在 test-only 脚本中，于真实 `a5_generate_3d()` 完成后使用本机正式 IGG API 采集：

```text
num_of_blocks()
block(i).get_name()
block(i).get_size()
block(i).grid_point(i, j, k)
```

每个 block 至少记录：

- 名称；
- I/J/K 点数；
- 8 个角点；
- 中心点；
- 每个方向 1/4、1/2、3/4 的固定组合抽样点；
- 首末两层和壁面法向相邻点；
- 质量报告最差 I/J/K 邻域的点。

同时记录：

- 总 block 数；
- 总节点数；
- 按结构网格尺寸推导的总单元数；
- block signature；
- 坐标探针的规范化 SHA-256；
- `.qualityReport` 全局与逐实体指标；
- 输入文件、API 源码和生成脚本 SHA-256；
- AutoGrid 版本、命令、环境和工作树状态。

若 A/A 运行证明 `.cgns` 字节稳定，可附加 CGNS SHA-256；否则它只能作为辅助字段，不能作为主判据。

### 八、A/A 重复性

在参数敏感性测试前，对三份现有 fixture 各做两次完全相同的默认真实生成：

```text
Rotor37 A1 / A2
WP100 A1 / A2
ori1 A1 / A2
```

比较：

- block inventory；
- I/J/K；
- 节点/单元数；
- 坐标探针；
- 所有质量指标；
- 最差位置；
- 生成耗时。

若数值不完全一致，增加第三次 A3。为每个连续指标计算 A/A 标准差，并采用：

```text
T_metric = max(
    5 × A/A 标准差,
    2 × 报告打印分辨率,
    工程最小变化量
)
```

所有后续“有变化”结论必须超过对应阈值。

### 九、逐项测试协议

每个控制独立建立目录：

```text
cases/<safe_control_id>/<fixture>/<variant>/
```

对每个 READY 控制执行：

1. 读取 matched baseline 默认值；
2. 选择安全且明显不同的测试值；
3. 只设置该控制；确有必要的模式/拓扑控制列入 `dependency_controls`；
4. 调用 setter；
5. 立即 getter 回读；
6. 生成 flow paths、B2B、3D 网格；
7. 再次 getter 回读；
8. 保存 `.trb`；
9. 新开一个 IGG 进程重开 `.trb` 并第三次回读；
10. 采集最终网格探针和 `.qualityReport`；
11. 与 matched baseline 比较；
12. 按证据层和最终状态判定。

值选择：

- bool：与默认相反，并测试切回；
- enum：至少一个非默认值，最终覆盖全部合法枚举；
- 点数：约为默认的 0.75× 和 1.25×，同时满足真实拓扑/多重网格约束；
- 优化步数：0 与一个足够大的非零值；
- 比例/权重：默认两侧的明显安全值；
- 绝对长度：0.5× 与 2×；
- tuple：一次只变一个分量；
- mode/topology：先应用模式，再应用依赖值。

不得把非法极值导致的生成失败当成参数无效；先确认测试值本身合法。

### 十、执行顺序

严格按以下门控执行：

#### Batch 0：preflight

- 扩展 API 审计；
- 8 项已知风险复现；
- applicability matrix；
- test-only mesh probe；
- 三个 fixture 的 A/A。

#### Batch 1：P0

逐项验证全部 10 个 P0。未全部完成 L4 前不要进入 P1。

特别注意：

- `row/target_points` 以 `row/mesh_level=user` 为依赖；
- `wizard/first_cell_width` 检查 wall distance 和 m/mm 单位；
- optimization steps 不要求点数变化；
- gap 控制只对真实 gap；
- configuration grid levels 同时检查质量报告 grid levels。

#### Batch 2：P1

按族串行：

1. wizard；
2. row flow-path/distribution；
3. row optimization；
4. default B2B 点数；
5. default B2B boundary-layer；
6. gap；
7. interface。

每族先做 1 个 pilot，确认探针对该类控制敏感，再执行该族其余控制。

#### Batch 3：P2 READY

按 topology、gap/partial-gap/fillet、interface、endwall、技术效果等族分批。只运行 applicability 为 READY 的组合。

#### Batch 4：交互

至少测试：

```text
wizard/grid_level × row/mesh_level
wizard/spanwise_paths × row/flow_path.number
wizard/first_cell_width × blade wall width
optimization steps × optimization mode/weight
B2B topology × topology-specific control
interface shape × shape-specific value
wildcard × exact selector
```

### 十一、readback 与最终状态

readback 规则：

- bool/int：归一化后精确一致；
- enum：API 数值映射回公共枚举后精确一致；
- float/长度：使用经说明的绝对/相对容差；
- 无 getter：明确标记 `NO_GETTER`，不能自动记 L2。

每个控制最终只能取以下状态之一：

```text
PASS_EFFECT_AND_QUALITY
PASS_EFFECT_ONLY
PASS_CALL_ONLY
FAIL_READBACK
FAIL_GENERATION
NO_EFFECT
BLOCKED_NO_FIXTURE
BLOCKED_ENVIRONMENT
NOT_APPLICABLE
```

证据等级：

```text
L0 STATIC_OK
L1 CALL_OK
L2 READBACK_OK
L3 PERSIST_OK
L4 EFFECT_OK
L5 QUALITY_SENSITIVE
L6 QUALITY_IMPROVABLE
L7 CFD_VERIFIED
```

“真实可用”至少 L4；“影响网格质量”至少 L5。  
不要把 `PASS_CALL_ONLY` 写成“参数有效”。

### 十二、质量判据

至少比较：

```text
mesh validity
overlapping status
negative cells
number of points
grid levels
minimum skewness angle
minimum spanwise skewness angle
maximum expansion ratio
maximum spanwise expansion ratio
maximum aspect ratio
wall distance min/max/avg/uniformity
每项最差 block 与 I/J/K
```

解释方向：

- skewness angle 最小值通常越大越好；
- spanwise skewness angle 最小值通常越接近 180°越好；
- expansion ratio 最大值越接近 1 越好；
- aspect ratio 必须结合边界层用途，不能机械判定；
- wall distance 用于目标符合度，不是越小越好；
- 任何负体积、overlap 或 invalid mesh 都是硬失败。

`QUALITY_SENSITIVE` 要求变化超过 A/A 阈值。  
`QUALITY_IMPROVABLE` 要求至少三个水平表现出可重复改善方向或最优区间，且不能以其他硬门槛失效为代价。

### 十三、单位专项

至少完成：

1. meter 项目长度输入；
2. millimeter 项目物理等价输入；
3. requested SI → project value → API value → getter 四级核对；
4. 最终物理长度/壁距等价性比较；
5. `interface/z_cst` 与 `interface/r_cst` 单位语义核对；
6. absolute / relative / normalized distance 分类复核；
7. 0 长度、负长度和极小长度的静态/运行时拒绝行为。

### 十四、资源与持续执行

完成 Batch 0 和 Batch 1 后，统计：

- 每个 fixture 平均/最大运行时间；
- 每次运行平均/最大磁盘占用；
- IGG/许可证失败率；
- 预计 P1/P2 总运行数、总耗时、总磁盘。

把估算写入 `campaign-summary.md`，然后继续按批次执行。不要因为全量规模大而停止；只有许可证、磁盘、缺少 fixture 或重复环境故障才可将对应项标记为阻塞。

如果出现同一环境故障：

1. 保留完整 stdout/stderr；
2. 用最小默认算例确认是否与参数无关；
3. 最多重试一次；
4. 环境仍失败则标记 `BLOCKED_ENVIRONMENT`，不要把所有参数记成 FAIL。

### 十五、产物要求

运行产物：

```text
runs/control-validation/<campaign_id>/
  environment.md
  preflight-findings.md
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

持久化中文报告：

```text
docs/MESH_CONTROL_VALIDATION_RESULTS.md
```

报告必须包含：

1. 环境、版本和源码指纹；
2. 341 个注册键逐项状态；
3. 扩展 API 审计发现的遗漏/排除项；
4. L0-L7 数量统计；
5. PASS/FAIL/NO_EFFECT/BLOCKED 数量；
6. 每个失败的命令、值、目标和最小复现目录；
7. 每个质量敏感参数的 baseline/variant/差值/阈值；
8. 缺失 fixture 的精确需求；
9. 已确认生产缺陷和建议优先级；
10. 哪些参数可以继续对用户宣称可用，哪些不可以；
11. 不夸大结论：明确区分调用成功、网格变化、质量变化和 CFD 验证。

### 十六、代码和 Git 纪律

- 测试代码遵循最小改动原则；
- 不引入 JSON 配置或包级子目录；
- 不安装第三方库；
- 新增/修改测试代码后更新 `docs/DevLog.md`；
- 不修改生产实现；
- 不提交 Git，除非用户另行明确要求；
- 若用户要求提交，commit 消息必须使用中文 Conventional Commits。

### 十七、完成标准

只有同时满足以下条件才算完成：

1. 完整扩展 API 审计已交付；
2. 341 个现有控制键均有唯一最终状态；
3. 所有 READY 控制均完成真实 IGG 证据链；
4. 所有 BLOCKED 控制均有精确阻塞原因和 fixture 需求；
5. 已知 8 项静态风险均有复现结论；
6. 所有结果可从运行目录追溯；
7. 中文持久报告完成；
8. 未修改生产代码、未触碰 archive、未破坏用户已有改动。

完成后向用户先报告结论和关键数字，再给出报告与 campaign 目录的绝对路径链接。

---

## 说明

该指令面向“执行验证”的后续任务。本次核对没有执行其中任何测试。
