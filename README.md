# SolidWorks Copilot

把 SolidWorks 当成一个可编程的建模内核：用「配方」参数化建模，用一句话读懂别人的模型，用数据说话提建议，并且把建议直接落到模型上。全部通过 COM 接口驱动本机 SolidWorks，无需人工点鼠标。

## 能做什么

| 场景 | 命令 |
|---|---|
| 参数化建模（法兰 / 支架 / 安装板 / 轴套…） | `build` |
| 读懂已有模型（尺寸、材料、质量、特征树、装配） | `inspect` |
| 工程审查（建模质量 / 结构 / 可制造 / 可装配 / 规范） | `review` |
| 输出审查报告（HTML） | `report` |
| 安全改模型（预演 → 备份 → 执行 → 回读） | `patch` |
| 环境体检 | `doctor` |

## 环境依赖

- **SolidWorks 2020–2026**（本机验证版本：2025，Revision 33.0.0）
- **Python 3.9+**
- **pywin32**（必须，COM 驱动）
- **Pillow**（截图压缩，建议）
- 中文模板（可选）：`gb_part.prtdot` / `gb_assembly.asmdot`

## 安装

```bash
git clone https://github.com/Toyota808/SolidWorks-Copilot.git
cd SolidWorks-Copilot
pip install pywin32 pillow
python scripts/swc.py doctor
```

装完先跑 `doctor`，它会告诉你缺什么。

## 快速上手

```bash
# 环境体检
python scripts/swc.py doctor

# 读一个零件
python scripts/swc.py inspect D:/model/零件.SLDPRT -o D:/out/档案.json

# 建模：先预演看参数，再 --apply 落盘
python scripts/swc.py build assets/recipes/flange.json
python scripts/swc.py build assets/recipes/flange.json --apply -s D:/out/法兰盘.SLDPRT

# 审查
python scripts/swc.py review D:/model/零件.SLDPRT

# 改模型：先预演，再 apply
python scripts/swc.py patch D:/model/零件.SLDPRT ops.json
python scripts/swc.py patch D:/model/零件.SLDPRT ops.json --apply
```

## 与 AI 助手配合使用

本项目的核心是一组**可独立运行的 Python 脚本**，不绑定任何特定 AI 框架。只要 AI 助手具备「执行本地命令」的能力（豆包电脑助手、Claude、Copilot 等），就能直接调用 `scripts/swc.py` 来驱动 SolidWorks。

若你想在 **WorkBuddy** 中把它作为「技能」使用，把整个目录放到 `~/.workbuddy/skills/SolidWorks-copilot/` 即可——`SKILL.md` 已内嵌完整使用说明，会被自动识别。

> 注：`SKILL.md` 是 WorkBuddy 的技能定义格式，仅 WorkBuddy 会解析它；其他 AI 助手直接跑 `scripts/` 下的脚本即可，功能完全一致。

## 目录结构

```
SolidWorks-Copilot/
├── SKILL.md               # WorkBuddy 技能定义（含完整使用说明）
├── scripts/               # 核心脚本，swc.py 为统一入口
│   ├── swc.py             #   命令入口
│   ├── swcore.py          #   COM 连接与类型库封装
│   ├── build.py           #   配方建模
│   ├── modelinfo.py       #   模型全景读取
│   ├── review.py          #   工程审查
│   ├── report.py          #   审查报告
│   ├── patch.py           #   安全修改与回滚
│   └── matlib.py          #   材料库发现
├── assets/recipes/        # 参数化建模配方（flange / plate / bushing）
└── references/            # 审查规则、COM 陷阱、排错手册
```

## 能力边界

- 不自动标注尺寸（SolidWorks 无人值守标注会卡死 COM，已验证）。
- 不做圆角 / 倒角 / 拔模的自动生成。
- 圆周阵列对切除特征不稳，用「同草图多圆 + 一次切除」代替。
- 不替代强度校核（受力仿真请用 SolidWorks Simulation）。

## License

[MIT](./LICENSE)
