# Kế hoạch: Bot thống kê chạy bộ VTS Running Club độc lập (Strava → Telegram)

*Phiên bản 1 — khảo sát ngày 05/11/2026. Tài liệu này là bản kế hoạch để thống nhất trước khi code.*

---

## 0. Tóm tắt cho người bận

1. **Không thể "cào" hoạt động của Club qua API nữa.** Strava đã gỡ 3 endpoint *Club Activities*, *Club Members*, *Club Administrators* từ **01/09/2026**. API chỉ còn trả thông tin chung của club (tên, số thành viên).
2. **Hướng khả thi và bền:** mỗi runner bấm **"Connect with Strava"** một lần (OAuth, quyền `activity:read`). Bot nhận **webhook** mỗi khi có hoạt động mới, tự lấy chi tiết (quãng đường, thời gian, pace, có bản đồ GPS hay không, nhập tay hay không), **chấm hợp lệ theo bộ luật do bạn cấu hình**, lưu vào cơ sở dữ liệu, rồi trả lời lệnh trên Telegram và soạn bài đăng hằng ngày (tái dùng `composer.py` hiện có).
3. **Ba ràng buộc phải biết trước:**
   - App Strava mới chỉ được **10 athlete** (tự nâng, không cần duyệt). Trên 10 phải **gửi Strava review** (cộng đồng báo chờ vài tuần đến vài tháng) và tài khoản dev phải có **Strava subscription** (khoảng 11,99 USD/tháng hoặc 79,99 USD/năm). Club hiện có ~28 runner có hoạt động.
   - Thoả thuận API (hiệu lực 01/06/2026) nói **dữ liệu của một user chỉ được hiển thị cho chính user đó**; ngoài ra có điều khoản giới hạn cache (cộng đồng dev đọc là 7 ngày). Bảng xếp hạng nội bộ là vùng xám. Cần thiết kế giảm thiểu rủi ro (mục 8) và bạn chấp nhận rủi ro app bị thu hồi.
   - Cần **một máy chủ chạy liên tục có HTTPS** để nhận webhook và OAuth callback. GitHub Actions không đáp ứng được (đã thấy trễ 3–4 giờ mỗi đêm).
4. **Lộ trình:** 4 giai đoạn, tổng khoảng **10–16 ngày công**, chạy song song với @tdht_bot 1–2 tuần để đối chiếu rồi chuyển hẳn.
5. **Cần bạn quyết định 7 điểm ở mục 10** trước khi bắt đầu code.

---

## 1. Hiện trạng

| Thành phần | Hiện tại | Vấn đề |
|---|---|---|
| Nguồn dữ liệu | Userbot Telegram gửi `/ranking`, `/today` cho @tdht_bot rồi bóc tách bảng ASCII | Phụ thuộc hoàn toàn vào bot của người khác: luật, định dạng, uptime |
| Lịch chạy | GitHub Actions cron 23:20, 23:50, 06:47 | 15/17 snapshot trong `data/snapshots/` bị gắn `late=true`; **không snapshot nào có `daily`** vì chạy sau 0h nên `/today` đã sang ngày mới |
| Soạn bài | `composer.py` (kho câu + tuỳ chọn LLM) | Tốt, **tái dùng được gần nguyên vẹn** |
| Lưu trữ | JSON/TXT commit vào repo | Đủ cho bài đăng, không đủ để thống kê từng hoạt động |
| Luật tính km | Chỉ có quy đổi đạp xe/bơi khi đọc `/today`; pace, GPS, tối thiểu km do @tdht_bot quyết | Không kiểm soát được |

Kết luận: giữ `composer.py`, thay toàn bộ phần lấy dữ liệu và lịch chạy.

---

## 2. Strava còn cho phép gì (khảo sát 05/11/2026)

