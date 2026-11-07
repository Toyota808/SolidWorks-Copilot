# -*- coding: utf-8 -*-
"""
review.py — 工程审查引擎

输入 modelinfo.inspect() 的档案，输出分级问题清单：
    严重 critical / 警告 warn / 提示 info / 建议 idea

每条问题都有三段：依据（数据）→ 建议（怎么做）→ 修复（可直接交给 patch.py 的指令）。
没有数据支撑的话不写，宁可少说也不给套话。

用法：
    python review.py 模型.SLDPRT [--json 结果.json]
"""
from __future__ import annotations

import math
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swcore as C

CAT_ORDER = ["建模质量", "结构合理性", "可制造性", "可装配性", "规范性"]

LEVEL_W = {"critical": 12, "warn": 6, "info": 2, "idea": 1}

# 等效壁厚下限（mm）
MIN_WALL = {"钢": 1.5, "铝": 1.2, "塑料": 1.0, "default": 1.2}

# 材料大类判断
MAT_KIND = [
    ("塑料", ("ABS", "尼龙", "POM", "PE", "PP", "PC", "PVC", "聚")),
    ("铝", ("6061", "1060", "5052", "7075", "铝")),
    ("钢", ("钢", "铁", "45", "Q235", "304", "316", "AISI")),
]


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def mat_kind(name):
    n = str(name or "").upper()
    for kind, keys in MAT_KIND:
        if any(k.upper() in n for k in keys):
            return kind
    return "钢"


def metrics(data):
    s = data["summary"]
    mp = s.get("mass") or {}
    b = s.get("bbox") or {}
    vol = _f(mp.get("volume_mm3"))
    area = _f(mp.get("surface_mm2"))
    size = [_f(x) for x in (b.get("size_mm") or [0, 0, 0])]
    dims = [x for x in size if x > 0]
    bb = 1.0
    for x in size:
        bb *= x

    com_off = com_ratio = 0.0
    com = mp.get("center_of_mass_mm")
    if com and b.get("min_mm") and b.get("max_mm") and len(com) == 3:
        ctr = [(_f(b["min_mm"][i]) + _f(b["max_mm"][i])) / 2 for i in range(3)]
        com_off = math.dist([_f(x) for x in com], ctr)
        diag = math.sqrt(sum(x * x for x in size)) or 1.0
        com_ratio = com_off / (diag / 2)

    # 细长比用「最大 / 次大」，不用 max/min：后者会把所有薄板都判成细长件
    plan = sorted(dims, reverse=True)[:2]
    slender = plan[0] / plan[1] if len(plan) == 2 and plan[1] > 0 else 0.0
    thick_ratio = (min(dims) / max(dims)) if dims and max(dims) > 0 else 0.0

    return {
        "volume_mm3": vol,
        "surface_mm2": area,
        "bbox_vol_mm3": bb,
        "fill_ratio": vol / bb if bb else 0.0,
        "eq_wall_mm": 2 * vol / area if area else 0.0,
        "slender_ratio": slender,
        "thick_ratio": thick_ratio,
        "com_offset_mm": com_off,
        "com_ratio": com_ratio,
        "size": size,
        "max_dim": max(dims) if dims else 0.0,
        "mid_dim": plan[1] if len(plan) > 1 else 0.0,
        "min_dim": min(dims) if dims else 0.0,
        "mass_g": _f(mp.get("mass_g")),
        "density": _f(mp.get("density_kg_m3")),
    }


def part_type(data, mt):
    feats = data.get("features", [])
    types = [f["type"] for f in feats]
    holes = sum(1 for t in types if t in ("Cut", "ICE", "HoleWzd", "Hole"))
    if any(t in ("BaseFlange", "EdgeFlange", "MiterFlange", "SketchBend") for t in types):
        return "钣金件"
    if "Shell" in types:
        return "壳体/罩类"
    if mt["thick_ratio"] and mt["thick_ratio"] < 0.10 and holes >= 1:
        return "板件（带孔）"
    if holes >= 2 and mt["thick_ratio"] < 0.35:
        return "法兰/安装盘"
    if mt["slender_ratio"] >= 3 and mt["thick_ratio"] > 0.4:
        return "轴类/长条形"
    if mt["slender_ratio"] <= 1.25 and mt["thick_ratio"] >= 0.35:
        return "块类/回转体"
    return "通用结构件"


