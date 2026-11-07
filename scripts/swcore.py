# -*- coding: utf-8 -*-
"""
swcore.py — SolidWorks COM 底层封装

这一层只做三件事：
  1. 连上 SolidWorks（只附加，不偷偷多开）
  2. 把 IDispatch 包成强类型接口（否则大部分方法会静默返回 None）
  3. 提供读写文档的基本原语，单位统一对外用 mm

所有对外 JSON 字段一律 mm / g / mm³，内部才用 SolidWorks 的 SI 单位。
"""
from __future__ import annotations

import os
import sys
import json
import time
import math
import shutil
import datetime

try:
    import pythoncom
    import win32com.client as _w
except ImportError:  # 让报错信息可读，而不是 ImportError 栈
    pythoncom = None
    _w = None

GUID = "{83A33D31-27C5-11CE-BFD4-00400513BB57}"   # SldWorks 类型库

# 文档类型
swDocPART, swDocASSEMBLY, swDocDRAWING = 1, 2, 3
DOC_CN = {1: "零件", 2: "装配体", 3: "工程图"}
DOC_EXT = {1: ".SLDPRT", 2: ".SLDASM", 3: ".SLDDRW"}

# 主版本号 -> 年份
SW_YEAR = {28: 2020, 29: 2021, 30: 2022, 31: 2023, 32: 2024, 33: 2025, 34: 2026}

# 特征类型 -> 中文名（GetTypeName2 的返回值）
FEAT_CN = {
    "RefPlane": "基准面", "RefAxis": "基准轴", "CoordSys": "坐标系",
    "ProfileFeature": "草图", "OriginProfileFeature": "原点",
    "Extrusion": "凸台-拉伸", "Boss": "凸台", "Revolve": "旋转",
    "Cut": "切除-拉伸", "ICE": "切除-拉伸", "ExtrusionCut": "切除-拉伸",
    "HoleWzd": "异型孔向导", "Hole": "孔",
    "Fillet": "圆角", "Chamfer": "倒角", "Shell": "抽壳", "Draft": "拔模",
    "Rib": "筋", "Sweep": "扫描", "Loft": "放样", "MirrorPattern": "镜像",
    "LPattern": "线性阵列", "CirPattern": "圆周阵列",
    "BaseFlange": "基体法兰", "EdgeFlange": "边线法兰", "SketchBend": "绘制的折弯",
    "MateGroup": "配合组", "Mate2": "配合", "CThread": "螺纹线",
    "LibraryFeature": "库特征", "MacroFeature": "宏特征",
}

# 建模特征白名单：判断模型复杂度、生成审查结论时只看这些
MODEL_FEATS = {
    "Extrusion", "Boss", "Revolve", "Cut", "ICE", "ExtrusionCut", "HoleWzd",
    "Hole", "Fillet", "Chamfer", "Shell", "Draft", "Rib", "Sweep", "Loft",
    "MirrorPattern", "LPattern", "CirPattern", "BaseFlange", "EdgeFlange",
    "SketchBend", "CThread", "LibraryFeature", "MacroFeature",
}

# 界面节点：特征树里不是几何的东西，统计时排除
UI_NODES = {
    "FavoriteFolder", "History", "SelectionSetFolder", "SensorFolder",
    "DesignBinder", "Annotation", "Note", "CommentFolder", "LightsAndCameras",
    "MaterialFolder", "TablesFolder", "SolidBodyFolder", "SurfaceBodyFolder",
    "RefPlaneFolder", "EnvFolder", "DocsFolder", "CtThreadFolder",
    "DetailCabinet", "DocFolder", "AttributeFolder", "WeldFolder",
    "CutListFolder", "DimPattern", "EqnFolder", "SketchBlockDef",
    "BoundingBoxFolder", "OriginProfileFeature",
}

# 基准面中文/英文别名
PLANES = {
    "front": ("前视", "front"), "top": ("上视", "top"), "right": ("右视", "right"),
}

# 草图约束状态
SKETCH_STATE = {0: "未知", 1: "完全定义", 2: "过定义", 3: "欠定义", 4: "欠定义(未求解)"}


class SWError(Exception):
    pass