| Khả năng | Trạng thái | Ý nghĩa với bot |
|---|---|---|
| `GET /clubs/{id}/activities`, `/clubs/{id}/members`, `/clubs/{id}/admins` | **Đã gỡ 01/09/2026** | Không "cào club" được qua API |
| `GET /clubs/{id}`, `GET /athlete/clubs` | Còn | Chỉ để kiểm tra runner có trong club hay không (nếu runner cấp quyền) |
| `GET /athlete/activities` (`after`, `before`, `page`, `per_page`) | Còn, cần token **của từng athlete** | Backfill từ ngày bắt đầu giải, đối soát định kỳ |
| `GET /activities/{id}` | Còn, chỉ với hoạt động của athlete đã cấp quyền | Lấy chi tiết để chấm luật |
| Trường dữ liệu có sẵn | `start_date_local`, `distance`, `moving_time`, `elapsed_time`, `average_speed`, `sport_type`, `manual`, `trainer`, `flagged`, `device_name`, `map.summary_polyline`, `splits_metric`, `laps`, `total_elevation_gain` | Đủ để kiểm tra: pace, km tối thiểu, có bản đồ GPS, nhập tay, máy chạy bộ, thiết bị |
| Webhook | 1 subscription/app; sự kiện `activity` (create/update/delete) và `athlete` (deauthorize); payload **chỉ có ID**; phải trả HTTP 200 trong **2 giây**, Strava thử lại tối đa 3 lần | Realtime, nhưng phải xử lý bất đồng bộ và có cơ chế đối soát khi lỡ sự kiện |
| Rate limit mặc định | 200 request/15 phút, 2.000/ngày (non-upload 100/1.000); tự nâng lên 400/4.000 | Với 30–60 runner dùng webhook là dư |
| Athlete capacity | 1 (mặc định) → **10** (tự nâng trong API Settings) → **9.999** (sau review) → ≥10.000 (Extended Access) | Phải nộp review sớm |
| Subscription | Bắt buộc cho Standard Tier từ 30/06/2026; "subscription chính là quyền dùng API, không thu thêm phí" | Chi phí cố định hằng năm |
| Chính sách hiển thị | "Strava Data của một user chỉ được hiển thị/tiết lộ trong app cho chính user đó" (tóm tắt Thoả thuận API 01/06/2026). Không có ngoại lệ ghi rõ cho club/leaderboard. | Rủi ro tuân thủ, xem mục 8 |
| Thay đổi 2027 | Base URL đổi sang `api-v3.strava.com` (từ 04/01/2027); token bắt buộc trong header, bỏ `oauth/deauthorize` (01/06/2027) | Thiết kế: base URL cấu hình được, dùng header `Authorization`, dùng `oauth/revoke` |

---

## 3. So sánh các phương án lấy dữ liệu

| Tiêu chí | **A. OAuth từng runner + webhook** | B. Cào web club bằng cookie đăng nhập | C. Giữ @tdht_bot (hiện trạng) | D. Strava Group Challenge chính thức |
|---|---|---|---|---|
| Đủ dữ liệu để chấm luật (GPS, pace, nhập tay) | ✅ Đầy đủ | ⚠️ Phải mở từng trang hoạt động, dễ thiếu | ❌ Chỉ có kết quả đã chấm | ❌ Không tuỳ biến luật |
| Realtime | ✅ Webhook | ❌ Poll | ❌ Hỏi bot | — |
| Độ ổn định | ✅ API có hợp đồng | ❌ Vỡ khi Strava đổi HTML, có bot-protection | ⚠️ Tuỳ người khác | ✅ |
| Tuân thủ điều khoản | ⚠️ Vùng xám về hiển thị chéo | ❌ Vi phạm ToS, nguy cơ khoá tài khoản | — | ✅ |
| Công sức onboarding runner | ⚠️ Mỗi người bấm Connect 1 lần | ✅ Không cần | ⚠️ Vẫn phải /register | Mỗi người join challenge |
| Chi phí | Subscription dev + máy chủ nhỏ | Máy chủ | 0 | Subscription cho người tạo |
| Kiểm soát của bạn | ✅ Hoàn toàn | ✅ | ❌ | ❌ |

