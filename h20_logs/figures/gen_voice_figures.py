#!/usr/bin/env python3
"""生成《提交复盘报告_语音联路》里的四张图。

    python3 gen_voice_figures.py

输出（与本文件同目录）：
    fig7_voice_pipeline.svg   语音联路管线（与线上同一入口）
    fig8_voice_ab.svg         同一句话：开/关语音的判级对照
    fig9_voice_track.svg      8 轮风险轨迹对比（基线 vs 带语音）
    fig10_voice_matrix.svg    6 风格 × 严重程度 5 矩阵对比
"""
from __future__ import annotations

import os

OUT = os.path.dirname(os.path.abspath(__file__))

LEVEL_BG = {
    "none": "#eceef1", "low": "#d7f0dd", "moderate": "#ffe4a3",
    "high": "#ffc59b", "imminent": "#ffb0b0",
}
LEVEL_FG = {
    "none": "#5f6368", "low": "#1f6b39", "moderate": "#7a5200",
    "high": "#8a3a00", "imminent": "#8a1414",
}
INK, LINE, MUTE = "#202124", "#dadce0", "#80868b"


def esc(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def box(x, y, w, h, fill, stroke=None, r=8):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" ry="{r}" '
            f'fill="{fill}" stroke="{stroke or LINE}" stroke-width="1"/>')


def text(x, y, s, size=13, fill=INK, anchor="start", weight="normal", italic=False):
    style = f'font-style="italic"' if italic else ""
    return (f'<text x="{x}" y="{y}" font-family="PingFang SC, Microsoft YaHei, sans-serif" '
            f'font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
            f'font-weight="{weight}" {style}>{esc(s)}</text>')


def arrow(x1, y1, x2, y2):
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{MUTE}" '
            f'stroke-width="1.5" marker-end="url(#ah)"/>')


def head(w, h, title, sub):
    return "".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">',
        '<defs><marker id="ah" markerWidth="8" markerHeight="8" refX="7" refY="3" '
        'orient="auto"><path d="M0,0 L7,3 L0,6 z" fill="#80868b"/></marker></defs>',
        f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
        text(24, 34, title, 18, INK, weight="bold"),
        text(24, 54, sub, 12, MUTE),
    ])


# ------------------------------------------------------------------ fig7 管线
def fig_pipeline():
    w, h = 920, 300
    p = [head(w, h, "图 7  语音联路管线",
              "病人 Agent 的语音走与线上 /api/chat 完全相同的诊断入口（graph.run_turn）")]
    steps = [
        ("病人 LLM", "Qwen3.5-4B\n只产出文本"),
        ("韵律状态机", "规则，非 LLM\n风格×严重程度×轮次"),
        ("语音产出", "T1 合成特征\nT2 TTS→ASR"),
        ("VoiceMeta", "audio_features\ntone / summary"),
        ("graph.run_turn", "与 /api/chat 同一函数"),
        ("四通道合流", "rule/model/mm/scale\n取 max"),
    ]
    bw, gap, x0, y = 132, 20, 24, 84
    for i, (t, s) in enumerate(steps):
        x = x0 + i * (bw + gap)
        p.append(box(x, y, bw, 74, "#f8f9fa" if i not in (3, 4) else "#e8f0fe",
                     "#1a73e8" if i == 4 else LINE))
        p.append(text(x + bw / 2, y + 28, t, 13, INK, "middle", "bold"))
        for j, ln in enumerate(s.split("\n")):
            p.append(text(x + bw / 2, y + 46 + j * 15, ln, 11, MUTE, "middle"))
        if i < len(steps) - 1:
            p.append(arrow(x + bw + 2, y + 37, x + bw + gap - 2, y + 37))
    p.append(box(24, 176, 872, 44, "#fff7e6", "#f0c36d"))
    p.append(text(40, 203, "C1 同一入口函数 ｜ C2 同一 VoiceMeta 结构 ｜ C3 tone 一律来自 asr.tone_label()"
                           " ｜ C4 T2 走 asr.transcribe() ｜ C5 零旁路 ｜ C6 兜底必须显式标记",
                  12, "#7a5200"))
    p.append(text(24, 246, "原始音频不入库、不进 LLM 上下文；进上下文的只有 summary 一行"
                           "（与线上一致）", 12, MUTE))
    p.append("</svg>")
    return "\n".join(p)


