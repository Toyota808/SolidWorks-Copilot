# SolidWorks COM 陷阱清单

每一条都是在本机（SolidWorks 2025 / Revision 33.0.0 / pywin32）实测踩出来的。
现象 → 成因 → 正确做法，照着改能省掉几小时。

---

## 1. 强类型绑定：不 bind 就静默失败

**现象**：`model.FirstFeature()` 能拿到第一个特征，但 `feat.GetNextFeature()`
返回 `None`，特征树遍历到第二个节点就断。全程不报错。

**成因**：`ISldWorks.ActiveDoc` / `OpenDoc6` 返回的是纯 `IDispatch`，
pywin32 后期绑定拿不到 vtable，派生接口的方法调用会失败并被吞掉。

**做法**：

```python
mod = win32com.client.gencache.EnsureModule(
    "{83A33D31-27C5-11CE-BFD4-00400513BB57}", 0, 33, 0)
doc  = mod.IModelDoc2(raw._oleobj_)
feat = mod.IFeature(raw_feat._oleobj_)
```

**每个节点都要单独 bind**，不能只 bind 头一个。`IDisplayDimension`、
`IDimension`、`IBody2`、`ISketch` 同理。

---

## 2. `CDispatch` 是可调用的

**现象**：`getattr(obj, "ActiveDoc")` 拿到对象，`callable()` 返回 `True`，
于是被当成方法调用了一次，返回 `None`，后面全崩。

**成因**：pywin32 的 `CDispatch` 实现了 `__call__`，但它不是方法。

**做法**：判断成员是不是 COM 对象，是就直接返回，不要 call。

```python
def is_com(o):
    return o is not None and (
        hasattr(o, "_oleobj_") or type(o).__name__ in ("CDispatch", "COMObject", "PyIDispatch"))

if not callable(member) or is_com(member):
    return member
```

---

## 3. `GetDocuments` 拿不到文档

**现象**：`doc_count = GetDocumentCount()` 是 15，
但 `GetDocuments(i)` 返回的是一个 15 元素的 tuple（忽略索引），
里面每个元素访问任何属性都报 `无效索引`。

**成因**：pywin32 解 SAFEARRAY of IDispatch 有缺陷，元素是失效的壳子。
`GetFirstDocument` / `GetNextDocument` 链式遍历也只返回第一个。

**做法**：从运行对象表（ROT）取文档完整路径，再换强类型对象：

```python
ctx = pythoncom.CreateBindCtx(0)
for mon in pythoncom.GetRunningObjectTable():
    nm = mon.GetDisplayName(ctx, None)      # "E:\xxx\零件.SLDPRT"
    # 用 GetOpenDocumentByName(nm) 拿到 IModelDoc2
```

注意 `GetDocumentCount` 会把装配体引用的子零件也数进去，比顶层文档多，正常。

---

## 4. `EnsureDispatch` 会偷偷多开一个 SolidWorks

**现象**：机器上莫名其妙出现两个 `SLDWORKS.exe`，一个占 2.5 GB、一个 350 MB。
之后操作时好时坏，改了不生效、读到旧数据。

**成因**：`gencache.EnsureDispatch("SldWorks.Application")` 和
`gencache.EnsureModule(...)` 的某些调用路径在类型库版本对不上时会**启动新实例**。

**做法**：

- 加载类型库只用 `EnsureModule`，并且按 33 / 32 / 34 / 31 依次尝试，全失败就报错。
- 启动实例只用纯 `Dispatch`（明确知道要启动时才用）。
- 默认只 `GetActiveObject` 附加已有实例，取不到就报错让人先打开 SolidWorks。
- 用 ROT 里的 `SolidWorks_PID_<pid>` 判断是否多开，多于一个就告警。

---

## 5. `GetBodies2` 不在 `IModelDoc2` 上

**现象**：`model.GetBodies2(0, True)` 报「找不到成员」。

**成因**：`GetBodies2` 属于 `IPartDoc`。

**做法**：

```python
pd = mod.IPartDoc(model._oleobj_)
bodies = pd.GetBodies2(0, True)     # 0 = swSolidBody
```

---

## 6. `FeatureCut4` 的参数是个雷区

**现象**：切除特征返回 `None`，草图有闭合轮廓也切不出来。

**成因**：27 个参数，位置错一个就静默失败。其中第一个参数 `Sd` 影响最大。

**做法**（实测可用组合）：

```python
FeatureCut4(
    False,          # Sd —— 必须 False，True 会返回 None
    False,          # Flip
    False,          # Dir
    1, 1,           # T1, T2 —— 两个方向都「完全贯穿」，避免草图与材料方向相反
    0.02, 0.02,     # D1, D2
    False, False, False, False, 0.0, 0.0,      # Dchk/Ddir/Dang
    False, False, False, False,                # OffsetReverse / TranslateSurface
    False,                                     # NormalCut
    True, True, False, False, False,           # UseFeatScope / UseAutoSelect / 其余
    0, 0.0, False, False)                      # T0 / StartOffset / FlipStartOffset / Optimize
```

参数个数严格按类型库，多传一个就 `takes from 1 to 28 positional arguments but 29 were given`。
类型库生成代码在：
`%LOCALAPPDATA%\Temp\gen_py\<py版本>\83A33D31-27C5-11CE-BFD4-00400513BB57x0x33x0.py`

---

## 7. `InsertSketch` 前必须选基准面