**Quyết định đề xuất: phương án A**, giữ C làm dự phòng trong thời gian chuyển đổi. Không làm B (rủi ro khoá tài khoản Strava của bạn và các runner). D không đáp ứng yêu cầu cấu hình luật.

---

## 4. Kiến trúc đề xuất

```
                 ┌──────────────────────────────────────────────────────────┐
  Runner bấm     │  Máy chủ nhỏ (Docker Compose), HTTPS qua Caddy           │
  "Connect with  │                                                          │
   Strava" ────► │  api        FastAPI: /auth/strava  /auth/callback        │
                 │             /webhook (GET verify, POST event)  /health   │
  Strava ──────► │     │                                                    │
  webhook        │     ▼  hàng đợi nội bộ                                   │
                 │  worker     lấy chi tiết hoạt động (httpx) → rule engine │
                 │             → lưu activities + evaluations               │
                 │     │                                                    │
                 │  bot        aiogram 3: lệnh runner (/me /today /ranking) │
                 │             lệnh admin (/rules /recheck /exclude ...)    │
                 │     │                                                    │
                 │  scheduler  APScheduler: đối soát 3h/lần, refresh token, │
                 │             bài đăng 06:30, backup DB hằng đêm           │
                 │     │                                                    │
                 │  db         SQLite (WAL) → Postgres khi cần              │
                 └──────────────────────────────────────────────────────────┘
                            │                         │
                            ▼                         ▼
                 Telegram group / admin        composer.py (tái dùng)
```

**Công nghệ:** Python 3.12, FastAPI, httpx, aiogram 3, SQLAlchemy 2 + SQLite (chuyển Postgres nếu >1 club), APScheduler, pydantic-settings, Caddy (HTTPS tự động), Docker Compose. Toàn bộ chạy trong **một process** (FastAPI + bot polling + scheduler) để đơn giản; tách process khi cần.

**Nơi chạy (chọn 1):**

| Lựa chọn | Ưu | Nhược |
|---|---|---|
| **VPS nhỏ (1 vCPU, 1 GB) hoặc Oracle Cloud Always Free** — khuyến nghị | Đơn giản, 1 docker-compose, debug dễ, luôn chạy | ~4–6 USD/tháng nếu VPS trả phí; cần domain hoặc DDNS cho HTTPS |
| Serverless miễn phí (Cloudflare Workers / Vercel + Neon/Supabase) | 0 đồng, luôn sẵn | Phải viết lại theo kiểu hàm, Telegram phải dùng webhook mode, cold start có thể vượt 2 giây của Strava |
| GitHub Actions như hiện tại | Quen | **Không nhận được webhook/OAuth**, cron trễ hàng giờ → loại |

---

## 5. Mô hình dữ liệu

Nguyên tắc: **lưu hoạt động thô, chấm điểm tách rời, bộ luật có version.** Đổi luật → chạy `/recheck` chấm lại toàn bộ, không mất dữ liệu.

| Bảng | Trường chính |
|---|---|
| `athletes` | `strava_id`, `display_name`, `telegram_id`, `club_member` (bool), `consent_at`, `active`, `joined_at` |
| `oauth_tokens` | `athlete_id`, `access_token`, `refresh_token`, `expires_at`, `scope` (mã hoá ở nơi lưu) |
| `events` | `id`, `name`, `start_date`, `end_date`, `timezone`, `rules_version_id`, `status` |
| `rule_sets` | `id`, `event_id`, `version`, `yaml`, `created_by`, `created_at` |
| `activities` | `strava_activity_id`, `athlete_id`, `sport_type`, `start_local`, `distance_m`, `moving_s`, `elapsed_s`, `avg_speed`, `has_map`, `manual`, `trainer`, `flagged`, `device_name`, `raw_json`, `fetched_at`, `deleted_at` |
| `evaluations` | `activity_id`, `event_id`, `rules_version`, `valid`, `reasons[]`, `converted_km`, `evaluated_at` |
| `overrides` | `activity_id`, `action` (exclude/include/adjust_km), `by_admin`, `note`, `at` |
| `posts` | `date`, `text`, `source`, `sent_at` |
| `audit_log` | mọi thay đổi luật, override, link/unlink |

