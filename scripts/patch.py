# -*- coding: utf-8 -*-
"""
patch.py — 把审查建议落到模型上（安全修改）

流程固定为四步，不给「直接改」的选项：
    1. 预演：列出每条操作改什么、从什么改成什么
    2. 备份：复制一份 .bak-时间戳 到旁边
    3. 执行：逐条执行，任何一条失败立即停止
    4. 回读：重新读一遍确认真的改到了，而不是「调用成功但没生效」

用法：
    python patch.py 模型.SLDPRT 操作.json            # 只预演
    python patch.py 模型.SLDPRT 操作.json --apply    # 真正执行
    python patch.py 模型.SLDPRT --rollback 备份文件   # 回滚

操作文件格式：{"operations": [ {"op": "...", ...}, ... ]}
"""
from __future__ import annotations

import os
import sys
import shutil
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swcore as C

OPS = ("set_dimension", "set_material", "set_property", "suppress", "unsuppress",
       "delete", "rename", "rebuild", "save", "save_as", "export", "round_dimensions")

# 会造成不可逆几何变化的操作，预演时额外提醒
DESTRUCTIVE = {"delete"}


def _num(v):
    try:
        return float(v)
    except Exception:
        raise C.SWError("需要数值，收到：{!r}".format(v))


def plan(doc, ops):
    """预演：给每条操作算出「当前值 → 目标值」"""
    dims = {d["name"]: d for d in doc.dimensions()}
    mats = (doc.materials() or [{}])[0].get("material", "")
    props = doc.custom_properties()
    feats = {f["name"]: f for f in doc.features()}
    out = []

    for i, op in enumerate(ops, 1):
        k = op.get("op")
        rec = {"step": i, "op": k, "ok": True}
        if k not in OPS:
            rec.update(ok=False, detail="未知操作：{}".format(k))
            out.append(rec)
            continue

        if k == "set_dimension":
            name = op.get("name")
            cur = dims.get(name, {}).get("value_mm")
            rec.update(target=name, before=cur, after=_num(op.get("value")),
                       detail="{}: {} → {} mm".format(name, cur, op.get("value")))
        elif k == "round_dimensions":
            step = _num(op.get("step", 0.5))
            names = op.get("names") or [d["name"] for d in doc.dimensions()
                                        if d.get("driven_state") != 2]
            changes = []
            for n in names:
                cur = dims.get(n, {}).get("value_mm")
                if cur is None:
                    continue
                new = round(cur / step) * step
                if abs(new - cur) > 1e-6:
                    changes.append({"name": n, "before": cur, "after": new})
            rec.update(changes=changes,
                       detail="{} 个尺寸取整到 {} mm".format(len(changes), step))
        elif k == "set_material":
            rec.update(before=mats, after=op.get("name"),
                       detail="材料 {} → {}".format(mats or "未指定", op.get("name")))
        elif k == "set_property":
            rec.update(before=props.get(op.get("name")),
                       detail="属性 {}: {} → {}".format(
                           op.get("name"), props.get(op.get("name"), "（无）"), op.get("value")))
        elif k in ("suppress", "unsuppress", "delete", "rename"):
            f = feats.get(op.get("feature"))
            if f is None:
                rec.update(ok=False, detail="找不到特征：{}".format(op.get("feature")))
            else:
                rec.update(before=f["suppressed"],
                           detail="{} {}".format(
                               {"suppress": "压缩", "unsuppress": "解除压缩",
                                "delete": "删除", "rename": "重命名"}[k],
                               op.get("feature")))
        elif k == "rebuild":
            rec.update(detail="重建模型")
        elif k in ("save", "save_as", "export"):
            rec.update(detail={"save": "保存", "save_as": "另存为", "export": "导出"}[k]
                       + " → {}".format(op.get("path") or "（原路径）"))
        out.append(rec)
    return out


