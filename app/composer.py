"""Soạn nội dung bài đăng hằng ngày cho VTS Running Club.

- Bản nháp dựng từ nhiều "kho câu" (mở bài, giới thiệu top, mẹo, kết bài...).
  Mỗi kho được xáo trộn cố định rồi xoay vòng theo ngày -> hạn chế lặp câu.
- Câu nào có chỗ trống ({years}, {target}, {rules_summary}...) chỉ được chọn khi ctx có giá trị
  tương ứng; một mục trong kho có thể là bộ (câu chính, câu thay thế) để tự rơi về câu thay thế.
- Nếu cấu hình LLM (Claude/Gemini), bản nháp được viết lại cho tự nhiên hơn;
  lỗi hoặc kết quả sai tên -> tự dùng lại bản nháp.

API dùng ngoài: build_context, compose, rewrite_with_llm, dmy, fmt_km, fmt_big.
cfg (dict): start, end (date), event_name, target_km (float|None), hashtags, show_daily_km,
            llm_provider, anthropic_key, anthropic_model, gemini_key, gemini_model, founded_year (int|None).
ctx có thể được bổ sung sau build_context: rules_summary (1 dòng), streak_days, female_target_km.
"""
from __future__ import annotations

import logging
import random
import string
from datetime import date, timedelta

import requests

log = logging.getLogger(__name__)

WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ nhật"]
MEDALS = ["🥇", "🥈", "🥉"]

# Quãng đường tham chiếu (đường bộ, xấp xỉ) để so sánh tổng km của cả đội
DISTANCES = [
    (95, "TP.HCM – Vũng Tàu"),
    (300, "TP.HCM – Đà Lạt"),
    (430, "TP.HCM – Nha Trang"),
    (650, "TP.HCM – Quy Nhơn"),
    (960, "TP.HCM – Đà Nẵng"),
    (1050, "TP.HCM – Huế"),
    (1700, "TP.HCM – Hà Nội"),
]

Template = str | tuple[str, ...]   # một câu, hoặc (câu chính, câu thay thế...) thử lần lượt

# ----------------------------------------------------------------- kho câu
HEADERS: list[Template] = [
    "🏃 VTS RUNNING CLUB | {weekday} {date} | Ngày {day_no}/{total_days}",
    "📣 BẢN TIN ĐƯỜNG CHẠY | Ngày {day_no}/{total_days} ({weekday} {date})",
    ("🔥 GIẢI CHẠY MỪNG {years} NĂM VTS | Ngày {day_no}/{total_days}",
     "🔥 {event_name_upper} | Ngày {day_no}/{total_days}"),
    "🌅 CHÀO NGÀY MỚI | {weekday} {date} | Còn {days_left} ngày",
    "👟 NHỊP CHẠY VTS | {weekday} {date} | Ngày {day_no}/{total_days}",
]