Lưu ý tuân thủ: tham số `retention_days` cho `raw_json` (xoá JSON thô sau N ngày, giữ số liệu đã chấm); xoá toàn bộ dữ liệu của athlete khi nhận sự kiện deauthorize hoặc `/unlink`.

---

## 6. Bộ luật cấu hình (phần "Settings" bạn yêu cầu)

Bộ luật là **một file YAML cho mỗi giải**, lưu trong DB có version. Ví dụ, lấy theo luật đang áp dụng ở giải vừa rồi (từ các "mẹo" trong `composer.py`):

```yaml
event:
  name: "Giải chạy mừng 9 năm VTS"
  start: 2027-09-15
  end: 2027-10-14
  timezone: Asia/Ho_Chi_Minh
  grace_days: 2          # hoạt động đồng bộ trễ sau ngày kết thúc vẫn tính nếu start_date còn trong giải
  target_km: 45

general:
  require_gps_map: true  # loại hoạt động không có bản đồ (treadmill, nhập tay, mất GPS)
  reject_manual: true
  reject_flagged: true
  trainer_policy: reject # reject | allow | factor (nhân hệ số riêng)
  pace_basis: moving     # moving | elapsed
  overlap_policy: reject_later   # 2 hoạt động cùng người trùng thời gian: giữ cái tải trước
  daily_cap_km: null     # giới hạn km quy đổi/ngày/người; null = không giới hạn
  retention_days_raw: 7

sports:
  - types: [Run, TrailRun, Walk, Hike]
    factor: 1.0
    min_distance_km: 1.0
    pace_min: "4:00"     # phút/km, nhanh hơn coi là bất thường
    pace_max: "17:00"
  - types: [Ride, GravelRide, MountainBikeRide]
    factor: 0.25         # 4 km đạp = 1 km chạy
    min_distance_km: 4.0
    pace_min: "1:30"
    pace_max: "10:00"
  - types: [Swim]
    factor: 4.0          # 100 m bơi = 400 m chạy
    min_distance_km: 0.1
    pace_unit: per_100m
    pace_min: "1:15"
    pace_max: "4:00"

excluded_types: [VirtualRide, VirtualRun, EBikeRide, Elliptical, StairStepper, WeightTraining, Yoga]

awards:                  # chỉ để báo cáo, không ảnh hưởng chấm luật
  streak_days: 25
  female_target_km: 45
```

**Cách bạn chỉnh luật (không cần sửa code, không cần deploy lại):**

| Cách | Dùng khi |
|---|---|
| `/rules` | Xem luật hiện hành, version, ngày đổi |
| `/rules set sports.run.pace_max 18:00` + nút bấm xác nhận | Sửa nhanh một giá trị |
| `/rules import` rồi gửi file YAML; `/rules export` | Thiết lập giải mới hoặc sửa nhiều chỗ |
| `/event new "Tên giải" 2027-09-15 2027-10-14` | Mở giải mới, kế thừa luật giải trước |
| `/recheck [event]` | Chấm lại toàn bộ hoạt động theo luật mới; bot báo số hoạt động đổi trạng thái |
| `/exclude <activity_id> "lý do"`, `/include`, `/adjust <id> <km>` | Can thiệp tay từng hoạt động, có ghi log |

Mỗi hoạt động không hợp lệ lưu **lý do rõ ràng** để trả lời runner, ví dụ: `NO_GPS_MAP`, `MANUAL_ENTRY`, `PACE_TOO_FAST (3:41/km < 4:00)`, `BELOW_MIN_DISTANCE`, `TYPE_EXCLUDED (VirtualRide)`, `OUTSIDE_EVENT_WINDOW`, `OVERLAP`, `FLAGGED_BY_STRAVA`, `TRAINER`.

