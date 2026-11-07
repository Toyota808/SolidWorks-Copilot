# -*- coding: utf-8 -*-
"""
matlib.py — SolidWorks 材料库名称解析

SetMaterialPropertyName2 要求材料名与材料库里的名字**逐字一致**，
差一个字就静默失败（返回成功但模型里还是「未指定」）。
这里把 .sldmat 里的材料名读出来，做模糊匹配和自动纠名。

用法：
    python matlib.py 铝            # 搜索含「铝」的材料
    python matlib.py --check 6061-T6
"""
from __future__ import annotations

import os
import re
import sys
import glob

_CACHE = None

# 常见材料库根目录（不同版本/安装位置都覆盖一下）
_ROOTS = [
    r"E:\Solidworks2025\SOLIDWORKS\lang",
    r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\lang",
    r"D:\Solidworks2025\SOLIDWORKS\lang",
    r"C:\ProgramData\SOLIDWORKS",
    os.path.expanduser(r"~\AppData\Roaming\SOLIDWORKS"),
]

# 中文习惯叫法 -> 材料库里可能的名字片段（按优先级）
ALIAS = {
    "钢": ["普通碳钢", "碳钢", "合金钢"],
    "碳钢": ["普通碳钢", "碳钢"],
    "不锈钢": ["不锈钢", "AISI 304", "304"],
    "铝": ["6061", "1060", "5052", "铝合金"],
    "铝合金": ["6061-T6", "6061", "1060 铝合金"],
    "6061": ["6061-T6", "6061"],
    "黄铜": ["黄铜", "Brass"],
    "铜": ["铜", "Copper"],
    "铸铁": ["灰铸铁", "铸铁"],
    "abs": ["ABS"],
    "尼龙": ["尼龙", "Nylon", "PA"],
    "pe": ["PE", "聚乙烯"],
    "pp": ["PP", "聚丙烯"],
    "pom": ["POM", "赛钢", "Delrin"],
    "钛": ["钛", "Titanium"],
}


def _decode(path):
    raw = open(path, "rb").read()
    for enc in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "ignore")


def find_files():
    out = []
    for root in _ROOTS:
        if not os.path.isdir(root):
            continue
        for p in glob.glob(os.path.join(root, "**", "*.sldmat"), recursive=True):
            if p not in out:
                out.append(p)
    return out


def scan(force=False):
    """扫描所有材料库，返回 [{name, library, path}]"""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    items = []
    for f in find_files():
        try:
            txt = _decode(f)
        except Exception:
            continue
        lib = os.path.splitext(os.path.basename(f))[0]
        # 材料库是 XML：<material name="普通碳钢" ...>
        for m in re.finditer(r'<material\s+name="([^"]+)"', txt):
            name = m.group(1).strip()
            if name:
                items.append({"name": name, "library": lib, "path": f})
    _CACHE = items
    return items


def resolve(want, strict=False):
    """把用户随口说的材料名映射到材料库里的真实名字。

    匹配顺序：精确 → 忽略大小写 → 别名（长关键词优先）→ 子串。
    别名必须按关键词长度倒序匹配，否则「不锈钢」会先命中「钢」→ 碳钢，判错。
    """
    want = str(want or "").strip()
    if not want:
        return want
    items = scan()
    names = [i["name"] for i in items]

    # 1. 精确
    for n in names:
        if n == want:
            return n
    # 2. 忽略大小写 / 空格
    low = want.lower().replace(" ", "")
    for n in names:
        if n.lower().replace(" ", "") == low:
            return n
    # 3. 别名表（长关键词优先，短名优先）
    for key in sorted(ALIAS, key=len, reverse=True):
        if key.lower() not in low:
            continue
        for cand in ALIAS[key]:
            hits = [n for n in names if cand.lower() in n.lower()]
            if hits:
                return sorted(hits, key=len)[0]
    # 4. 子串（双向，短名优先）
    if not strict:
        hits = [n for n in names if low in n.lower()]
        if hits:
            return sorted(hits, key=len)[0]
        hits = [n for n in names if n.lower() in low]
        if hits:
            return sorted(hits, key=len)[0]
    return want


def suggest(want, limit=8):
    """列出最接近的几个候选，用于报错时提示"""
    want = str(want or "").strip().lower()
    items = scan()
    scored = []
    for i in items:
        n = i["name"].lower()
        if not want:
            continue
        score = 3 if want in n else 0
        score += 2 if n.startswith(want) else 0
        score += len(set(want) & set(n)) / max(len(set(want) | set(n)), 1)
        if score > 1:
            scored.append((score, i["name"], i["library"]))
    scored.sort(key=lambda x: -x[0])
    return [{"name": s[1], "library": s[2]} for s in scored[:limit]]


def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not argv or argv[0] in ("-h", "--help"):
        print("用法：python matlib.py <材料名关键词>   |   python matlib.py --check <材料名>")
        return 0
    if argv[0] == "--check":
        want = " ".join(argv[1:])
        got = resolve(want)
        ok = got.lower() == want.lower() or got == resolve(got)
        print("{} -> {}".format(want, got))
        print("{}".format("✓ 材料库中存在" if got.lower() == str(want).lower() else "! 已自动纠名，请确认"))
        return 0

    key = " ".join(argv)
    hits = [i for i in scan() if key.lower() in i["name"].lower()]
    print("匹配「{}」的材料：{} 个（库内共 {} 条）".format(key, len(hits), len(scan())))
    for i in hits[:30]:
        print("  · {}  [{}]".format(i["name"], i["library"]))
    if not hits:
        for s in suggest(key):
            print("  近似：{}  [{}]".format(s["name"], s["library"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
