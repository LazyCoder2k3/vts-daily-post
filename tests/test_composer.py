"""Kiểm tra app.composer: nội dung đủ tên, hashtag cuối, phase theo ngày, không còn dấu vết @tdht_bot, LLM fallback."""
from __future__ import annotations

import inspect
from datetime import date, timedelta

import pytest

from app import composer

START, END = date(2026, 9, 15), date(2026, 10, 14)

ROWS = [
    {"nick": "Nguyễn Văn An", "total": 120.5},
    {"nick": "Trần Thị Bình", "total": 100.0},
    {"nick": "Lê Cường", "total": 90.25},
    {"nick": "Phạm Dũng", "total": 10.0},
]
DAILY = {"top": [("Lê Cường", 12.3), ("Trần Thị Bình", 8.0), ("Hoàng Em", 5.5)], "active": 7, "sum": 45.6}


def make_cfg(**over) -> dict:
    cfg = {
        "start": START, "end": END, "event_name": "Giải chạy VTS 2026", "target_km": 45,
        "hashtags": "#VTSRunningClub", "show_daily_km": True, "llm_provider": "none",
        "anthropic_key": "", "anthropic_model": "m", "gemini_key": "", "gemini_model": "g",
        "founded_year": 2018,
    }
    cfg.update(over)
    return cfg


def posting_days():
    d = START + timedelta(days=1)
    while d <= END + timedelta(days=1):
        yield d
        d += timedelta(days=1)


def render(day: date, cfg: dict | None = None, extra: dict | None = None, rows=ROWS, daily=DAILY) -> tuple[dict, str]:
    cfg = cfg or make_cfg()
    ctx = composer.build_context(day, rows, daily, day - timedelta(days=1), cfg)
    if extra:
        ctx.update(extra)
    return ctx, composer.compose(ctx)


def assert_clean(text: str) -> None:
    assert "{" not in text and "}" not in text, f"còn chỗ trống chưa điền: {text}"
    assert "None" not in text
    assert "tdht" not in text.lower()


# ----------------------------------------------------------------- nội dung cơ bản
def test_compose_has_all_names_and_hashtag_last():
    _, text = render(date(2026, 9, 25))
    for n in ("Nguyễn Văn An", "Trần Thị Bình", "Lê Cường", "Hoàng Em"):
        assert n in text
    assert "Phạm Dũng" not in text                      # chỉ top 3 bảng tổng
    assert text.rstrip().endswith("#VTSRunningClub")
    assert "🥇 Nguyễn Văn An" in text and "🥈 Trần Thị Bình" in text and "🥉 Lê Cường" in text
    assert "1. Lê Cường – 12,3 km" in text
    assert "7 anh chị em" in text and "45,6 km" in text
    assert "321 km" in text                             # tổng 320,75 -> 321
    assert_clean(text)


def test_top3_total_km_not_leaked():
    _, text = render(date(2026, 9, 25))
    assert "120" not in text and "100,0" not in text and "90,2" not in text


def test_show_daily_km_false_hides_numbers():
    _, text = render(date(2026, 9, 25), make_cfg(show_daily_km=False))
    assert "1. Lê Cường" in text and "12,3 km" not in text


def test_daily_none_and_empty_top():
    _, text = render(date(2026, 9, 25), daily=None)
    assert "⚡" not in text and "📊" not in text
    _, text2 = render(date(2026, 9, 25), daily={"top": [], "active": 0, "sum": 0.0})
    assert "chưa có tracklog hợp lệ" in text2
    assert_clean(text2)


@pytest.mark.parametrize("day, phase", [
    (START + timedelta(days=1), "mid"),
    (date(2026, 10, 1), "mid"),
    (END - timedelta(days=6), "last_week"),
    (END - timedelta(days=1), "last_week"),
    (END, "final_day"),
    (END + timedelta(days=1), "finale"),
])
def test_phase_by_day(day, phase):
    ctx, text = render(day)
    assert ctx["phase"] == phase
    assert_clean(text)
    if phase == "final_day":
        assert "NGÀY CUỐI CÙNG" in text
    if phase == "finale":
        assert text.startswith("🏁 KHÉP LẠI GIẢI CHẠY VTS 2026")
        assert "chung cuộc" in text and "Ban tổ chức" in text


def test_every_posting_day_renders_cleanly_and_varies():
    headers = set()
    for d in posting_days():
        _, text = render(d)
        assert_clean(text)
        headers.add(text.splitlines()[0])
    assert len(headers) >= 5                            # xoay vòng theo ngày, không lặp một câu


# ----------------------------------------------------------------- years / event_name
def test_years_from_founded_year_and_mentioned():
    ctx, _ = render(date(2026, 9, 25))
    assert ctx["years"] == 8
    joined = "\n".join(render(d)[1] for d in posting_days()).lower()
    assert "8 năm" in joined


def test_no_years_when_founded_year_none_uses_event_name():
    cfg = make_cfg(founded_year=None)
    texts = [render(d, cfg)[1] for d in posting_days()]
    joined = "\n".join(texts)
    assert_clean(joined)
    assert "năm vts" not in joined.lower()
    assert "Tổng Công ty" not in joined
    assert "Giải chạy VTS 2026" in joined or "GIẢI CHẠY VTS 2026" in joined
    _, finale = render(END + timedelta(days=1), cfg)
    assert "Giải chạy VTS 2026 đã chính thức khép lại" in finale


