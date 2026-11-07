# -*- coding: utf-8 -*-
"""
modelinfo.py — 一次读取，拿到模型的全貌

读什么：特征树（含压缩/报错）、草图（图元数/约束状态）、尺寸、自定义属性、
        材料、质量/质心/惯量、包围盒、配置、外部参考、诊断信息、装配层级与配合。

为什么要一次读全：SolidWorks COM 每次跨进程调用都有开销，而且打开的文档
可能被用户切走，多次读容易拿到不一致的状态。一次读完整档，后续分析全部离线做。

用法：
    python modelinfo.py 模型.SLDPRT [-o 档案.json]
"""
from __future__ import annotations

import os
import sys
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swcore as C

# 草图约束状态
SKETCH_STATE = {
    0: "未知", 1: "完全定义", 2: "过定义", 3: "欠定义", 4: "欠定义(未求解)",
    5: "空草图",
}


def _file_meta(path):
    st = os.stat(path)
    return {
        "name": os.path.basename(path),
        "path": os.path.abspath(path),
        "size_kb": round(st.st_size / 1024.0, 1),
        "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
    }


def _sketches(doc):
    """草图详情，按名字去重（同一条草图会以父特征+独立特征出现两次）"""
    out = []
    for f in doc.features():
        if f["type"] != "ProfileFeature":
            continue
        feat = doc.find_feature(f["name"])
        if feat is None:
            continue
        sk = C.V(feat, "GetSpecificFeature2")
        if sk is None:
            continue
        segs = C.V(sk, "GetSketchSegments", default=None)
        pts = C.V(sk, "GetSketchPoints2", default=None)
        try:
            state = int(C.u(C.V(sk, "GetConstrainedStatus", default=4), 4) or 4)
        except Exception:
            state = 4
        out.append({
            "name": f["name"],
            "segments": len(segs) if segs else 0,
            "points": len(pts) if pts else 0,
            "state": state,
            "state_cn": SKETCH_STATE.get(state, "未知"),
            "fully_defined": state == 1,
        })
    return out


def _diagnose(doc):
    """报错/压缩统计。

    只统计真正参与几何的特征：特征树里的光源、文件夹、配置表节点既不是
    「建模特征」，压缩它们也没有任何工程含义，混进来会让结论变得可笑。
    """
    keep = set(C.MODEL_FEATS) | {"ProfileFeature", "RefPlane", "RefAxis"}
    feats = [f for f in doc.features() if f["type"] in keep]
    errors = [{"name": f["name"], "type": f["type_cn"], "code": f["error"]}
              for f in feats if f["error"]]
    suppressed = [{"name": f["name"], "type": f["type_cn"]}
                  for f in feats if f["suppressed"]]
    return {
        "error_features": errors,
        "suppressed_features": suppressed,
        "needs_rebuild": doc.needs_rebuild(),
        "messages": doc.whats_wrong(),
    }


def _assembly(doc):
    comps, mates = [], []
    try:
        arr = doc.m.GetComponents(False)
        for c in (arr or []):
            comps.append({
                "name": str(C.V(c, "Name2", default="") or C.V(c, "Name", default="") or ""),
                "path": str(C.V(c, "GetPathName", default="") or ""),
                "suppressed": bool(C.u(C.V(c, "IsSuppressed", default=False), False)),
                "fixed": bool(C.u(C.V(c, "IsFixed", default=False), False)),
            })
    except Exception:
        pass
    feat = doc._feat(C.V(doc.m, "FirstFeature"))
    guard = 0
    while feat is not None and guard < 5000:
        guard += 1
        if str(C.V(feat, "GetTypeName2", default="")) in ("MateGroup", "Mate2"):
            sub = doc._feat(C.V(feat, "GetFirstSubFeature"))
            d = 0
            while sub is not None and d < 500:
                d += 1
                tn = str(C.V(sub, "GetTypeName2", default="") or "")
                mates.append({
                    "name": str(C.V(sub, "Name", default="") or ""),
                    "type": tn,
                    "type_cn": C.FEAT_CN.get(tn, tn),
                    "suppressed": bool(C.u(C.V(sub, "IsSuppressed", default=False), False)),
                })
                sub = doc._feat(C.V(sub, "GetNextSubFeature"))
        feat = doc._feat(C.V(feat, "GetNextFeature"))
    return {"component_count": len(comps), "components": comps,
            "mate_count": len(mates), "mates": mates}


