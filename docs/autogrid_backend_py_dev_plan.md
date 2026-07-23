# 旧 AutoGrid 后端开发方案（已被取代）

状态：**已废止，仅保留文件名用于历史链接兼容。**

原方案中的包级目录、JSON/YAML 配置、模板化工作流、作业数据库和扩展 CLI 设想，已被当前根目录的扁平、无配置、无模板实现取代，不再作为开发依据。

当前有效实现与文档为：

- `mesh.py`：单次运行 CLI、Schema v2 摘要和中文报告；
- `controls.py`：AutoGrid 17.1 类型化控制注册表和 setter 审计；
- `autogrid.py`：正式 AutoGrid 17.1 Python API 脚本与严格执行；
- `quality.py`：完整 `.qualityReport` 状态机和兼容质量判定；
- `docs/MESH_CONTROL_ITEMS.md`：当前控制契约；
- `docs/QUALITY_CRITERIA.md`：当前质量 Schema 与硬门槛；
- `docs/DevLog.md`：当前开发记录。

当前实现不读取 JSON/YAML 参数文件，不依赖 `.trb` 模板，不创建包级子目录，也不实现自动调参、DOE、求解器调用、`y+` 或网格无关性闭环。