def _mk(fid, level, cat, title, evidence, suggestion, fix=None):
    return {"id": fid, "level": level, "category": cat, "title": title,
            "evidence": evidence, "suggestion": suggestion, "fix": fix}


# ---------------------------------------------------------------- 规则
def rule_modeling(data, mt, ptype):
    out = []
    diag = data.get("diagnostics") or {}
    h = data.get("health") or {}
    s = data.get("summary") or {}

    errs = diag.get("error_features") or []
    if errs:
        names = "、".join(e["name"] for e in errs[:4])
        out.append(_mk("M01", "critical", "建模质量", "存在报错特征",
                       "{} 个特征报错：{}".format(len(errs), names),
                       "先修这些特征的父级参考（草图/边线/基准面是否被删或压缩），"
                       "否则任何改尺寸都会连锁失败。改型前必须先清干净。"))

    if diag.get("needs_rebuild"):
        out.append(_mk("M02", "warn", "建模质量", "模型未重建",
                       "NeedsRebuild = True，几何与质量属性可能滞后于最新改动",
                       "重建后再看质量/干涉结果，否则是在用旧数据下判断。",
                       {"op": "rebuild"}))

    under = [x for x in (data.get("sketches") or [])
             if not x["fully_defined"] and x["segments"] > 0]
    if under:
        names = "、".join(x["name"] for x in under[:4])
        out.append(_mk("M03", "warn", "建模质量", "草图未完全定义",
                       "{}/{} 个草图欠定义：{}".format(
                           len(under), s.get("sketch_count", 0), names),
                       "补齐尺寸与几何关系。欠定义草图在改尺寸、切配置、装配定位时会漂移，"
                       "是改型失败最常见的原因。"))

    empty = [x for x in (data.get("sketches") or []) if x["segments"] == 0]
    if empty:
        out.append(_mk("M04", "info", "建模质量", "存在空草图",
                       "{} 个草图没有任何图元：{}".format(
                           len(empty), "、".join(x["name"] for x in empty[:4])),
                       "删掉空草图，减少特征树噪音，也避免别人误以为这里有设计意图。"))

    mats = [m for m in (s.get("materials") or []) if m.get("material")]
    if not mats:
        out.append(_mk("M05", "warn", "建模质量", "未指定材料",
                       "实体没有绑定材料，密度按默认值计算，质量/质心不可信",
                       "先在「编辑材料」里选真实材料。材料决定密度、屈服强度，"
                       "是后续所有力学与成本判断的基准。"))

    props = data.get("properties") or {}
    need = ["图号", "名称", "材料", "版本"]
    miss = [k for k in need if not str(props.get(k, "")).strip()]
    if miss:
        out.append(_mk("M06", "info", "建模质量", "缺少关键自定义属性",
                       "未填写：{}".format("、".join(miss)),
                       "补齐图号/名称/材料/版本。工程图和 BOM 靠这些字段自动带出，"
                       "缺了就得手工填，容易出错。"))

    sup = diag.get("suppressed_features") or []
    if sup:
        out.append(_mk("M07", "idea", "建模质量", "存在压缩特征",
                       "{} 个特征被压缩：{}".format(
                           len(sup), "、".join(x["name"] for x in sup[:4])),
                       "确认是否还需要保留。长期压缩的特征容易在后面改型时被误恢复，"
                       "造成意外几何。"))

    if s.get("solid_bodies", 0) > 1:
        out.append(_mk("M08", "info", "建模质量", "多实体零件",
                       "包含 {} 个实体".format(s.get("solid_bodies")),
                       "确认是刻意保留（如焊接件、需分别加工的嵌件）还是忘了合并。"
                       "多实体在出工程图与算 BOM 时容易被漏项。"))

    fc = s.get("feature_count", 0)
    has_pattern = any(f["type"] in ("LPattern", "CirPattern", "MirrorPattern")
                      for f in data.get("features", []))
    if fc >= 40 and not has_pattern:
        out.append(_mk("M09", "idea", "建模质量", "特征数量偏多且未见阵列",
                       "共 {} 个建模特征，没有阵列/镜像".format(fc),
                       "重复结构改用线性/圆周阵列，改一处就整体生效，"
                       "也能显著缩短重建时间。"))
    return out