---

## 7. Luồng nghiệp vụ

**7.1 Onboarding runner (1 lần)**
`/start` trong Telegram → bot gửi link *Connect with Strava* có `state` gắn với Telegram ID và dòng đồng ý hiển thị tên + km trong nhóm → runner chấp thuận trên Strava → `/auth/callback` lưu token, đọc tên Strava → bot xác nhận và **backfill** hoạt động từ ngày bắt đầu giải (`GET /athlete/activities?after=`). Tuỳ chọn: kiểm tra runner có trong club qua `GET /athlete/clubs`.

**7.2 Realtime**
Webhook `create` → trả 200 ngay → worker gọi `GET /activities/{id}` → rule engine → lưu → (tuỳ chọn) nhắn nhóm: "✅ Thái Nguyễn Văn vừa hoàn thành 6,2 km, pace 6:05" hoặc nhắn riêng: "❌ Hoạt động 'Morning Run' không được tính: không có bản đồ GPS". Webhook `update` (đổi loại/tiêu đề/privacy) → lấy lại và chấm lại. Webhook `delete` → đánh dấu `deleted_at`, trừ khỏi bảng.

**7.3 Đối soát (chống lỡ webhook)**
Mỗi 3 giờ: với từng athlete, `GET /athlete/activities?after=<lần đối soát trước - 2 ngày>`; thêm cái thiếu, phát hiện cái bị xoá. Refresh token trước khi hết hạn (token sống 6 giờ).

**7.4 Bài đăng hằng ngày**
06:30 giờ VN (APScheduler trên máy chủ, không còn phụ thuộc cron GitHub): tổng hợp từ DB → `composer.build_context` + `compose` + `rewrite_with_llm` như hiện nay → gửi admin duyệt, hoặc gửi thẳng nhóm nếu bật `AUTO_POST`.

**7.5 Lệnh Telegram**

| Nhóm | Lệnh |
|---|---|
| Runner | `/me` (tổng, số ngày, hoạt động gần nhất và lý do nếu bị loại), `/today`, `/ranking`, `/log [n]`, `/rules`, `/unlink` |
| Admin | `/status` (webhook còn sống, athlete đã kết nối/capacity, rate limit), `/recheck`, `/exclude /include /adjust`, `/event`, `/rules ...`, `/export csv` (cho Ban tổ chức), `/broadcast`, `/post now` |

---

## 8. Rủi ro và cách giảm thiểu

| Rủi ro | Mức | Giảm thiểu |
|---|---|---|
| **Capacity 10 athlete; review Strava chậm** | Cao | Tạo app và tự nâng lên 10 ngay Giai đoạn 0. Nộp review ngay khi MVP chạy được (cần ảnh chụp nút *Connect with Strava*, màn hình hiển thị dữ liệu, logo *Powered by Strava*). Trong lúc chờ: chạy song song @tdht_bot. **Không** lách bằng nhiều app (vi phạm, mỗi app vẫn cần subscription). |
| **Chính sách "chỉ hiển thị cho chính user"** và giới hạn cache | Cao | Mỗi runner tích đồng ý rõ ràng khi Connect; nhóm Telegram kín; chỉ hiển thị tên + km quy đổi, không link hoạt động; `retention_days_raw`; xoá dữ liệu khi deauthorize; bạn chấp nhận khả năng app bị thu hồi và có fallback C. Có thể hỏi Strava developer support bằng mô tả use-case trước khi nộp review. |
| Subscription dev hết hạn → app ngừng | Trung | Nhắc gia hạn trong `/status`; ghi vào lịch |
| Lỡ webhook, máy chủ chết | Trung | Đối soát 3 giờ/lần; healthcheck + cảnh báo Telegram; `restart: always`; backup SQLite hằng đêm vào repo private hoặc object storage |
| Upload trễ / sửa / xoá hoạt động sau khi đã đăng bài | Trung | Xử lý `update`/`delete`; `grace_days`; bài đăng ghi "số liệu chốt lúc HH:MM" |
| Gian lận (nhập tay, treadmill, đạp xe gắn nhãn Run) | Trung | Luật `require_gps_map`, `reject_manual`, pace min; cờ `device_name`; admin override có log |
| Đổi base URL/header 2027 | Thấp | Base URL trong cấu hình; đã dùng header `Authorization` |
| Rate limit | Thấp | Webhook + đối soát theo `after`; cache 15 phút cho `/ranking` |
| Bảo mật token, tên đồng nghiệp | Trung | Repo private, secrets ngoài repo, mã hoá cột token, chỉ admin có quyền lệnh quản trị |

