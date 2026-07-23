# AutoGrid 17.1 网格控制目录与 API 审计

本文档说明当前已实现的控制模型。完整、可执行的唯一参数来源是项目根目录 `controls.py` 中的静态 `ControlSpec` 注册表；本文不维护第二份容易失步的手工键清单。

## 实现边界

当前只开放“不改变物理几何”的网格生成控制，包括拓扑选择、点数、分布、聚集、边界层、优化、接口以及已有技术效果的网格离散控制。

明确排除：

- 周期数、转速、通道数量和物理项目类型；
- gap/partial-gap/fillet 的物理宽度、半径和几何位置；
- 入口、出口、端壁和远场的物理域位置；
- 曲面链接、输入几何替换、几何修复或变形；
- 实体创建、删除、复制和重命名；
- 仅显示、交互选择、demo、legacy 空操作和重复别名。

程序不接受任意 AutoGrid 方法名，也不提供 `.trb` 字符串覆盖入口。

## 注册表契约

每个 `ControlSpec` 都包含：

| 字段 | 含义 |
|---|---|
| `key` | 稳定语义键 |
| `scope` / `target_kind` / `hierarchy` | 作用域、运行时对象和选择器层级 |
| `value_type` | `bool`、`int`、`float`、`enum`、整数元组或浮点元组 |
| `minimum` / `maximum` / `enum_values` | 已知数值边界或合法枚举 |
| `si_length` | 是否以米作为 CLI 输入单位 |
| `priority` | P0/P1/P2 使用优先级 |
| `stage` | 固定应用阶段 |
| `setter` / `setter_by_value` | 正式 AutoGrid 17.1 API |
| `getter` | 可选回读 API；为空表示 setter 无异常即视为已应用 |
| `topologies` | 适用 B2B 拓扑；不匹配时严格失败 |
| `not_applicable_when` | 已知不适用条件说明 |

当前注册表共有 341 个控制键：P0 10 个、P1 59 个、P2 272 个。

## P0：高频基础控制

| 控制键 | 作用域 | 含义 |
|---|---|---|
| `configuration/grid_levels` | configuration | 全局多重网格层级 |
| `row/mesh_level` | row | coarse/medium/fine/user 网格级别 |
| `row/target_points` | row | user 级别目标点数 |
| `row/flow_path.number` | row | 叶排 flow path 数 |
| `row/optimization.steps` | row | 普通优化步数 |
| `row/optimization.gap_steps` | row | gap 优化步数 |
| `wizard/grid_level` | row wizard | RowWizard 网格级别 |
| `wizard/first_cell_width` | row wizard | 首层单元宽度，输入单位为米 |
| `wizard/spanwise_paths` | row wizard | RowWizard 展向 flow paths 数 |
| `gap/spanwise_points` | 已有 gap | gap 展向点数 |

其中高频项还具有 `mesh.py` 的显式 CLI 参数；其余控制统一通过 `--set` 使用。

## P1：常用精细控制

P1 共 59 项，覆盖：

- 流向权重和 span interpolation；
- hub/shroud 点聚集与流向分布；
- Default B2B 点数及表面流向点数；
- 边界层点数、厚度、膨胀率和增长率；
- 前缘/尾缘尺度和聚集；
- skewness、orthogonality、wake、high-stagger 优化控制；
- row-to-row matching、interface 相对位置和 matching 精度。

查询完整目录：

```powershell
python mesh.py --list-controls P1
python mesh.py --describe-control blade/b2b.default.streamwise_inlet_points
```

## P2：高级与已有实体控制

P2 共 272 项，覆盖：

- Default、HOH、H&I B2B 拓扑及其完整点数/聚集控制；
- gap 与 partial-gap 拓扑、点数和优化控制；
- 高级入口、出口、喉部、重叠、声学和远场控制；
- fillet、snubber、endwall、blade sheet 和 solid-body 的纯网格控制；
- 已存在孔、端壁孔、针肋、basin hole 和 ZR/3D 技术效果的点数、聚集与优化控制。

这些控制不会创建技术效果实体。选择器指向不存在的实体或不适用的拓扑时，AutoGrid 阶段会返回失败，不会静默忽略。