def rule_structure(data, mt, ptype):
    out = []
    if mt["slender_ratio"] >= 8:
        out.append(_mk("S01", "warn", "结构合理性", "细长比过大",
                       "长宽比 {:.1f}（最长边 {:.0f} mm / 次长边 {:.0f} mm）".format(
                           mt["slender_ratio"], mt["max_dim"], mt["mid_dim"]),
                       "细长件在切削力或装配力下容易弯曲。可加中间支撑、改工字/箱型截面提高"
                       "惯性矩，或调整工艺（先粗后精、增加去应力工序）。"))
    elif mt["slender_ratio"] >= 4:
        out.append(_mk("S02", "info", "结构合理性", "偏细长",
                       "长宽比 {:.1f}".format(mt["slender_ratio"]),
                       "留意悬伸端的挠度与振动；必要时在自由端加筋或缩短悬伸。"))

    if mt["com_ratio"] >= 0.18:
        out.append(_mk("S03", "warn", "结构合理性", "质心明显偏离几何中心",
                       "质心偏移 {:.1f} mm，占包围盒半对角线 {:.0f}%".format(
                           mt["com_offset_mm"], mt["com_ratio"] * 100),
                       "偏载会放大振动、增加轴承附加力矩。可用配重、对称布置孔位、"
                       "或把大质量特征往中心挪来修正。"))

    eq = mt["eq_wall_mm"]
    kind = mat_kind((data["summary"].get("materials") or [{}])[0].get("material"))
    limit = MIN_WALL.get(kind, MIN_WALL["default"])
    if 0 < eq < limit:
        out.append(_mk("S04", "warn", "结构合理性", "等效壁厚偏薄",
                       "按 2V/A 估算等效壁厚 ≈ {:.2f} mm，低于{}件常用下限 {:.1f} mm".format(
                           eq, kind, limit),
                       "薄壁易翘曲、易让刀。受力部位加筋或加厚；如果是有意做的薄壁件，"
                       "建议补一次静力分析确认刚度。"))

    fill = mt["fill_ratio"]
    if fill and fill < 0.12:
        out.append(_mk("S05", "info", "结构合理性", "填充率很低（薄壁/框架）",
                       "实体只占包围盒体积的 {:.1f}%".format(fill * 100),
                       "确认刚度是否够。薄壁框架建议按主受力方向布置加强筋，"
                       "而不是整体加厚（重量代价太大）。"))
    elif fill > 0.85 and ptype in ("通用结构件", "法兰/安装盘", "块类/回转体"):
        out.append(_mk("S06", "idea", "结构合理性", "材料利用率偏低（接近实心）",
                       "填充率 {:.0f}%，减重空间明显".format(fill * 100),
                       "非配合面可挖空、开减重孔或做下沉台阶。减重孔要避开设主传力路径，"
                       "孔径取壁厚的 2～3 倍以上，避免削弱过度。"))

    types = [f["type"] for f in data.get("features", [])]
    has_cut = any(t in ("Cut", "ICE", "HoleWzd") for t in types)
    has_round = any(t in ("Fillet", "Chamfer") for t in types)
    if has_cut and not has_round:
        out.append(_mk("S07", "warn", "结构合理性", "有切除但没有任何圆角/倒角",
                       "存在 {} 个切除特征，圆角/倒角特征 0 个".format(
                           sum(1 for t in types if t in ("Cut", "ICE", "HoleWzd"))),
                       "内尖角是疲劳裂纹的起点，应力集中系数可达 2～3。"
                       "受力件的内角建议 R ≥ 0.5×壁厚；非配合锐边加 0.5～1 mm 倒角，"
                       "同时也避免装配时割手。"))

    if ptype.startswith("板件") and mt["min_dim"] and mt["max_dim"] / max(mt["min_dim"], 0.01) > 50:
        out.append(_mk("S08", "info", "结构合理性", "薄板易翘曲",
                       "板厚 {:.1f} mm，平面尺寸 {:.0f} mm，厚宽比 1:{:.0f}".format(
                           mt["min_dim"], mt["max_dim"], mt["max_dim"] / mt["min_dim"]),
                       "厚宽比小于 1:50 时加工/焊接易翘曲。可加翻边、压筋，"
                       "或在图纸上注明去应力与平面度公差。"))
    return out