---

## 9. Lộ trình

| Giai đoạn | Nội dung | Ước lượng | Kết quả |
|---|---|---|---|
| **GĐ0 — Chuẩn bị** | Chốt 7 quyết định mục 10. Tạo Strava API app, tự nâng lên 10 athlete, kiểm tra subscription. Thuê/đăng ký máy chủ + domain (hoặc Cloudflare Tunnel). Tạo bot Telegram mới hoặc tái dùng bot nhận bài. | 1–2 ngày | Có đủ khoá, máy chủ trả lời HTTPS |
| **GĐ1 — MVP lõi** | OAuth + lưu token; webhook verify + nhận sự kiện; client Strava có refresh token; schema DB; rule engine + bộ test theo YAML; `/start` `/me` `/ranking` `/today`; backfill. Thử với 2–3 người. **Nộp review tăng capacity** ngay khi có ảnh chụp. | 4–6 ngày | Bot chấm được hoạt động thật realtime |
| **GĐ2 — Cấu hình và vận hành** | `/rules` (xem/set/import/export), `/event`, `/recheck`, override, thông báo lý do loại, đối soát định kỳ, `/status`, backup, xử lý deauthorize | 3–5 ngày | Bạn tự đổi luật không cần code |
| **GĐ3 — Báo cáo** | Port `composer.py`, bài đăng 06:30, `/export csv`, tài liệu hướng dẫn runner (1 trang, có ảnh), tài liệu vận hành | 2–3 ngày | Thay thế hoàn toàn luồng cũ |
| **GĐ4 — Chuyển đổi** | Mời toàn bộ runner Connect; chạy song song với @tdht_bot 1–2 tuần, đối chiếu lệch; sau đó tắt userbot, chuyển code cũ vào `legacy/` | 1–2 tuần lịch | Độc lập hoàn toàn |
| Sau này | Dashboard web nhỏ cho Ban tổ chức, nhiều club/giải cùng lúc, xuất Google Sheet, hệ số theo giới/độ tuổi | — | — |

Ước lượng tính theo ngày công, chưa gồm thời gian chờ Strava review.

---

## 10. Việc cần bạn quyết định trước khi code

1. **Máy chủ:** VPS trả phí (~4–6 USD/tháng), Oracle Cloud Always Free, hay serverless? *(Đề xuất: VPS/Oracle Free + Docker.)*
2. **Chi phí Strava subscription** cho tài khoản dev (~79,99 USD/năm): chấp nhận? Nếu không, bot chỉ chạy được ở chế độ 1 athlete (vô nghĩa với club).
3. **Quyền xin runner:** `activity:read` (bỏ qua hoạt động để chế độ "Only You") hay `activity:read_all`? *(Đề xuất: `activity:read`, ít nhạy cảm, dễ được đồng ý.)*
4. **Thông báo realtime từng hoạt động vào nhóm** hay chỉ bài tổng hợp mỗi sáng? *(Đề xuất: nhắn riêng runner khi bị loại; nhóm chỉ nhận bài sáng, có thể bật/tắt.)*
5. **Chấp nhận rủi ro tuân thủ** ở mục 8 (bảng xếp hạng chéo user) với các biện pháp giảm thiểu?
6. **Luật mặc định cho giải tới:** các con số trong YAML mục 6 có đúng với quy chế Ban tổ chức không? Hoạt động `Walk`/`Hike` tính hệ số 1.0? `VirtualRide` loại hẳn hay 0.25?
7. **Giữ @tdht_bot làm dự phòng** trong GĐ4 hay cắt ngay?

