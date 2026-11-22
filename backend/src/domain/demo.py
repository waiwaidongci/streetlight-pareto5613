"""临汀市虚拟路灯台账、原始报障（热线/巡检重复上报）与已归并暗区事件。"""


# 灯组：(片区, 点位, 灯具数)
_SITES = (
    ("滨河片区", "柳汀大道北段", 32),
    ("老城片区", "东门巷", 5),
    ("滨河片区", "汀桥路公交站段", 24),
    ("滨河片区", "长堤辅道", 3),
    ("老城片区", "西仓里路口", 2),
    ("滨河片区", "渡口支路", 9),
    ("滨河片区", "桐浦堤下通道", 6),
    ("老城片区", "埠头街南段", 14),
    ("老城片区", "石平码头广场路", 11),
    ("老城片区", "钟楼西街", 17),
)

# 每个灯组的原始报障：(日, 时, 分, 来源)。同灯组、半小时内的多条会被归并为同一工单。
# 热线与巡检常对同一处暗灯各报一遍，这里刻意构造重复上报与跨窗口的独立故障。
_REPORTS = (
    # LH-01 柳汀大道北段：14 日早晚两处故障、18 日一处
    ((14, 20, 5, "热线"), (14, 20, 18, "巡检"), (14, 21, 40, "热线"),
     (18, 19, 12, "热线"), (18, 19, 30, "巡检")),
    # LH-02 东门巷：五盏灯、报修密集，16 日两条相隔 40 分钟属不同故障
    ((14, 18, 2, "热线"), (16, 21, 10, "巡检"), (16, 21, 50, "热线"),
     (19, 22, 5, "热线"), (19, 22, 20, "巡检")),
    # LH-03 汀桥路公交站段
    ((15, 7, 30, "巡检"), (17, 19, 45, "热线"), (17, 20, 0, "巡检"), (20, 6, 15, "热线")),
    # LH-04 长堤辅道：3 盏灯
    ((16, 23, 0, "热线"), (20, 5, 40, "巡检"), (20, 6, 5, "热线")),
    # LH-05 西仓里路口
    ((19, 20, 25, "热线"),),
    (), (), (), (), (),
)


def demo_rows() -> list[dict]:
    rows = []
    for number, (district, site, fixtures) in enumerate(_SITES, start=1):
        site_id = f"LH-{number:02d}"
        rows.append({
            "record_type": "灯组台账", "site_id": site_id, "district": district,
            "site_name": site, "fixture_count": fixtures,
            "incident_id": None, "report_date": None,
            "report_ref": None, "source": None, "occurred_at": None, "legacy_order": None,
        })

    # 原始报障行：热线/巡检明细，带确定性报障编号。
    report_seq = 0
    for number, entries in enumerate(_REPORTS, start=1):
        site_id = f"LH-{number:02d}"
        for day, hour, minute, source in entries:
            report_seq += 1
            rows.append({
                "record_type": "原始报障", "site_id": site_id,
                "district": None, "site_name": None, "fixture_count": None,
                "incident_id": None, "report_date": None,
                "report_ref": f"RP-2611{report_seq:03d}",
                "source": source,
                "occurred_at": f"2026-11-{day:02d}T{hour:02d}:{minute:02d}:00",
                "legacy_order": None,
            })

    # 兼容旧口径的已归并暗区工单（与原始报障同周期，供无归并版本时的测算使用）。
    legacy_incidents = (
        (1, (14, 18, 14)),
        (2, (14, 16, 17, 19)),
        (3, (15, 17, 20)),
        (4, (16, 20)),
        (5, (19,)),
    )
    for number, days in legacy_incidents:
        site_id = f"LH-{number:02d}"
        for sequence, day in enumerate(days, start=1):
            rows.append({
                "record_type": "暗区工单", "site_id": site_id,
                "district": None, "site_name": None, "fixture_count": None,
                "incident_id": f"LT-2611{day}-{number:02d}-{sequence:02d}",
                "report_date": f"2026-11-{day:02d}",
                "report_ref": None, "source": None, "occurred_at": None, "legacy_order": None,
            })
    return rows