def _health(feats, mass, diag, sketches):
    """建模健康度 0-100，只反映「模型本身是否规整」，不代表设计好坏"""
    score = 100
    score -= 18 * len(diag["error_features"])
    score -= 10 if diag["needs_rebuild"] else 0
    bad = [s for s in sketches if not s["fully_defined"] and s["segments"] > 0]
    score -= min(30, 6 * len(bad))
    empty = [s for s in sketches if s["segments"] == 0]
    score -= min(10, 3 * len(empty))
    if not feats:
        score = 0
    if mass.get("volume_mm3", 0) <= 0 and feats:
        score -= 25
    score = max(0, min(100, score))
    grade = "优" if score >= 85 else ("良" if score >= 70 else ("中" if score >= 50 else "差"))
    return {"score": score, "grade": grade,
            "under_defined": len(bad), "empty_sketches": len(empty)}


def inspect(sw, path, reload=True, with_assembly=True):
    """读取模型，返回完整档案字典"""
    t0 = time.time()
    doc = sw.open(path, reload=reload)
    feats = doc.features()
    model_feats = [f for f in feats if f["type"] in C.MODEL_FEATS]
    sketches = _sketches(doc)
    mass = doc.mass_props()
    bbox = doc.bbox()
    diag = _diagnose(doc)

    data = {
        "file": _file_meta(path),
        "document": {
            "title": doc.title,
            "type": doc.type_cn,
            "type_id": doc.dtype,
            "configs": doc.configs(),
            "active_config": doc.active_config(),
            "has_path": bool(doc.path),
            "modified": bool(C.V(doc.m, "GetSaveFlag", default=False)),
            "dirty": doc.dirty,
        },
        "summary": {
            "feature_count": len(model_feats),
            "solid_bodies": doc.body_count(),
            "mass": mass,
            "bbox": bbox,
            "materials": doc.materials(),
            "sketch_count": len([s for s in sketches if s["segments"] > 0]),
            "under_defined_sketches": len([s for s in sketches
                                           if not s["fully_defined"] and s["segments"] > 0]),
        },
        "features": feats,
        "sketches": sketches,
        "dimensions": doc.dimensions(),
        "properties": doc.custom_properties(),
        "diagnostics": diag,
        "health": _health(model_feats, mass, diag, sketches),
        "elapsed_sec": round(time.time() - t0, 2),
    }
    if doc.dtype == C.swDocASSEMBLY and with_assembly:
        data["assembly"] = _assembly(doc)
    return data


def brief(data):
    """终端用中文摘要"""
    s = data["summary"]
    m = s["mass"] or {}
    b = s["bbox"] or {}
    d = data["document"]
    lines = [
        "【{}】{} · {} 个建模特征 · {} 个实体".format(
            data["file"]["name"], d["type"], s["feature_count"], s["solid_bodies"]),
    ]
    if b.get("size_mm"):
        lines.append("外形尺寸：{} × {} × {} mm".format(
            *[round(x, 2) for x in b["size_mm"]]))
    mat = "、".join(sorted({x["material"] for x in s["materials"] if x["material"]})) or "未指定"
    lines.append("体积 {:.0f} mm³ · 质量 {:.1f} g · 材料 {}".format(
        m.get("volume_mm3", 0), m.get("mass_g", 0), mat))
    h = data["health"]
    lines.append("建模健康度：{}/100（{}）".format(h["score"], h["grade"]))
    if h["under_defined"]:
        lines.append("  · {} 个草图未完全定义".format(h["under_defined"]))
    if h["empty_sketches"]:
        lines.append("  · {} 个空草图".format(h["empty_sketches"]))
    if data["diagnostics"]["error_features"]:
        lines.append("  · {} 个特征报错".format(len(data["diagnostics"]["error_features"])))
    if data["diagnostics"]["needs_rebuild"]:
        lines.append("  · 模型有改动未重建")
    if d.get("dirty"):
        lines.append("  · 注意：文件已打开且有未保存改动，读到的是内存版本")
    if data.get("assembly"):
        a = data["assembly"]
        lines.append("装配：{} 个零件 · {} 个配合".format(a["component_count"], a["mate_count"]))
    return "\n".join(lines)


def main(argv):
    C.setup_stdout()
    ap = argparse.ArgumentParser(description="读取 SolidWorks 模型档案")
    ap.add_argument("path")
    ap.add_argument("-o", "--out", help="档案 JSON 输出路径")
    ap.add_argument("--no-assembly", action="store_true")
    a = ap.parse_args(argv)

    sw = C.connect()
    data = inspect(sw, a.path, with_assembly=not a.no_assembly)
    if a.out:
        C.jdump(data, a.out)
        print("档案已保存：{}".format(a.out))
    print(brief(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