OPENERS: dict[str, list[Template]] = {
    "mid": [
        "Chào buổi sáng cả nhà VTS! Đường chạy hôm nay đã sẵn sàng, còn đôi giày của bạn thì sao? 👟",
        ("Một ngày mới, thêm một cơ hội để cộng km cho bản thân và cho kỷ niệm {years} năm VTS.",
         "Một ngày mới, thêm một cơ hội để cộng km cho bản thân và cho {event_name}."),
        "Không cần chạy nhanh, chỉ cần không dừng lại — đó là bí quyết của mọi runner bền bỉ.",
        "Cà phê chưa kịp nguội mà bảng xếp hạng đã nóng rồi đây ☕🔥",
        ("Mỗi bước chân hôm nay là một lời chúc mừng {years} năm VTS 🎂",
         "Mỗi bước chân hôm nay đều góp vào thành tích chung của {event_name} 👣"),
        "Hôm qua cả đội lại có thêm những buổi chạy thật đẹp. Cùng điểm lại nhé!",
        "Công việc bận rộn đến đâu cũng hãy dành cho mình 20 phút vận động nhé.",
        "Km không tự nhiên mà có, nhưng niềm vui thì có ngay khi bạn bắt đầu chạy 😄",
        "Sáng sớm hay tối muộn đều được, miễn là hôm nay có thêm vài km trên Strava!",
        "Bảng xếp hạng vẫn đang rất mở, chưa có gì là chắc chắn cả!",
        "Còn {days_left} ngày — vẫn đủ thời gian để bất kỳ ai tạo nên bất ngờ.",
        "Một buổi chạy ngắn cũng có thể làm ngày hôm nay khác hẳn. Mình cùng bắt đầu nhé!",
        "Bền bỉ mỗi ngày một chút, đến cuối giải nhìn lại sẽ thấy thành quả rất lớn.",
        "Không cần áp lực thành tích, chỉ cần đều đặn là bạn đã thắng chính mình rồi.",
    ],
    "monday": [
        "Thứ Hai rồi! Khởi động tuần mới bằng vài km nhẹ nhàng để lấy năng lượng cho cả tuần nhé 💪",
        "Thứ Hai không đáng sợ nếu bắt đầu bằng một buổi chạy. Tuần mới, km mới!",
    ],
    "saturday": [
        "Cuối tuần là thời điểm vàng cho một buổi chạy dài hơn thường lệ. Rủ cả nhà đi cùng cho vui!",
        "Thứ Bảy thảnh thơi — chạy, đi bộ hay đạp xe đều được quy đổi km cả đấy 🚴",
    ],
    "sunday": [
        "Chủ nhật thư thả — một buổi đi bộ công viên cũng là thêm km cho giải đấy!",
        "Chủ nhật nạp năng lượng, sẵn sàng cho tuần mới. Đừng quên vài km trước khi nghỉ ngơi nhé!",
    ],
    "last_week": [
        "Chỉ còn {days_left} ngày! Đây là lúc tăng tốc — hoặc ít nhất đừng để chuỗi ngày chạy bị đứt 😉",
        ("Đếm ngược {days_left} ngày về đích. Ai còn thiếu so với mốc {target} km thì đây là cơ hội cuối!",
         "Đếm ngược {days_left} ngày về đích. Ai còn thiếu km thì đây là cơ hội cuối!"),
        "Tuần cuối cùng đã đến! Bảng xếp hạng có thể đảo chiều bất cứ lúc nào.",
        ("Còn {days_left} ngày nữa là khép lại giải chạy mừng {years} năm VTS. Về đích cùng nhau nhé!",
         "Còn {days_left} ngày nữa là khép lại {event_name}. Về đích cùng nhau nhé!"),
        "Nước rút thôi cả nhà! {days_left} ngày, mỗi ngày thêm một chút là đủ một bước tiến lớn.",
        "Đích đến đã rất gần. Giữ nhịp, giữ sức, giữ niềm vui!",
    ],
    "final_day": [
        "HÔM NAY LÀ NGÀY CUỐI CÙNG của giải chạy! Ai còn thiếu km thì xỏ giày ngay thôi, hết hôm nay là khép sổ ⏰",
    ],
    "finale": [
        ("{event_name} đã chính thức khép lại — chúc mừng {years} năm VTS! 🎂 "
         "Cảm ơn tất cả anh chị em đã cùng góp những bước chân ý nghĩa 💙",
         "{event_name} đã chính thức khép lại. "
         "Cảm ơn tất cả anh chị em đã cùng góp những bước chân ý nghĩa 💙"),
    ],
}

LEAD_INTROS: list[Template] = [
    "Top 3 đang dẫn đầu bảng tổng:",
    "Ba cái tên đang giữ vững ngôi đầu:",
    "Tốp đầu bảng xếp hạng hiện tại:",
    "Những người đang dẫn đoàn:",
    "Bục vinh quang (tạm thời) thuộc về:",
    "Ba \"đầu tàu\" của VTS Running Club:",
    "Đang cầm cờ dẫn đầu cuộc đua:",
    "Top 3 tính đến sáng nay:",
    "Nhóm dẫn đầu hôm nay gọi tên:",
    "Top 3 tạm thời của đường đua:",
]

