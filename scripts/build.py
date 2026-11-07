# -*- coding: utf-8 -*-
"""
build.py — 用 JSON 配方在 SolidWorks 里建模

设计思路：把「设计意图」和「建模动作」分开。
  · vars  里放参数（直径、厚度、孔数…），steps 里全部引用变量
  · 改一个 var，整套模型联动，不用重画

配方格式见 references/recipe-schema.md，示例见 assets/recipes/*.json

用法：
    python build.py 配方.json              # 预演：只校验参数，不建模
    python build.py 配方.json --apply      # 真正建模
    python build.py 配方.json --apply --out 输出.SLDPRT
"""
from __future__ import annotations

import ast
import math
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swcore as C

# 表达式里只允许这些语法，禁止函数调用/属性访问，避免配方变成任意代码执行
_ALLOWED = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.FloorDiv,
    ast.USub, ast.UAdd,
)


def ev(expr, vars_):
    """安全求值：支持变量、四则运算、幂、括号"""
    if expr is None:
        return None
    if isinstance(expr, (int, float)) and not isinstance(expr, bool):
        return float(expr)
    s = str(expr).strip()
    try:
        return float(s)
    except ValueError:
        pass
    tree = ast.parse(s, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError("表达式含不允许的语法 {}：{}".format(type(node).__name__, s))
        if isinstance(node, ast.Name) and node.id not in vars_:
            raise ValueError("未知变量 {}（可用：{}）".format(node.id, ", ".join(sorted(vars_))))
    return float(eval(compile(tree, "<recipe>", "eval"), {"__builtins__": {}}, dict(vars_)))


class Builder:
    def __init__(self, sw, recipe):
        self.sw = sw
        self.r = recipe
        self.vars = {k: float(v) for k, v in (recipe.get("vars") or {}).items()}
        self.doc = None
        self.log = []
        self._last_sketch = None

    # -- 表达式
    def num(self, v, default=None):
        if v is None:
            return default
        return ev(v, self.vars)

    # -- 草图原语
    def _sketch_on(self, plane):
        """在基准面上开草图。

        必须先选中基准面再 InsertSketch，否则 SolidWorks 不会创建草图，
        后续 CreateCircle 会静默失败（返回 None 但不报错）。
        """
        p = self.doc.plane(plane)
        if p is None:
            raise C.SWError("找不到基准面：{}".format(plane))
        self.doc.m.ClearSelection2(True)
        p.Select2(False, 0)
        self.doc.m.SketchManager.InsertSketch(True)
        if C.V(self.doc.m.SketchManager, "ActiveSketch", default=None) is None:
            raise C.SWError("无法在 {} 面上创建草图".format(plane))

    def _sketch_close(self):
        self.doc.m.SketchManager.InsertSketch(True)
        names = [f["name"] for f in self.doc.features() if f["type"] == "ProfileFeature"]
        self._last_sketch = names[-1] if names else None
        return self._last_sketch

    def _extrude(self, depth, both=False, draft=0.0):
        fm = self.doc.m.FeatureManager
        d = C.m(depth)
        f = fm.FeatureExtrusion3(
            True, False, bool(both), 0, 0, d, d,
            False, False, False, False, float(draft), float(draft),
            False, False, False, False,
            True, True, True, 0, 0.0, False)
        if f is None:
            raise C.SWError("拉伸失败（草图可能不闭合）")
        return C.V(f, "Name")

    def _cut(self, depth, through=True):
        """切除。T1=T2=1 表示两个方向都贯穿，避免草图所在面与材料方向相反时切不到。"""
        fm = self.doc.m.FeatureManager
        d = C.m(depth)
        t1 = t2 = 1 if through else 0
        f = fm.FeatureCut4(
            False, False, False, t1, t2, d, d,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False,
            False, True, True, False, False, False,
            0, 0.0, False, False)
        if f is None:
            raise C.SWError("切除失败（轮廓不闭合，或切除方向没有材料）")
        return C.V(f, "Name")

    # -- 步骤
    def op_circle(self, st):
        plane = st.get("plane", "front")
        r = self.num(st.get("r"))
        cx = self.num(st.get("cx"), 0.0) or 0.0
        cy = self.num(st.get("cy"), 0.0) or 0.0
        self._sketch_on(plane)
        seg = self.doc.m.SketchManager.CreateCircleByRadius(
            C.m(cx), C.m(cy), 0.0, C.m(r))
        if seg is None:
            raise C.SWError("画圆失败")
        return "⌀{:.1f}mm @({:.1f},{:.1f})".format(r * 2, cx, cy)

    def op_rect(self, st):
        plane = st.get("plane", "front")
        w = self.num(st.get("w"))
        h = self.num(st.get("h"))
        cx = self.num(st.get("cx"), 0.0) or 0.0
        cy = self.num(st.get("cy"), 0.0) or 0.0
        self._sketch_on(plane)
        x1, x2 = C.m(cx - w / 2), C.m(cx + w / 2)
        y1, y2 = C.m(cy - h / 2), C.m(cy + h / 2)
        if self.doc.m.SketchManager.CreateCornerRectangle(
                x1, y1, 0, x2, y2, 0) is None:
            raise C.SWError("画矩形失败")
        return "{:.1f}×{:.1f}mm".format(w, h)

    def op_polygon(self, st):
        """正多边形（内接圆半径）"""
        plane = st.get("plane", "front")
        n = int(self.num(st.get("n"), 6))
        r = self.num(st.get("r"))
        self._sketch_on(plane)
        sm = self.doc.m.SketchManager
        pts = []
        for i in range(n):
            ang = 2 * math.pi * i / n
            pts.append(sm.CreateLine(C.m(r * math.cos(ang)), C.m(r * math.sin(ang)), 0,
                                     C.m(r * math.cos(2 * math.pi * (i + 1) / n)),
                                     C.m(r * math.sin(2 * math.pi * (i + 1) / n)), 0))
        if any(p is None for p in pts):
            raise C.SWError("画多边形失败")
        return "{}-{:.0f}mm".format(n, r)

    def op_extrude(self, st):
        depth = self.num(st.get("depth"))
        both = bool(st.get("both", False))
        draft = self.num(st.get("draft"), 0.0) or 0.0
        self._sketch_close()
        name = self._extrude(depth, both, draft)
        return "拉伸 {:.2f}mm → {}".format(depth, name)

    def op_cut(self, st):
        through = bool(st.get("through_all", True))
        depth = self.num(st.get("depth"), 10.0) or 10.0
        self._sketch_close()
        name = self._cut(depth, through)
        return "{} → {}".format("完全贯穿" if through else "切深 {:.2f}mm".format(depth), name)

    def op_bolt_circle(self, st):
        """均布孔：在同一个草图里画 n 个圆，一次切除完成。

        比「一个孔 + 圆周阵列」可靠得多——阵列接口对 ICE 类型的切除特征
        经常返回空，而多圆一次切除是确定能成的。
        """
        plane = st.get("plane", "front")
        n = int(self.num(st.get("n"), 4))
        pcd = self.num(st.get("pcd"))
        d = self.num(st.get("d"))
        start = self.num(st.get("start_angle"), 0.0) or 0.0
        self._sketch_on(plane)
        sm = self.doc.m.SketchManager
        for i in range(n):
            ang = math.radians(start) + 2 * math.pi * i / n
            x = C.m(pcd / 2 * math.cos(ang))
            y = C.m(pcd / 2 * math.sin(ang))
            if sm.CreateCircleByRadius(x, y, 0.0, C.m(d / 2)) is None:
                raise C.SWError("第 {} 个孔画失败".format(i + 1))
        self._sketch_close()
        name = self._cut(1.0, True)
        return "{}×⌀{:.1f} 均布（PCD {:.1f}）→ {}".format(n, d, pcd, name)

    def op_holes(self, st):
        """按坐标列表画多个圆并一次切除（角孔、腰形孔阵列等）。

        at: [[x, y], [x, y], ...] 单位 mm；支持表达式。
        """
        plane = st.get("plane", "front")
        d = self.num(st.get("d"))
        at = st.get("at") or []
        if not at:
            raise C.SWError("holes 需要 at 坐标列表")
        self._sketch_on(plane)
        sm = self.doc.m.SketchManager
        for i, xy in enumerate(at):
            x = self.num(xy[0], 0.0) or 0.0
            y = self.num(xy[1], 0.0) or 0.0
            if sm.CreateCircleByRadius(C.m(x), C.m(y), 0.0, C.m(d / 2)) is None:
                raise C.SWError("第 {} 个孔画失败".format(i + 1))
        self._sketch_close()
        name = self._cut(1.0, True)
        return "{}×⌀{:.1f} 定位孔 → {}".format(len(at), d, name)

    def op_shell(self, st):
        t = self.num(st.get("thickness"))
        outward = bool(st.get("outward", False))
        f = self.doc.m.FeatureManager.InsertFeatureShell(C.m(t), outward)
        if f is None:
            raise C.SWError("抽壳失败")
        return "抽壳 {:.2f}mm → {}".format(t, C.V(f, "Name"))

    def op_material(self, st):
        want = st.get("name")
        try:
            import matlib
            used = matlib.resolve(want)
        except Exception:
            used = want
        ok, got = self.doc.set_material(used)
        return "材料 {}{}".format(got or used, "" if ok else "（写入未确认）")

    def op_property(self, st):
        self.doc.set_custom_property(st["name"], st.get("value", ""))
        return "属性 {}={}".format(st["name"], st.get("value"))

    # -- 主流程
    def validate(self, steps):
        """预演：只解析表达式和操作名，不碰 SolidWorks。

        预演不能复用执行函数——那时还没有文档对象，会直接 AttributeError。
        """
        report = []
        for i, st in enumerate(steps, 1):
            op = st.get("op")
            if op not in OPS:
                raise C.SWError("第 {} 步未知操作：{}（支持：{}）".format(
                    i, op, "、".join(OPS)))
            label, _, keys = OPS[op]
            vals = {}
            for k in keys:
                if k in st and st[k] is not None:
                    vals[k] = self.num(st[k])
            if op in ("circle", "rect", "polygon", "bolt_circle", "holes", "extrude", "cut", "shell") \
                    and "plane" in st and st["plane"] not in ("front", "top", "right"):
                raise C.SWError("第 {} 步 plane 只能是 front/top/right，收到：{}".format(i, st["plane"]))
            report.append({"step": i, "op": op, "label": label, "params": vals})
        return report

    def run(self, apply=False):
        steps = self.r.get("steps") or []
        if not steps:
            raise C.SWError("配方里没有 steps")

        if not apply:
            return {"ok": True, "dry_run": True, "steps": self.validate(steps)}

        self.doc = self.sw.new_part(self.r.get("template"))
        for i, st in enumerate(steps, 1):
            op = st.get("op")
            if op not in OPS:
                raise C.SWError("第 {} 步未知操作：{}".format(i, op))
            label, fn, _ = OPS[op]
            try:
                msg = fn(self, st)
                self.log.append({"step": i, "op": op, "ok": True, "detail": msg})
            except Exception as e:
                self.log.append({"step": i, "op": op, "ok": False, "error": str(e)})
                raise C.SWError("第 {} 步 {} 失败：{}".format(i, label, e))
        return {"ok": True, "steps": self.log}


# name -> (中文名, 执行函数, 预演需要求值的数值参数)
OPS = {
    "circle": ("画圆", Builder.op_circle, ["r", "cx", "cy"]),
    "rect": ("画矩形", Builder.op_rect, ["w", "h", "cx", "cy"]),
    "polygon": ("正多边形", Builder.op_polygon, ["n", "r"]),
    "extrude": ("拉伸", Builder.op_extrude, ["depth", "draft"]),
    "cut": ("切除", Builder.op_cut, ["depth"]),
    "bolt_circle": ("均布孔", Builder.op_bolt_circle, ["n", "pcd", "d"]),
    "holes": ("定位孔", Builder.op_holes, ["d"]),
    "shell": ("抽壳", Builder.op_shell, ["thickness"]),
    "material": ("赋材料", Builder.op_material, []),
    "property": ("写属性", Builder.op_property, []),
}


def run_recipe(sw, recipe_path, apply=False, out_path=None, shot=True):
    recipe = C.jload(recipe_path)
    b = Builder(sw, recipe)
    result = {"recipe": recipe.get("name"), "file": recipe_path}
    if not apply:
        result.update(b.run(apply=False))
        return result

    r = b.run(apply=True)
    result.update(r)

    doc = b.doc
    target = out_path
    if not target:
        base = recipe.get("save_as") or (os.path.splitext(os.path.basename(recipe_path))[0] + ".SLDPRT")
        target = os.path.join(os.path.dirname(os.path.abspath(recipe_path)), base)
    if not os.path.splitext(target)[1]:
        target += ".SLDPRT"

    result["saved"] = doc.save_as(target)
    result["path"] = os.path.abspath(target)
    result["metrics"] = {
        "mass": doc.mass_props(),
        "bbox": doc.bbox(),
        "materials": doc.materials(),
        "features": [{"name": f["name"], "type": f["type_cn"]} for f in doc.features(model_only=True)],
    }
    if shot:
        png = os.path.splitext(target)[0] + "_预览.png"
        result["preview"] = doc.screenshot(png)
    return result


def main(argv):
    C.setup_stdout()
    ap = argparse.ArgumentParser(description="按 JSON 配方建模")
    ap.add_argument("recipe")
    ap.add_argument("--apply", action="store_true", help="真正建模（默认只预演）")
    ap.add_argument("--out", "-s", help="零件保存路径")
    ap.add_argument("--no-shot", action="store_true", help="不生成预览图")
    ap.add_argument("--json", help="结果写入 JSON")
    a = ap.parse_args(argv)

    sw = C.connect()
    res = run_recipe(sw, a.recipe, apply=a.apply, out_path=a.out, shot=not a.no_shot)

    if a.json:
        C.jdump(res, a.json)
    if not a.apply:
        print("预演通过：{} 步".format(len(res.get("steps") or [])))
        for s in res.get("steps") or []:
            params = "  ".join("{}={:.3g}".format(k, v) for k, v in (s["params"] or {}).items())
            print("  {}. {:<10} {}".format(s["step"], s["label"], params))
        return 0

    m = res["metrics"]["mass"]
    b = res["metrics"]["bbox"]
    print("建模完成 → {}".format(res["path"]))
    for s in res["steps"]:
        print("  {}. {:<8} {}".format(s["step"], s["op"], s["detail"] if s["ok"] else s["error"]))
    if b:
        print("外形：{} × {} × {} mm".format(*[round(x, 2) for x in b.get("size_mm", [])]))
    print("体积：{:.1f} mm³  质量：{:.1f} g".format(
        m.get("volume_mm3", 0), m.get("mass_g", 0)))
    if res.get("preview"):
        print("预览：{}".format(res["preview"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