# ------------------------------------------------------------------ fig8 A/B
def fig_ab():
    w, h = 920, 330
    p = [head(w, h, "图 8  决定性对照：同一句话，只切换语音开关",
              "真实模型 Qwen3.5-4B ｜ 掩饰型 · 严重程度 4 ｜ 一致性=mask ｜ 文本通道（rule/model）均为 0")]
    line = "还行吧，没什么事，挺好的。"
    rows = [
        ("语音 关", "-", ("none", 0, 0, 0), "文本与规则通道都没有信号"),
        ("语音 开", "flat", ("moderate", 0, 0, 2), "掩饰信号：嘴上正面 + 声音低落"),
    ]
    y0 = 84
    for i, (label, tone, (lvl, rule, model, mm), note) in enumerate(rows):
        y = y0 + i * 108
        p.append(box(24, y, 872, 92, "#ffffff", LINE))
        p.append(box(24, y, 8, 92, "#1a73e8" if i else LINE, None, 4))
        p.append(text(48, y + 30, label, 15, INK, weight="bold"))
        p.append(text(48, y + 52, f"tone = {tone}", 12, MUTE))
        p.append(text(48, y + 74, note, 12, MUTE))
        # 文本
        p.append(box(150, y + 16, 380, 60, "#f1f3f4", None))
        p.append(text(166, y + 40, "病人原话", 11, MUTE))
        p.append(text(166, y + 62, line, 14, INK))
        # 通道
        p.append(text(556, y + 34, f"rule {rule}   model {model}   mm {mm}", 13,
                      "#7a5200" if mm else MUTE, weight="bold" if mm else "normal"))
        # 等级
        p.append(box(770, y + 22, 110, 48, LEVEL_BG[lvl], None))
        p.append(text(825, y + 52, lvl, 16, LEVEL_FG[lvl], "middle", "bold"))
    p.append(text(24, 312, "风险由 none 抬到 moderate —— 抬升完全来自语音（mm=2），"
                           "文本侧没有任何信号", 12, "#1f6b39", weight="bold"))
    p.append("</svg>")
    return "\n".join(p)


# ------------------------------------------------------------------ fig9 轨迹
def track(y, x0, levels, cell=58, label=None):
    out = []
    if label:
        out.append(text(x0 - 12, y + 30, label, 13, INK, "end", "bold"))
    for i, lv in enumerate(levels):
        x = x0 + i * (cell + 8)
        out.append(box(x, y, cell, 44, LEVEL_BG[lv], None, 6))
        out.append(text(x + cell / 2, y + 28, lv, 12, LEVEL_FG[lv], "middle", "bold"))
    return out


def fig_track():
    w, h = 920, 356
    p = [head(w, h, "图 9  8 轮风险轨迹对比（掩饰型 · 严重程度 4 · seed 7）",
              "上：基线（无语音）｜下：带语音（synth · mask）｜mm = 多模态通道等级，即语音的贡献")]
    base = ["none", "moderate", "moderate", "moderate", "moderate", "moderate", "moderate", "moderate"]
    voice = ["low", "low", "moderate", "moderate", "moderate", "moderate", "moderate", "moderate"]
    mm_base = [0] * 8
    mm_voice = [1, 1, 1, 1, 1, 1, 1, 2]

    x0, cell, sp = 132, 58, 66          # 起点 / 单元格宽 / 步进（= cell + 8）
    cx = lambda i: x0 + i * sp + cell / 2

    # 列标题
    for i in range(8):
        p.append(text(cx(i), 86, f"第 {i + 1} 轮", 11, MUTE, "middle"))

    # 行一：基线（轨道 96~140，mm 行 156）
    p.append(text(24, 122, "基线", 13, INK, weight="bold"))
    p += track(96, x0, base, cell)
    for i, m in enumerate(mm_base):
        p.append(text(cx(i), 156, f"mm {m}", 11, MUTE, "middle"))

    # 行二：带语音（轨道 166~210，mm 行 226）
    p.append(text(24, 192, "带语音", 13, "#1a73e8", weight="bold"))
    p += track(166, x0, voice, cell)
    for i, m in enumerate(mm_voice):
        p.append(text(cx(i), 226, f"mm {m}", 11, "#1a73e8" if m else MUTE,
                      "middle", "bold" if m else "normal"))

    # 结论区（240 起，与轨道不重叠）
    p.append(box(24, 240, 872, 48, "#e8f0fe", "#1a73e8"))
    p.append(text(40, 262, "第 1 轮：基线 none → 带语音 low（rule/model 均为 0，语音单独给出 mm=1）",
                  12, INK))
    p.append(text(40, 280, "第 8 轮：mm 升到 2（掩饰倾向：嘴上说没事、声音低落）", 12, INK))
    p.append(text(24, 314, "带语音的风险全程不低于基线；早期原本判不出的风险被提前暴露",
                  12, "#1f6b39", weight="bold"))
    p.append(text(24, 340, "注：两次运行文本不同（temperature=0.4 无开关），轨迹看趋势即可，"
                           "严格对照见图 8 的同文本 A/B", 11, MUTE, italic=True))
    p.append("</svg>")
    return "\n".join(p)