查询完整目录：

```powershell
python mesh.py --list-controls P2
python mesh.py --describe-control blade/b2b.hoh.wake_control
```

## 路径与选择器

控制表达式格式为：

```text
<实体选择器路径>/<局部键>=<值>
```

示例：

```text
configuration/grid_levels=3
row:*/optimization.steps=200
row:diffuser_axial/flow_path.number=89
row:#2/blade:#1/b2b.default.streamwise_inlet_points=33
row:Rotor/blade:Main Blade/gap:shroud/spanwise_points=17
```

规则：

1. `*` 表示 wildcard，`#N` 表示从 1 开始的索引，其他文本表示区分大小写的实体名。
2. 名称包含 `/` 或 `=` 时必须改用索引。
3. 精确选择器优先于 wildcard；参数顺序不影响解析结果。
4. 同一选择器、同一键重复出现直接报错。
5. 静态几何未携带可靠数量的已有技术效果，在 AutoGrid 内通过正式数量/accessor API 再做严格确认。

## 类型与单位

控制值不会执行任意 Python：

- 布尔：`true`、`false`、`1`、`0`；
- 整数和有限浮点数；
- 注册表中列出的精确枚举；
- 逗号分隔的定长整数/浮点元组。

长度参数始终按米输入。转换公式为：

```text
project_value = requested_si / units_factor
```

请求值、项目单位值、传给 API 的值和 getter 回读值均进入 `run_summary.json`。

## 应用阶段

控制顺序不依赖命令行书写顺序，而由注册表阶段确定：

```text
configuration
→ wizard
→ RowWizard.generate()
→ topology
→ distribution
→ boundary_layer
→ optimization
→ interface
→ existing_effect
→ B2B/3D 网格生成
```

这可避免 RowWizard 覆盖后置的拓扑、点数和优化设置。

## AutoGrid 17.1 setter 审计

审计针对本机正式文件：

```text
C:\ProgramData\NUMECA\fine171\_python\_autogrid\Autogrid.py
```

`audit_autogrid_source()` 按“所属类 + 方法名”逐个检查所有 `set_*` 和 `a5_set_*` 定义。当前结果：

| 状态 | 数量 | 含义 |
|---|---:|---|
| `mapped` | 361 | 映射到至少一个规范控制键 |
| `excluded` | 360 | 有明确静态理由或受控规则排除 |
| `unaudited` | 0 | 不允许存在 |
| 合计 | 721 | AutoGrid 17.1 setter 定义数 |

`audit_control_bindings()` 反向验证注册表引用的方法确实存在于正确的 17.1 对象上。当前 703 个绑定中，701 个是正式 API 方法，2 个是对 `RSInterface` enable/disable 成对接口的受控布尔适配；缺失绑定为 0。

审计可复现：

```powershell
python -c "from collections import Counter; from controls import audit_autogrid_source; p=r'C:\ProgramData\NUMECA\fine171\_python\_autogrid\Autogrid.py'; print(Counter(x['status'] for x in audit_autogrid_source(p)))"
```

精确排除理由保存在 `EXCLUDED_SETTERS` 和 `AUDIT_EXCLUSION_RULES`；测试要求每个 17.1 setter 必须是 `mapped` 或 `excluded`，并要求注册表不存在缺失 setter/getter。

## 严格失败策略

以下情况返回静态错误码 2，且不启动 IGG：

- 未知控制键；
- 类型、枚举或范围错误；
- 选择器层级错误或几何实体未匹配；
- 重复定义或等优先级冲突；
- `--no-row-wizard` 与 wizard 控制同时出现；
- 长度控制缺少有效 `UNITS-FACTOR`。

以下情况在 AutoGrid 阶段返回错误码 1，并记录失败控制：

- 实体在 AutoGrid 项目中不存在；
- 控制不适用于当前拓扑；
- 17.1 setter/getter 或实体 accessor 缺失；
- setter、getter 或网格生成抛出异常。

dry-run 只在 `controls.resolved` 中记录 `planned`，不会把控制写成 `applied`。
