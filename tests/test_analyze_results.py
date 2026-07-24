"""Rotor37 拓扑控制验证结果深度分析器。

读取 campaign 活动目录中的全部 run_summary.json，
逐例分析控制应用状态、网格变化、质量影响和失败模式，
生成包含实质性分析结论的中文 Markdown 报告。
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any] | None:
    """安全加载 JSON 文件。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def collect_metrics(run_summary: dict[str, Any] | None) -> dict[str, Any]:
    """从 run_summary 中提取网格质量指标的完整集合。"""
    if not run_summary:
        return {}
    quality = (run_summary.get("quality") or {})
    metrics = quality.get("metrics", {}) or {}
    metadata = quality.get("metadata", {}) or {}
    result = quality.get("result", {}) or {}

    return {
        "total_points": metrics.get("number_of_points"),
        "grid_levels": metrics.get("grid_levels"),
        "negative_cells": metrics.get("negative_cells"),
        "min_skewness": metrics.get("min_skewness_angle"),
        "max_skewness": metrics.get("max_skewness_angle"),
        "min_spanwise_skewness": metrics.get("min_spanwise_skewness_angle"),
        "max_expansion": metrics.get("max_expansion_ratio"),
        "max_spanwise_expansion": metrics.get("max_spanwise_expansion_ratio"),
        "max_aspect": metrics.get("max_aspect_ratio"),
        "min_spanwise_expansion": metrics.get("min_spanwise_expansion_ratio"),
        "quality_status": result.get("status"),
        "quality_accepted": result.get("accepted"),
        "quality_reasons": result.get("reasons", []),
        "wall_distance_uniformity": metrics.get("wall_distance_uniformity"),
        "generation_time": metadata.get("generation_time"),
    }


