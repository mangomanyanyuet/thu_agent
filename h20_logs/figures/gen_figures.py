# -*- coding: utf-8 -*-
"""生成复盘报告配图（纯标准库输出 SVG，无第三方依赖）。

数据全部来自 h20 日志：
  - data/traces/agent-20261017.jsonl
  - /tmp/mt_backend.log（TRACE_ECHO=1 回显的 [trace] 行）
运行：python3 gen_figures.py
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = ("Hiragino Sans GB, STHeiti Medium, STHeiti, PingFang SC, "
        "Arial Unicode MS, sans-serif")

C_BLUE = "#e8f1fd"
C_BLUE_S = "#3a6ea5"
C_GREEN = "#e6f6ec"
C_GREEN_S = "#2f8a55"
C_RED = "#fdeaea"
C_RED_S = "#b03a3a"
C_GRAY = "#f2f2f2"
C_GRAY_S = "#8a8a8a"
C_YEL = "#fdf6e3"
C_YEL_S = "#b58900"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class Svg:
    def __init__(self, w, h, title):
        self.w, self.h = w, h
        self.b = [
            '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
            'viewBox="0 0 %d %d">' % (w, h, w, h),
            '<defs>'
            '<marker id="ar" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">'
            '<path d="M0,0 L10,4 L0,8 z" fill="#666"/></marker>'
            '<marker id="arr" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">'
            '<path d="M0,0 L10,4 L0,8 z" fill="#b03a3a"/></marker>'
            '<marker id="ag" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">'
            '<path d="M0,0 L10,4 L0,8 z" fill="#2f8a55"/></marker>'
            '</defs>',
            '<style>text{font-family:%s}</style>' % FONT,
            '<rect width="%d" height="%d" fill="#ffffff"/>' % (w, h),
            '<text x="28" y="34" font-size="20" font-weight="700" fill="#111">%s</text>' % esc(title),
        ]

    def add(self, s):
        self.b.append(s)

    def box(self, cx, cy, w, h, lines, fill=C_BLUE, stroke=C_BLUE_S, fs=13):
        x, y = cx - w / 2.0, cy - h / 2.0
        s = ('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="9" fill="%s" '
             'stroke="%s" stroke-width="1.5"/>' % (x, y, w, h, fill, stroke))
        n = len(lines)
        for i, ln in enumerate(lines):
            ty = cy + (i - (n - 1) / 2.0) * (fs + 6)
            s += ('<text x="%.1f" y="%.1f" text-anchor="middle" dominant-baseline="middle" '
                  'font-size="%d" fill="#1a1a1a">%s</text>' % (cx, ty, fs, esc(ln)))
        self.add(s)

    def diamond(self, cx, cy, w, h, lines, fill=C_YEL, stroke=C_YEL_S, fs=13):
        self.add('<path d="M%.1f,%.1f L%.1f,%.1f L%.1f,%.1f L%.1f,%.1f Z" fill="%s" '
                 'stroke="%s" stroke-width="1.5"/>' %
                 (cx, cy - h / 2.0, cx + w / 2.0, cy, cx, cy + h / 2.0,
                  cx - w / 2.0, cy, fill, stroke))
        n = len(lines)
        for i, ln in enumerate(lines):
            ty = cy + (i - (n - 1) / 2.0) * (fs + 6)
            self.add('<text x="%.1f" y="%.1f" text-anchor="middle" dominant-baseline="middle" '
                     'font-size="%d" fill="#1a1a1a">%s</text>' % (cx, ty, fs, esc(ln)))

    def arrow(self, x1, y1, x2, y2, color="#666", dash="", marker="ar"):
        d = ' stroke-dasharray="%s"' % dash if dash else ""
        self.add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                 'stroke-width="1.6" marker-end="url(#%s)"%s/>' % (x1, y1, x2, y2, color, marker, d))

    def note(self, x, y, lines, fs=11, color="#777", anchor="start"):
        for i, ln in enumerate(lines):
            self.add('<text x="%.1f" y="%.1f" text-anchor="%s" font-size="%d" fill="%s">%s</text>'
                     % (x, y + i * (fs + 3), anchor, fs, color, esc(ln)))

    def save(self, name):
        self.b.append('</svg>')
        p = os.path.join(HERE, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(self.b))
        print("wrote", p, os.path.getsize(p), "bytes")


# ============================================================ 图1 因果决策流
def fig1():
    s = Svg(1120, 780, "图 1 · 对话内容如何驱动状态转移（Qwen3.5-4B 实测）")
    cx = 560
    s.box(cx, 88, 300, 56, ["用户一轮输入", "文本 + 语音 + 面部（本环境后两者恒空）"], C_GRAY, C_GRAY_S)
    s.note(cx + 170, 78, ["实测：h20 无摄像头/麦克风，", "语音与面部通道恒为 none"], 11, "#a00")

    s.arrow(cx - 60, 116, 300, 168)
    s.arrow(cx + 60, 116, 820, 168)

    s.box(300, 200, 320, 62, ["① 情绪通道", "complete() 并要求输出 <emotion> 标签"], C_BLUE, C_BLUE_S)
    s.box(820, 200, 320, 62, ["② 风险通道", "规则词表 + structured(RiskAssessment) + 多模态"], C_BLUE, C_BLUE_S)

    s.arrow(300, 231, 300, 288)
    s.arrow(820, 231, 820, 288)

    s.box(300, 320, 320, 62, ["情绪标签 → 三通道融合", "→ 写轮次记录 / 报告 / 日报"], C_GREEN, C_GREEN_S)
    s.box(820, 320, 320, 62, ["risk_lvl = none / watch / high", "（取三通道最大值，只升不降）"], C_GREEN, C_GREEN_S)
    s.note(150, 372, ["⚠ 情绪【不改变相位】，", "只影响报告与日报"], 11, "#2f8a55")

    s.arrow(820, 351, 820, 400)
    s.diamond(820, 445, 230, 80, ["risk_lvl == high ?"])

    s.arrow(820 - 115, 445, 330, 445, "#b03a3a", "", "arr")
    s.box(300, 445, 330, 74, ["否：调用 complete() 生成回复", "相位按【轮次 / 时长】推进", "watch 仅标记，不升级"], C_BLUE, C_BLUE_S)

    s.arrow(820, 485, 820, 528, "#b03a3a", "", "arr")
    s.box(820, 570, 340, 84, ["是：相位 = CRISIS", "返回固定危机话术 + 热线", "不再调用生成；should_finish=true"], C_RED, C_RED_S)
    s.note(660, 545, ["实测 2 次：V2 轮2、V3 轮1"], 11, "#b03a3a", "end")

    s.arrow(300, 482, 300, 620)
    s.box(300, 655, 430, 70, ["轮次 ≥ 4（MAX_TURNS）或 已用 ≥ 240s", "→ MAIN → CLOSING → DONE"], C_YEL, C_YEL_S)
    s.note(540, 640, ["实测：V1 由 1088s 超时触发；", "V4 由轮次 4 触发"], 11, "#b08900")

    s.arrow(300, 690, 560, 690)
    s.arrow(820, 612, 820, 660)
    s.arrow(820, 660, 600, 690, "#b03a3a", "", "arr")
    s.box(680, 726, 300, 52, ["POST /companion/{sid}/finish 落库"], C_GRAY, C_GRAY_S)
    s.note(560, 742, ["同日合并取最高等级 → #130 保留 high"], 11, "#777", "end")
    s.save("fig1_content_to_state.svg")


# ============================================================ 图2 相位状态机
def fig2():
    s = Svg(1060, 430, "图 2 · 陪伴会话相位状态机（含触发条件与实测到达次数）")
    phases = [
        ("consent", "知情同意", "6 次"),
        ("quality", "质量前置", "6 次"),
        ("opening", "开场白", "6 次"),
        ("main", "主体对话", "5 次"),
        ("closing", "收尾", "4 次"),
        ("done", "完成", "1 次"),
    ]
    xs = [110 + i * 160 for i in range(len(phases))]
    y = 150
    for (key, name, cnt), x in zip(phases, xs):
        s.box(x, y, 130, 56, [key, name], C_BLUE, C_BLUE_S)
        s.add('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" fill="#777">%s</text>'
              % (x, y + 50, esc(cnt)))
    labels = ["三项同意全勾选", "质检完成", "收到第 1 轮", "轮次<4 且 <240s",
              "轮次≥4 或 ≥240s", "再次发言/收尾"]
    for i in range(len(phases) - 1):
        x1, x2 = xs[i] + 65, xs[i + 1] - 65
        s.arrow(x1, y, x2, y)
        s.add('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="10.5" fill="#666">%s</text>'
              % ((x1 + x2) / 2.0, y - 12, esc(labels[i])))

    # crisis 分支
    s.arrow(xs[3], y + 28, xs[3], 268, "#b03a3a", "6 4", "arr")
    s.box(xs[3], 305, 190, 62, ["crisis", "任意阶段 risk=high"], C_RED, C_RED_S)
    s.add('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" fill="#777">实测 2 次</text>'
          % (xs[3], 305 + 48))
    s.add('<text x="%.1f" y="%.1f" font-size="11" fill="#b03a3a">（短路：不再生成、不再推进 main）</text>'
          % (xs[3] + 105, 310))

    s.note(40, 380, [
        "说明：CRISIS 由 engine.respond() 直接返回，不写 tracer.phase，因此 [trace] 的 phase.transition 里看不到它，",
        "但返回给前端的 phase 字段为 crisis，前端据此自动调用 /finish。",
        "M1 / M2 / V1 均未调用 /finish，会话数据未落库（M2、V1 停在 closing/done 之前）。",
    ])
    s.save("fig2_phase_machine.svg")


# ============================================================ 图3 时间轴风险
def fig3():
    s = Svg(1140, 560, "图 3 · 各轮对话的风险等级演变时间轴（23:08 – 23:59）")
    x0, x1 = 90, 1060
    y_base = 400

    def tpx(hhmm):
        parts = hhmm.split(":")
        h, m = int(parts[0]), int(parts[1])
        mins = (int(h) - 23) * 60 + int(m)
        base = 8 * 60  # 23:08
        return x0 + (mins - base) / 51.0 * (x1 - x0)

    def lpy(lvl):
        return {"none": y_base, "watch": y_base - 70, "moderate": y_base - 140,
                "high": y_base - 210}[lvl]

    # 网格
    for lvl, name in [("none", "none（无风险）"), ("watch", "watch（观察/low）"),
                      ("moderate", "moderate（中危）"), ("high", "high（高危）")]:
        y = lpy(lvl)
        col = "#b03a3a" if lvl == "high" else ("#d0d0d0" if lvl != "none" else "#dcdcdc")
        s.add('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1" '
              'stroke-dasharray="4 4"/>' % (x0, y, x1, y, col))
        s.add('<text x="%.1f" y="%.1f" text-anchor="end" font-size="12" fill="%s">%s</text>'
              % (x0 - 10, y + 4, "#b03a3a" if lvl == "high" else "#666", esc(name)))
    for m in range(8, 60, 5):
        x = tpx("23:%02d" % m)
        s.add('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="#eee" stroke-width="1"/>'
              % (x, y_base + 12, x, y_base + 12))
        s.add('<text x="%.1f" y="%d" text-anchor="middle" font-size="11" fill="#888">23:%02d</text>'
              % (x, y_base + 30, m))

    sessions = [
        ("M2  cmp_49691582", "#9aa0a6", "23:08", "23:11",
         [("23:09", "none", "平静（mock）"), ("23:10", "none", "平静"),
          ("23:10", "none", "平静"), ("23:11", "none", "平静 → closing")], 0),
        ("V1  cmp_50949582", "#3a6ea5", "23:29", "23:47",
         [("23:30", "none", "惊讶 conf .48"), ("23:47", "none", "平静 → closing（超时）")], 1),
        ("V2  cmp_52155908", "#2f8a55", "23:49", "23:51",
         [("23:50", "none", "疲惫 v-0.70｜模型=watch"),
          ("23:50", "high", "模型=high｜“I simply don't have the energy to…”")], 2),
        ("V3  cmp_52313753", "#b58900", "23:52", "23:54",
         [("23:54", "high", "模型=high｜“Work has been pretty stressful la…”")], 3),
        ("V4  cmp_52527311", "#7b4fa8", "23:55", "23:59",
         [("23:56", "none", "疲惫 v-0.70"), ("23:57", "none", "疲惫 v+0.20"),
          ("23:58", "none", "⚠ 标签解析失败→平静"), ("23:58", "none", "紧张 → closing"),
          ("23:59", "none", "愉悦 → done → 落库")], 4),
    ]
    for name, color, st, et, turns, row in sessions:
        xa, xb = tpx(st + ":00"), tpx(et + ":00")
        s.add('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="%s" opacity="0.07"/>'
              % (xa, 70 + row * 6, xb - xa, 300, color))
        s.add('<text x="%.1f" y="%d" font-size="12" font-weight="600" fill="%s">%s</text>'
              % (xa + 4, 96 + row * 0, color, esc(name)))
        for i, (tt, lvl, lab) in enumerate(turns):
            x = tpx(tt + ":00")
            y = lpy(lvl)
            fill = C_RED_S if lvl == "high" else color
            s.add('<circle cx="%.1f" cy="%.1f" r="6.5" fill="%s" stroke="#fff" stroke-width="1.5"/>'
                  % (x, y, fill))
            up = (i % 2 == 0)
            ty = y - 14 if up else y + 20
            anchor = "middle"
            s.add('<text x="%.1f" y="%.1f" text-anchor="%s" font-size="10.5" fill="%s">%s</text>'
                  % (x, ty, anchor, fill if lvl == "high" else "#555", esc(lab)))

    s.note(x0, 470, [
        "○ 灰 = Mock 期（llm_provider=mock，风险全部由关键词规则判定，模型通道等价于不存在）",
        "● 红 = 真实模型判定为 high，两例的 evidence 均为英文，且均为过报（“工作压力大”被判 high）",
        "⚠ 圆点上方文字为该轮模型输出的情绪标签或风险证据；用户原话被 tracer 脱敏，日志中未保留。",
    ])
    s.save("fig3_timeline_risk.svg")


# ============================================================ 图4 V4 情绪×相位
def fig4():
    s = Svg(1040, 470, "图 4 · V4（cmp_52527311）逐轮「对话内容线索 → 相位」对应关系")
    turns = [
        ("第 1 轮", "疲惫", -0.70, 0.10, 0.72, "main", "opening → main（轮次1 / 27s）"),
        ("第 2 轮", "疲惫", 0.20, 0.15, 0.68, "main", "main → main（轮次2 / 75s）"),
        ("第 3 轮", "⚠ 解析失败", 0.15, -0.17, 0.10, "main", "main → main（轮次3 / 129s）"),
        ("第 4 轮", "紧张", 0.10, 0.30, 0.68, "closing", "main → closing（轮次4 / 166s）"),
        ("第 5 轮", "愉悦", 0.60, 0.15, 0.64, "done", "closing → done（轮次5 / 190s）"),
    ]
    xs = [170 + i * 170 for i in range(5)]
    band_colors = {"main": "#e8f1fd", "closing": "#fdf6e3", "done": "#e6f6ec"}
    band_stroke = {"main": "#3a6ea5", "closing": "#b58900", "done": "#2f8a55"}

    # 相位带
    for (_, _, _, _, _, ph, _), x in zip(turns, xs):
        s.add('<rect x="%.1f" y="90" width="150" height="300" rx="8" fill="%s" stroke="%s" '
              'stroke-width="1.2" opacity="0.55"/>' % (x - 75, band_colors[ph], band_stroke[ph]))
        s.add('<text x="%.1f" y="112" text-anchor="middle" font-size="13" font-weight="700" '
              'fill="%s">%s</text>' % (x, band_stroke[ph], esc(ph)))

    # valence 轴
    y0, y1 = 300, 140  # v = -1 .. +1
    def vy(v):
        return y0 + (v + 1) / 2.0 * (y1 - y0)
    s.add('<line x1="100" y1="%.1f" x2="960" y2="%.1f" stroke="#ddd"/>' % (vy(0), vy(0)))
    s.add('<text x="96" y="%.1f" text-anchor="end" font-size="11" fill="#999">愉悦度 0</text>' % (vy(0) + 4))
    s.add('<text x="96" y="%.1f" text-anchor="end" font-size="11" fill="#999">+1</text>' % (vy(1) + 4))
    s.add('<text x="96" y="%.1f" text-anchor="end" font-size="11" fill="#999">-1</text>' % (vy(-1) + 4))

    for (name, label, v, a, conf, ph, _), x in zip(turns, xs):
        fail = "失败" in label
        col = "#b03a3a" if fail else ("#2f8a55" if v >= 0 else "#3a6ea5")
        top = vy(v)
        s.add('<rect x="%.1f" y="%.1f" width="54" height="%.1f" rx="4" fill="%s" opacity="0.85"/>'
              % (x - 27, top, vy(0) - top if v >= 0 else top - vy(0), col))
        s.add('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="12.5" font-weight="700" '
              'fill="%s">%s</text>' % (x, (top + vy(0)) / 2 - 12 if v >= 0 else (top + vy(0)) / 2 + 4, col, esc("%+.2f" % v)))
        s.add('<text x="%.1f" y="325" text-anchor="middle" font-size="13" fill="#222">%s</text>' % (x, esc(name)))
        s.add('<text x="%.1f" y="345" text-anchor="middle" font-size="12" font-weight="600" '
              'fill="%s">情绪 %s</text>' % (x, col, esc(label)))
        s.add('<text x="%.1f" y="363" text-anchor="middle" font-size="10.5" fill="#888">置信度 %.2f</text>' % (x, conf))

    # 相位转移说明（底部）
    for (_, _, _, _, _, _, trans), x in zip(turns, xs):
        s.note(x, 405, [trans], 11, "#333", "middle")

    s.add('<text x="96" y="70" font-size="12" fill="#666">底色 = 该轮所在相位</text>')
    s.add('<text x="96" y="440" font-size="11.5" fill="#b03a3a">'
          '第 3 轮模型未输出 &lt;emotion&gt; 标签，情绪退回关键词「平静」，但相位照常推进 —— 情绪失败不影响状态机。</text>')
    s.save("fig4_v4_emotion_phase.svg")


if __name__ == "__main__":
    fig1()
    fig2()
    fig3()
    fig4()