# ---------------------------------------------------------------- COM 基础
def is_com(obj):
    """是不是 COM 对象。

    pywin32 的 CDispatch 本身带 __call__，直接 callable(member) 判断会把
    一个 COM 属性误当成方法调用，返回 None —— 这是最容易踩的坑之一。
    """
    return obj is not None and (
        hasattr(obj, "_oleobj_") or type(obj).__name__ in ("CDispatch", "COMObject", "PyIDispatch")
    )


def V(obj, name, *args, default=None):
    """取成员值，自动兼容「属性」和「方法」两种形态"""
    if obj is None:
        return default
    try:
        member = getattr(obj, name)
    except Exception:
        return default
    if not callable(member) or is_com(member):
        return member
    try:
        return member(*args)
    except TypeError:
        try:
            return member()
        except Exception:
            return default
    except Exception:
        return default


_TLB = {}


def typelib(major=33):
    """加载 SldWorks 类型库模块。

    绝不能用 EnsureDispatch：类型库版本对不上时它会**再启动一个 SolidWorks 进程**，
    机器上就出现多个实例，COM 在它们之间乱跳，表现为「改了不生效」「读到旧数据」。
    """
    if major in _TLB:
        return _TLB[major]
    if _w is None:
        raise SWError("缺少 pywin32：python -m pip install pywin32")
    for ver in (major, 33, 32, 34, 31):
        if ver <= 0:
            continue
        try:
            mod = _w.gencache.EnsureModule(GUID, 0, ver, 0)
        except Exception:
            continue
        if mod is not None:
            _TLB[major] = mod
            return mod
    raise SWError("加载 SolidWorks 类型库失败")


def bind(obj, iface):
    """把纯 IDispatch 包成强类型接口。

    不做这一步的话，IModelDoc2.GetNextFeature() 之类会返回 None 且不报错，
    特征树遍历到第二个节点就断了。
    """
    if obj is None or not is_com(obj):
        return obj
    try:
        cls = getattr(typelib(), iface, None)
        if cls is None:
            return obj
        return cls(obj._oleobj_)
    except Exception:
        return obj


# ---------------------------------------------------------------- 工具
def jdump(obj, path=None):
    txt = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    if path:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(txt)
    return txt


