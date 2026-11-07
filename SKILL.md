---
name: SolidWorks-copilot
description: 在自己的电脑上通过 COM 驱动 SolidWorks 做三件事——按要求参数化建模、快速读懂已有三维模型、给出有数据支撑的修改建议并能直接改。适用于用户提到 SolidWorks/三维建模/零件/装配体/改尺寸/审查模型/出图前检查，或给出 .SLDPRT/.SLDASM/.SLDDRW 文件要分析、要改的时候。支持设计意图配方建模、模型全景读取、工程审查、安全修改与回滚、审查报告输出。
---

# SolidWorks Copilot

把 SolidWorks 当成一个可编程的建模内核：用配方建模，用一句话读懂别人的模型，
用数据说话提建议，并且把建议直接落到模型上。

---

## 什么时候用这个技能

| 用户说的是 | 用哪个命令 |
|---|---|
| 「画一个法兰 / 支架 / 安装板」「按这个尺寸建个模型」 | `build` |
| 「看看这个模型」「这零件多大、多重、什么材料」 | `inspect` |
| 「这模型有什么问题」「帮我把把关 / 审一下」 | `review` |
| 「给我一份检查报告」「发给同事看」 | `report` |
| 「把厚度改成 8」「换成铝」「把这个特征删掉」 | `patch` |
| 「SolidWorks 连不上 / 报错了」 | `doctor` + `references/troubleshooting.md` |

文件路径一律用绝对路径。中文路径可以直接传，不用转义。

---

## 三条硬规矩

1. **只附加，不多开。** 绝不后台再起一个 SolidWorks。机器上同时跑两个实例时，
   COM 会在实例间乱跳，表现是「改了不生效」「读到旧数据」，极难排查。
   连接失败就报错让用户先打开 SolidWorks，或显式加 `--launch`。
2. **不碰用户的文件。** 只允许关闭本工具自己打开过的文档。用户手动打开的模型
   一律只读——除非用户明确要求改那个文件，那也必须先备份。
3. **改之前先预演。** `patch` 默认只输出「改什么、从什么改成什么」，
   加 `--apply` 才写盘，写盘前自动生成 `.bak-时间戳` 备份。

---

## 命令

所有命令都在 `scripts/` 目录下执行，入口统一是 `swc.py`。

```bash
# 环境体检（先跑这个，比后面所有报错加起来都有用）
python swc.py doctor

# 建模：先预演看参数，再 --apply
python swc.py build assets/recipes/flange.json
python swc.py build assets/recipes/flange.json --apply -s D:/out/法兰盘.SLDPRT

# 读模型
python swc.py inspect D:/model/零件.SLDPRT -o D:/out/档案.json

# 审查
python swc.py review D:/model/零件.SLDPRT

# 报告
python swc.py report D:/model/零件.SLDPRT -o D:/out/审查报告.html

# 改模型：先预演，再 apply
python swc.py patch D:/model/零件.SLDPRT ops.json
python swc.py patch D:/model/零件.SLDPRT ops.json --apply

# 内置配方清单 / 全链路自检
python swc.py recipes
python swc.py selftest
```

---

## 1. 按需求建模

建模思路是「先定变量，再定步骤」，写成一个 JSON 配方，改一个变量整套尺寸联动。

```json
{
  "name": "圆形法兰盘",
  "vars": {"D": 120, "d": 60, "t": 15, "n": 6, "pcd": 95, "bd": 11},
  "steps": [
    {"op": "circle",  "plane": "front", "r": "D/2"},
    {"op": "extrude", "depth": "t"},
    {"op": "circle",  "plane": "front", "r": "d/2"},
    {"op": "cut",     "through_all": true},
    {"op": "bolt_circle", "plane": "front", "n": "n", "pcd": "pcd", "d": "bd"},
    {"op": "material", "name": "普通碳钢"},
    {"op": "property", "name": "图号", "value": "FL-001"}
  ]
}
```

支持的 op：`circle` `rect` `polygon` `holes` `extrude` `cut` `bolt_circle`
`shell` `material` `property`。详细字段见 `references/recipe-schema.md`。

**必须做的校验**：建完用体积核对几何。例如上面这个法兰，
理论体积 = π(60²−30²)×15 − 6×π×5.5²×15 = 118681.5 mm³。
跑完 `build` 会打印实测体积，对不上就是某个特征没生效——不要跳过这一步。

内置配方：`assets/recipes/` 下的 flange（法兰盘）、plate（安装板）、bushing（轴套）。
新需求的通用做法：复制一个最接近的，改 `vars` 和 `steps`。

---

## 2. 读懂已有模型