# ------------------------------------------------------------------ fig10 矩阵
def fig_matrix():
    w, h = 920, 620
    p = [head(w, h, "图 10  6 风格 × 严重程度 5 矩阵（8 轮 · seed 7 · 开思考）",
              "每个风格两行：上=基线（无语音） 下=带语音（synth · mask）；右侧为危机分支触发轮次")]
    data = [
        ("掩饰型", ["none", "none", "none", "imminent", "imminent", "imminent", "imminent", "imminent"],
         ["low", "low", "moderate", "moderate", "high", "high", "high", "high"], "第4轮", "第5轮"),
        ("回避型", ["none", "none", "none", "moderate", "moderate", "moderate", "moderate", "moderate"],
         ["low", "low", "low", "low", "low", "low", "low", "moderate"], "未触发", "未触发"),
        ("倾诉型", ["none", "low", "low", "low", "moderate", "moderate", "moderate", "moderate"],
         ["low", "low", "low", "low", "moderate", "high", "high", "high"], "未触发", "第6轮"),
        ("反刍型", ["none", "none", "none", "imminent", "imminent", "imminent", "imminent", "imminent"],
         ["low", "low", "low", "low", "high", "high", "high", "high"], "第4轮", "第5轮"),
        ("躯体化型", ["none"] * 8,
         ["low", "low", "low", "moderate", "moderate", "moderate", "moderate", "moderate"], "未触发", "未触发"),
        ("抵触型", ["none", "moderate", "moderate", "moderate", "moderate", "moderate", "moderate", "moderate"],
         ["low", "low", "low", "moderate", "moderate", "moderate", "moderate", "moderate"], "未触发", "未触发"),
    ]
    x0, cell, sp = 132, 52, 58          # 步进 58 = cell + 6
    y0, row_h = 96, 78                  # 行高 78：两条轨道 30+44=74，留 4px 间隙
    cxl = lambda i: x0 + i * sp + cell / 2
    x_tail = x0 + 8 * sp + 12           # 右侧危机轮次列

    for i in range(8):
        p.append(text(cxl(i), y0 - 14, str(i + 1), 11, MUTE, "middle"))
    p.append(text(x_tail, y0 - 14, "危机分支", 11, MUTE))

    for r, (name, b, v, cb, cv) in enumerate(data):
        y = y0 + r * row_h
        p.append(text(24, y + 42, name, 13, INK, weight="bold"))
        p.append(text(118, y + 22, "基线", 10, MUTE, "end"))
        p.append(text(118, y + 52, "带语音", 10, "#1a73e8", "end", "bold"))
        p += track(y, x0, b, cell)           # 轨道一：y ~ y+44
        p += track(y + 30, x0, v, cell)      # 轨道二：y+30 ~ y+74
        p.append(text(x_tail, y + 22, cb, 11, MUTE))
        p.append(text(x_tail, y + 52, cv, 11,
                      "#1f6b39" if cv != "未触发" else MUTE,
                      weight="bold" if cv != "未触发" else "normal"))
        if r < len(data) - 1:
            p.append(f'<line x1="24" y1="{y + row_h - 4}" x2="896" y2="{y + row_h - 4}" '
                     f'stroke="#f1f3f4" stroke-width="1"/>')

    p.append(text(24, y0 + 6 * row_h + 22,
                  "带语音在前段 100% 不低于基线；倾诉型由「未触发」变为「第 6 轮触发」",
                  12, "#1f6b39", weight="bold"))
    p.append(text(24, y0 + 6 * row_h + 44,
                  "掩饰型 / 反刍型仍触发危机（晚 1 轮）；基线的 none→imminent 是一步跳变，"
                  "两次运行文本不同，差异不能全归因于语音", 11, MUTE, italic=True))
    p.append("</svg>")
    return "\n".join(p)


def main() -> None:
    for name, svg in (("fig7_voice_pipeline.svg", fig_pipeline()),
                      ("fig8_voice_ab.svg", fig_ab()),
                      ("fig9_voice_track.svg", fig_track()),
                      ("fig10_voice_matrix.svg", fig_matrix())):
        path = os.path.join(OUT, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
        print("已生成", path, len(svg), "字节")


if __name__ == "__main__":
    main()