def execute(doc, op):
    k = op.get("op")
    if k == "set_dimension":
        ok, got = doc.set_dimension(op["name"], _num(op["value"]))
        return ok, "{} 实际值 {} mm".format(op["name"], got)

    if k == "round_dimensions":
        step = _num(op.get("step", 0.5))
        dims = {d["name"]: d["value_mm"] for d in doc.dimensions()}
        names = op.get("names") or list(dims)
        done, failed = 0, []
        for n in names:
            cur = dims.get(n)
            if cur is None:
                continue
            new = round(cur / step) * step
            if abs(new - cur) <= 1e-6:
                continue
            ok, got = doc.set_dimension(n, new)
            if ok:
                done += 1
            else:
                failed.append(n)
        return not failed, "取整 {} 个尺寸{}".format(
            done, "，失败：" + "、".join(failed) if failed else "")

    if k == "set_material":
        ok, got = doc.set_material(op["name"])
        return ok, "材料 {}（写入后读回 {}）".format(op["name"], got)

    if k == "set_property":
        doc.set_custom_property(op["name"], op.get("value", ""))
        now = doc.custom_properties().get(op["name"])
        ok = str(now or "").strip() == str(op.get("value", "")).strip()
        return ok, "{} = {}（回读）".format(op["name"], now)

    if k in ("suppress", "unsuppress"):
        feat = doc.find_feature(op["feature"])
        if feat is None:
            return False, "找不到特征 {}".format(op["feature"])
        state = 0 if k == "suppress" else 1          # 0=压缩 1=解除
        C.V(feat, "SetSuppression2", state)
        doc.rebuild()
        now = [f for f in doc.features() if f["name"] == op["feature"]]
        got = bool(now and now[0]["suppressed"])
        return got == (k == "suppress"), "{} 当前压缩状态 {}".format(op["feature"], got)

    if k == "delete":
        feat = doc.find_feature(op["feature"])
        if feat is None:
            return False, "找不到特征 {}".format(op["feature"])
        doc.m.ClearSelection2(True)
        feat.Select2(False, 0)
        try:
            doc.m.EditDelete()
        except Exception:
            doc.m.Extension.DeleteSelection2(0)
        doc.rebuild()
        still = doc.find_feature(op["feature"]) is not None
        return not still, "删除 {} → {}".format(op["feature"], "已删除" if not still else "仍存在")

    if k == "rename":
        feat = doc.find_feature(op["feature"])
        if feat is None:
            return False, "找不到特征 {}".format(op["feature"])
        try:
            feat.Name = op["new_name"]
        except Exception as e:
            return False, "重命名失败：{}".format(e)
        return doc.find_feature(op["new_name"]) is not None, "{} → {}".format(
            op["feature"], op["new_name"])

    if k == "rebuild":
        doc.rebuild()
        return not doc.needs_rebuild(), "重建完成"

    if k == "save":
        ok = doc.save()
        return ok, "已保存 {}".format(doc.path)

    if k == "save_as":
        ok = doc.save_as(op["path"], bool(op.get("overwrite", True)))
        return ok, "另存为 {}".format(op["path"])

    if k == "export":
        ok = doc.export(op["path"])
        return ok, "导出 {}".format(op["path"])

    return False, "未实现：{}".format(k)


def run(sw, path, ops_file, apply=False):
    ops = C.jload(ops_file).get("operations") or []
    if not ops:
        raise C.SWError("操作文件里没有 operations")
    path = os.path.abspath(path)
    doc = sw.open(path, reload=True)

    result = {"file": path, "apply": apply, "plan": plan(doc, ops)}
    bad = [p for p in result["plan"] if not p["ok"]]
    if bad:
        result["ok"] = False
        result["aborted"] = "预演阶段就有 {} 条不可执行，已中止".format(len(bad))
        return result, doc

    if not apply:
        result["ok"] = True
        return result, doc

    bak = C.backup(path)
    result["backup"] = bak
    done = []
    for i, op in enumerate(ops, 1):
        try:
            ok, detail = execute(doc, op)
        except Exception as e:
            ok, detail = False, "异常：{}".format(e)
        done.append({"step": i, "op": op.get("op"), "ok": ok, "detail": detail})
        if not ok:
            result["ok"] = False
            result["steps"] = done
            result["aborted"] = "第 {} 步失败，已停止。可用 --rollback {} 回滚".format(i, bak)
            return result, doc

    doc.save()
    result["ok"] = True
    result["steps"] = done
    result["after"] = {
        "mass": doc.mass_props(),
        "materials": doc.materials(),
        "properties": doc.custom_properties(),
        "dimensions": doc.dimensions(),
    }
    return result, doc


def rollback(path, bak):
    if not os.path.exists(bak):
        raise C.SWError("备份文件不存在：{}".format(bak))
    shutil.copy2(bak, path)
    return True


def main(argv):
    C.setup_stdout()
    ap = argparse.ArgumentParser(description="安全修改 SolidWorks 模型")
    ap.add_argument("path")
    ap.add_argument("ops", nargs="?", help="操作 JSON")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只预演）")
    ap.add_argument("--rollback", metavar="备份文件", help="从备份回滚")
    ap.add_argument("--json", help="结果写入 JSON")
    a = ap.parse_args(argv)

    if a.rollback:
        rollback(os.path.abspath(a.path), a.rollback)
        print("已回滚：{} ← {}".format(a.path, a.rollback))
        return 0
    if not a.ops:
        ap.error("需要操作文件，或使用 --rollback")

    sw = C.connect()
    result, doc = run(sw, a.path, a.ops, apply=a.apply)
    if a.json:
        C.jdump(result, a.json)

    print("{} · {}".format("执行" if a.apply else "预演", os.path.basename(a.path)))
    for p in result["plan"]:
        mark = "✓" if p["ok"] else "✗"
        print("  {} {}. {:<18} {}".format(mark, p["step"], p["op"], p.get("detail", "")))
    if result.get("backup"):
        print("  备份：{}".format(result["backup"]))
    for s in result.get("steps") or []:
        print("  {} {}. {:<18} {}".format("✓" if s["ok"] else "✗", s["step"], s["op"], s["detail"]))
    if result.get("aborted"):
        print("中止：{}".format(result["aborted"]))
        return 1
    if a.apply:
        m = (result.get("after") or {}).get("mass") or {}
        print("完成后：体积 {:.1f} mm³ · 质量 {:.1f} g".format(
            m.get("volume_mm3", 0), m.get("mass_g", 0)))
    else:
        print("预演通过，加 --apply 才会真正写入")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
