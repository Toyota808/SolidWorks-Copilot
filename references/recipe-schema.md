# 配方格式（recipe schema）

一个配方就是一份 JSON：变量表 + 建模步骤。改 `vars` 里一个数，整套尺寸联动。

## 顶层字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 零件名，写进报告和日志 |
| `desc` | 否 | 一句话说明用途，`swc.py recipes` 会显示 |
| `template` | 否 | `.prtdot` 绝对路径；省略则用默认 GB 零件模板 |
| `save_as` | 否 | 默认文件名；命令行 `-s/--save` 优先级更高 |
| `vars` | 是 | 变量表，值必须是数字 |
| `steps` | 是 | 建模步骤，按顺序执行 |

`vars` 里的值可以互相引用吗？不可以——变量只存数字，表达式写在 `steps` 里。

## 表达式

`steps` 里所有数值字段都支持表达式：`"D/2"`、`"t*1.5"`、`"(L-2*e)/3"`。

支持的语法：变量、`+ - * / // % **`、括号、负号。
**不允许**函数调用和属性访问（会直接报错），这是防止配方变成任意代码执行。

未知变量会报错并列出可用变量名。

## 坐标系

SolidWorks 默认单位是米，配方里一律用 **mm**，内部自动换算。

- `front`（前视基准面）= XY 平面，Z 为拉伸方向
- `top`（上视基准面）= XZ 平面
- `right`（右视基准面）= YZ 平面

草图默认以原点为中心，用 `cx` / `cy` 偏移。

## 操作一览

### circle — 画圆

```json
{"op": "circle", "plane": "front", "r": "D/2", "cx": 0, "cy": 0}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `plane` | 否 | `front`/`top`/`right`，默认 `front` |
| `r` | 是 | 半径（mm），常写 `D/2` |
| `cx`, `cy` | 否 | 圆心偏移（mm） |

### rect — 画矩形

```json
{"op": "rect", "plane": "front", "w": "L", "h": "W", "cx": 0, "cy": 0}
```

以 `(cx, cy)` 为中心，宽 `w` 高 `h`。

### polygon — 正多边形

```json
{"op": "polygon", "plane": "front", "n": 6, "r": 20}
```

`r` 是外接圆半径，顶点从 0° 开始。

### holes — 按坐标画多个孔并一次切除

```json
{"op": "holes", "plane": "front", "d": 6.5,
 "at": [["x", "y"], ["-x", "y"], ["-x", "-y"], ["x", "-y"]]}
```

`at` 里每项是 `[x, y]`，支持表达式。**一次草图、一次切除**，比逐个孔稳。

### bolt_circle — 圆周均布孔

```json
{"op": "bolt_circle", "plane": "front", "n": 6, "pcd": 95, "d": 11, "start_angle": 0}
```

`pcd` 是分布圆直径，`d` 是孔径，`start_angle` 是第一个孔的角度（度）。
同样是「一个草图多个圆 + 一次切除」，不用阵列接口。

### extrude — 拉伸

```json
{"op": "extrude", "depth": "t", "both": false, "draft": 0}
```

| 字段 | 说明 |
|---|---|
| `depth` | 拉伸深度（mm） |
| `both` | `true` = 双向对称拉伸 |
| `draft` | 拔模角（度），默认 0 |

### cut — 切除

```json
{"op": "cut", "through_all": true}
```

| 字段 | 说明 |
|---|---|
| `through_all` | 默认 `true`。两个方向都贯穿，避免草图面与材料方向相反时切不到 |
| `depth` | `through_all=false` 时的切深（mm） |

### shell — 抽壳

```json
{"op": "shell", "thickness": 2, "outward": false}
```

整个实体抽壳（不移除面），得到封闭薄壁体。

### material — 赋材料

```json
{"op": "material", "name": "6061-T6"}
```

可以写习惯叫法（`铝`、`不锈钢`、`普通碳钢`），工具会去材料库里纠名，
纠不到就原样写入并在回读时报告「写入未确认」。

### property — 写自定义属性

```json
{"op": "property", "name": "图号", "value": "FL-001"}
```

---

## 完整示例：三段式阶梯套

```json
{
  "name": "阶梯套",
  "desc": "外径两级、内孔贯通的套类件",
  "vars": {"D1": 40, "D2": 30, "d": 16, "H1": 20, "H2": 15},
  "steps": [
    {"op": "circle",  "plane": "front", "r": "D1/2"},
    {"op": "extrude", "depth": "H1"},
    {"op": "circle",  "plane": "front", "r": "D2/2"},
    {"op": "cut",     "through_all": true},
    {"op": "circle",  "plane": "front", "r": "d/2"},
    {"op": "cut",     "through_all": true},
    {"op": "material", "name": "黄铜"}
  ]
}
```

注意：这不是真正的阶梯轴——第二次切除会把整个内孔贯通，而不是在外圆上做台阶。
要真正的台阶需要「在已有实体的面上开草图」，目前配方语法只支持三个基准面。
这种需求建议：先用配方建主体，再用 `patch` 或人工在 SolidWorks 里补。

---

## 建完必须做的验证

跑 `--apply` 后工具会打印实测体积。**拿纸笔算一遍对上再收工**，例如：

- 法兰：`π(R²−r²)t − n·π(d/2)²t`
- 板：`L·W·t − n·π(d/2)²t`
- 套：`π(R²−r²)H`

对不上就是某个特征没生效（通常是切除方向或草图没建成）。
这一步能抓出 90% 的静默失败。