def rule_dfm(data, mt, ptype):
    out = []
    dims = [d for d in (data.get("dimensions") or []) if d.get("driven_state") != 2]
    odd = [d for d in dims if abs(d["value_mm"] - round(d["value_mm"])) > 0.05]
    if len(odd) >= 3:
        out.append(_mk("F01", "info", "可制造性", "尺寸多为非整数",
                       "{} 个驱动尺寸不是整数，例如 {}".format(
                           len(odd), "、".join("{:.2f}".format(d["value_mm"]) for d in odd[:3])),
                       "机加工习惯按 0.5 mm 或整数取值。非标尺寸会抬高刀具与量具成本，"
                       "非配合面建议取整。",
                       {"op": "round_dimensions", "step": 0.5,
                        "names": [d["name"] for d in odd[:12]]}))

    if not dims and (data["summary"].get("feature_count") or 0) > 3:
        out.append(_mk("F02", "warn", "可制造性", "模型里没有任何尺寸标注",
                       "有 {} 个建模特征，但读到 0 个尺寸".format(
                           data["summary"].get("feature_count")),
                       "全是「画上去」的几何，没法参数化改型，也导不出规范工程图。"
                       "关键尺寸（外形、孔位、孔径、厚度）至少要标出来。"))

    eq = mt["eq_wall_mm"]
    if ptype in ("壳体/罩类",) and 0 < eq < 2.0:
        out.append(_mk("F03", "warn", "可制造性", "壳体壁厚偏薄",
                       "等效壁厚 ≈ {:.2f} mm".format(eq),
                       "注塑件壁厚建议 1.5～3 mm 且尽量均匀，突变处做渐变过渡，"
                       "否则缩水、变形、短射都会出现。"))

    if mt["thick_ratio"] and mt["thick_ratio"] < 0.05 and mt["min_dim"] < 3:
        out.append(_mk("F04", "warn", "可制造性", "最薄尺寸低于常规加工余量",
                       "最小尺寸 {:.2f} mm".format(mt["min_dim"]),
                       "铣削薄壁低于 1 mm 时极易振刀。可留工艺筋、分序加工，"
                       "或改用线切割/激光。"))
    return out


