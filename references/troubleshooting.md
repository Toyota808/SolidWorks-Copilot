# 排错

先跑体检，它会一次性告诉你环境缺什么：

```bash
python scripts/swc.py doctor
```

---

## 连不上 SolidWorks

**报错**：`没有正在运行的 SolidWorks。请先手动打开 SolidWorks（或加 --launch）`

按顺序查：

1. SolidWorks 真的开着吗？`tasklist | findstr SLDWORKS`
2. 有几个实例？`python scripts/swc.py info` 会打印 PID 列表，多于一个就是多开
3. 许可正常吗？手工打开 SolidWorks 能新建零件吗
4. 确实是新装/刚重启 → 加 `--launch` 让工具启动它

**不要**为了「省事」直接用 `Dispatch`。它可能启动第二个实例，之后所有操作
都会变得不可预测。

---

## 多开实例（最常见、最难查）

**现象**：同一个文件，`review` 一次说没问题，再跑一次说有问题；
`patch` 说改成功了但打开看没变。

**判断**：

```bash
tasklist | findstr SLDWORKS      # 出现两行就是多开
python scripts/swc.py doctor     # 会打印 "检测到 N 个实例"
```

**处理**：关掉多余的那个（保留你正在用的窗口），再重跑。
如果两个都是空实例，全关掉重新打开一个。

**为什么会多开**：别的自动化脚本用了 `EnsureDispatch`，或类型库版本不匹配时
触发 `gencache` 重新生成。本技能已经避免了这两条路径，但如果机器上有其他
SolidWorks 自动化工具在跑，仍可能被它带出来。

---

## 报「找不到成员」/ 属性全是 None

**成因**：拿到的是纯 `IDispatch`，没做类型库强类型绑定。

**查法**：

```python
import win32com.client as w
app = w.GetActiveObject("SldWorks.Application")
print(type(app))          # 应该是 gen_py...ISldWorks
```

如果是 `CDispatch`，就说明没绑上。绑定的正确写法见
`references/com-traps.md` 第 1 条。

---

## 建模失败：草图是空的

**现象**：`build --apply` 在「拉伸」这步失败，报「草图可能不闭合」。

**成因**：`InsertSketch` 之前没选中基准面，草图根本没创建，
后续 `CreateCircleByRadius` 返回 None 但不报错。

**自查**：跑 `--apply` 前先跑一次预演，确认参数对；再检查 `plane` 字段
是不是 `front`/`top`/`right`（写 `前视` 不认，会走默认分支）。

---

## 建模失败：切除返回 None

**成因**：`FeatureCut4` 参数错位，或切除方向没有材料。

**做法**：确认用的是内置的 `cut` 操作（参数已经调对了）。
如果一定要自己调 API，见 `references/com-traps.md` 第 6 条的确切参数顺序。

---

## 改尺寸「成功」但几何没变

**两种可能**：

1. **从动尺寸**。`DrivenState == 2` 的尺寸只反映测量值，不驱动几何。
   工具会直接报错拒绝，不会假装成功。要改型得先有驱动尺寸。
2. **改的配置不对**。`SetSystemValue3(值, 0, None)` 的第二个参数 0 = 所有配置。
   传 1 只改当前配置，容易看起来「没生效」。

---

## 材料设了但读出来还是「未指定」

**成因**：材料名和材料库不逐字一致。库里是 `6061-T6 (SS)`，写 `6061-T6` 就不匹配，
而 API 不会报错。

**查法**：

```bash
python scripts/matlib.py 6061        # 搜索真实名字
python scripts/matlib.py --check 铝   # 看会纠成什么
```

本技能的 `set_material` 会自动纠名，纠完仍不匹配会在返回里明确写「写入未确认」。

---

## 报告图片糊 / 文件巨大

**现象**：报告 5 MB 以上。

**成因**：SolidWorks 只有 `SaveBMP`，输出的是未压缩位图。

**处理**：装 Pillow（`pip install pillow`），工具会自动转真 PNG，
5 MB 能降到 150 KB 左右。装不上也能跑，只是文件大。

---

## 脚本卡住不动

**成因**（按可能性排序）：

1. 草图处于编辑态，遍历特征时 SolidWorks 在等 UI 输入 → 先退出草图
2. 弹了模态对话框（比如缺模板、文件被占用）→ 去看 SolidWorks 窗口有没有弹窗
3. 装配体太大，光加载就要几分钟 → 等，或者只分析单个零件
4. 调了 `AddDimension2` 这类需要交互的 API → 别用

**排查手法**：所有命令都套 `timeout`，卡住就直接放弃这条路，换做法：

```bash
timeout 300 python scripts/swc.py review D:/model/零件.SLDPRT
```

---

## 文件被占用 / 无法覆盖

**成因**：文件已在 SolidWorks 里打开，或有 `~$xxx.SLDPRT` 锁文件残留（崩溃后常见）。

**处理**：先在 SolidWorks 里关掉该文档；锁文件在确认 SolidWorks 已全部退出后
可以删掉。工具自己打开文档前会检查是否已打开，并有 `reload` 开关。

---

## 关闭文档时把用户的模型也关了

不应该发生。`close_owned()` 只关本会话登记过的文件，用户手动打开的一律跳过。
如果发现被误关，检查是不是直接调了 `CloseDoc(title)` 而不是走 `close_owned()`。

同理，`swc.py patch` 只对命令行显式给出的那一个文件动手，且有备份。

---

## 想看某个 SolidWorks 方法的真实参数

别猜，从类型库读：

```python
import win32com.client as w
mod = w.gencache.EnsureModule(
    "{83A33D31-27C5-11CE-BFD4-00400513BB57}", 0, 33, 0)
print([n for n in dir(mod.ISketchManager) if n.startswith("Create")])
print(mod.IFeatureManager.FeatureCut4.__doc__)
```

或者直接 grep 生成代码（参数顺序最清楚）：

```
%LOCALAPPDATA%\Temp\gen_py\<py版本>\83A33D31-27C5-11CE-BFD4-00400513BB57x0x33x0.py
```

---

## 自检

怀疑环境有问题时，跑全链路自检，它会在系统临时目录里建一个零件并走完
建模→读取→审查→修改→回读：

```bash
python scripts/swc.py selftest
```

自检不会碰你的任何模型。