def jload(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def backup(path, tag="bak"):
    """改模型前先备份，返回备份文件路径"""
    if not path or not os.path.exists(path):
        return None
    dst = "{}.{}-{}".format(path, tag, stamp())
    shutil.copy2(path, dst)
    return dst


def mm(v):
    return round(float(v) * 1000.0, 4)


def u(v, default=None):
    """解包 COM 的多返回值。

    类型库里很多方法被声明成 [out] + retval，pywin32 会返回 tuple，
    直接 int()/float() 会抛 TypeError。统一取第一个元素。
    """
    if isinstance(v, tuple):
        return v[0] if v else default
    return default if v is None else v


def m(v_mm):
    return float(v_mm) / 1000.0


_STDOUT_OK = False


def setup_stdout():
    """让中文正常输出。必须幂等：重复包一层会把上一个 wrapper 的 buffer 关掉。"""
    global _STDOUT_OK
    if _STDOUT_OK:
        return
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:
            continue
        try:
            if hasattr(s, "reconfigure"):
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    _STDOUT_OK = True


# ---------------------------------------------------------------- 会话
def rot_pids():
    """运行对象表里已注册的 SolidWorks 进程号。

    SolidWorks 每个实例都把自身注册成 "SolidWorks_PID_<pid>"，
    这是判断「是否多开」的唯一可靠手段。
    """
    pids = []
    if pythoncom is None:
        return pids
    try:
        ctx = pythoncom.CreateBindCtx(0)
        for mon in pythoncom.GetRunningObjectTable():
            try:
                nm = str(mon.GetDisplayName(ctx, None))
            except Exception:
                continue
            if nm.startswith("SolidWorks_PID_"):
                try:
                    pids.append(int(nm.rsplit("_", 1)[1]))
                except Exception:
                    pass
    except Exception:
        pass
    return pids


def rot_docs():
    """运行对象表里已打开文档的完整路径。

    ISldWorks.GetDocuments 在 pywin32 下返回的 SAFEARRAY 解不出来（元素是失效的
    CDispatch，一访问就报「无效索引」），GetFirstDocument/GetNextDocument 也只能
    拿到第一个。ROT 里每个打开文档都注册了完整路径，是唯一可靠的枚举入口。
    """
    out = []
    if pythoncom is None:
        return out
    try:
        ctx = pythoncom.CreateBindCtx(0)
        for mon in pythoncom.GetRunningObjectTable():
            try:
                nm = str(mon.GetDisplayName(ctx, None))
            except Exception:
                continue
            low = nm.lower()
            if len(nm) > 3 and nm[1] == ":" and low.endswith((".sldprt", ".sldasm", ".slddrw")):
                if nm not in out:
                    out.append(nm)
    except Exception:
        pass
    return out


class SW:
    """一个 SolidWorks 会话"""

    def __init__(self, app, attached):
        self.app = app
        self.attached = attached
        self.rev = str(V(app, "RevisionNumber", default="0.0.0") or "0.0.0")
        try:
            self.major = int(self.rev.split(".")[0])
        except Exception:
            self.major = 33
        self.year = SW_YEAR.get(self.major, 0)
        self.exe_dir = str(V(app, "GetExecutablePath", default="") or "")
        self.pids = rot_pids()
        # 本会话打开过的文件：只允许对这些做关闭/覆盖，避免误伤用户正在编辑的模型
        self.owned = set()
        self.owned_unsaved = []

    # -- 基本信息
    def info(self):
        return {
            "revision": self.rev,
            "year": self.year,
            "exe_dir": self.exe_dir,
            "attached": self.attached,
            "instances": self.pids,
            "multi_instance": len(self.pids) > 1,
            "doc_count": int(V(self.app, "GetDocumentCount", default=0) or 0),
            "documents": [{"title": d.title, "type": d.type_cn, "path": d.path,
                           "owned": self.is_owned(d.path)} for d in self.documents()],
            "templates": self.templates(),
        }

    def is_owned(self, path):
        if not path:
            return False
        return os.path.normcase(os.path.abspath(path)) in self.owned

    # -- 模板
    def templates(self):
        """找零件/装配体/工程图模板（优先 GB）"""
        cands = {"part": [], "assembly": [], "drawing": []}
        ext = {"part": ".prtdot", "assembly": ".asmdot", "drawing": ".drwdot"}
        roots = []
        for pid in (199, 200, 201, 196, 197, 198):
            try:
                p = V(self.app, "GetUserPreferenceStringValue", pid, default=None)
                if p and os.path.isfile(str(p)):
                    roots.append(str(p))
            except Exception:
                pass
        dirs = [
            os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                         "SOLIDWORKS", "SOLIDWORKS {}".format(self.year or "2025"), "templates"),
            os.path.join(self.exe_dir, "data", "templates") if self.exe_dir else "",
            os.path.expanduser(r"~\AppData\Roaming\SOLIDWORKS"),
        ]
        for r in roots:
            e = os.path.splitext(r)[1].lower()
            for k, x in ext.items():
                if e == x:
                    cands[k].insert(0, r)
        for d in dirs:
            if not d or not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                low = name.lower()
                for k, x in ext.items():
                    if low.endswith(x):
                        cands[k].append(os.path.join(d, name))
        out = {}
        for k, lst in cands.items():
            lst = list(dict.fromkeys(lst))
            gb = [p for p in lst if "gb_" in os.path.basename(p).lower()]
            out[k] = (gb or lst or [None])[0]
            out[k + "_all"] = lst
        return out

    def default_template(self, kind="part"):
        return self.templates().get(kind)

    # -- 文档
    def documents(self):
        out, seen = [], set()
        for p in rot_docs():
            key = os.path.normcase(os.path.abspath(p))
            if key in seen:
                continue
            seen.add(key)
            try:
                d = V(self.app, "GetOpenDocumentByName", p, default=None)
            except Exception:
                d = None
            if d is None or isinstance(d, tuple):
                continue
            out.append(Doc(bind(d, "IModelDoc2"), self))
        if not out:
            d = V(self.app, "ActiveDoc", default=None)
            if d is not None:
                out.append(Doc(bind(d, "IModelDoc2"), self))
        return out

    def active_doc(self):
        d = V(self.app, "ActiveDoc", default=None)
        return Doc(bind(d, "IModelDoc2"), self) if d is not None else None

    def is_open(self, path):
        p = os.path.abspath(path)
        d = V(self.app, "GetOpenDocumentByName", p, default=None)
        if d is None or isinstance(d, tuple):
            for rp in rot_docs():
                if os.path.normcase(os.path.abspath(rp)) == os.path.normcase(p):
                    d = V(self.app, "GetOpenDocumentByName", rp, default=None)
                    break
        if d is None or isinstance(d, tuple):
            return None
        return Doc(bind(d, "IModelDoc2"), self)

    def open(self, path, readonly=False, reload=False):
        """打开模型；reload=True 时若已打开则先关再开，避免读到内存旧版本"""
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise SWError("文件不存在：" + path)
        ext = os.path.splitext(path)[1].lower()
        dtype = {".sldprt": swDocPART, ".sldasm": swDocASSEMBLY, ".slddrw": swDocDRAWING}.get(ext)
        if dtype is None:
            raise SWError("不支持的文件类型：" + ext)

        hit = self.is_open(path)
        if hit is not None:
            if not reload:
                try:
                    self.app.ActivateDoc3(os.path.basename(path), False, 2, 0)
                except Exception:
                    pass
                return hit
            if V(hit.m, "GetSaveFlag", default=False):
                hit.dirty = True
                return hit          # 有未保存改动，不动它
            try:
                self.app.CloseDoc(hit.title)
            except Exception:
                pass

        opts = 1 if not readonly else 3       # 1=静默 2=只读
        model, err, warn = _open_ret(self.app.OpenDoc6(path, dtype, opts, "", 0, 0))
        if model is None:
            raise SWError("打开失败 err={} warn={}\n{}".format(err, warn, path))
        self.owned.add(os.path.normcase(os.path.abspath(path)))
        return Doc(bind(model, "IModelDoc2"), self)

    def new_part(self, template=None):
        tpl = template if (template and os.path.isfile(str(template))) else self.default_template("part")
        if not tpl:
            raise SWError("找不到零件模板 .prtdot，请在 SolidWorks 中设置默认模板")
        model = self.app.NewDocument(tpl, 0, 0, 0)
        if model is None:
            raise SWError("新建零件失败，模板：" + tpl)
        d = Doc(bind(model, "IModelDoc2"), self)
        self.owned_unsaved.append(d.title)
        return d

    def close_owned(self):
        """只关本会话打开的文档；用户自己开的文件一律不动"""
        closed, kept = [], []
        for d in self.documents():
            if self.is_owned(d.path) or (not d.path and d.title in self.owned_unsaved):
                try:
                    self.app.CloseDoc(d.title)
                    closed.append(d.title)
                except Exception:
                    pass
            else:
                kept.append(d.title)
        return {"closed": closed, "kept": kept}