def rule_dfa(data, mt, ptype):
    out = []
    dims = [d for d in (data.get("dimensions") or []) if d.get("driven_state") != 2]
    # 孔径检查：靠尺寸名里的「孔/直径」猜不可靠，只对能确定的做提示
    small = [d for d in dims if 0 < d["value_mm"] < 3.0]
    if len(small) >= 2:
        out.append(_mk("A01", "info", "可装配性", "存在小直径尺寸",
                       "{} 个尺寸小于 3 mm，最小 {:.2f} mm".format(
                           len(small), min(d["value_mm"] for d in small)),
                       "小于 ⌀3 的孔用标准钻头容易断，深径比超过 3 时更难。"
                       "优先选 ⌀3.2（M3 底孔）、⌀4.2（M4）、⌀5.5（M5）等标准规格。"))

    if data.get("assembly"):
        a = data["assembly"]
        if a.get("component_count", 0) > 1 and a.get("mate_count", 0) == 0:
            out.append(_mk("A02", "critical", "可装配性", "装配体没有任何配合",
                           "{} 个零件，0 个配合".format(a["component_count"]),
                           "没有配合就等于零件各自飘着，位置靠不住，也没法做运动仿真。"
                           "至少给基准件加固定，再逐个加面贴合/同轴配合。"))
        elif a.get("mate_count", 0) > a.get("component_count", 0) * 4:
            out.append(_mk("A03", "info", "可装配性", "配合数量偏多",
                           "{} 个零件却有 {} 个配合".format(
                               a["component_count"], a["mate_count"]),
                           "配合过多会互相打架、重建变慢。能用一个配合约束住的，"
                           "不要拆成三四个；同类零件用阵列/镜像代替逐个装配。"))
        fixed = [c for c in a.get("components", []) if c.get("fixed")]
        if a.get("component_count", 0) > 1 and not fixed:
            out.append(_mk("A04", "warn", "可装配性", "装配体没有固定基准件",
                           "{} 个零件全部浮动".format(a["component_count"]),
                           "把机架/底板等基准件设为固定，否则整体位姿不确定，"
                           "出图与仿真都会漂。"))
    return out


def rule_norm(data, mt, ptype):
    out = []
    d = data.get("document") or {}
    props = data.get("properties") or {}
    if not d.get("has_path", True):
        out.append(_mk("N01", "warn", "规范性", "文件尚未保存到磁盘",
                       "文档没有磁盘路径（新建后没做过保存）",
                       "先保存再谈版本管理。未保存的模型一旦 SolidWorks 崩溃就全没了。"))
    elif d.get("modified"):
        out.append(_mk("N01b", "info", "规范性", "有改动未保存",
                       "文件有未保存的修改",
                       "确认改动无误后保存，否则下次打开会退回旧版本。"))

    if not str(props.get("图号", "")).strip():
        out.append(_mk("N02", "info", "规范性", "未填写图号",
                       "自定义属性「图号」为空",
                       "图号是工程图标题栏与 BOM 的主键，缺失会导致装配清单对不上。"))

    import re
    name = (data.get("file") or {}).get("name", "")
    if re.match(r"^(零件|Part)\d+\.", name, re.I):
        out.append(_mk("N03", "idea", "规范性", "文件名仍是默认名",
                       "文件名「{}」是 SolidWorks 自动命名".format(name),
                       "改成「图号_名称」的形式，例如 FL-001_圆形法兰盘。"
                       "装配时靠文件名找零件，默认名一多就分不清。"))

    cfgs = d.get("configs") or []
    if len(cfgs) > 6:
        out.append(_mk("N04", "idea", "规范性", "配置数量偏多",
                       "共 {} 个配置：{}".format(len(cfgs), "、".join(cfgs[:5])),
                       "确认每个配置都还在用。配置爆炸会让重建变慢，也让 BOM 混乱；"
                       "过期的配置建议清理。"))
    return out


