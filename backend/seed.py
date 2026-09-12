"""种子数据：造一个演示用户，方便打开网站就能看到效果。

运行：python seed.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import perception  # noqa: E402
import storage  # noqa: E402
from models import RiskFlag, UserProfile, now_iso  # noqa: E402

USER = "demo_user"


def main() -> None:
    storage.upsert_profile(UserProfile(
        user_id=USER, nickname="小林",
        personality_traits=["敏感", "内向", "责任心强"],
        coping_style=["独处", "听音乐", "偶尔跑步"],
        social_support=["大学室友阿宁", "姐姐", "养了一只叫豆豆的猫"],
        known_triggers=["被领导当众批评", "加班到很晚"],
        events=["上个月项目上线延期", "两周前和对象吵架"],
        preferences=["不喜欢被说教", "喜欢被提问而不是被建议"],
    ))

    # 14 天每日记录 + PHQ-9 / GAD-7（构造一个逐步恶化的趋势）
    base = date.today() - timedelta(days=13)
    phq = [4, 5, 5, 6, 7, 7, 8, 9, 10, 11, 11, 12, 13, 14]
    gad = [3, 4, 4, 5, 6, 6, 7, 8, 9, 10, 10, 11, 12, 13]
    narratives = [
        "今天还算顺利，把需求文档写完了，晚上和室友吃了火锅。",
        "有点累，睡得不太好，但总体还行。",
        "早上开会有点紧张，不过后来还好。",
        "感觉有点提不起劲，工作任务好多。",
        "今天被领导说了几句，有点难受，晚上没怎么睡着。",
        "一直在想昨天的事，觉得自己做得不够好。",
        "周末也在想工作，出去走了一会但没什么用。",
        "失眠了，凌晨三点才睡，白天很困。",
        "什么都不想做，饭也吃不下。",
        "和朋友说了说，稍微好一点，但晚上又不行了。",
        "感觉自己是个负担，拖累了大家。",
        "今天没出门，貓一直陪着我。",
        "很累，睡了很久还是累。",
        "不想动，也不想说话，觉得活着没什么意思。",
    ]
    for i in range(14):
        day = (base + timedelta(days=i)).isoformat()
        rec = perception.build_daily_record(
            USER, day, f"video://{USER}/{day}.mp4",
            narratives[i], narratives[i],
        )
        # 让最后几天呈现明显负性，用于演示多模态通道
        if i >= 10:
            rec.video_features["emotion_dist"]["sad"] = 0.55 + i * 0.02
            rec.narrative_analysis["neg_hits"] = 3 + i
            rec.narrative_analysis["tone"] = "negative"
        storage.add_daily_record(rec)

        items_p = [max(0, min(3, phq[i] // len(perception.SCALES["PHQ-9"]["items"])
                              + (1 if j < phq[i] % len(perception.SCALES["PHQ-9"]["items"]) else 0)))
                   for j in range(len(perception.SCALES["PHQ-9"]["items"]))]
        # 最后三天第9题给分，触发量表风险通道
        if i >= 11:
            items_p[8] = 1 if i == 11 else (2 if i == 12 else 3)
        sp = perception.score_scale(USER, "PHQ-9", items_p, day)
        storage.add_scale_score(sp)

        items_g = [max(0, min(3, gad[i] // len(perception.SCALES["GAD-7"]["items"])
                              + (1 if j < gad[i] % len(perception.SCALES["GAD-7"]["items"]) else 0)))
                   for j in range(len(perception.SCALES["GAD-7"]["items"]))]
        sg = perception.score_scale(USER, "GAD-7", items_g, day)
        storage.add_scale_score(sg)

        lvl, ev = perception.scale_risk_signal("PHQ-9", items_p)
        if lvl > 0:
            storage.add_risk_flag(RiskFlag(
                id=None, user_id=USER, session_id="scale", ts=now_iso(),
                source="scale_item", evidence=ev,
                level=["none", "low", "moderate", "high", "imminent"][lvl],
            ))

    print(f"✅ 种子数据完成：user_id={USER}")
    print(f"   PHQ-9 最新: {perception.score_scale(USER, 'PHQ-9', [1,2,2,2,1,2,2,1,3]).__dict__['total_score']} （示意）")
    print("   启动后端: uvicorn main:app --reload --port 8000")


if __name__ == "__main__":
    main()