def _open_ret(res):
    if isinstance(res, tuple):
        if len(res) >= 3:
            return res[0], res[1], res[2]
        if len(res) == 2:
            return res[0], res[1], 0
    return res, 0, 0


def connect(launch=False, visible=True):
    """连接 SolidWorks。

    默认只附加已有实例（launch=False）：多开是这个技能最大的环境风险，
    宁可报错让用户先打开 SolidWorks，也不要在后台偷偷起一个。
    """
    if _w is None:
        raise SWError("缺少 pywin32，请先运行：python -m pip install pywin32")
    pythoncom.CoInitialize()

    app = None
    attached = False
    try:
        app = _w.GetActiveObject("SldWorks.Application")
        if not V(app, "RevisionNumber", default=None):
            app = None
        else:
            attached = True
    except Exception:
        app = None

    if app is None:
        if not launch:
            raise SWError(
                "没有正在运行的 SolidWorks。\n"
                "请先手动打开 SolidWorks（或加 --launch 允许本工具启动）。"
            )
        app = _w.Dispatch("SldWorks.Application")   # 纯后期绑定，不用 EnsureDispatch
        deadline = time.time() + 120
        while time.time() < deadline:
            if V(app, "RevisionNumber", default=None):
                break
            time.sleep(2)

    sw = SW(app, attached)
    if visible:
        try:
            app.Visible = True
        except Exception:
            pass
    return sw