# ---------------------------------------------------------------- 汇总
def review(data, rules=None):
    mt = metrics(data)
    ptype = part_type(data, mt)
    findings = []
    for fn in (rule_modeling, rule_structure, rule_dfm, rule_dfa, rule_norm):
        if rules and fn.__name__ not in rules:
            continue
        try:
            findings.extend(fn(data, mt, ptype) or [])
        except Exception as e:
            findings.append(_mk("X00", "info", "其它", "{} 规则执行异常".format(fn.__name__),
                                str(e), "这是工具问题，不影响模型本身。"))

    order = LEVEL_IDX
    findings.sort(key=lambda it: (CAT_ORDER.index(it["category"]) if it["category"] in CAT_ORDER else 9,
                                  order.get(it["level"], 9)))

    total = 100
    for f in findings:
        total -= LEVEL_W.get(f["level"], 1)
    total = max(0, min(100, total))

    by_cat = {}
    for f in findings:
        by_cat[f["category"]] = by_cat.get(f["category"], 0) + 1

    fixes = [f["fix"] for f in findings if f.get("fix")]

    return {
        "file": data.get("file") or {},
        "part_type": ptype,
        "metrics": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in mt.items()},
        "score": {
            "total": total,
            "grade": "优" if total >= 85 else ("良" if total >= 70 else ("中" if total >= 50 else "需整改")),
            "by_category": {c: by_cat.get(c, 0) for c in CAT_ORDER},
            "critical": sum(1 for f in findings if f["level"] == "critical"),
            "warn": sum(1 for f in findings if f["level"] == "warn"),
            "info": sum(1 for f in findings if f["level"] == "info"),
            "idea": sum(1 for f in findings if f["level"] == "idea"),
        },
        "findings": findings,
        "fixes": fixes,
        "headline": _headline(findings, total, ptype),
    }


def _headline(findings, total, ptype):
    crit = [f for f in findings if f["level"] == "critical"]
    warn = [f for f in findings if f["level"] == "warn"]
    if crit:
        return "识别为{}。有 {} 个必须先处理的问题，最要紧的是「{}」。".format(
            ptype, len(crit), crit[0]["title"])
    if warn:
        return "识别为{}。整体能做出来，但有 {} 处会影响改型或加工，优先处理「{}」。".format(
            ptype, len(warn), warn[0]["title"])
    if total >= 85:
        return "识别为{}。模型规整，没有硬伤，剩下的都是可选优化。".format(ptype)
    return "识别为{}。未发现硬伤，按下面几条做工艺性优化即可。".format(ptype)


LEVEL_CN = {"critical": "严重", "warn": "警告", "info": "提示", "idea": "建议"}
LEVEL_IDX = {"critical": 0, "warn": 1, "info": 2, "idea": 3}


def render(rep):
    s = rep["score"]
    lines = ["设计审查 · {} · {}".format(rep["file"].get("name", ""), rep["part_type"]),
             "综合评分 {}/100（{}）｜严重 {} · 警告 {} · 提示 {} · 建议 {}".format(
                 s["total"], s["grade"], s["critical"], s["warn"], s["info"], s["idea"]),
             rep["headline"], ""]

    rows = list(rep["findings"])
    rows.sort(key=lambda it: (CAT_ORDER.index(it["category"]) if it["category"] in CAT_ORDER else 9,
                              LEVEL_IDX.get(it["level"], 9)))

    cat = None
    for it in rows:
        if it["category"] != cat:
            cat = it["category"]
            lines.append("── {} ──".format(cat))
        lines.append("[{}] {}".format(LEVEL_CN.get(it["level"], "提示"), it["title"]))
        lines.append("    依据：{}".format(it["evidence"]))
        lines.append("    建议：{}".format(it["suggestion"]))
        if it.get("fix"):
            lines.append("    可执行：{}".format(C.jdump(it["fix"]).replace("\n", " ")))
    if not rows:
        lines.append("未发现需要处理的问题。")
    return "\n".join(lines)


def main(argv):
    C.setup_stdout()
    ap = argparse.ArgumentParser(description="三维模型设计审查")
    ap.add_argument("path")
    ap.add_argument("--json", help="结果写入 JSON")
    a = ap.parse_args(argv)

    import modelinfo
    sw = C.connect()
    data = modelinfo.inspect(sw, a.path)
    rep = review(data)
    print(render(rep))
    if a.json:
        C.jdump(rep, a.json)
        print("\n结果已保存：{}".format(a.json))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