**现象**：`InsertSketch(True)` 返回 `None`，接着 `CreateCircleByRadius` 也返回
`None`，最后拉伸失败——但每一步都不抛异常。

**成因**：没有选中基准面/平面时，SolidWorks 不会真的创建草图。

**做法**：

```python
plane = doc.plane("front")          # 按「前视/上视/右视」找 RefPlane 特征
plane.Select2(False, 0)             # 先选
doc.m.SketchManager.InsertSketch(True)
if doc.m.SketchManager.ActiveSketch is None:
    raise ...                       # 显式检查
```

---

## 8. 草图没关就遍历特征 → 卡死

**现象**：脚本挂住不动，几十分钟没反应，最后被超时杀掉。

**成因**：草图处于编辑态时遍历特征树/尺寸，SolidWorks 可能等待 UI 输入。

**做法**：任何分析、任何写操作之前先退出草图：

```python
if doc.m.SketchManager.ActiveSketch is not None:
    doc.m.SketchManager.InsertSketch(True)
```

---

## 9. `Value` 和 `SystemValue` 不是一回事

**现象**：100 mm 的板长读出来是 100000 mm，大 1000 倍。

**成因**：`IDimension.Value` 返回**文档单位**（这台机器是 mm），
`IDimension.SystemValue` 才是米。

**做法**：一律读 `SystemValue`，再 ×1000 转 mm。
写回用 `SetSystemValue3(米, 0, None)`，第二个参数 0 = 应用到所有配置。

---

## 10. 从动尺寸改了没用

**现象**：`SetSystemValue3` 返回 0（成功），`SystemValue` 也确实变成新值，
但模型几何一点不变。

**成因**：这是从动（参考）尺寸，只反映测量值，不驱动几何。

**做法**：先查 `DrivenState`，`2` 就是从动，直接报错拒绝，别假装改成功：

```python
if int(dim.DrivenState) == 2:
    raise SWError("从动尺寸不可改")
```

---

## 11. `AddDimension2` 会弹模态框把 COM 卡死

**现象**：调用后进程挂住，只能超时终止；严重时 SolidWorks 直接崩。

**成因**：`IModelDoc2.AddDimension2(x,y,z)` 在无人值守、没有有效选择集时会
等待用户拾取，弹出交互界面。

**结论**：**不要用**。所以本技能的配方建模只产出几何，不含尺寸标注。
需要参数化改型的模型，尺寸得人工在草图里标。

---

## 12. `SaveBMP` 存的是位图，改扩展名不压缩

**现象**：一张 1400×950 的「PNG」有 4 MB，塞进 HTML 报告后 5 MB+。

**成因**：`IModelDoc2.SaveBMP` 无论文件名后缀是什么，写的都是 BMP 位图。

**做法**：先存 `.bmp`，再用 Pillow 转真 PNG：

```python
m.SaveBMP(tmp_bmp, 1400, 950)
Image.open(tmp_bmp).convert("RGB").save(png, "PNG", optimize=True)
```

实测报告从 5.3 MB 降到 161 KB。

---

## 13. 材料名必须逐字一致，而且挂在文档级

**现象**：`SetMaterialPropertyName2(cfg, "", "6061-T6")` 不报错，但材料还是「未指定」。

**成因**：材料库里真名是 `6061-T6 (SS)`，差一个括号就不匹配。
另外单位实体上的 `GetMaterialPropertyName2` 在单实体零件上取不到值。

**做法**：

- 名称从 `.sldmat`（UTF-16 编码的 XML）里读出来做纠名，别信用户随手写的。
- 读取走文档级 `IModelDoc2.MaterialIdName`，格式 `库名|材料名|编号`。
- 写后**回读确认**，不能拿返回值当成功。

---

## 14. 多返回值是 tuple

**现象**：`int(feat.GetErrorCode2())` 抛
`TypeError: int() argument must be ... not 'tuple'`。

**成因**：类型库里 `[out]` + retval 的方法，pywin32 返回 tuple。

**做法**：统一解包。

```python
def u(v, default=None):
    if isinstance(v, tuple):
        return v[0] if v else default
    return default if v is None else v
```

---

## 15. 打开已存在的文件会读到内存旧版本

**现象**：命令行改了文件，再打开读到的还是旧数据。

**成因**：文件已在 SolidWorks 里打开时，`OpenDoc6` 直接激活已有文档，
不重新读磁盘。

**做法**：需要最新磁盘内容时，先判断文件是否已打开；已打开且无未保存改动，
就 `CloseDoc` 再开。有未保存改动则不要动，标记出来告诉用户读到的是内存版本。

---

## 16. 方法名要靠类型库确认，别靠记忆

**现象**：`ISketchManager.CreateCornerRect` 报 no attribute。

**成因**：真实名字是 `CreateCornerRectangle`。这类差异靠猜会浪费大量时间。

**做法**：直接从类型库生成代码里查：

```python
mod = gencache.EnsureModule(GUID, 0, 33, 0)
[ n for n in dir(mod.ISketchManager) if n.startswith("Create") ]
```

或者 grep 那个 `gen_py` 文件里的 `def AddDimension`。

---

## 17. 圆周阵列对切除特征不稳

**现象**：`FeatureCircularPattern5` 各种参数组合都返回 `None`，
`InsertCircularPattern2` 也是。

**做法**：改用等价做法——在**同一个草图里画 n 个圆**，一次切除完成。
几何结果一样，稳定性高得多。这也是 `bolt_circle` / `holes` 操作的实现方式。
