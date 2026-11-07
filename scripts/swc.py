# -*- coding: utf-8 -*-
"""
swc.py — SolidWorks Copilot 统一入口

    python swc.py doctor                      环境体检（实例数/模板/材料库/COM）
    python swc.py info                        当前会话与已打开文档
    python swc.py inspect <模型> [-o out.json]  读模型全景档案
    python swc.py review  <模型> [--json out]   工程审查（文字结论）
    python swc.py report  <模型> [-o out.html]  生成 HTML 审查报告
    python swc.py build   <配方.json> [--apply] 参数化建模（默认预演）
    python swc.py patch   <模型> <操作.json> [--apply]  安全修改（默认预演）
    python swc.py recipes                     列出内置配方
    python swc.py selftest                    自检：建模→读取→审查→修改全链路

约定：
  · 默认只附加已运行的 SolidWorks，连不上就报错，绝不后台偷偷多开
  · 破坏性操作先备份，改完回读确认
  · 只允许关闭本工具自己打开的文档，用户手动打开的模型一律不碰
"""
from __future__ import annotations

import os
import sys
import argparse
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import swcore as C


# ------------------------------------------------------------------ doctor
def cmd_doctor(a):
    C.setup_stdout()
    print("=" * 64)
    print("SolidWorks Copilot · 环境体检")
    print("=" * 64)
    bad = 0

    if C._w is None:
        print("[x] 未安装 pywin32：python -m pip install pywin32")
        return 2
    print("[v] pywin32 就绪")

    pids = C.rot_pids()
    if not pids:
        print("[ ] 未注册 PID 信息（不影响使用，只是无法判断是否多开）")
    elif len(pids) == 1:
        print("[v] 单实例运行，PID {}".format(pids[0]))
    else:
        bad += 1
        print("[!] 检测到 {} 个 SolidWorks 实例：{}".format(len(pids), pids))
        print("    多开会让 COM 在实例间乱跳（改了不生效、读到旧数据），只保留一个再跑")

    try:
        sw = C.connect()
    except Exception as e:
        print("[x] 连接失败：{}".format(e))
        return 3
    print("[v] 已连接 版本 {}（{}）  {}".format(
        sw.rev, sw.year, "附加到已有实例" if sw.attached else "本工具启动"))

    t = sw.templates()
    for k, cn in (("part", "零件"), ("assembly", "装配体"), ("drawing", "工程图")):
        p = t.get(k)
        if p:
            print("[v] {}模板：{}".format(cn, os.path.basename(p)))
        else:
            bad += 1 if k != "drawing" else 0
            print("[!] {}模板未找到".format(cn))

    try:
        import matlib
        n = len(matlib.scan())
        print("[v] 材料库 {} 条".format(n) if n else "[!] 材料库为空，赋材料会失败")
        if not n:
            bad += 1
    except Exception as e:
        print("[!] 材料库读取失败：{}".format(e))

    docs = sw.documents()
    print("\n当前打开 {} 个顶层文档：".format(len(docs)))
    for d in docs:
        tag = "本会话打开" if sw.is_owned(d.path) else "非本会话"
        print("  · [{}] {}  ({})".format(d.type_cn, d.title, tag))
    if not docs:
        print("  （无）")

    print("\n结论：{}".format("环境就绪" if not bad else "有 {} 处告警，建议先处理".format(bad)))
    return 0 if not bad else 1


# ------------------------------------------------------------------ info
def cmd_info(a):
    C.setup_stdout()
    sw = C.connect()
    i = sw.info()
    print("版本 {}（{}）  实例 {}  {}".format(
        i["revision"], i["year"], i["instances"] or "-", "（多开！）" if i["multi_instance"] else ""))
    print("已打开文档 {} 个：".format(len(i["documents"])))
    for d in i["documents"]:
        print("  · [{}] {}  {}".format(d["type"], d["title"], "（本工具打开）" if d["owned"] else ""))
    return 0


# ------------------------------------------------------------------ inspect
def cmd_inspect(a):
    C.setup_stdout()
    import modelinfo
    sw = C.connect()
    data = modelinfo.inspect(sw, a.path)
    if a.out:
        C.jdump(data, a.out)
        print("档案已保存：{}".format(a.out))
    print(modelinfo.brief(data))
    return 0