# ---------------------------------------------------------------- 文档
class Doc:
    """一个模型文档。读方法全部返回 mm 单位；写方法都要显式调用。"""

    def __init__(self, model, sw):
        self.m = model
        self.sw = sw
        self.dirty = False
        self.note = ""

    # -- 基本属性
    @property
    def title(self):
        return str(V(self.m, "GetTitle", default="") or "")

    @property
    def path(self):
        return str(V(self.m, "GetPathName", default="") or "")

    @property
    def dtype(self):
        try:
            return int(V(self.m, "GetType", default=0) or 0)
        except Exception:
            return 0

    @property
    def type_cn(self):
        return DOC_CN.get(self.dtype, "未知")

    @property
    def saved(self):
        return bool(self.path) and not bool(V(self.m, "GetSaveFlag", default=False))

    def rebuild(self):
        try:
            self.m.EditRebuild3()
        except Exception:
            try:
                self.m.ForceRebuild3(False)
            except Exception:
                pass
        return True

    def needs_rebuild(self):
        return bool(V(self.m, "GetNeedsRebuild", default=False))

    def close_active_sketch(self):
        """草图处于编辑状态时，遍历特征/尺寸会卡死甚至弹模态框。

        任何分析、任何写操作之前都必须先退出草图编辑。
        """
        try:
            if V(self.m.SketchManager, "ActiveSketch", default=None) is not None:
                self.m.SketchManager.InsertSketch(True)
                return True
        except Exception:
            pass
        return False

    # -- 特征树
    def _feat(self, f):
        return bind(f, "IFeature")

    def features(self, model_only=False):
        """按树顺序返回特征（含被压缩的）。

        每个节点都要单独强类型化，否则 GetNextFeature 中途返回 None。
        """
        self.close_active_sketch()
        out = []
        feat = self._feat(V(self.m, "FirstFeature"))
        guard = 0
        while feat is not None and guard < 8000:
            guard += 1
            info = self._finfo(feat, 0)
            out.append(info)
            sub = self._feat(V(feat, "GetFirstSubFeature"))
            depth = 0
            while sub is not None and depth < 8:
                depth += 1
                out.append(self._finfo(sub, depth))
                sub = self._feat(V(sub, "GetNextSubFeature"))
            feat = self._feat(V(feat, "GetNextFeature"))
        if model_only:
            out = [f for f in out if f["type"] in MODEL_FEATS]
        # 草图既挂在拉伸下面、又作为独立特征出现，按名字去重（保留 depth 小的那条），
        # 否则「欠定义草图数」会翻倍，审查结论跟着错。
        seen, uniq = set(), []
        for f in out:
            key = (f["name"], f["type"])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(f)
        return uniq

    def _finfo(self, feat, depth):
        tname = str(V(feat, "GetTypeName2", default="") or "")
        return {
            "name": str(V(feat, "Name", default="") or ""),
            "type": tname,
            "type_cn": FEAT_CN.get(tname, tname or "未知"),
            "suppressed": bool(u(V(feat, "IsSuppressed", default=False), False)),
            "error": int(u(V(feat, "GetErrorCode2", default=0), 0) or 0),
            "depth": depth,
        }

    def find_feature(self, name):
        key = str(name).strip()
        feat = self._feat(V(self.m, "FirstFeature"))
        guard = 0
        while feat is not None and guard < 8000:
            guard += 1
            if str(V(feat, "Name", default="") or "").strip() == key:
                return feat
            sub = self._feat(V(feat, "GetFirstSubFeature"))
            depth = 0
            while sub is not None and depth < 8:
                depth += 1
                if str(V(sub, "Name", default="") or "").strip() == key:
                    return sub
                sub = self._feat(V(sub, "GetNextSubFeature"))
            feat = self._feat(V(feat, "GetNextFeature"))
        return None

    def plane(self, which="front"):
        """按 前视/上视/右视 找基准面特征"""
        keys = PLANES.get(str(which).lower(), PLANES["front"])
        feat = self._feat(V(self.m, "FirstFeature"))
        while feat is not None:
            if str(V(feat, "GetTypeName2", default="")) == "RefPlane":
                nm = str(V(feat, "Name", default="")).lower()
                if any(k in nm for k in keys):
                    return feat
            feat = self._feat(V(feat, "GetNextFeature"))
        return None

    # -- 尺寸
    def dimensions(self):
        """所有驱动/从动尺寸。

        IDisplayDimension 和 IDimension 都要强类型化，否则 GetDimension 返回 None；
        数值用 SystemValue（恒为米），Value 是文档单位（可能是 mm 或 inch），不可靠。
        """
        self.close_active_sketch()
        out, seen = [], set()
        feat = self._feat(V(self.m, "FirstFeature"))
        guard = 0
        while feat is not None and guard < 8000:
            guard += 1
            owner = str(V(feat, "Name", default="") or "")
            dd = bind(V(feat, "GetFirstDisplayDimension"), "IDisplayDimension")
            n = 0
            while dd is not None and n < 400:
                n += 1
                dim = bind(V(dd, "GetDimension"), "IDimension")
                if dim is not None:
                    info = self._diminfo(dim, owner)
                    key = info["name"]
                    if key and key not in seen:
                        seen.add(key)
                        out.append(info)
                dd = bind(V(dd, "GetNext3"), "IDisplayDimension")
            feat = self._feat(V(feat, "GetNextFeature"))
        return out

    def _diminfo(self, dim, owner):
        full = str(V(dim, "FullName", default="") or "")
        try:
            val = mm(float(u(V(dim, "SystemValue", default=0), 0) or 0))
        except Exception:
            val = 0.0
        # swDimensionDriven_e: 1=驱动（可改） 2=从动（参考，不可改）
        st = int(u(V(dim, "DrivenState", default=1), 1) or 1)
        return {
            "name": full,
            "feature": owner,
            "value_mm": val,
            "driven_state": st,
            "type": "从动尺寸" if st == 2 else "驱动尺寸",
        }

    def set_dimension(self, full_name, value_mm):
        """按 'D1@草图1@零件1.Part' 改尺寸，单位 mm。

        两个坑：
          1. 从动（参考）尺寸改不动——调用会返回「成功」，几何一点不变，
             所以必须先用 DrivenState 挡掉，否则就是在骗自己。
          2. 是否生效要看 SystemValue（米），不能看 Value（文档单位）。
        """
        dim = None
        try:
            dim = self.m.Parameter(full_name)
        except Exception:
            dim = None
        if dim is None:
            raise SWError("找不到尺寸：{}".format(full_name))

        st = int(u(V(dim, "DrivenState", default=1), 1) or 1)
        if st == 2:
            raise SWError("{} 是从动（参考）尺寸，改它不会改变几何；"
                          "要改型请改驱动尺寸，或先在草图里补一个驱动尺寸".format(full_name))

        try:
            dim.SetSystemValue3(m(value_mm), 0, None)   # 0 = 应用到所有配置
        except Exception:
            try:
                dim.SystemValue = m(value_mm)
            except Exception as e:
                raise SWError("写尺寸失败：{}".format(e))
        self.rebuild()

        got = mm(float(u(V(dim, "SystemValue", default=0), 0) or 0))
        return abs(got - float(value_mm)) < 0.01, got

    # -- 实体 / 质量
    def bodies(self, visible_only=True):
        """实体列表。GetBodies2 在 IPartDoc 接口上，不在 IModelDoc2 上。"""
        pd = bind(self.m, "IPartDoc")
        arr = None
        for fn in (
            lambda: pd.GetBodies2(0, bool(visible_only)),
            lambda: pd.GetBodies2(0, False),
            lambda: self.m.GetBodies2(0, bool(visible_only)),
        ):
            try:
                arr = fn()
                if arr:
                    break
            except Exception:
                continue
        return [bind(b, "IBody2") for b in (arr or [])]

    def body_count(self):
        return len(self.bodies(False))

    def mass_props(self):
        ext = V(self.m, "Extension")
        mp = V(ext, "CreateMassProperty")
        if mp is None:
            return {}
        out = {
            "mass_g": round(float(V(mp, "Mass", default=0) or 0) * 1000, 3),
            "volume_mm3": round(float(V(mp, "Volume", default=0) or 0) * 1e9, 2),
            "surface_mm2": round(float(V(mp, "SurfaceArea", default=0) or 0) * 1e6, 2),
            "density_kg_m3": round(float(V(mp, "Density", default=0) or 0), 2),
        }
        try:
            com = list(V(mp, "CenterOfMass") or [])
            out["center_of_mass_mm"] = [mm(x) for x in com]
        except Exception:
            pass
        try:
            mom = list(V(mp, "GetMomentOfInertia", 0) or [])
            out["inertia"] = [round(float(x), 6) for x in mom]
        except Exception:
            pass
        return out

    def bbox(self):
        boxes = []
        for b in self.bodies(False):
            try:
                bb = list(V(b, "GetBodyBox") or [])
                if len(bb) >= 6:
                    boxes.append([float(x) for x in bb[:6]])
            except Exception:
                continue
        if not boxes:
            return {}
        lo = [min(b[i] for b in boxes) for i in range(3)]
        hi = [max(b[i + 3] for b in boxes) for i in range(3)]
        return {
            "min_mm": [mm(x) for x in lo],
            "max_mm": [mm(x) for x in hi],
            "size_mm": [mm(hi[i] - lo[i]) for i in range(3)],
        }

    # -- 材料 / 属性 / 配置
    def configs(self):
        try:
            names = V(self.m, "GetConfigurationNames") or []
            return [str(x) for x in names]
        except Exception:
            return []

    def active_config(self):
        """当前配置名。

        IModelDoc2.ConfigurationName 在后期绑定下取不到，要走配置管理器。
        """
        cm = V(self.m, "ConfigurationManager")
        nm = str(V(V(cm, "ActiveConfiguration"), "Name", default="") or "")
        if nm:
            return nm
        cfg = self.configs()
        return cfg[0] if cfg else ""

    def materials(self):
        """材料。

        名称挂在**文档级** MaterialIdName 上（格式「库名|材料名|编号」），
        实体级 GetMaterialPropertyName2 在单实体零件上取不到值。
        """
        cfg = self.active_config()
        out = []
        for b in self.bodies(False):
            name = str(u(V(b, "MaterialIdName", default=""), "") or "")
            db = ""
            if name and "|" in name:
                parts = name.split("|")
                db = parts[0]
                name = parts[1] if len(parts) > 1 else name
            out.append({"material": name, "database": db})
        if not out or not any(x["material"] for x in out):
            mid = str(u(V(self.m, "MaterialIdName", default=""), "") or "")
            if mid:
                parts = mid.split("|")
                out = [{"material": parts[1] if len(parts) > 1 else mid,
                        "database": parts[0] if len(parts) > 1 else ""}]
        return out

    def set_material(self, name, cfg=None):
        """给零件赋材料。

        材料名必须与材料库逐字一致（库里是 "6061-T6 (SS)"，写 "6061-T6" 会静默
        失败），所以先过一遍 matlib 纠名。
        """
        cfg = cfg or self.active_config()
        pd = bind(self.m, "IPartDoc")
        if pd is None:
            raise SWError("当前文档不是零件，无法直接赋材料")
        real = str(name)
        try:
            import matlib
            real = matlib.resolve(name)
        except Exception:
            real = str(name)
        ok = False
        for meth in ("SetMaterialPropertyName2", "SetMaterialPropertyName"):
            fn = getattr(pd, meth, None)
            if fn is None:
                continue
            try:
                fn(cfg, "", real)
                ok = True
                break
            except Exception:
                continue
        self.rebuild()
        got = (self.materials() or [{}])[0].get("material", "")
        hit = bool(got) and (got == real or real in got or got in real)
        return hit, got or real

    def custom_properties(self, scope=""):
        ext = V(self.m, "Extension")
        cpm = V(ext, "CustomPropertyManager", scope)
        out = {}
        if cpm is None:
            return out
        names = V(cpm, "GetNames", default=None)
        if not names:
            return out
        for n in names:
            val = ""
            for getter in ("Get6", "Get4", "Get2", "Get"):
                fn = getattr(cpm, getter, None)
                if fn is None:
                    continue
                try:
                    r = fn(n)
                    if isinstance(r, tuple):
                        val = r[0] if r[0] not in (None, "") else (r[1] if len(r) > 1 else "")
                    else:
                        val = r
                    val = str(val or "")
                    break
                except Exception:
                    continue
            out[str(n)] = val
        return out

    def set_custom_property(self, name, value, scope=""):
        """写自定义属性。Add3 成功时返回 None，不能拿返回值当成败依据，
        是否生效由调用方回读确认。"""
        ext = V(self.m, "Extension")
        cpm = V(ext, "CustomPropertyManager", scope)
        if cpm is None:
            raise SWError("无法获取自定义属性管理器")
        try:
            cpm.Add3(name, 30, str(value), 2)      # 30=文本, 2=覆盖已有
            return True
        except Exception:
            try:
                cpm.Add2(name, 30, str(value))
                return True
            except Exception as e:
                raise SWError("写属性失败 {}: {}".format(name, e))

    # -- 诊断
    def whats_wrong(self):
        out = []
        try:
            for w in (V(self.m, "What's Wrong") or []):
                out.append(str(w))
        except Exception:
            pass
        return out

    # -- 视图 / 截图
    def screenshot(self, out_png, width=1400, height=950):
        """截图。

        SolidWorks 只有 SaveBMP，存出来的是未压缩位图（1400×950 约 4 MB），
        即使改扩展名为 .png 也不会压缩。这里先存 bmp，再用 Pillow 转真 PNG，
        报告体积能小一个数量级。
        """
        try:
            self.m.ShowNamedView2("*Isometric", 7)
            self.m.ViewZoomtofit2()
            self.m.ClearSelection2(True)
        except Exception:
            pass
        os.makedirs(os.path.dirname(os.path.abspath(out_png)) or ".", exist_ok=True)
        bmp = os.path.splitext(out_png)[0] + ".__tmp.bmp"
        ok = False
        for fn in (lambda: self.m.SaveBMP(bmp, width, height),
                   lambda: self.m.SaveBMP(bmp)):
            try:
                fn()
                if os.path.exists(bmp) and os.path.getsize(bmp) > 2000:
                    ok = True
                    break
            except Exception:
                continue
        if not ok:
            return None
        try:
            from PIL import Image
            with Image.open(bmp) as im:
                im.convert("RGB").save(out_png, "PNG", optimize=True)
            os.remove(bmp)
        except Exception:
            shutil.move(bmp, out_png)
        return out_png

    # -- 保存 / 导出
    def save(self):
        try:
            r = self.m.Save3(1, 0, 0)
            if isinstance(r, tuple):
                return r[0] in (0, True)
        except Exception:
            pass
        try:
            return bool(self.m.Save())
        except Exception:
            return False

    def save_as(self, path, overwrite=True):
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        dtype = {".sldprt": swDocPART, ".sldasm": swDocASSEMBLY, ".slddrw": swDocDRAWING}.get(
            os.path.splitext(path)[1].lower(), self.dtype)
        ok = False
        try:
            r = self.m.Extension.SaveAs(path, 0, 1 if overwrite else 2, None, 0, 0)
            if isinstance(r, tuple):
                ok = r[0] in (0, True)
            else:
                ok = bool(r)
        except Exception:
            ok = False
        if not ok:
            try:
                ok = bool(self.m.SaveAs4(path, 0, 1 if overwrite else 2, 0, 0))
            except Exception:
                ok = False
        if ok:
            self.sw.owned.add(os.path.normcase(path))
        return ok

    def export(self, path):
        """导出 STEP/IGES/DXF 等"""
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            r = self.m.Extension.SaveAs(path, 0, 1, None, 0, 0)
            return bool(r[0] if isinstance(r, tuple) else r)
        except Exception:
            return False
