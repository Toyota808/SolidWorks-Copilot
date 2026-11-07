# -*- coding: utf-8 -*-
"""
report.py — 把审查结论渲染成单文件 HTML 报告

设计目标：一屏看完「这模型有什么问题、凭什么这么说、怎么改」。
图片 base64 内联，发给同事或打印都不会掉图；不依赖任何外部资源。

用法：
    python report.py 模型.SLDPRT [-o 报告.html] [--no-shot] [--json 结果.json]
"""
from __future__ import annotations

import base64
import datetime
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swcore as C
import modelinfo
import review as R

LEVEL = {
    "critical": ("严重", "#b42318", "#fef3f2", "#fecdca"),
    "warn": ("警告", "#b54708", "#fffaeb", "#fedf89"),
    "info": ("提示", "#175cd3", "#eff8ff", "#b2ddff"),
    "idea": ("建议", "#475467", "#f9fafb", "#e4e7ec"),
}

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{--ink:#101828;--sub:#667085;--line:#eaecf0;--bg:#fcfcfd;--card:#fff;
--brand:#175cd3;--soft:#f9fafb}
body{background:var(--bg);color:var(--ink);padding:42px 24px 68px;font-size:15px;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
line-height:1.66;-webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:28px;
padding-bottom:20px;border-bottom:1px solid var(--line);margin-bottom:24px}
h1{font-size:24px;font-weight:700;letter-spacing:-.4px}
.meta{color:var(--sub);font-size:12.5px;margin-top:8px;word-break:break-all;line-height:1.55}
.score{font-size:52px;font-weight:800;line-height:.95;letter-spacing:-2.5px;text-align:right}
.grade{font-size:12.5px;color:var(--sub);margin-top:7px;text-align:right;white-space:nowrap}
.verdict{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--brand);
border-radius:0 12px 12px 0;padding:15px 20px;margin-bottom:24px;color:#344054}
.grid{display:grid;grid-template-columns:1.02fr .98fr;gap:18px;margin-bottom:18px}
@media(max-width:880px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:19px 21px}
.card h2{font-size:11.5px;font-weight:700;color:var(--sub);letter-spacing:.09em;
text-transform:uppercase;margin-bottom:13px}
.shot{width:100%;border-radius:8px;display:block;background:#f2f4f7}
.metrics{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.metric{background:var(--soft);border-radius:10px;padding:11px 13px}
.metric .k{font-size:11.5px;color:var(--sub)}
.metric .v{font-size:17.5px;font-weight:700;margin-top:3px;word-break:break-all}
.metric .v small{font-size:11.5px;font-weight:500;color:var(--sub);margin-left:3px}
.axes{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:26px}
.axis{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.axis .t{font-size:12px;color:var(--sub);display:flex;justify-content:space-between;gap:6px}
.axis .track{height:4px;background:#f2f4f7;border-radius:99px;margin-top:8px;overflow:hidden}
h3.sec{font-size:14px;font-weight:700;margin:28px 0 11px;display:flex;align-items:center;gap:9px}
h3.sec .n{display:inline-flex;align-items:center;justify-content:center;min-width:20px;
height:20px;padding:0 6px;border-radius:99px;background:#f2f4f7;color:var(--sub);
font-size:11.5px;font-weight:600}
.item{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:15px 17px;margin-bottom:9px}
.item .top{display:flex;align-items:center;gap:9px;margin-bottom:8px;flex-wrap:wrap}
.tag{font-size:11px;font-weight:700;padding:2.5px 9px;border-radius:99px;border:1px solid transparent}
.iid{font-size:11px;color:#98a2b3;font-family:ui-monospace,Consolas,monospace}
.item .title{font-size:14.5px;font-weight:650;flex:1 1 auto}
.row{display:flex;gap:10px;font-size:13.5px;color:#344054;margin-top:5px}
.row b{color:var(--sub);font-weight:600;flex:0 0 30px}
.fix{font-family:ui-monospace,Consolas,monospace;font-size:12px;background:#101828;
color:#d0d5dd;padding:11px 13px;border-radius:9px;overflow-x:auto;margin-top:10px;white-space:pre}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--sub);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--line);font-size:12px}
td{padding:7px 10px;border-bottom:1px solid #f4f5f7}
td.d{color:var(--sub)}
td.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#344054}
.pill{font-size:11px;padding:1.5px 7px;border-radius:99px;background:#f2f4f7;color:#475467}
.pill.bad{background:#fef3f2;color:#b42318}
.empty{padding:56px 0;text-align:center;color:#98a2b3;font-size:13px}
footer{margin-top:38px;padding-top:15px;border-top:1px solid var(--line);color:#98a2b3;
font-size:12px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
@media print{body{background:#fff;padding:0;font-size:11pt}.card,.item{break-inside:avoid}}
"""


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def b64(path):
    try:
        with open(path, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode()
    except Exception:
        return ""


def _color(v):
    return "#067647" if v >= 85 else ("#175cd3" if v >= 70 else ("#b54708" if v >= 50 else "#b42318"))


def build_html(data, rep, shot=None, title="三维模型设计审查报告"):
    s = data.get("summary") or {}
    m = s.get("mass") or {}
    b = s.get("bbox") or {}
    d = data.get("document") or {}
    sc = rep["score"]
    fee = data.get("file") or {}

    mats = [x["material"] for x in (s.get("materials") or []) if x.get("material")]
    mat_txt = "、".join(dict.fromkeys(mats)) or "未指定"
    size = " × ".join("{:g}".format(x) for x in (b.get("size_mm") or [])) or "—"

    metrics = [
        ("外形尺寸", "{}<small>mm</small>".format(size)),
        ("体积", "{:,.0f}<small>mm³</small>".format(m.get("volume_mm3", 0))),
        ("质量", "{:,.1f}<small>g</small>".format(m.get("mass_g", 0))),
        ("材料", "{}<small>{:.0f} kg/m³</small>".format(esc(mat_txt), m.get("density_kg_m3", 0))),
        ("建模特征", "{}<small>个</small>".format(s.get("feature_count", 0))),
        ("实体 / 草图", "{} / {}<small>个</small>".format(s.get("solid_bodies", 0), s.get("sketch_count", 0))),
    ]
    mh = "".join('<div class="metric"><div class="k">{}</div><div class="v">{}</div></div>'.format(k, v)
                 for k, v in metrics)

    axes = ""
    for cat in R.CAT_ORDER:
        n = sc["by_category"].get(cat, 0)
        total = max(1, sum(sc["by_category"].values()))
        pct = 100 if n == 0 else max(6, int(100 * n / total))
        col = "#067647" if n == 0 else ("#b54708" if n <= 2 else "#b42318")
        axes += ('<div class="axis"><div class="t"><span>{}</span>'
                 '<span style="color:{};font-weight:600">{} 项</span></div>'
                 '<div class="track"><div style="width:{}%;height:100%;border-radius:99px;background:{}"></div></div></div>'
                 ).format(cat, col, n, pct, col)

    rows = list(rep["findings"])
    rows.sort(key=lambda it: (R.CAT_ORDER.index(it["category"]) if it["category"] in R.CAT_ORDER else 9,
                              R.LEVEL_IDX.get(it["level"], 9)))
    body, cat = [], None
    for it in rows:
        if it["category"] != cat:
            cat = it["category"]
            cnt = sum(1 for x in rows if x["category"] == cat)
            body.append('<h3 class="sec">{}<span class="n">{}</span></h3>'.format(esc(cat), cnt))
        cn, col, bg, bd = LEVEL.get(it["level"], ("提示", "#475467", "#f9fafb", "#e4e7ec"))
        fix = '<div class="fix">{}</div>'.format(esc(C.jdump(it["fix"]))) if it.get("fix") else ""
        body.append("""<div class="item">
  <div class="top"><span class="tag" style="color:{c};background:{bg};border-color:{bd}">{cn}</span>
  <span class="title">{t}</span><span class="iid">{i}</span></div>
  <div class="row"><b>依据</b><span>{e}</span></div>
  <div class="row"><b>建议</b><span>{s}</span></div>{fix}
</div>""".format(c=col, bg=bg, bd=bd, cn=cn, t=esc(it["title"]), i=esc(it.get("id", "")),
                e=esc(it["evidence"]), s=esc(it["suggestion"]), fix=fix))
    if not rows:
        body.append('<div class="item"><div class="title">未发现需要处理的问题</div></div>')

    feats = data.get("features") or []
    frows = []
    for f in feats[:20]:
        st = ('<span class="pill bad">压缩</span>' if f.get("suppressed")
              else ('<span class="pill bad">报错</span>' if f.get("error") else '<span class="pill">正常</span>'))
        frows.append("<tr><td>{}</td><td class='d'>{}</td><td>{}</td></tr>".format(
            esc(f["name"]), esc(f["type_cn"]), st))
    if len(feats) > 20:
        frows.append("<tr><td colspan='3' class='d'>… 其余 {} 项省略</td></tr>".format(len(feats) - 20))

    dims = [x for x in (data.get("dimensions") or []) if x.get("driven_state") != 2][:14]
    drows = "".join("<tr><td class='mono'>{}</td><td class='d'>{}</td><td class='mono'>{} mm</td></tr>".format(
        esc(x["name"]), esc(x.get("feature", "")), x["value_mm"]) for x in dims) or \
        "<tr><td colspan='3' class='d'>没有驱动尺寸（模型不可参数化改型）</td></tr>"

    props = data.get("properties") or {}
    if isinstance(props, dict):
        items = list(props.items())
    else:
        items = [(p.get("name"), p.get("value")) for p in props]
    prows = "".join("<tr><td>{}</td><td class='mono'>{}</td></tr>".format(esc(k), esc(v))
                    for k, v in items[:12]) or "<tr><td colspan='2' class='d'>未填写自定义属性</td></tr>"

    shot_html = ('<img class="shot" src="{}" alt="模型快照">'.format(b64(shot)) if shot
                 else '<div class="empty">未生成快照</div>')
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · {name}</title><style>{css}</style></head>
<body><div class="wrap">
<header>
  <div><h1>{title}</h1>
    <div class="meta">{name} &nbsp;·&nbsp; {ptype}<br>{path}<br>
    生成于 {now} &nbsp;·&nbsp; 该模型共 {fixn} 条建议</div></div>
  <div><div class="score" style="color:{scol}">{score}</div>
    <div class="grade">{grade} · 严重 {crit} · 警告 {warn}</div></div>
</header>
<div class="verdict">{headline}</div>
<div class="grid">
  <div class="card"><h2>模型快照</h2>{shot}</div>
  <div class="card"><h2>关键指标</h2><div class="metrics">{metrics}</div></div>
</div>
<div class="axes">{axes}</div>
{body}
<h3 class="sec">主要建模特征<span class="n">{fcount}</span></h3>
<div class="card"><table>
<thead><tr><th style="width:44%">名称</th><th style="width:34%">类型</th><th>状态</th></tr></thead>
<tbody>{frows}</tbody></table></div>
<h3 class="sec">驱动尺寸</h3>
<div class="card"><table>
<thead><tr><th style="width:44%">尺寸</th><th style="width:34%">所属特征</th><th>数值</th></tr></thead>
<tbody>{drows}</tbody></table></div>
<h3 class="sec">自定义属性</h3>
<div class="card"><table>
<thead><tr><th style="width:32%">名称</th><th>值</th></tr></thead>
<tbody>{prows}</tbody></table></div>
<footer><span>SolidWorks Copilot · 自动设计审查</span>
<span>结论由模型几何与特征数据推导，重大改动请结合工况复核</span></footer>
</div></body></html>""".format(
        title=esc(title), name=esc(fee.get("name", "")), ptype=esc(rep.get("part_type", "")),
        path=esc(fee.get("path", "")), now=now, fixn=len(rows),
        scol=_color(sc["total"]), score=sc["total"], grade=esc(sc["grade"]),
        crit=sc["critical"], warn=sc["warn"], headline=esc(rep["headline"]),
        shot=shot_html, metrics=mh, axes=axes, body="".join(body),
        fcount=len(feats), frows="".join(frows), drows=drows, prows=prows, css=CSS)


def make(sw, path, out=None, shot=True, json_out=None):
    data = modelinfo.inspect(sw, path)
    rep = R.review(data)

    png = None
    if shot:
        try:
            doc = sw.open(path, reload=False)
            png = doc.screenshot(os.path.splitext(os.path.abspath(path))[0] + "_快照.png")
        except Exception as e:
            print("（快照生成失败：{}）".format(e))

    html = build_html(data, rep, png)
    out = out or os.path.splitext(os.path.abspath(path))[0] + "_审查报告.html"
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    if json_out:
        C.jdump({"inspect": data, "review": rep}, json_out)
    return out, rep


def main(argv):
    C.setup_stdout()
    ap = argparse.ArgumentParser(description="生成模型审查报告")
    ap.add_argument("path")
    ap.add_argument("-o", "--out")
    ap.add_argument("--no-shot", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args(argv)

    sw = C.connect()
    out, rep = make(sw, a.path, a.out, shot=not a.no_shot, json_out=a.json)
    print("报告已生成：{}".format(out))
    print("评分 {}/100（{}）· 严重 {} · 警告 {} · 共 {} 条".format(
        rep["score"]["total"], rep["score"]["grade"],
        rep["score"]["critical"], rep["score"]["warn"], len(rep["findings"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
