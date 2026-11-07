# 装配体配合与干涉检查实战手册

基于《水下网衣清洗机器人》清洗头装配体实测（6 零件 + 1 装配体、16 个配合）。
每条都是踩出来的，配合 `write_mates.py` 一起看。

---

## 1. swMateType_e 枚举值（别信直觉）

| 配合类型 | 枚举值 |
|---|---|
| 重合 swMateCOINCIDENT | **0** |
| 同心 swMateCONCENTRIC | **1** |
| 垂直 swMatePERPENDICULAR | 2 |
| 平行 swMatePARALLEL | 3 |
| 相切 swMateTANGENT | 4 |
| 距离 swMateDISTANCE | **5** |
| 角度 swMateANGLE | **6** |

**坑**：直觉上容易以为「同心=0、距离=6」，实测**同心=1、距离=5**。写反了配合会静默失败，
或者生成的配合类型错乱（回读 `IMate2.Type` 对不上）。写之前照这张表，写之后回读 `Type` 验证。

---

## 2. AddMate5 的标准调用姿势

```python
m.ClearSelection2(True)
C.bind(faceA, "IEntity").Select(False)   # 第一个面（标记 1）
C.bind(faceB, "IEntity").Select(True)    # 第二个面（标记 2）
asm.AddMate5(mtype, align, False, dist, 0, 0, 0, 0, 0, 0, 0, False, False, 0)
```

- `mtype`：见上表。
- `align`：对齐/反向（0=对齐，1=反向翻转）。
- `dist`：**米**单位，`0.043` = 43 mm（不是毫米！）。

AddMate5 靠的是**当前选择集里的两个面**，选面用 `IEntity.Select`，不是传坐标。

---

## 3. 大量面遍历卡死 → FeatureByName 秒定位

刷盘有 **9690 个面**（7000+ 刷毛阵列）。`IBody2.GetFaces()` 全遍历 + 逐面
`GetSurface()` / `CylinderParams()` 判断，慢到超时被杀。

**解法**：用 `IComponent2.FeatureByName(特征名)` 直接拿某个特征的面，绕过全遍历：

```python
feat = comp.FeatureByName("切除-拉伸2")   # 中心 φ12 孔
faces = feat.GetFaces()                    # 只有这一个特征的面，秒回
```

先摸清零件特征树（`inspect` 能看），记住关键配合面在哪个特征里（孔在「切除-拉伸2」、
盘面在「凸台-拉伸1」），再用特征名精确定位。

---

## 4. FeatureByName 返回的是局部坐标

特征面参数（法向、圆柱轴、坐标）是**零件局部坐标**，不是装配坐标。
刷盘轴沿 Y，盘面法向是**局部 Y**；装配时旋转了 -90° 后才对应装配 X。

**判断面前先看组件的变换矩阵**，别拿局部法向直接当装配方向用：

```python
t = C.bind(C.V(comp, "Transform2"), "IMathTransform")
a = [float(v) for v in (C.V(t, "ArrayData", default=[]) or [])]
# a[0:3]/a[3:6]/a[6:9] 是旋转矩阵，a[9:12] 是平移(米)
```

---

## 5. 同心配合会装反，用 align 翻转

同心配合的默认对齐方向可能让零件装反（刷盘毛刷朝后了）。实测：
- 加 `align=1` 反向对齐翻转；
- 或配合后读组件旋转矩阵首行验证朝向，确认朝对再继续。

---

## 6. ToolsCheckInterference2 组件数组必须用 VARIANT

直接传 `list` / `tuple` 报「内存已锁定」。

```python
from win32com.client import VARIANT
import pythoncom
arr = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [compA, compB])
result = asm.ToolsCheckInterference2(2, arr, False, None, None)
```

全装配排查：两两组合遍历组件，逐对调用，记录有干涉的配对即可。

---

## 7. IMathUtility 在新实例里可能异常

新启动的 SolidWorks 实例里，`GetMathUtility()` 返回的对象调
`CreatePoint` / `CreateTransform` 会报 `E_UNEXPECTED`。

**解法**：改用**纯配合方案**——不手动 `SetTransform`，直接 AddMate5 让求解器自动把零件摆到位。
这也更稳，避免手算变换矩阵算错。

---

## 8. Transform2 属性 dir() 不显示

`IComponent2.Transform2` 是 get/set 属性，但 `dir()` 不列出来（gen_py 生成问题）。
直接访问即可，或在类型库生成文件里 grep `def Transform` 确认。

读组件位置/朝向、判断毛刷朝向、核对轴向定位，都靠它。

---

## 9. 配合叠加冲突（重跑前先恢复干净装配体）

错误配合已经 `Save` 到磁盘后，重跑脚本会在旧配合上**叠加**，导致过约束 / 位置冲突，
越跑越乱。

**解法**：每次重跑前，从原始干净文件**覆盖恢复装配体**（如从压缩包重新解压 `.SLDASM`），
再跑配合脚本。验证无误后再保存，避免把半成品写进磁盘。

---

## 10. 固定基准（FixComponent / UnfixComponent）

装配前先确定「谁是固定件」。SolidWorks 默认**第一个插入的零件被固定**，不一定是你要的基准。
本例默认固定的是刷盘，实际基准应是固定供水管。

```python
# 解除误固定的件
m.ClearSelection2(True); comp.Select4(False, None, False); asm.UnfixComponent()
# 固定真正的基准件（先选中再 FixComponent）
m.ClearSelection2(True); base.Select4(False, None, False); asm.FixComponent()
```