Trả lời xong 7 điểm này, bước tiếp theo là tôi dựng khung GĐ1 (cấu trúc thư mục bên dưới, Dockerfile, rule engine kèm test) trên nhánh này.

---

## 11. Cấu trúc repo đề xuất

```
vts-daily-post/
├── app/
│   ├── main.py            # FastAPI: /auth/strava /auth/callback /webhook /health; khởi động bot + scheduler
│   ├── config.py          # pydantic-settings, đọc .env
│   ├── db/                # models.py, session.py, migrations (alembic)
│   ├── strava/            # client.py (httpx + refresh), oauth.py, webhook.py, types.py
│   ├── rules/             # schema.py (pydantic), engine.py (evaluate), defaults.yaml
│   ├── bot/               # aiogram: handlers/member.py, handlers/admin.py, keyboards.py
│   ├── jobs/              # reconcile.py, daily_post.py, backup.py, token_refresh.py
│   └── composer.py        # tái dùng từ bản hiện tại
├── tests/                 # test rule engine (bảng trường hợp YAML), test webhook, test backfill
├── deploy/                # docker-compose.yml, Caddyfile, .env.example
├── docs/                  # kế hoạch này, hướng dẫn runner, runbook vận hành
├── legacy/                # main.py cũ + workflow cũ, giữ làm fallback trong GĐ4
└── data/                  # giữ lịch sử snapshot/posts cũ
```

---

## Nguồn tham khảo (truy cập 05/11/2026)

- Strava, *An Update To Our Developer Program* (01/06/2026): https://communityhub.strava.com/insider-journal-9/an-update-to-our-developer-program-13428
- Strava API Changelog (gỡ Club Activities/Members/Admins 01/09/2026; base URL 2027): https://developers.strava.com/docs/changelog/
- Strava API Reference (Clubs chỉ còn `GET /clubs/{id}`, `GET /athlete/clubs`; trường của Activity): https://developers.strava.com/docs/reference/
- Strava Rate Limits (200/15 phút, 2.000/ngày; athlete capacity): https://developers.strava.com/docs/rate-limits/
- Strava Webhooks: https://developers.strava.com/docs/webhooks/
- Strava Getting Started (tự nâng lên 10 athlete): https://developers.strava.com/docs/getting-started/
- Strava API FAQ (tiers, subscription): https://communityhub.strava.com/developers-knowledge-base-14/strava-api-faq-12906
- Strava API Agreement (hiệu lực 01/06/2026, "chỉ hiển thị cho chính user"): https://www.strava.com/legal/api
- Thảo luận cộng đồng về leaderboard club trong Standard Tier và cache 7 ngày (chưa có trả lời chính thức): https://communityhub.strava.com/developers-api-7/standard-tier-showing-members-activities-in-a-private-club-group-retention-beyond-the-7-day-cache-14100
- Tổng hợp thay đổi 2026 (bên thứ ba): https://appsforstrava.com/blog/strava-developer-program-changes-2026 , https://tryterra.co/blog/strava-api-changes-2026

---

## Trạng thái 05/11/2026

Đã implement **GĐ1–GĐ3** trong repo này: OAuth từng runner + webhook, rule engine YAML có version, lệnh runner/admin trên Telegram, đối soát định kỳ, bài đăng sáng, backup, CLI, Docker Compose với Caddy hoặc Cloudflare Tunnel. Cách cài đặt và vận hành xem [README](../README.md), [docs/VAN_HANH.md](VAN_HANH.md), [docs/HUONG_DAN_RUNNER.md](HUONG_DAN_RUNNER.md). Code cũ nằm ở `legacy/`. GĐ4 (mời toàn bộ runner, chạy song song rồi tắt luồng cũ) chưa thực hiện.