DAILY_INTROS: list[Template] = [
    "Top 3 km {period}:",
    "Những đôi chân \"nóng\" nhất {period}:",
    "Ngôi sao {period}:",
    "Ba runner cày km nhiều nhất {period}:",
    "Vinh danh {period}:",
    "Bứt phá {period}:",
    "Chăm chỉ nhất {period}:",
    "Bảng vàng {period}:",
    "Top tăng tốc {period}:",
]

# Các con số luật (pace, quy đổi...) KHÔNG viết cứng ở đây: lấy từ ctx["rules_summary"] do daily_post sinh.
TIPS: list[Template] = [
    "Nhớ bật định vị trước khi chạy — tracklog không có bản đồ (treadmill, mất GPS) sẽ không được tính.",
    "Luật tính km của giải: {rules_summary} Gõ /rules để xem đầy đủ.",
    "Mốc tham khảo: chạy đều {per_day} km/ngày thì đến hết ngày {report_no} là {benchmark} km. "
    "Gõ /me để xem mình đang ở đâu nhé!",
    "Có giải riêng cho người chạy đều nhất ({streak_days} ngày liên tục trở lên) — chạy đều quan trọng hơn chạy dài!",
    "Có giải riêng cho VĐV nữ hoàn thành {female_target_km} km có thành tích cao nhất — các chị em ơi, cố lên!",
    "Chờ điện thoại/đồng hồ báo \"GPS Ready\" rồi mới bấm Start để không bị mất bản đồ.",
    "Chạy xong, mở lại hoạt động trên Strava kiểm tra đã có bản đồ chưa nhé.",
    "Khởi động 5 phút trước và giãn cơ sau khi chạy để hạn chế chấn thương, còn chạy đều đến cuối giải.",
    "Uống đủ nước trước và sau khi chạy, nhất là những hôm trời nắng nóng.",
    "Chưa kết nối Strava với bot? Gõ /connect trong Telegram để km của bạn được tự động ghi nhận.",
    "Gõ /me để xem tổng km, số ngày chạy và các hoạt động gần nhất của mình (kèm lý do nếu bị loại).",
    "Mục tiêu của giải là {target} km trong {total_days} ngày — trung bình chỉ {per_day} km mỗi ngày, ai cũng làm được!",
    "Hoạt động đồng bộ lên Strava là bot ghi nhận ngay — không cần gửi ảnh hay báo cáo tay.",
    "Nếu bận, chia nhỏ thành 2 buổi trong ngày vẫn hiệu quả và dễ giữ nhịp hơn.",
    "Theo dõi nhịp tim trong lúc chạy để giữ sức bền, đặc biệt ở các buổi recovery.",
    "Thắc mắc về luật tính km? Gõ /rules trong Telegram, bot trả lời ngay.",
]

CLOSINGS: dict[str, list[Template]] = {
    "mid": [
        "Hẹn gặp mọi người trên đường chạy hôm nay! 🏃‍♀️🏃",
        "Chạy vui, chạy khỏe, chạy an toàn nhé cả nhà! 💙",
        "Mỗi km đều đáng giá — hãy để lại dấu chân của bạn trên bảng xếp hạng!",
        "Xỏ giày thôi nào! 👟",
        "Kudos cho những ai đã chạy hôm qua — và cho cả những ai sẽ chạy hôm nay! 👏",
        "Không ai bị bỏ lại phía sau — cùng nhau về đích nhé!",
        "Tag một đồng nghiệp bạn muốn rủ chạy chiều nay đi! 😄",
        ("Chạy vì sức khỏe, vì tinh thần, vì {years} năm VTS! 🎉",
         "Chạy vì sức khỏe, vì tinh thần, vì màu cờ VTS Running Club! 🎉"),
        "Chúc cả nhà một ngày nhiều năng lượng và thêm thật nhiều km đẹp!",
        "Giữ lửa mỗi ngày, vạch đích sẽ tự đến gần hơn thôi!",
    ],
    "last_week": [
        "Tăng tốc về đích thôi cả nhà! 🏁",
        "Những ngày cuối là những ngày đẹp nhất — đừng bỏ lỡ nhé! 🔥",
        "Về đích cùng nhau, không ai bỏ cuộc! 💪",
        "Mỗi km tuần này đều có thể thay đổi bảng xếp hạng. Chạy thôi! 🏃",
    ],
    "final_day": [
        "Tiếng còi kết thúc sắp vang — hẹn gặp tất cả ở vạch đích! 🏁",
    ],
    "finale": [
        ("Hẹn gặp lại cả nhà ở những giải chạy tiếp theo. Chúc mừng {years} năm VTS! 🎂🎉",
         "Hẹn gặp lại cả nhà ở những giải chạy tiếp theo! 🎉"),
    ],
}