# ------------------------------------------------------------------ review
def cmd_review(a):
    C.setup_stdout()
    import modelinfo
    import review as R
    sw = C.connect()
    data = modelinfo.inspect(sw, a.path)
    rep = R.review(data)
    print(R.render(rep))
    if a.json:
        C.jdump({"inspect": data, "review": rep}, a.json)
        print("\nJSON 已保存：{}".format(a.json))
    return 0


# ------------------------------------------------------------------ report
def cmd_report(a):
    C.setup_stdout()
    import report as rp
    sw = C.connect()
    out, rep = rp.make(sw, a.path, a.out, shot=not a.no_shot, json_out=a.json)
    print("报告已生成：{}".format(out))
    print("评分 {}/100（{}）· 严重 {} · 警告 {} · 共 {} 条".format(
        rep["score"]["total"], rep["score"]["grade"],
        rep["score"]["critical"], rep["score"]["warn"], len(rep["findings"])))
    return 0


# ------------------------------------------------------------------ build
def cmd_build(a):
    C.setup_stdout()
    import build as bd
    sw = C.connect()
    res = bd.run_recipe(sw, a.recipe, apply=a.apply, out_path=a.save, shot=not a.no_shot)
    if a.out:
        C.jdump(res, a.out)
    if not a.apply:
        print("预演通过：{} 步".format(len(res.get("steps") or [])))
        for s in res.get("steps") or []:
            ps = "  ".join("{}={:.3g}".format(k, v) for k, v in (s["params"] or {}).items())
            print("  {}. {:<10} {}".format(s["step"], s["label"], ps))
        return 0
    m = res["metrics"]["mass"]
    b = res["metrics"]["bbox"]
    print("建模完成 → {}".format(res["path"]))
    for s in res["steps"]:
        print("  {}. {:<12} {}".format(s["step"], s["op"], s["detail"]))
    if b.get("size_mm"):
        print("外形 {} × {} × {} mm".format(*[round(x, 2) for x in b["size_mm"]]))
    print("体积 {:.1f} mm³  质量 {:.1f} g".format(m.get("volume_mm3", 0), m.get("mass_g", 0)))
    if res.get("preview"):
        print("预览 {}".format(res["preview"]))
    return 0


# ------------------------------------------------------------------ patch
def cmd_patch(a):
    C.setup_stdout()
    import patch as pt
    sw = C.connect()
    res, _ = pt.run(sw, a.path, a.ops, apply=a.apply)
    if a.out:
        C.jdump(res, a.out)
    print("{} · {}".format("执行" if a.apply else "预演", os.path.basename(a.path)))
    for p in res["plan"]:
        print("  {} {}. {:<18} {}".format("v" if p["ok"] else "x", p["step"], p["op"],
                                          p.get("detail", "")))
    if res.get("backup"):
        print("  备份 {}".format(res["backup"]))
    for s in res.get("steps") or []:
        print("  {} {}. {:<18} {}".format("v" if s["ok"] else "x", s["step"], s["op"], s["detail"]))
    if res.get("aborted"):
        print("中止：{}".format(res["aborted"]))
        return 1
    if not a.apply:
        print("预演通过，加 --apply 才会真正写入")
    return 0


# ------------------------------------------------------------------ recipes
def cmd_recipes(a):
    C.setup_stdout()
    d = os.path.join(ROOT, "assets", "recipes")
    files = sorted(glob.glob(os.path.join(d, "*.json")))
    if not files:
        print("没有内置配方")
        return 0
    print("内置配方（{} 个）：".format(len(files)))
    for f in files:
        try:
            r = C.jload(f)
        except Exception:
            continue
        vs = ", ".join("{}={}".format(k, v) for k, v in (r.get("vars") or {}).items())
        print("  · {}  {}".format(os.path.basename(f), r.get("name", "")))
        print("      {}".format(r.get("desc", "")))
        print("      参数：{}".format(vs))
    return 0