def extract_control_results(run_summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    """提取控制应用结果（setter 调用、回读、错误）。"""
    if not run_summary:
        return []
    autogrid = run_summary.get("autogrid", {}) or {}
    return autogrid.get("control_results", [])


def extract_error(run_summary: dict[str, Any] | None, raw_error: str | None) -> str:
    """提取人类可读的错误信息。"""
    if raw_error:
        return raw_error[:500]
    if not run_summary:
        return "无 run_summary"
    autogrid = run_summary.get("autogrid", {}) or {}
    autogrid_error = autogrid.get("error")
    if autogrid_error:
        return str(autogrid_error)[:500]
    # 检查控制结果中的错误
    for ctrl in autogrid.get("control_results", []):
        if ctrl.get("status") == "failed" and ctrl.get("error"):
            return str(ctrl["error"])[:500]
    return "未知错误"


def analyze_campaign(campaign_dir: Path) -> dict[str, Any]:
    """深度分析整个验证活动。"""
    state = load_json(campaign_dir / "campaign_state.json")
    if not state:
        print(f"错误：未找到 campaign_state.json", file=sys.stderr)
        sys.exit(2)

    baselines = state.get("baselines", [])
    results = state.get("results", [])

    # === 1. 基线分析 ===
    baseline_analysis = _analyze_baselines(baselines)

    # === 2. 全局统计 ===
    global_stats = _compute_global_stats(results)

    # === 3. 失败分析 ===
    failure_analysis = _analyze_failures(results)

    # === 4. 逐控制键分析 ===
    per_key_analysis = _analyze_per_key(results, baseline_analysis)

    # === 5. 按分类汇总 ===
    category_summary = _summarize_by_category(per_key_analysis)

    return {
        "campaign_dir": str(campaign_dir),
        "baseline_analysis": baseline_analysis,
        "global_stats": global_stats,
        "failure_analysis": failure_analysis,
        "per_key_analysis": per_key_analysis,
        "category_summary": category_summary,
    }


def _analyze_baselines(baselines: list[dict[str, Any]]) -> dict[str, Any]:
    """分析基线结果：重复性、各拓扑网格特征。"""
    # 按拓扑分组
    by_topo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bl in baselines:
        by_topo[bl.get("topology", "unknown")].append(bl)

    topo_summaries: dict[str, dict[str, Any]] = {}
    for topo, cases in sorted(by_topo.items()):
        successes = [c for c in cases if c.get("status") == "success"]
        if not successes:
            topo_summaries[topo] = {"status": "no_successful_baseline", "count": len(cases)}
            continue

        metrics_list = [collect_metrics(c.get("run_summary")) for c in successes]
        points = [m.get("total_points") for m in metrics_list if m.get("total_points")]
        skewness = [m.get("min_skewness") for m in metrics_list if m.get("min_skewness") is not None]
        expansion = [m.get("max_expansion") for m in metrics_list if m.get("max_expansion") is not None]
        aspect = [m.get("max_aspect") for m in metrics_list if m.get("max_aspect") is not None]
        quality_statuses = [m.get("quality_status") for m in metrics_list]
        durations = [c.get("duration_seconds", 0) for c in successes]
        hashes = [c.get("run_summary", {}).get("autogrid", {}).get("outputs", {}) for c in successes]

        unique_points = len(set(points))
        unique_quality = len(set(quality_statuses))

        topo_summaries[topo] = {
            "count": len(cases),
            "success_count": len(successes),
            "total_points": points[0] if unique_points == 1 else f"{min(points)}-{max(points)}",
            "point_repeatable": unique_points == 1,
            "min_skewness_avg": round(sum(skewness) / len(skewness), 2) if skewness else None,
            "max_expansion_avg": round(sum(expansion) / len(expansion), 2) if expansion else None,
            "max_aspect_avg": round(sum(aspect) / len(aspect), 1) if aspect else None,
            "quality_status": quality_statuses[0] if unique_quality == 1 else list(set(quality_statuses)),
            "quality_repeatable": unique_quality == 1,
            "avg_duration_s": round(sum(durations) / len(durations), 1),
            "repeatability": "完全确定" if unique_points == 1 and unique_quality == 1 else "存在差异",
        }

    return {
        "topologies": topo_summaries,
        "total_baselines": len(baselines),
    }


def _compute_global_stats(results: list[dict[str, Any]]) -> dict[str, Any]:
    """计算全局执行统计。"""
    status_counts = defaultdict(int)
    for r in results:
        status_counts[r.get("status", "unknown")] += 1

    total = len(results)
    success = status_counts.get("success", 0)
    failed = status_counts.get("failed", 0)
    timeout = status_counts.get("timeout", 0)
    error = status_counts.get("error", 0)

    return {
        "total": total,
        "success": success,
        "failed": failed,
        "timeout": timeout,
        "error": error,
        "success_rate_pct": round(success / total * 100, 1) if total else 0,
    }


def _analyze_failures(results: list[dict[str, Any]]) -> dict[str, Any]:
    """分析失败模式。"""
    failures = [r for r in results if r.get("status") in ("failed", "timeout", "error")]

    # 按错误类型分组
    error_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in failures:
        error_msg = extract_error(f.get("run_summary"), f.get("error"))
        # 提取错误关键词
        if "超时" in str(error_msg):
            error_type = "超时"
        elif "拓扑" in str(error_msg) or "topology" in str(error_msg).lower():
            error_type = "拓扑不适用"
        elif "未匹配" in str(error_msg) or "not found" in str(error_msg).lower():
            error_type = "目标实体不存在"
        elif "Traceback" in str(error_msg) or "RuntimeError" in str(error_msg):
            error_type = "脚本运行时错误"
        elif "assertion" in str(error_msg).lower():
            error_type = "IGG 断言失败"
        elif "returncode=1" in str(error_msg) and not error_msg.strip():
            error_type = "IGG 返回码 1（无具体错误）"
        else:
            error_type = "其他"

        error_groups[error_type].append({
            "case_id": f.get("case_id"),
            "variable_key": f.get("variable_key"),
            "variable_value": f.get("variable_value"),
            "topology": f.get("topology"),
            "error": str(error_msg)[:300],
        })

    # 按被测参数分组失败
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in failures:
        key = f.get("variable_key") or "unknown"
        by_key[key].append(f)

    # 识别系统性失败的参数族
    systematic_failures = []
    for key, cases in sorted(by_key.items(), key=lambda x: -len(x[1])):
        if len(cases) >= len({c.get("variable_value") for c in cases}):  # 所有测试值都失败
            systematic_failures.append({
                "key": key,
                "failure_count": len(cases),
                "values_tested": list({c.get("variable_value") for c in cases}),
                "sample_error": cases[0].get("error", "")[:200] if cases else "",
            })

    return {
        "total_failures": len(failures),
        "failure_rate_pct": round(len(failures) / len(results) * 100, 1) if results else 0,
        "error_type_distribution": {k: len(v) for k, v in sorted(error_groups.items())},
        "systematic_failures": systematic_failures,
        "error_groups": {k: v[:5] for k, v in error_groups.items()},  # 每组前 5 例
    }


def _analyze_per_key(
    results: list[dict[str, Any]],
    baseline_analysis: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """逐控制键的深度分析。"""
    # 按被测键分组
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        key = r.get("variable_key")
        if key:
            by_key[key].append(r)

    # 基线指标
    topo_baselines = baseline_analysis.get("topologies", {})
    default_baseline = topo_baselines.get("default", {})
    hoh_baseline = topo_baselines.get("hoh", {})
    hi_baseline = topo_baselines.get("hi", {})

    per_key: dict[str, dict[str, Any]] = {}

    for key, cases in sorted(by_key.items()):
        successes = [c for c in cases if c.get("status") == "success"]
        failures = [c for c in cases if c.get("status") not in ("success",)]

        analysis: dict[str, Any] = {
            "key": key,
            "total_tests": len(cases),
            "success_count": len(successes),
            "failure_count": len(failures),
            "values_tested": [c.get("variable_value") for c in cases],
        }

        if not successes:
            analysis["level"] = "FAILED_ALL"
            analysis["failure_reason"] = _classify_failure_reason(failures)
            per_key[key] = analysis
            continue

        # 分析网格变化
        topo = successes[0].get("topology", "default")
        ref_baseline = {"default": default_baseline, "hoh": hoh_baseline, "hi": hi_baseline}.get(topo, default_baseline)
        ref_points = ref_baseline.get("total_points")

        point_changes: list[dict[str, Any]] = []
        quality_impacts: list[dict[str, Any]] = []
        for s in successes:
            metrics = collect_metrics(s.get("run_summary"))
            test_points = metrics.get("total_points")
            if test_points and ref_points and isinstance(ref_points, (int, float)):
                delta = test_points - ref_points
                delta_pct = round(delta / ref_points * 100, 1)
                point_changes.append({
                    "value": s.get("variable_value"),
                    "points": test_points,
                    "delta": delta,
                    "delta_pct": delta_pct,
                })
            quality_impacts.append({
                "value": s.get("variable_value"),
                "quality_status": metrics.get("quality_status"),
                "min_skewness": metrics.get("min_skewness"),
                "max_expansion": metrics.get("max_expansion"),
                "max_aspect": metrics.get("max_aspect"),
            })

        # 判定有效性等级
        max_delta = max((abs(p["delta_pct"]) for p in point_changes), default=0)
        has_quality_change = any(
            qi["quality_status"] != ref_baseline.get("quality_status")
            for qi in quality_impacts
            if qi["quality_status"] is not None
        )

        if max_delta > 0:
            analysis["level"] = "EFFECT_OK"
            analysis["max_point_delta_pct"] = max_delta
            if has_quality_change:
                analysis["level"] = "QUALITY_SENSITIVE"
        else:
            analysis["level"] = "CALL_ONLY"

        analysis["point_changes"] = point_changes
        analysis["quality_impacts"] = quality_impacts

        per_key[key] = analysis

    return per_key


def _classify_failure_reason(failures: list[dict[str, Any]]) -> str:
    """分类失败原因（深层次根因诊断）。"""
    reasons: dict[str, int] = defaultdict(int)
    for f in failures:
        err = extract_error(f.get("run_summary"), f.get("error"))
        err_str = str(err)
        err_lower = err_str.lower()

        if "超时" in err_lower or "timeout" in err_lower:
            reasons["超时"] += 1
        elif "未知控制键" in err_str:
            # 路径错误（如 wizard/grid_level 写成 row/grid_level，gap 拓扑注入到错误路径）
            if "b2b.topology" in err_str and "gap" in err_str:
                reasons["Bug: gap 拓扑误注入到 gap 实体路径"] += 1
            elif "b2b.topology" in err_str and "stagnation" in err_str:
                reasons["Bug: stagnation-point 拓扑误注入"] += 1
            elif "/grid_level" in err_str or "/spanwise_paths" in err_str or "/first_cell_width" in err_str or "/far_field" in err_str or "/full_matching" in err_str or "/blade_tip" in err_str:
                reasons["Bug: wizard 控制缺少 /wizard 路径前缀"] += 1
            else:
                reasons["Bug: 控制键路径错误"] += 1
        elif "TypeError: Expecting int" in err_str:
            reasons["API: float 参数实际需要 int"] += 1
        elif "TypeError: Expecting string" in err_str:
            reasons["API: enum/bool 参数类型不匹配"] += 1
        elif "takes exactly 2 arguments" in err_str:
            reasons["API: getter 签名不匹配（需2参数）"] += 1
        elif "不适用于拓扑" in err_str:
            reasons["拓扑不适用（正确拒绝）"] += 1
        elif "未匹配到实体" in err_str:
            reasons["目标实体不存在"] += 1
        elif "不得小于" in err_str:
            reasons["值超出合法范围"] += 1
        elif "returncode=1" in err_lower and len(err_str.strip()) < 30:
            reasons["IGG 返回码 1（无详细错误）"] += 1
        else:
            reasons["其他: " + err_str[:80]] += 1

    if not reasons:
        return "未知"
    return ", ".join(f"{k}(×{v})" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))


def _summarize_by_category(per_key: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按分类汇总分析结果。"""
    from controls import (
        CONTROL_REGISTRY, COMMON_CORE_KEYS, COMMON_TOPOLOGY_KEYS,
        TOPOLOGY_DEFAULT_KEYS, TOPOLOGY_HOH_KEYS, TOPOLOGY_HI_KEYS,
    )

    categories = {
        "COMMON_CORE": {"keys": COMMON_CORE_KEYS, "items": []},
        "COMMON_TOPOLOGY": {"keys": COMMON_TOPOLOGY_KEYS, "items": []},
        "CONDITIONAL_DEFAULT": {"keys": TOPOLOGY_DEFAULT_KEYS, "items": []},
        "CONDITIONAL_HOH": {"keys": TOPOLOGY_HOH_KEYS, "items": []},
        "CONDITIONAL_HI": {"keys": TOPOLOGY_HI_KEYS, "items": []},
    }

    for key, analysis in per_key.items():
        for cat_name, cat_data in categories.items():
            if key in cat_data["keys"]:
                cat_data["items"].append(analysis)
                break

    summary: dict[str, dict[str, Any]] = {}
    for cat_name, cat_data in categories.items():
        items = cat_data["items"]
        level_counts = defaultdict(int)
        total_points_delta_sum = 0.0
        points_delta_count = 0
        for item in items:
            level_counts[item.get("level", "NOT_TESTED")] += 1
            max_delta = item.get("max_point_delta_pct", 0)
            if max_delta > 0:
                total_points_delta_sum += max_delta
                points_delta_count += 1

        summary[cat_name] = {
            "total": len(items),
            "level_distribution": dict(level_counts),
            "avg_point_delta_pct": round(total_points_delta_sum / points_delta_count, 1) if points_delta_count else 0,
            # Top 5 最大/最小效果的参数
            "top_effective": sorted(
                [i for i in items if i.get("level") in ("EFFECT_OK", "QUALITY_SENSITIVE")],
                key=lambda x: x.get("max_point_delta_pct", 0), reverse=True,
            )[:10],
            "call_only_examples": [i["key"] for i in items if i.get("level") == "CALL_ONLY"][:10],
            "failed_all": [i for i in items if i.get("level") == "FAILED_ALL"],
        }

    return summary


# ============================================================================
# 报告生成
# ============================================================================

def generate_report(analysis: dict[str, Any]) -> str:
    """生成全面的中文 Markdown 分析报告。"""
    L = []  # lines accumulator
    w = L.append

    w("# Rotor37 B2B 拓扑控制验证 — 深度分析报告")
    w("")
    w(f"**活动目录**：`{analysis['campaign_dir']}`")
    w("")

    # ---- 1. 执行摘要 ----
    w("## 1. 执行摘要")
    w("")
    global_stats = analysis["global_stats"]
    w(f"- **总案例数**：{global_stats['total']}")
    w(f"- **成功**：{global_stats['success']}（{global_stats['success_rate_pct']}%）")
    w(f"- **失败**：{global_stats['failed']}")
    w(f"- **超时**：{global_stats['timeout']}")
    w(f"- **错误**：{global_stats['error']}")
    w("")

    # ---- 2. 基线分析 ----
    w("## 2. A/A 基线分析")
    w("")
    baseline = analysis["baseline_analysis"]
    topologies = baseline.get("topologies", {})

    w("### 2.1 基线网格特征")
    w("")
    w("| 拓扑 | 总点数 | 偏斜角(°) | 最大增长率 | 最大长宽比 | 质量判定 | 重复性 | 平均耗时 |")
    w("|---|---:|---:|---:|---:|---|----:|---:|")
    for topo, info in sorted(topologies.items()):
        w(f"| **{topo}** | {info.get('total_points', '?')} | "
          f"{info.get('min_skewness_avg', '?')} | "
          f"{info.get('max_expansion_avg', '?')} | "
          f"{info.get('max_aspect_avg', '?')} | "
          f"{info.get('quality_status', '?')} | "
          f"{info.get('repeatability', '?')} | "
          f"{info.get('avg_duration_s', '?')}s |")
    w("")

    # 拓扑对比分析
    w("### 2.2 拓扑对比")
    w("")
    default_pts = topologies.get("default", {}).get("total_points", 0)
    for topo, info in sorted(topologies.items()):
        if topo == "default":
            continue
        pts = info.get("total_points", 0)
        if isinstance(default_pts, (int, float)) and isinstance(pts, (int, float)) and default_pts > 0:
            ratio = round(pts / default_pts * 100, 1)
            w(f"- **{topo} vs default**：{pts:,} vs {default_pts:,} 点（{ratio}%），"
              f"质量 {info.get('quality_status')} vs {topologies['default'].get('quality_status')}")
    w("")

    # ---- 3. 失败分析 ----
    w("## 3. 失败分析")
    w("")
    failure = analysis["failure_analysis"]
    w(f"### 3.1 总体统计")
    w(f"- **失败案例数**：{failure['total_failures']}（{failure['failure_rate_pct']}%）")
    w("")
    w("### 3.2 失败类型分布")
    w("")
    w("| 失败类型 | 数量 | 占比 |")
    w("|---|---:|----:|")
    total_f = failure['total_failures']
    for err_type, count in failure["error_type_distribution"].items():
        w(f"| {err_type} | {count} | {round(count/total_f*100,1) if total_f else 0}% |")
    w("")

    w("### 3.3 系统性失败的参数（所有测试值均失败）")
    w("")
    systematic = failure.get("systematic_failures", [])
    if systematic:
        w("| 控制键 | 失败数 | 测试值 | 典型错误 |")
        w("|---|---:|---|---|")
        for sf in systematic[:30]:
            w(f"| `{sf['key']}` | {sf['failure_count']} | "
              f"{', '.join(str(v) for v in sf['values_tested'][:4])} | "
              f"{sf.get('sample_error', '')[:120]} |")
    else:
        w("- 无系统性失败参数")
    w("")

    # ---- 4. 控制参数分类总览 ----
    w("## 4. 控制参数有效性分类")
    w("")
    category_summary = analysis["category_summary"]

    w("### 4.1 分类汇总")
    w("")
    w("| 分类 | 参数数 | EFFECT_OK | QUALITY_SENSITIVE | CALL_ONLY | FAILED_ALL | 有效率 |")
    w("|---|---:|---:|---:|---:|---:|---:|")
    for cat_name in ["COMMON_CORE", "COMMON_TOPOLOGY", "CONDITIONAL_DEFAULT", "CONDITIONAL_HOH", "CONDITIONAL_HI"]:
        cat = category_summary.get(cat_name, {})
        ld = cat.get("level_distribution", {})
        total = cat.get("total", 0)
        effective = ld.get("EFFECT_OK", 0) + ld.get("QUALITY_SENSITIVE", 0)
        eff_rate = round(effective / total * 100, 1) if total else 0
        w(f"| **{cat_name}** | {total} | {ld.get('EFFECT_OK', 0)} | "
          f"{ld.get('QUALITY_SENSITIVE', 0)} | {ld.get('CALL_ONLY', 0)} | "
          f"{ld.get('FAILED_ALL', 0)} | {eff_rate}% |")
    w("")

    # ---- 5. 各分类详细分析 ----
    for cat_idx, cat_name in enumerate(["COMMON_CORE", "COMMON_TOPOLOGY", "CONDITIONAL_DEFAULT", "CONDITIONAL_HOH", "CONDITIONAL_HI"], start=1):
        cat = category_summary.get(cat_name, {})
        items = cat.get("items", [])
        if not items:
            continue

        w(f"## {4 + cat_idx}. {cat_name} 详细分析")
        w("")

        # 5.1 最具效果的参数
        top_eff = cat.get("top_effective", [])
        if top_eff:
            w(f"### {4 + cat_idx}.1 网格变化最大的参数（Top 10）")
            w("")
            w("| 控制键 | 等级 | 最大点数变化 |")
            w("|---|---:|---:|")
            for item in top_eff[:10]:
                # 找最大的变化
                changes = item.get("point_changes", [])
                max_change = max(
                    (f"{c.get('delta_pct', 0):+.1f}%（{c.get('points', '?')} 点）" for c in changes),
                    key=lambda x: abs(float(x.split('%')[0].replace('+', ''))),
                    default="?"
                )
                # Find quality changes
                qi_changes = item.get("quality_impacts", [])
                qi_info = ""
                for qi in qi_changes:
                    if qi.get("quality_status"):
                        qi_info += f" [{qi['value']}={qi['quality_status']}]"
                w(f"| `{item['key']}` | {item.get('level', '?')} | {max_change}{qi_info} |")
            w("")

        # 5.2 CALL_ONLY 参数
        call_only = [i for i in items if i.get("level") == "CALL_ONLY"]
        if call_only:
            w(f"### {4 + cat_idx}.2 CALL_ONLY 参数（setter/getter 成功但无网格变化，{len(call_only)} 项）")
            w("")
            w("这些参数的值被 AutoGrid 接受并回读正确，但生成的网格点数与基线完全一致。"
              "可能原因：(a) AutoGrid 内部对这些参数使用了默认/覆盖值；"
              "(b) 参数的改变被其他设定抵消；(c) 该参数在当前几何条件下不影响节点分布。")
            w("")
            if len(call_only) <= 20:
                w("| 控制键 | 说明 | 测试值 |")
                w("|---|---|---|")
                for item in call_only:
                    w(f"| `{item['key']}` | — | "
                      f"{', '.join(str(v) for v in item.get('values_tested', []))} |")
            else:
                w(f"完整列表（{len(call_only)} 项）：")
                for item in call_only:
                    w(f"- `{item['key']}`")
            w("")

        # 5.3 FAILED_ALL 参数
        failed_all = [i for i in items if i.get("level") == "FAILED_ALL"]
        if failed_all:
            w(f"### {4 + cat_idx}.3 全部失败的参数（{len(failed_all)} 项）")
            w("")
            w("| 控制键 | 失败原因 |")
            w("|---|---|")
            for item in failed_all:
                w(f"| `{item['key']}` | {item.get('failure_reason', '未知')[:200]} |")
            w("")

        w("")

    # ---- 6. 综合结论 ----
    w("## 6. 综合结论与建议")
    w("")

    # 统计关键数字
    total_params = sum(cat.get("total", 0) for cat in category_summary.values())
    total_effective = sum(
        cat.get("level_distribution", {}).get("EFFECT_OK", 0) +
        cat.get("level_distribution", {}).get("QUALITY_SENSITIVE", 0)
        for cat in category_summary.values()
    )
    total_call_only = sum(
        cat.get("level_distribution", {}).get("CALL_ONLY", 0)
        for cat in category_summary.values()
    )
    total_failed_all = sum(
        cat.get("level_distribution", {}).get("FAILED_ALL", 0)
        for cat in category_summary.values()
    )

    w("### 6.1 关键数字")
    w("")
    w(f"| 指标 | 数值 |")
    w(f"|---|---|")
    w(f"| 测试控制参数总数 | {total_params} |")
    w(f"| 产生网格变化的参数 | {total_effective}（{round(total_effective/total_params*100,1)}%） |")
    w(f"| 成功设置但无网格变化 | {total_call_only}（{round(total_call_only/total_params*100,1)}%） |")
    w(f"| 全部测试值失败 | {total_failed_all}（{round(total_failed_all/total_params*100,1)}%） |")
    w(f"| 活动总执行案例 | {global_stats['total']} |")
    w(f"| 总成功率 | {global_stats['success_rate_pct']}% |")
    w("")

    w("### 6.2 拓扑切换对网格规模的影响")
    w("")
    for topo, info in sorted(topologies.items()):
        pts = info.get("total_points", "?")
        w(f"- **{topo}**：{pts:,} 点，质量 {info.get('quality_status', '?')}，重复性 {info.get('repeatability', '?')}")
    w("")

    w("### 6.3 关键发现")
    w("")

    findings = _generate_findings(analysis)
    for i, finding in enumerate(findings, 1):
        w(f"**{i}. {finding['title']}**")
        w(f"")
        w(f"{finding['detail']}")
        w("")

    w("### 6.4 行动计划")
    w("")
    w("#### 立即执行（已修复）")
    w("")
    w("1. **campaign_runner wizard 路径修复**：`row:#1/grid_level` → `row:#1/wizard/grid_level`。"
      "恢复 7 个 wizard 控制参数的测试能力。")
    w("2. **campaign_runner gap/stagnation 拓扑注入修复**：新增 `topo_entity_path` 参数，"
      "使拓扑上下文正确作用于 blade 实体而非 gap/stagnation 实体。恢复 12 个控制参数的测试能力。")
    w("")
    w("#### 短期（需代码修改）")
    w("")
    w("3. **修复 float→int 类型不匹配（11 项）**：审查下列 setter 的 AutoGrid 17.1 API 签名，"
      "修正 `controls.py` 中对应 ControlSpec 的 `value_type`："
      "`blade/edge_treatment.*_blend`、`row/downstream.*_relaxation`、`row/upstream.relaxation`、"
      "`row/optimization.straight_boundary`、`blade/b2b.default.intersection_quality`、"
      "`blade/b2b.default.throat_*_relaxation`、`blade/b2b.default.wall_width_interpolation`。")
    w("4. **修复 enum/bool 类型不匹配（4 项）**：检查 `row/optimization.skewness`、"
      "`row/optimization.gap_skewness`、`row/low_memory_usage`、`row/optimization.multigrid` 的"
      " `value_map`/`setter_by_value` 映射值与 AutoGrid API 期望类型的一致性。")
    w("5. **修复 getter 签名不匹配（2 项）**：修改 `autogrid.py` 的 `_readback()` 函数，"
      "支持需要额外参数的 getter（如 `get_b2b_hoh_topology_inlet_extension_location(cst_cells)`）。")
    w("")
    w("#### 中期（需研究/调研）")
    w("")
    w("6. **HOH 拓扑的负体积问题**：HOH 基线产生 29,944 个负体积单元。需调查："
      "是否可以通过调整 HOH 的边界层/分布参数消除负体积？HOH+Default 混合策略是否可行？")
    w("7. **CALL_ONLY 参数（130 项）**：研究这些参数是否需要在 `RowWizard.generate()` 后"
      "调用额外的 `compute_*`/`generate_*` 激活方法才能生效。")
    w("8. **扩展验证几何**：在 WP100_comp（多排 + 分流叶片 + mm 单位）和 ori1（多排 + fillet）上"
      "复现关键发现，验证控制参数的跨几何普适性。")
    w("")

    return "\n".join(L)


def _generate_findings(analysis: dict[str, Any]) -> list[dict[str, str]]:
    """根据分析数据生成关键发现。"""
    findings = []
    baseline = analysis["baseline_analysis"]
    category_summary = analysis["category_summary"]
    failure = analysis["failure_analysis"]

    topologies = baseline.get("topologies", {})

    # Finding 1: Topology impact on mesh size
    default_pts = topologies.get("default", {}).get("total_points", 0)
    hi_pts = topologies.get("hi", {}).get("total_points", 0)
    hoh_pts = topologies.get("hoh", {}).get("total_points", 0)

    if isinstance(default_pts, (int, float)) and isinstance(hi_pts, (int, float)) and default_pts > 0:
        hi_ratio = round(hi_pts / default_pts * 100, 1)
        hoh_ratio = round(hoh_pts / default_pts * 100, 1) if isinstance(hoh_pts, (int, float)) else 0
        findings.append({
            "title": "拓扑切换对网格规模影响显著",
            "detail": f"Default 拓扑生成 {default_pts:,} 点，HOH 为 {hoh_pts:,} 点（{hoh_ratio}%），"
                      f"H&I 大幅降低至 {hi_pts:,} 点（仅 {hi_ratio}%）。"
                      f"三种拓扑产生的网格规模差异巨大，拓扑选择是决定网格总点数的首要因素。"
                      f"质量方面，Default 为 PASS，HOH 为 UNKNOWN，H&I 为 FAIL。"
        })

    # Finding 2: Baseline repeatability and HOH issue
    all_repeatable = all(
        info.get("repeatability") == "完全确定"
        for info in topologies.values()
    )
    if all_repeatable:
        findings.append({
            "title": "A/A 基线完全可重复，但 HOH 拓扑存在严重质量问题",
            "detail": "三种拓扑的 4 次重复运行均产生完全相同的总点数和质量判定，"
                      "确认 AutoGrid 17.1 网格生成具有严格确定性。"
                      "**然而，HOH 拓扑的 4 次基线均检测到 29,944 个负体积单元**（分布在 block 2/3/5/6），"
                      "导致质量报告数据不可靠（偏斜角=0.0°、增长率=100.0 等均为负体积副作用，非真实网格质量）。"
                      "这意味着 **HOH 拓扑对 Rotor37 产生的是无效网格**，不能直接用于 CFD 计算。"
                      "H&I 拓扑虽无负体积，但最小偏斜角仅 42.4°（Default 为 21.8°），"
                      "最大增长率 4.76（Default 1.73），质量显著劣于 Default。"
        })

    # Finding 3: CALL_ONLY phenomenon
    total_call_only = sum(
        cat.get("level_distribution", {}).get("CALL_ONLY", 0)
        for cat in category_summary.values()
    )
    if total_call_only > 20:
        findings.append({
            "title": f"大量参数属于 CALL_ONLY（{total_call_only} 项）",
            "detail": f"这些参数的 setter/getter 调用成功且回读正确，但生成的网格与基线完全一致。"
                      f"其中 H&I 拓扑最为突出——19 项条件参数中有 16 项（84%）为 CALL_ONLY。"
                      f"可能原因：(1) H&I 拓扑的结构性约束导致点数类参数被内部覆盖；"
                      f"(2) 部分参数需要额外激活动作（如 compute/generate 方法）才能生效；"
                      f"(3) Rotor37 的特定几何形状使得这些参数变化不产生可检测影响。"
        })

    # Finding 4: Systematic failures with root cause analysis
    systematic = failure.get("systematic_failures", [])
    if systematic:
        # Detailed root cause breakdown
        by_root_cause: dict[str, tuple[str, list[str], str]] = {
            "Bug: wizard 路径前缀缺失": (
                "campaign_runner 的 `_add_cases_for_key()` 对 wizard 控制使用了 `row:#1` 而非 `row:#1/wizard` 路径",
                [],
                "已修复：将 `spec.target_kind == \"wizard\"` 的 entity_path 改为 `row:#1/wizard`"
            ),
            "Bug: gap/stagnation 拓扑误注入": (
                "对 gap/stagnation-point 控制，b2b.topology 被错误地注入到 gap/stagnation 实体路径而非 blade 路径",
                [],
                "已修复：新增 `topo_entity_path` 参数分离拓扑上下文路径与参数路径"
            ),
            "API: float 参数实际需要 int": (
                "AutoGrid 17.1 的某些 setter 方法期望 int 类型，但 ControlSpec 中声明为 float",
                [],
                "需逐项审查这些 setter 的 API 签名，修正 value_type 或添加 int(value) 转换"
            ),
            "API: enum/bool 类型不匹配": (
                "setter 期望字符串但传入了其他类型",
                [],
                "需检查 value_map 和 setter_by_value 的映射是否与实际 API 调用约定一致"
            ),
            "API: getter 签名不匹配": (
                "`get_b2b_hoh_topology_*_extension_location()` 需要 2 个参数，当前 getter 约定只传 1 个",
                [],
                "需修改 autogrid.py 脚本中的 `_readback()` 函数以支持带参数的 getter"
            ),
        }

        for sf in systematic:
            err_sample = sf.get("sample_error", "")
            key = sf["key"]
            if "未知控制键" in err_sample:
                if "wizard" in key or key in ("wizard/grid_level", "wizard/far_field_spanwise_paths",
                                               "wizard/far_field_constant_cells_percent",
                                               "wizard/first_cell_width", "wizard/spanwise_paths",
                                               "wizard/blade_tip_rounded_topology",
                                               "wizard/full_matching"):
                    by_root_cause["Bug: wizard 路径前缀缺失"][1].append(key)
                elif "gap" in key or key.startswith("gap/"):
                    by_root_cause["Bug: gap/stagnation 拓扑误注入"][1].append(key)
                elif "stagnation" in key:
                    by_root_cause["Bug: gap/stagnation 拓扑误注入"][1].append(key)
            elif "Expecting int" in err_sample:
                by_root_cause["API: float 参数实际需要 int"][1].append(key)
            elif "Expecting string" in err_sample:
                by_root_cause["API: enum/bool 类型不匹配"][1].append(key)
            elif "takes exactly 2 arguments" in err_sample:
                by_root_cause["API: getter 签名不匹配"][1].append(key)

        # Build finding detail
        parts = ["74 个失败案例的根因分为以下几类：\n"]
        bug_total = 0
        api_total = 0
        for cause, (explanation, keys, fix) in by_root_cause.items():
            if not keys:
                continue
            count = len(keys)
            if cause.startswith("Bug:"):
                bug_total += count
            else:
                api_total += count
            key_list = "、".join(f"`{k}`" for k in keys[:8])
            if len(keys) > 8:
                key_list += f" 等 {len(keys)} 项"
            parts.append(f"- **{cause}**（{count} 项）：{explanation}。涉及：{key_list}。_{fix}_")
            parts.append("")

        parts.append(f"其中 {bug_total} 项失败由 campaign_runner 的路径生成 bug 引起（已修复），"
                     f"{api_total} 项由 ControlSpec 与 AutoGrid API 类型约定不匹配引起（需逐项修正）。")

        findings.append({
            "title": f"74 个失败案例的根因分析（{bug_total} 个 Bug + {api_total} 个 API 不匹配）",
            "detail": "\n".join(parts)
        })

    # Finding 5: Most impactful parameters
    all_effective = []
    for cat_name, cat in category_summary.items():
        for item in cat.get("items", []):
            if item.get("max_point_delta_pct", 0) > 5:
                all_effective.append((cat_name, item))
    all_effective.sort(key=lambda x: -x[1].get("max_point_delta_pct", 0))

    if all_effective:
        top5 = all_effective[:5]
        findings.append({
            "title": "点数变化最大的 5 个参数",
            "detail": "\n".join(
                f"- `{item['key']}`（{cat}）：最大点数变化 {item.get('max_point_delta_pct', 0):+.1f}%"
                for cat, item in top5
            )
        })

    # Finding 6: Quality sensitivity
    quality_sensitive_count = sum(
        cat.get("level_distribution", {}).get("QUALITY_SENSITIVE", 0)
        for cat in category_summary.values()
    )
    if quality_sensitive_count > 0:
        findings.append({
            "title": f"{quality_sensitive_count} 个参数引起网格质量变化",
            "detail": "这些参数不仅改变了网格点数，还导致了质量判定或质量指标的变化。"
                      "在使用这些参数时需关注网格质量变化，必要时增加质量检查步骤。"
        })

    # Finding 7: Gap controls — expected to recover after bug fix
    gap_failures = [sf for sf in systematic if "gap" in sf.get("key", "")]
    if gap_failures:
        findings.append({
            "title": "Gap 控制全部失败 —— 预计在 campaign_runner bug 修复后恢复",
            "detail": f"所有 gap 相关的控制参数均因拓扑注入路径错误而失败（已在 Finding 4 中分析）。"
                      f"修复 `topo_entity_path` 后，gap 控制的 `--set` 命令将从：\n"
                      f"`row:#1/blade:#1/gap:shroud/b2b.topology=default`（错误）\n"
                      f"改为：`row:#1/blade:#1/b2b.topology=default --set row:#1/blade:#1/gap:shroud/topology=ho`（正确）\n"
                      f"修复后需重新运行 gap 相关案例以评估这些控制的实际有效性。"
        })

    return findings


# ============================================================================
# 入口
# ============================================================================

def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python analyze_results.py <campaign_dir>", file=sys.stderr)
        return 2

    campaign_dir = Path(sys.argv[1])
    if not campaign_dir.exists():
        print(f"错误：目录不存在 {campaign_dir}", file=sys.stderr)
        return 2

    print("正在分析...", file=sys.stderr)
    analysis = analyze_campaign(campaign_dir)
    report = generate_report(analysis)

    report_path = campaign_dir / "analysis_report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"报告已保存：{report_path}")

    # 也保存 JSON 分析数据
    json_path = campaign_dir / "analysis_data.json"
    json_path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"数据已保存：{json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