def test_target_none_drops_target_sentences():
    cfg = make_cfg(target_km=None)
    ctx, _ = render(date(2026, 9, 25), cfg)
    assert ctx["target"] is None and ctx["per_day"] is None
    joined = "\n".join(render(d, cfg)[1] for d in posting_days())
    assert_clean(joined)
    assert "Mục tiêu của giải" not in joined and "Mốc tham khảo" not in joined


# ----------------------------------------------------------------- TIPS: lệnh mới, luật từ ctx
def test_tips_use_new_bot_commands_not_legacy():
    joined = "\n".join(render(d)[1] for d in posting_days())
    assert "/me" in joined and "/connect" in joined and "/rules" in joined
    for legacy in ("/log", "/register", "/list", "@tdht_bot"):
        assert legacy not in joined


def test_rule_numbers_only_from_rules_summary():
    joined = "\n".join(render(d)[1] for d in posting_days())
    assert "4:00" not in joined and "17:00" not in joined and "100 m bơi" not in joined
    summary = "Chạy/đi bộ: pace 4:00–17:00/km, tối thiểu 1 km; Đạp xe ×0,25: tối thiểu 4 km."
    joined2 = "\n".join(render(d, extra={"rules_summary": summary})[1] for d in posting_days())
    assert summary in joined2


def test_award_tips_are_data_driven():
    joined = "\n".join(render(d)[1] for d in posting_days())
    assert "ngày liên tục" not in joined and "VĐV nữ" not in joined
    extra = {"streak_days": 25, "female_target_km": 45}
    joined2 = "\n".join(render(d, extra=extra)[1] for d in posting_days())
    assert "25 ngày liên tục" in joined2 and "VĐV nữ hoàn thành 45 km" in joined2


def test_source_has_no_legacy_bot_reference():
    src = inspect.getsource(composer).lower()
    assert "tdht" not in src
    assert "tổng công ty" not in src


# ----------------------------------------------------------------- pick / usable
def test_usable_filters_and_falls_back():
    pool = ["a {x}", ("b {years}", "b {event_name}"), "c"]
    assert composer.usable(pool, {"x": None, "years": None, "event_name": "E"}) == ["b {event_name}", "c"]
    assert composer.usable(pool, {"x": 1, "years": 2, "event_name": "E"}) == ["a {x}", "b {years}", "c"]
    assert composer.pick([], date(2026, 1, 1), "s") == ""


def test_pick_is_deterministic_and_rotates():
    pool = ["a", "b", "c"]
    d = date(2026, 9, 20)
    assert composer.pick(pool, d, "s") == composer.pick(pool, d, "s")
    assert composer.pick(pool, d, "s") != composer.pick(pool, d + timedelta(days=1), "s")


def test_format_helpers():
    assert composer.dmy(date(2026, 10, 5)) == "5/10"
    assert composer.fmt_km(12.34) == "12,3"
    assert composer.fmt_big(1234.6) == "1.235"


# ----------------------------------------------------------------- LLM
def test_rewrite_none_returns_draft():
    ctx, draft = render(date(2026, 9, 25))
    assert composer.rewrite_with_llm(ctx, draft, [], make_cfg(llm_provider="none")) == (draft, "template")
    assert composer.rewrite_with_llm(ctx, draft, [], make_cfg(llm_provider="anthropic")) == (draft, "template")  # thiếu key


def test_rewrite_llm_falls_back_when_name_missing(monkeypatch):
    ctx, draft = render(date(2026, 9, 25))
    monkeypatch.setattr(composer, "_anthropic", lambda prompt, cfg: "Bài mới chỉ có Nguyễn Văn An và Lê Cường.")
    cfg = make_cfg(llm_provider="anthropic", anthropic_key="k")
    assert composer.rewrite_with_llm(ctx, draft, [], cfg) == (draft, "template")


def test_rewrite_llm_accepts_and_appends_hashtag(monkeypatch):
    ctx, draft = render(date(2026, 9, 25))
    captured = {}

    def fake(prompt, cfg):
        captured["prompt"] = prompt
        return "Chào cả nhà!\n🥇 Nguyễn Văn An\n🥈 Trần Thị Bình\n🥉 Lê Cường\n1. Lê Cường – 12,3 km\n2. Trần Thị Bình\n3. Hoàng Em"

    monkeypatch.setattr(composer, "_gemini", fake)
    cfg = make_cfg(llm_provider="gemini", gemini_key="k")
    text, source = composer.rewrite_with_llm(ctx, draft, ["bài cũ 1"], cfg)
    assert source == "gemini"
    assert text.endswith("#VTSRunningClub")
    assert "Giải chạy VTS 2026" in captured["prompt"] and "bài cũ 1" in captured["prompt"]
    assert "năm" not in captured["prompt"].split("BẢN NHÁP")[0].split("khuôn khổ")[0]   # không còn "mừng N năm"


def test_rewrite_llm_error_falls_back(monkeypatch):
    ctx, draft = render(date(2026, 9, 25))

    def boom(prompt, cfg):
        raise RuntimeError("API down")

    monkeypatch.setattr(composer, "_anthropic", boom)
    cfg = make_cfg(llm_provider="anthropic", anthropic_key="k")
    assert composer.rewrite_with_llm(ctx, draft, [], cfg) == (draft, "template")