`inspect` 一次读全，输出 JSON + 中文摘要：

- 特征树（含压缩、报错、父子层级）、草图（图元数、约束状态）
- 驱动/从动尺寸（含所属特征）、自定义属性、配置
- 材料、质量、体积、表面积、质心、惯量、包围盒
- 诊断：报错特征、待重建、SolidWorks 原生报错信息
- 装配体：零件清单、固定状态、配合数量

看结果时注意两点：

- **`feature_count` 只统计建模特征**（拉伸/切除/圆角/阵列…），不含基准面和文件夹。
- 摘要有 `has_path` 和 `modified`：前者说明文件存没存过盘，后者说明有没有未保存改动。
  有未保存改动时读到的是内存版本，和磁盘文件不一致。

---

## 3. 提合理化建议

`review` 输出五个维度的问题清单，每条都是「依据 → 建议」两段，
有把握的还会带一段可直接执行的修复指令：

| 维度 | 查什么 |
|---|---|
| 建模质量 | 报错特征、未重建、欠定义草图、空草图、缺材料、缺属性、无谓复杂度 |
| 结构合理性 | 细长比、质心偏移、等效壁厚、填充率、切除处无圆角（应力集中） |
| 可制造性 | 非整尺寸、无尺寸标注（不可参数化）、薄壁、超薄特征 |
| 可装配性 | 装配无配合/无固定件、配合过多、小直径孔 |
| 规范性 | 未保存、缺图号、默认文件名、配置爆炸 |

评分 0–100，`严重 −12 / 警告 −6 / 提示 −2 / 建议 −1`。

**写建议的原则**：只写有数支撑的话。没有实测数据就宁可不说，
不要输出「建议优化结构」这种谁都能说的废话。阈值与规则来源见
`references/review-rules.md`。

---

## 4. 改模型

审查里的修复指令可以直接作为 `patch` 的输入。操作文件格式：

```json
{"operations": [
  {"op": "set_dimension", "name": "D1@Sketch1@零件1.Part", "value": 120},
  {"op": "set_material",  "name": "6061-T6"},
  {"op": "set_property",  "name": "版本", "value": "B"},
  {"op": "suppress",      "feature": "切除-拉伸3"},
  {"op": "rebuild"}
]}
```

可用 op：`set_dimension` `round_dimensions` `set_material` `set_property`
`suppress` `unsuppress` `delete` `rename` `rebuild` `save` `save_as` `export`。

流程固定四步，没有「直接改」的捷径：**预演 → 备份 → 执行 → 回读**。
任何一步失败立即停止并给出回滚命令：

```bash
python swc.py patch 零件.SLDPRT ops.json --apply
# 失败时：
python patch.py 零件.SLDPRT --rollback 零件.SLDPRT.bak-20261107_190900
```

**改尺寸前必须确认是驱动尺寸。** 从动（参考）尺寸改它，API 会返回「成功」，
但几何纹丝不动——工具已经会挡掉这种情况并报错。

---

## 能力边界（这些做不到，别答应）

- **不自动标注尺寸。** SolidWorks 的 `AddDimension2` 在无人值守时会弹模态框
  把 COM 卡死，已验证不可用。所以配方建模产出的是「几何」，草图里的尺寸
  需要人工补。模型要参数化改型，得先有人在草图里标驱动尺寸。
- **不做圆角/倒角/拔模的自动生成。** 需要选边线，跑批时不稳。审查会指出
  「有切除但无圆角」，但不会替你加。
- **圆周阵列接口对切除特征不稳**，所以用「同一草图多个圆 + 一次切除」代替。
- **不替代强度校核。** 审查只做几何与工艺层面判断，受力仿真要在
  SolidWorks Simulation 里做。
- 装配体读取得动，但**干涉检查**要看模型大小，大装配可能很慢，慎用。

---

## 环境

- SolidWorks 2020–2026（本机验证版本 2025，Revision 33.0.0）
- Python 3.9+，`pywin32`（必须），`Pillow`（截图压缩，强烈建议）
- 中文模板：需要 `gb_part.prtdot` / `gb_assembly.asmdot`
- 材料库：从安装目录 `lang/*/sldmaterials/*.sldmat` 自动发现

```bash
python -m pip install pywin32 pillow
```

装完先跑 `python swc.py doctor`，它会告诉你缺什么。

---

## 排错

连不上、报「找不到成员」、改了不生效、文档被占用 → 看
`references/troubleshooting.md`。
想改这个技能自身的代码，先读 `references/com-traps.md`，
里面是踩过的 12 个 SolidWorks COM 陷阱，每条都写着错误现象和成因。