# ----------------------------------------------------------------- tiện ích
def dmy(d: date) -> str:
    return f"{d.day}/{d.month}"


def fmt_km(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def fmt_big(x: float) -> str:
    return f"{round(x):,}".replace(",", ".")


def _fields(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


def usable(pool: list[Template], ctx: dict) -> list[str]:
    """Lọc kho câu: giữ câu mà mọi chỗ trống đều có giá trị (khác None) trong ctx.

    Mục dạng bộ (câu chính, câu thay thế...) lấy câu đầu tiên dùng được."""
    out: list[str] = []
    for item in pool:
        variants = item if isinstance(item, tuple) else (item,)
        for v in variants:
            if all(ctx.get(f) is not None for f in _fields(v)):
                out.append(v)
                break
    return out


def pick(pool: list[str], day: date, salt: str) -> str:
    """Xáo trộn cố định theo 'salt' rồi xoay vòng theo ngày: ngày liền kề không trùng câu."""
    if not pool:
        return ""
    order = list(pool)
    random.Random(f"{salt}:{len(pool)}").shuffle(order)
    return order[day.toordinal() % len(order)]


def say(pool: list[Template], ctx: dict, salt: str) -> str:
    """pick + format với ctx (chỉ chọn trong các câu dùng được)."""
    return pick(usable(pool, ctx), ctx["today"], salt).format(**ctx)


def compare_distance(total: float, day: date) -> str:
    if total < DISTANCES[0][0]:
        return "tích tiểu thành đại, rồi sẽ thành một chặng dài!"
    top_km, top_name = DISTANCES[-1]
    if total >= 2 * top_km:
        return f"gấp {total / top_km:.1f}".replace(".", ",") + f" lần quãng đường {top_name}!"
    reached = [d for d in DISTANCES if d[0] <= total]
    window = reached[-min(4, len(reached)):]
    km, name = random.Random(f"dist:{day.toordinal()}:{round(total)}:{len(window)}").choice(window)
    templates = [
        "đã vượt quãng đường {name} (khoảng {km} km)!",
        "đủ để chinh phục cung {name} (xấp xỉ {km} km)!",
        "đã chạy nhiều hơn chặng {name} ({km} km)!",
        "nếu ghép thành một hành trình thì đã qua mốc {name} (~{km} km)!",
    ]
    template = random.Random(f"dist-template:{day.toordinal()}:{round(total)}:{name}").choice(templates)
    return template.format(name=name, km=fmt_big(km))


# ----------------------------------------------------------------- context
def build_context(today: date, rows: list[dict], daily: dict | None,
                  prev_date: date | None, cfg: dict) -> dict:
    """rows: [{"nick": str, "total": km}] theo thứ tự xếp hạng; daily: {"top": [(nick, km)], "active": n, "sum": km}."""
    start, end = cfg["start"], cfg["end"]
    event_name = (cfg.get("event_name") or "Giải chạy VTS Running Club").strip()
    founded_year = cfg.get("founded_year")
    report_day = today - timedelta(days=1)
    total_days = (end - start).days + 1
    day_no = (today - start).days + 1
    report_no = min(day_no - 1, total_days)
    days_left = max(0, (end - today).days + 1)

    if today > end:
        phase = "finale"
    elif days_left == 1:
        phase = "final_day"
    elif days_left <= 7:
        phase = "last_week"
    else:
        phase = "mid"

    if prev_date is None or prev_date >= report_day:
        period = f"hôm qua ({dmy(report_day)})"
        if phase == "finale":
            period = f"ngày chạy cuối cùng ({dmy(report_day)})"
    else:
        period = f"từ {dmy(prev_date)} đến {dmy(report_day)}"

    target_km = cfg.get("target_km")
    years = (end.year - int(founded_year)) if founded_year is not None else None
    if years is not None and years <= 0:
        years = None
    per_day = (target_km / total_days) if target_km else None
    return {
        "today": today,
        "weekday_idx": today.weekday(),
        "weekday": WEEKDAYS[today.weekday()],
        "date": dmy(today),
        "phase": phase,
        "day_no": day_no,
        "report_no": report_no,
        "total_days": total_days,
        "days_left": days_left,
        "event_name": event_name,
        "event_name_upper": event_name.upper(),
        "years": years,
        "target": fmt_big(target_km) if target_km else None,
        "per_day": fmt_km(per_day) if per_day else None,
        "benchmark": fmt_km(per_day * report_no) if per_day else None,
        "top3": [r["nick"] for r in rows[:3]],
        "grand": sum(r["total"] for r in rows),
        "daily": daily,
        "period": period,
        "hashtags": cfg.get("hashtags", "") or "",
        "show_daily_km": cfg.get("show_daily_km", True),
        # bổ sung tuỳ ý bởi daily_post; None = câu liên quan không được chọn
        "rules_summary": None,
        "streak_days": None,
        "female_target_km": None,
    }


# ----------------------------------------------------------------- bản nháp
def compose(ctx: dict) -> str:
    phase = ctx["phase"]
    out: list[str] = []

    if phase == "finale":
        out.append(f"🏁 KHÉP LẠI {ctx['event_name_upper']} | {ctx['date']}")
    elif phase == "final_day":
        out.append(f"🔔 NGÀY CUỐI CÙNG | {ctx['weekday']} {ctx['date']}")
    else:
        out.append(say(HEADERS, ctx, "header"))
    out.append("")

    opener_pool = OPENERS[phase]
    if phase == "mid" and ctx["weekday_idx"] in (0, 5, 6):
        opener_pool = OPENERS[{0: "monday", 5: "saturday", 6: "sunday"}[ctx["weekday_idx"]]]
    out.append(say(opener_pool, ctx, f"open-{phase}"))
    out.append("")

    # Top 3 bảng tổng — chỉ tên, không km
    if ctx["top3"]:
        intro = ("Top 3 chung cuộc (tạm tính, chờ Ban tổ chức xác nhận):"
                 if phase == "finale" else say(LEAD_INTROS, ctx, "lead"))
        out.append(f"🏆 {intro}")
        out += [f"{MEDALS[i]} {n}" for i, n in enumerate(ctx["top3"])]
        out.append("")

    # Top 3 trong ngày
    daily = ctx["daily"]
    period_cap = ctx["period"][0].upper() + ctx["period"][1:]
    if daily is not None:
        if not daily["top"]:
            out.append(f"⚡ {period_cap} chưa có tracklog hợp lệ nào được ghi nhận. Hôm nay ai sẽ mở hàng? 😄")
        else:
            out.append("⚡ " + say(DAILY_INTROS, ctx, "daily"))
            for i, (nick, km) in enumerate(daily["top"], 1):
                out.append(f"{i}. {nick} – {fmt_km(km)} km" if ctx["show_daily_km"] else f"{i}. {nick}")
        out.append("")

    # Số liệu chung của cả đội
    stats_lines: list[str] = []
    if daily and daily.get("active"):
        stats_lines.append(f"📊 {period_cap} có {daily['active']} anh chị em ghi nhận hoạt động hợp lệ, "
                           f"cả đội cộng thêm {fmt_km(daily['sum'])} km.")
    if ctx["grand"] > 0:
        stats_lines.append(f"🌏 Tổng quãng đường cả đội: {fmt_big(ctx['grand'])} km — "
                           f"{compare_distance(ctx['grand'], ctx['today'])}")
    if stats_lines:
        out += stats_lines + [""]

    if phase == "finale":
        out.append("📌 Kết quả chính thức và lễ trao giải sẽ do Ban tổ chức công bố.")
    elif phase == "final_day":
        out.append("💡 Nhớ bật định vị và kiểm tra tracklog có bản đồ — hoạt động không có map sẽ không được tính.")
    else:
        out.append("💡 " + say(TIPS, ctx, "tip"))
    out.append("")

    out.append(say(CLOSINGS[phase], ctx, f"close-{phase}"))
    if ctx["hashtags"]:
        out += ["", ctx["hashtags"]]
    return "\n".join(out).strip()


# ----------------------------------------------------------------- LLM
PROMPT = """Bạn viết nội dung cho nhóm chạy bộ nội bộ "VTS Running Club" trong khuôn khổ "{event_name}" \
(giải chạy online nội bộ, km được tính tự động từ Strava). Hãy viết lại BẢN NHÁP bên dưới thành một bài đăng \
MỚI cho nhóm, tự nhiên, vui và truyền cảm hứng, bằng tiếng Việt.

Quy tắc bắt buộc:
- Giữ CHÍNH XÁC tên người như trong bản nháp (không sửa dấu, không viết tắt, không đổi thứ tự).
- Phần top 3 bảng tổng: chỉ nêu tên kèm 🥇🥈🥉, TUYỆT ĐỐI không nêu số km của họ.
- Phần top 3 trong ngày: giữ đúng tên và số km như bản nháp.
- Giữ đúng mọi con số, ngày tháng, tên lệnh (/me, /connect, /rules) và luật tính km như bản nháp. \
Không bịa thêm thông tin, giải thưởng, thời tiết, sự kiện.
- 90–170 từ, văn bản thuần (không markdown, không dấu **), emoji vừa phải, mỗi danh sách tên một dòng.
- Không lặp lại câu mở đầu / kết thúc của các bài gần đây.
- Dòng cuối cùng giữ nguyên hashtag: {hashtags}
Chỉ trả về nội dung bài đăng, không giải thích.

BẢN NHÁP:
{draft}

CÁC BÀI GẦN ĐÂY (tránh lặp):
{history}
"""


def _anthropic(prompt: str, cfg: dict) -> str:
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": cfg["anthropic_key"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": cfg["anthropic_model"], "max_tokens": 1200,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


def _gemini(prompt: str, cfg: dict) -> str:
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{cfg['gemini_model']}:generateContent",
        params={"key": cfg["gemini_key"]},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=90,
    )
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def rewrite_with_llm(ctx: dict, draft: str, history: list[str], cfg: dict) -> tuple[str, str]:
    """Trả về (nội dung, nguồn). Nguồn = 'template' nếu không dùng/không đạt LLM.

    Hàm đồng bộ (dùng requests); trong code async hãy gọi qua asyncio.to_thread."""
    provider = (cfg.get("llm_provider") or "none").lower()
    if provider not in ("anthropic", "gemini"):
        return draft, "template"
    if provider == "anthropic" and not cfg.get("anthropic_key"):
        log.warning("[LLM] llm_provider=anthropic nhưng thiếu anthropic_key, dùng bản template.")
        return draft, "template"
    if provider == "gemini" and not cfg.get("gemini_key"):
        log.warning("[LLM] llm_provider=gemini nhưng thiếu gemini_key, dùng bản template.")
        return draft, "template"

    prompt = PROMPT.format(event_name=ctx.get("event_name", "giải chạy"), hashtags=ctx["hashtags"], draft=draft,
                           history="\n---\n".join(history[-5:]) or "(chưa có)")
    try:
        text = (_anthropic if provider == "anthropic" else _gemini)(prompt, cfg).strip()
    except Exception as e:  # noqa: BLE001
        log.warning("[LLM] Lỗi gọi %s, dùng bản template: %s", provider, e)
        return draft, "template"

    names = list(ctx["top3"]) + [n for n, _ in (ctx["daily"]["top"] if ctx["daily"] else [])]
    missing = [n for n in names if n not in text]
    if not text or missing:
        log.warning("[LLM] Kết quả thiếu tên %s, dùng bản template.", missing)
        return draft, "template"
    if ctx["hashtags"] and ctx["hashtags"] not in text:
        text += "\n\n" + ctx["hashtags"]
    return text, provider
