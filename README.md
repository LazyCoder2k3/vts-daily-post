# Bot thống kê chạy bộ VTS Running Club (Strava → Telegram)

Bot Telegram tự vận hành cho giải chạy nội bộ: mỗi runner kết nối Strava một lần, bot nhận webhook khi có hoạt động mới, chấm hợp lệ theo bộ luật YAML do Ban tổ chức cấu hình ngay trên Telegram, lưu vào SQLite, trả lời `/me` `/today` `/ranking` và soạn bài tổng kết mỗi sáng.

Kế hoạch gốc và lý do thiết kế: [`docs/KE_HOACH_BOT_STRAVA.md`](docs/KE_HOACH_BOT_STRAVA.md). Runbook: [`docs/VAN_HANH.md`](docs/VAN_HANH.md). Hướng dẫn runner 1 trang: [`docs/HUONG_DAN_RUNNER.md`](docs/HUONG_DAN_RUNNER.md).

## Mục lục

1. [Bot làm gì](#1-bot-làm-gì)
2. [Vì sao không cào Club được nữa và các ràng buộc](#2-vì-sao-không-cào-club-được-nữa-và-các-ràng-buộc)
3. [Chuẩn bị](#3-chuẩn-bị)
4. [Cài đặt từng bước với Docker](#4-cài-đặt-từng-bước-với-docker)
5. [Chạy local không Docker](#5-chạy-local-không-docker)
6. [Lệnh Telegram](#6-lệnh-telegram)
7. [Tham chiếu file luật YAML](#7-tham-chiếu-file-luật-yaml)
8. [Luồng dữ liệu và lịch chạy](#8-luồng-dữ-liệu-và-lịch-chạy)
9. [Xử lý sự cố](#9-xử-lý-sự-cố)
10. [Bot cũ trong legacy](#10-bot-cũ-trong-legacy)
11. [Kiểm thử](#11-kiểm-thử)

---

## 1. Bot làm gì

- Runner gõ `/connect`, bấm **Connect with Strava**, chấp thuận quyền `activity:read`. Xong một lần.
- Strava gửi webhook mỗi khi có hoạt động mới / sửa / xoá. Bot lấy chi tiết, chấm theo luật (GPS, nhập tay, pace, km tối thiểu, loại hoạt động, trùng giờ...), lưu kết quả và lý do.
- Runner xem `/me`, `/today`, `/ranking`, `/log`. Bị loại thì nhận tin nhắn riêng kèm lý do.
- Admin đổi luật bằng `/rules set ...` hoặc gửi file YAML, chấm lại bằng `/recheck`, can thiệp từng hoạt động bằng `/exclude` `/include` `/adjust`.
- 06:30 mỗi sáng bot soạn bài tổng kết (tái dùng `composer.py`), gửi admin duyệt hoặc đăng thẳng vào nhóm.

```
 Runner ──/connect──► Bot Telegram ──link──► Strava OAuth ──callback──► lưu token (mã hoá)
                                                                              │
 Strava ──webhook (chỉ có ID)──► FastAPI /webhook ──► lấy chi tiết ──► rule engine ──► SQLite
                                                                              │
                 /me /today /ranking  ◄── aiogram ◄──────────────────────────┤
                 bài 06:30 / backup 02:15 / đối soát 3h  ◄── APScheduler ◄───┘
```

Một process duy nhất (`uvicorn app.main:app`, cổng 8080) chạy FastAPI (`/health`, `/auth/strava`, `/connect/{token}`, `/auth/callback`, `/webhook`), bot aiogram 3 (long polling) và APScheduler. Dữ liệu: SQLite `data/vts.db`.

## 2. Vì sao không cào Club được nữa và các ràng buộc

Từ **01/09/2026** Strava gỡ ba endpoint *Club Activities*, *Club Members*, *Club Administrators*. API chỉ còn trả thông tin chung của club. Cách duy nhất còn lại để đọc hoạt động là **từng runner cấp quyền** cho app qua OAuth. Bot này đi theo hướng đó.

| Ràng buộc của Strava | Ảnh hưởng | Bot xử lý thế nào |
|---|---|---|
| App mới chỉ được **10 athlete** (tự nâng từ 1 lên 10 trong API Settings) | Quá 10 người thì runner thứ 11 không kết nối được | Nộp **review** để lên 9.999 athlete ngay khi bot chạy được (xem `docs/VAN_HANH.md`). `/status` hiện số athlete đã kết nối |
| Tài khoản dev cần **Strava subscription** | Hết hạn thì API ngừng | Ghi ngày gia hạn vào lịch. Giá khoảng 11,99 USD/tháng hoặc 79,99 USD/năm (giá Mỹ, kiểm tra lại) |
| Thoả thuận API: **dữ liệu của user chỉ hiển thị cho chính user đó**; giới hạn cache (cộng đồng đọc là 7 ngày) | Bảng xếp hạng nội bộ là vùng xám | Runner **đồng ý rõ ràng** khi `/connect` (hiển thị tên + km trong nhóm); nhóm Telegram **kín**; chỉ hiện tên và km quy đổi, không link hoạt động; xoá JSON thô sau `RAW_RETENTION_DAYS`; xoá dữ liệu khi runner `/unlink` hoặc rút quyền trên Strava |
| Webhook chỉ gửi ID, phải trả 200 trong 2 giây | Dễ lỡ sự kiện | Xử lý bất đồng bộ; đối soát mỗi `RECONCILE_INTERVAL_HOURS` giờ |
| Cần máy chủ HTTPS chạy liên tục | GitHub Actions không nhận webhook được | VPS nhỏ + Caddy, hoặc Cloudflare Tunnel |
| Năm 2027: base URL đổi sang `api-v3.strava.com`, bỏ `oauth/deauthorize` | Phải sửa cấu hình | `STRAVA_API_BASE`, `STRAVA_OAUTH_BASE` trong `.env` |

Bạn chấp nhận rủi ro app có thể bị Strava thu hồi nếu họ diễn giải chặt chính sách hiển thị.

## 3. Chuẩn bị

**a) Strava API app** — https://www.strava.com/settings/api

1. Tạo app. *Authorization Callback Domain* = tên miền của bạn, ví dụ `run.example.com` (không có `https://`, không có đường dẫn). Chạy local thì dùng `localhost`.
2. Ghi lại **Client ID**, **Client Secret**.
3. Nâng athlete capacity lên 10 (nút tự nâng trong trang API Settings).
4. Kiểm tra tài khoản dev đã có subscription.

**b) Telegram bot** — nhắn @BotFather

1. `/newbot` → lấy token.
2. Nếu muốn gõ lệnh ngay trong nhóm: `/setprivacy` → chọn bot → **Disable** (tắt Group Privacy).
3. Mở bot, gõ `/id` để lấy Telegram ID của bạn (điền vào `ADMIN_TELEGRAM_IDS`). Thêm bot vào nhóm kín rồi gõ `/id` trong nhóm để lấy `GROUP_CHAT_ID`.

**c) Máy chủ**

- VPS nhỏ (1 vCPU, 1 GB RAM đủ) có Docker và Docker Compose, kèm **một tên miền** trỏ A record về máy chủ, cổng 80/443 mở. Hoặc
- Máy bất kỳ (kể cả sau NAT) + **Cloudflare Tunnel**: không cần mở cổng, không cần IP công khai.

## 4. Cài đặt từng bước với Docker

```bash
git clone <repo-của-bạn> vts-daily-post && cd vts-daily-post
cp .env.example .env
```

Điền `.env`: `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_WEBHOOK_VERIFY_TOKEN` (chuỗi tuỳ ý), `PUBLIC_BASE_URL=https://run.example.com`, `DOMAIN=run.example.com`, `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_IDS`, `GROUP_CHAT_ID`. Tạo khoá mã hoá token:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # hoặc dùng docker run --rm vts-bot:latest python -m app.cli gen-key sau khi build
.venv/bin/python -m app.cli gen-key        # dán kết quả vào TOKEN_ENCRYPTION_KEY
```

Chuẩn bị thư mục dữ liệu (container chạy user uid 10001):

```bash
mkdir -p data/backups && sudo chown -R 10001:10001 data
```

Khởi chạy. Compose chỉ tự đọc `.env` nằm cạnh file compose (`deploy/`), nên luôn kèm `--env-file .env`; đặt alias cho gọn:

```bash
alias dc='docker compose --env-file .env -f deploy/docker-compose.yml'
dc up -d --build
dc logs -f app          # chờ dòng báo bot và scheduler đã chạy
curl -fsS https://run.example.com/health
```

Dùng Cloudflare Tunnel thay Caddy: tạo tunnel trong Cloudflare Zero Trust, Public Hostname `run.example.com` → Service `http://app:8080`, điền `CF_TUNNEL_TOKEN` vào `.env`, rồi:

```bash
alias dc='docker compose --env-file .env -f deploy/docker-compose.cloudflared.yml'
dc up -d --build
```

Sau khi `/health` trả 200 qua HTTPS:

```bash
dc exec app python -m app.cli webhook subscribe                     # đăng ký webhook với Strava (1 subscription/app)
dc exec app python -m app.cli webhook list                          # kiểm tra
dc exec app python -m app.cli event new "Giải chạy VTS 2027" 2027-01-01 2027-01-31
dc exec app python -m app.cli rules show                            # xem luật kế thừa từ app/rules/defaults.yaml
```

Chỉnh luật bằng Telegram (`/rules set sports.run.pace_max 18:00`) hoặc CLI (`rules set`). Cuối cùng mời runner mở bot và gõ `/connect`. Tự kết nối tài khoản Strava của bạn trước để kiểm tra `/me`.

Lệnh thường dùng sau đó:

| Việc | Lệnh |
|---|---|
| Xem log | `dc logs -f app` / `dc logs caddy` |
| Nâng cấp code | `git pull && dc up -d --build` |
| Chạy CLI | `dc exec app python -m app.cli <lệnh>` |
| Dừng | `dc down` (dữ liệu vẫn ở `data/`) |

## 5. Chạy local không Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # điền như trên; PUBLIC_BASE_URL là URL tunnel bên dưới
python -m app.cli init-db
uvicorn app.main:app --reload --port 8080
```

Strava phải gọi được vào máy bạn qua HTTPS. Mở thêm một terminal:

```bash
cloudflared tunnel --url http://localhost:8080     # in ra https://xxx.trycloudflare.com
# hoặc: ngrok http 8080
```

Điền URL đó vào `PUBLIC_BASE_URL`, đặt *Authorization Callback Domain* của app Strava đúng host đó, khởi động lại uvicorn, rồi `python -m app.cli webhook subscribe`. URL tunnel miễn phí đổi mỗi lần chạy, nên mỗi lần phải sửa lại hai chỗ này và subscribe lại (`webhook delete <id>` trước).

## 6. Lệnh Telegram

**Runner**

| Lệnh | Ý nghĩa |
|---|---|
| `/start` | Giới thiệu bot và điều khoản hiển thị dữ liệu |
| `/connect` | Nhận link **Connect with Strava** (hết hạn sau 30 phút) |
| `/me` | Tổng km quy đổi, số ngày có hoạt động, hoạt động gần nhất và lý do nếu bị loại |
| `/today [dd/mm]` | Thống kê ngày hôm nay hoặc ngày chỉ định |
| `/ranking [n]` | Bảng xếp hạng giải đang mở, top n |
| `/log [n]` | n hoạt động gần nhất của bạn kèm trạng thái và lý do |
| `/rules` | Tóm tắt luật đang áp dụng |
| `/unlink` | Thu hồi quyền, xoá token. Dữ liệu hoạt động của bạn bị xoá |
| `/id` | In Telegram ID của bạn (trong nhóm: ID nhóm) |
| `/help` | Danh sách lệnh |

**Admin** (Telegram ID nằm trong `ADMIN_TELEGRAM_IDS`)

| Lệnh | Ý nghĩa |
|---|---|
| `/admin` | Danh sách lệnh admin |
| `/status` | Webhook còn sống, số athlete đã kết nối, lần đối soát cuối, rate limit, giải đang mở |
| `/event` | Liệt kê giải |
| `/event new "Tên" 2027-01-01 2027-01-31 [draft]` | Tạo giải mới, kế thừa luật giải trước; `draft` = chưa kích hoạt |
| `/event activate <id>` / `/event close <id>` | Kích hoạt / đóng giải |
| `/rules` | Xem YAML luật hiện hành và version |
| `/rules set <đường.dẫn> <giá trị>` | Sửa một giá trị. Ví dụ: `sports.run.pace_max 18:00`, `general.require_gps_map false`, `event.grace_days 3`, `excluded_types VirtualRide,Yoga` |
| `/rules import` rồi gửi file `.yaml` | Thay toàn bộ luật |
| `/rules export` | Nhận file YAML hiện hành |
| `/rules reset` | Về luật mặc định `app/rules/defaults.yaml` |
| `/recheck` | Chấm lại toàn bộ hoạt động theo luật hiện hành, báo số hoạt động đổi trạng thái |
| `/exclude <activity_id> [lý do]` | Loại một hoạt động (ghi log) |
| `/include <activity_id> [lý do]` | Tính lại một hoạt động đã bị loại |
| `/adjust <activity_id> <km> [lý do]` | Đặt km quy đổi bằng tay |
| `/activity <id>` | Chi tiết một hoạt động và kết quả chấm |
| `/athletes` | Danh sách runner đã kết nối |
| `/backfill <strava_id\|all>` | Lấy lại hoạt động từ ngày bắt đầu giải |
| `/export` | File CSV cho Ban tổ chức |
| `/post [dry]` | Soạn bài sáng ngay; `dry` = chỉ gửi admin xem |
| `/settings` | Xem cờ thông báo |
| `/settings set <notify_rejected_dm\|notify_valid_dm\|notify_valid_group\|auto_post_group> <0\|1>` | Đổi cờ thông báo không cần khởi động lại |
| `/broadcast <text>` | Nhắn riêng tới mọi runner đã kết nối |
| `/reconcile` | Đối soát với Strava ngay |
| `/backup` | Sao lưu DB ngay vào `BACKUP_DIR` |
| `/webhook list\|subscribe\|delete <id>` | Quản lý subscription webhook |
| `/unlink_athlete <strava_id> [delete]` | Gỡ một runner; `delete` = xoá cả hoạt động |

**CLI** (`python -m app.cli ...`, trong Docker: `dc exec app python -m app.cli ...`)

```
init-db | gen-key
webhook list | webhook subscribe | webhook delete ID
event new NAME START END [--draft] | event list | event activate ID | event close ID
rules show | rules export [FILE] | rules import FILE | rules set PATH VALUE
recheck | reconcile | post [--dry-run] [--date YYYY-MM-DD] | backup
athletes | backfill <strava_id|all>
```

## 7. Tham chiếu file luật YAML

Luật là một file YAML cho mỗi giải, lưu trong DB có version; đổi luật rồi `/recheck` để chấm lại, không mất dữ liệu. Mặc định: [`app/rules/defaults.yaml`](app/rules/defaults.yaml). Lược đồ: [`app/rules/schema.py`](app/rules/schema.py).

**`event`**

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `name` | chuỗi | Tên giải |
| `start`, `end` | ngày `YYYY-MM-DD` | Thời gian giải (theo ngày địa phương của hoạt động). `end` ≥ `start` |
| `timezone` | chuỗi | Múi giờ, mặc định `Asia/Ho_Chi_Minh` |
| `grace_days` | số nguyên | Sau ngày kết thúc còn nhận hoạt động đồng bộ trễ bao nhiêu ngày (hoạt động vẫn phải diễn ra trong giải) |
| `target_km` | số hoặc `null` | Mục tiêu km để báo cáo |

**`general`** — áp dụng cho mọi môn, trừ khi môn ghi đè

| Trường | Giá trị | Ý nghĩa |
|---|---|---|
| `require_gps_map` | `true`/`false` | Loại hoạt động không có bản đồ GPS (treadmill, nhập tay, mất GPS) |
| `reject_manual` | `true`/`false` | Loại hoạt động nhập tay |
| `reject_flagged` | `true`/`false` | Loại hoạt động bị Strava gắn cờ |
| `trainer_policy` | `reject` \| `allow` | Hoạt động trên máy tập |
| `pace_basis` | `moving` \| `elapsed` | Tính pace theo thời gian di chuyển hay tổng thời gian |
| `overlap_policy` | `reject_later` \| `ignore` | Hai hoạt động cùng người trùng giờ: loại cái tải sau, hoặc bỏ qua kiểm tra |
| `daily_cap_km` | số hoặc `null` | Tổng km quy đổi tối đa mỗi người mỗi ngày; phần vượt bị cắt |
| `min_moving_ratio` | 0–1 hoặc `null` | `moving_time / elapsed_time` tối thiểu; thấp hơn là dừng quá nhiều |
| `allowed_visibility` | danh sách hoặc `null` | Chỉ tính hoạt động có visibility trong danh sách, ví dụ `[everyone, followers_only]` |

**`sports[]`** — mỗi mục là một nhóm môn

| Trường | Ý nghĩa |
|---|---|
| `key` | Tên ngắn duy nhất (`run`, `ride`, `swim`) dùng trong `/rules set sports.<key>.<trường>` |
| `types` | Danh sách `sport_type` của Strava: `Run`, `TrailRun`, `Walk`, `Hike`, `Ride`, `GravelRide`, `MountainBikeRide`, `Swim`... |
| `factor` | km quy đổi = km thực × factor (đạp `0.25`, bơi `4.0`) |
| `min_distance_km`, `max_distance_km` | Quãng đường tối thiểu / tối đa (km); `max` có thể `null` |
| `pace_unit` | `per_km` (phút:giây mỗi km) hoặc `per_100m` (phút:giây mỗi 100 m, dùng cho bơi) |
| `pace_min` | Pace **nhanh nhất** cho phép, ví dụ `"4:00"`; nhanh hơn là bất thường |
| `pace_max` | Pace **chậm nhất** cho phép, ví dụ `"17:00"` |
| `require_gps_map`, `trainer_policy` | Ghi đè `general` cho riêng môn này; bỏ trống = dùng `general` |

Pace viết dạng `m:ss` (hoặc `h:mm:ss`), luôn trong dấu ngoặc kép trong YAML.

**`excluded_types`**: danh sách `sport_type` loại hẳn (`VirtualRide`, `VirtualRun`, `EBikeRide`, `Elliptical`, `StairStepper`, `WeightTraining`, `Yoga`, `Workout`). Loại không nằm trong `sports[]` lẫn `excluded_types` bị loại với mã `TYPE_UNKNOWN`.

**`awards`**: cặp khoá-giá trị tự do chỉ để báo cáo (`streak_days`, `female_target_km`), không ảnh hưởng chấm luật.

**Cách `/rules set` ép kiểu giá trị**: `true/false/yes/no/on/off` → bool; số → số; `a,b,c` → danh sách; `null` → bỏ trống. Đường dẫn: `event.grace_days`, `general.pace_basis`, `sports.run.pace_max`, `excluded_types`.

**Mã lý do loại** (hiện trong `/me`, `/log`, tin nhắn riêng, CSV; có thể kèm chi tiết sau dấu `:`, ví dụ `PACE_TOO_FAST:3:41`)

| Mã | Nghĩa |
|---|---|
| `OUTSIDE_EVENT_WINDOW` | Ngoài thời gian giải |
| `AFTER_GRACE_PERIOD` | Đồng bộ quá hạn sau khi giải kết thúc |
| `TYPE_EXCLUDED` | Loại hoạt động không được tính |
| `TYPE_UNKNOWN` | Loại hoạt động không có trong luật |
| `NO_GPS_MAP` | Không có bản đồ GPS |
| `MANUAL_ENTRY` | Nhập tay |
| `TRAINER` | Máy tập / treadmill |
| `FLAGGED_BY_STRAVA` | Bị Strava gắn cờ |
| `BELOW_MIN_DISTANCE` | Chưa đủ quãng đường tối thiểu |
| `ABOVE_MAX_DISTANCE` | Vượt quãng đường tối đa |
| `PACE_TOO_FAST` | Pace nhanh bất thường |
| `PACE_TOO_SLOW` | Pace quá chậm |
| `LOW_MOVING_RATIO` | Thời gian dừng quá nhiều |
| `OVERLAP` | Trùng thời gian với hoạt động khác |
| `DAILY_CAP` | Vượt giới hạn km trong ngày |
| `DAILY_CAP_TRIMMED` | Đã cắt theo giới hạn km trong ngày (vẫn hợp lệ phần còn lại) |
| `VISIBILITY` | Chế độ hiển thị không được tính |
| `ADMIN_EXCLUDED` | Ban tổ chức loại |
| `DELETED` | Đã xoá trên Strava |

## 8. Luồng dữ liệu và lịch chạy

| Khi nào | Việc |
|---|---|
| Runner `/connect` xong | Lưu token (mã hoá Fernet), đọc tên Strava, kiểm tra club nếu có `STRAVA_CLUB_ID`, **backfill** từ ngày bắt đầu giải |
| Webhook `create` | Trả 200 ngay → lấy `GET /activities/{id}` → chấm → lưu → nhắn riêng runner nếu bị loại (`NOTIFY_REJECTED_DM`), nhắn riêng/nhóm nếu hợp lệ (`NOTIFY_VALID_DM`, `NOTIFY_VALID_GROUP`) |
| Webhook `update` / `delete` | Lấy lại và chấm lại / đánh dấu đã xoá, trừ khỏi bảng |
| Webhook `athlete` deauthorize | Xoá token, tắt runner, xoá dữ liệu hoạt động |
| Mỗi `RECONCILE_INTERVAL_HOURS` giờ (mặc định 3) | Với từng runner, `GET /athlete/activities?after=` lùi `RECONCILE_LOOKBACK_DAYS` ngày (mặc định 2): thêm cái thiếu, phát hiện cái đã xoá. Refresh token sắp hết hạn (token Strava sống 6 giờ) |
| `DAILY_POST_TIME` (mặc định 06:30) | Tổng hợp hôm qua → `composer` (tuỳ chọn viết lại bằng LLM) → gửi admin, hoặc thẳng nhóm nếu `AUTO_POST_GROUP=true` |
| 02:15 | Backup SQLite vào `BACKUP_DIR`, giữ `BACKUP_KEEP` bản |
| 03:00 | Xoá `raw_json` của hoạt động cũ hơn `RAW_RETENTION_DAYS` ngày (số liệu đã chấm vẫn giữ) |

Giờ tính theo `TIMEZONE`. Mọi thay đổi luật, override, link/unlink ghi vào `audit_log`.

## 9. Xử lý sự cố

| Triệu chứng | Kiểm tra / cách xử lý |
|---|---|
| Hoạt động mới không về | `/webhook list` (hoặc `python -m app.cli webhook list`) phải có 1 subscription với `callback_url` = `PUBLIC_BASE_URL/webhook`. `curl -fsS https://DOMAIN/health` từ ngoài. `dc logs caddy`: chứng chỉ phải xin được (DNS đúng, cổng 80/443 mở). Nếu URL đổi: `webhook delete <id>` rồi `subscribe`. Chạy `/reconcile` để lấy bù |
| `webhook subscribe` báo lỗi verify | `STRAVA_WEBHOOK_VERIFY_TOKEN` trong `.env` khớp với tiến trình đang chạy (khởi động lại sau khi đổi); máy chủ phải trả lời trong 2 giây |
| Runner bấm link báo **"Liên kết hết hạn"** | Link `/connect` sống 30 phút và dùng một lần. Gõ `/connect` lấy link mới |
| Runner thứ 11 không kết nối được | Hết capacity 10 athlete. Nộp review Strava (xem `docs/VAN_HANH.md`). Tạm thời `/unlink_athlete` người không còn tham gia |
| `/status` báo token hỏng, log 401 | Runner đã rút quyền trong Strava → Settings → My Apps, hoặc token lỗi. Runner gõ `/connect` lại |
| Lỗi `TOKEN_ENCRYPTION_KEY không khớp với dữ liệu trong DB` | Khoá trong `.env` khác khoá đã dùng để mã hoá. Khôi phục khoá cũ; nếu mất khoá, mọi runner phải `/connect` lại |
| `unable to open database file` | Quyền thư mục: `sudo chown -R 10001:10001 data` |
| Mất / hỏng `data/vts.db` | `dc stop app`; copy bản mới nhất trong `data/backups/` về `data/vts.db`; xoá `data/vts.db-wal`, `data/vts.db-shm` nếu có; `dc start app`; `/reconcile` |
| Log 429 rate limit | Bot tự lùi. Tránh `/backfill all` nhiều lần trong 15 phút |
| Bài sáng không gửi | `dc logs app` lúc `DAILY_POST_TIME`; thử `/post dry`. Kiểm tra `GROUP_CHAT_ID`, bot còn trong nhóm |
| Bot không trả lời lệnh trong nhóm | Tắt Group Privacy ở @BotFather (`/setprivacy`), hoặc gõ lệnh dạng `/me@TenBot` |

## 10. Bot cũ trong legacy

`legacy/` giữ bot thế hệ trước: userbot Telegram hỏi `/ranking` và `/today` từ @tdht_bot, bóc tách bảng ASCII, lưu snapshot vào `data/snapshots/` và soạn bài qua GitHub Actions (`legacy/main.py`, `legacy/composer.py`, `legacy/test_snapshot.py`, workflow `legacy/daily.yml.disabled`). Dữ liệu lịch sử vẫn nằm ở `data/snapshots/`, `data/posts/`, `data/raw/`.

Chạy lại khi cần dự phòng:

1. `cp legacy/daily.yml.disabled .github/workflows/daily.yml`.
2. Trong file đó sửa `python main.py` → `python legacy/main.py`, và thêm bước `pip install telethon` (đã bỏ khỏi `requirements.txt`).
3. `legacy/main.py` đọc `.env` và ghi `data/` **tương đối với thư mục `legacy/`**. Muốn dùng lại lịch sử ở `data/` gốc: `ln -s ../data legacy/data`, hoặc sửa dòng `DATA = BASE / "data"` thành `DATA = BASE.parent / "data"`.
4. Khai báo lại secrets `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING`, `NOTIFY_BOT_TOKEN`, `NOTIFY_CHAT_ID` trong GitHub Actions.

## 11. Kiểm thử

```bash
python -m pytest -q
```

Test dùng SQLite trong bộ nhớ và không gọi Strava/Telegram thật (`tests/conftest.py`). CI chạy cùng lệnh ở `.github/workflows/ci.yml`. Trong Docker: `dc exec app python -m pytest -q`.