# ------------------------------------------------------------------ selftest
def cmd_selftest(a):
    """自检：在系统临时目录建一个零件，跑完建模→读取→审查→修改→回读"""
    C.setup_stdout()
    import modelinfo
    import review as R
    import build as bd
    import patch as pt

    tmp = os.path.join(os.environ.get("TEMP", "/tmp"), "swc_selftest")
    os.makedirs(tmp, exist_ok=True)
    print("自检目录：{}".format(tmp))
    sw = C.connect()
    try:
        sw.app.Visible = True
    except Exception:
        pass

    ok = True
    step = lambda n, t: print("\n[{}/5] {}".format(n, t))

    step(1, "按配方建模")
    recipe = os.path.join(ROOT, "assets", "recipes", "flange.json")
    res = bd.run_recipe(sw, recipe, apply=True,
                        out_path=os.path.join(tmp, "selftest_flange.SLDPRT"), shot=True)
    vol = res["metrics"]["mass"]["volume_mm3"]
    print("   体积 {:.1f} mm³  特征 {} 个".format(vol, len(res["metrics"]["features"])))
    ok &= os.path.exists(res.get("path") or "")

    step(2, "读取档案")
    data = modelinfo.inspect(sw, res["path"])
    print("   " + modelinfo.brief(data).replace("\n", "\n   "))
    ok &= data["summary"]["feature_count"] >= 3 and data["summary"]["solid_bodies"] == 1

    step(3, "工程审查")
    rep = R.review(data)
    print("   评分 {}/100（{}）· 共 {} 条建议".format(
        rep["score"]["total"], rep["score"]["grade"], len(rep["findings"])))
    for f in rep["findings"][:3]:
        print("   · [{}] {}".format(
            {"critical": "严重", "warn": "警告", "info": "提示", "idea": "建议"}[f["level"]],
            f["title"]))
    ok &= len(rep["findings"]) > 0

    step(4, "安全修改")
    ops = os.path.join(tmp, "ops.json")
    C.jdump({"operations": [{"op": "set_material", "name": "铝"},
                            {"op": "set_property", "name": "版本", "value": "B"},
                            {"op": "rebuild"}]}, ops)
    pres, _ = pt.run(sw, res["path"], ops, apply=True)
    for s in pres.get("steps") or []:
        print("   {} {}".format("v" if s["ok"] else "x", s["detail"]))
    ok &= bool(pres.get("ok"))

    step(5, "回读确认")
    data2 = modelinfo.inspect(sw, res["path"])
    mat = (data2["summary"]["materials"] or [{}])[0].get("material", "")
    dens = data2["summary"]["mass"]["density_kg_m3"]
    print("   材料 {}  密度 {} kg/m³  版本 {}".format(
        mat, dens, (data2["properties"] or {}).get("版本")))
    ok &= dens > 0 and mat != ""

    print("\n" + "=" * 64)
    print("自检{}".format("通过" if ok else "未通过"))
    print("=" * 64)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(prog="swc", description="SolidWorks Copilot")
    ap.add_argument("--launch", action="store_true",
                    help="允许在 SolidWorks 未运行时启动它（默认不启动，避免多开）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="环境体检").set_defaults(func=cmd_doctor)
    sub.add_parser("info", help="会话信息").set_defaults(func=cmd_info)
    sub.add_parser("recipes", help="列出内置配方").set_defaults(func=cmd_recipes)
    sub.add_parser("selftest", help="全链路自检").set_defaults(func=cmd_selftest)

    p = sub.add_parser("inspect", help="读取模型档案")
    p.add_argument("path")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("review", help="工程审查")
    p.add_argument("path")
    p.add_argument("--json")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("report", help="生成 HTML 报告")
    p.add_argument("path")
    p.add_argument("-o", "--out")
    p.add_argument("--no-shot", action="store_true")
    p.add_argument("--json")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("build", help="按配方建模")
    p.add_argument("recipe")
    p.add_argument("--apply", action="store_true")
    p.add_argument("-s", "--save")
    p.add_argument("--no-shot", action="store_true")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("patch", help="安全修改模型")
    p.add_argument("path")
    p.add_argument("ops")
    p.add_argument("--apply", action="store_true")
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_patch)

    a = ap.parse_args()
    # --launch 透传给 connect
    if a.launch:
        _orig = C.connect
        C.connect = lambda **kw: _orig(launch=True, **kw)
    return a.func(a) or 0


if __name__ == "__main__":
    sys.exit(main())
