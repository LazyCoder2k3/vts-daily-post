# Runbook vận hành bot VTS Running Club

Dành cho người quản trị bot. Lệnh `dc` ở đây là alias:

```bash
alias dc='docker compose --env-file .env -f deploy/docker-compose.yml'   # hoặc deploy/docker-compose.cloudflared.yml
```

Cài đặt ban đầu xem [README](../README.md) mục 3–4.

## 1. Trước giải

**T-7 ngày**

- [ ] Strava subscription của tài khoản dev còn hạn qua ngày kết thúc giải + `grace_days`.
- [ ] Athlete capacity đủ cho số runner dự kiến (`/status`). Chưa đủ: nộp review (mục 8), lên kế hoạch dự phòng.
- [ ] `git pull && dc up -d --build`; `curl -fsS https://DOMAIN/health` trả 200.
- [ ] `/webhook list` có đúng 1 subscription, `callback_url` khớp `PUBLIC_BASE_URL/webhook`.
- [ ] Tạo giải: `/event new "Tên giải" YYYY-MM-DD YYYY-MM-DD draft`. Chỉnh luật theo quy chế Ban tổ chức: `/rules`, `/rules set ...` hoặc `/rules import` file YAML. Kiểm tra lại `/rules export`.
- [ ] Gửi `docs/HUONG_DAN_RUNNER.md` vào nhóm; nhắc runner `/connect` sớm.
- [ ] Tự `/connect` tài khoản của bạn, chạy thử một hoạt động thật, xem `/me`, `/log`, `/activity <id>`.
- [ ] `/settings` đúng ý: `notify_rejected_dm 1`, `auto_post_group` theo quyết định Ban tổ chức.
- [ ] `/backup` chạy được; xem file xuất hiện trong `data/backups/`.

**T-1 ngày**

- [ ] `/athletes`: đủ người. Ai chưa kết nối thì `/broadcast` nhắc.
- [ ] `/event activate <id>`. `/status` hiện giải đang mở.
- [ ] `/post dry` để xem bài mẫu không lỗi (nội dung chưa có số liệu là bình thường).
- [ ] Ghi vào lịch cá nhân: ngày kết thúc giải, ngày hết hạn subscription, ngày chứng chỉ HTTPS (Caddy tự gia hạn, chỉ cần theo dõi log).

## 2. Trong giải — mỗi ngày 5 phút

- [ ] Sáng: đọc bài 06:30 bot gửi. Đúng thì chuyển vào nhóm (nếu `auto_post_group=0`).
- [ ] `/status`: webhook còn sống, lần đối soát cuối trong vòng vài giờ, không có token hỏng, rate limit chưa chạm.
- [ ] Xem tin nhắn runner thắc mắc bị loại: `/activity <id>` để xem lý do; sai luật thì `/rules set` rồi `/recheck`; ngoại lệ cá nhân thì `/include` / `/adjust` kèm lý do.
- [ ] Runner mới: `/athletes`; nếu người ta `/connect` sau ngày bắt đầu, bot đã backfill; cần thì `/backfill <strava_id>`.
- [ ] Nghi lỡ webhook (runner nói đã chạy mà `/me` chưa có): `/reconcile`.

Hằng tuần: `ls -la data/backups/` có file mới mỗi ngày; `dc logs --since 24h app | grep -i error`.

## 3. Sau giải

- [ ] Chờ hết `grace_days` để hoạt động đồng bộ trễ vào đủ. `/reconcile` lần cuối.
- [ ] Rà soát: `/recheck`, xem danh sách override còn hợp lý.
- [ ] `/export` lấy CSV cho Ban tổ chức. Lưu kèm `/rules export` (luật áp dụng) để tra cứu sau.
- [ ] `/backup` rồi copy bản backup ra nơi khác (máy cá nhân, kho riêng).
- [ ] `/event close <id>`.
- [ ] Hỏi runner ai muốn `/unlink`; người rời club: `/unlink_athlete <strava_id> delete`.

## 4. Nâng cấp phiên bản

```bash
cd vts-daily-post
dc exec app python -m app.cli backup      # phòng xa
git pull
dc up -d --build
dc logs -f app                            # chờ bot + scheduler lên
curl -fsS https://DOMAIN/health
```

`/status` sau khi lên. Thời gian gián đoạn vài chục giây; webhook Strava thử lại tối đa 3 lần, phần lỡ sẽ được đối soát bù.

## 5. Khôi phục backup

```bash
dc stop app
ls -la data/backups/                      # chọn bản gần nhất còn tốt
cp data/vts.db data/vts.db.hong-$(date +%F)          # giữ bản hỏng để tra cứu
cp data/backups/<file-chọn> data/vts.db
rm -f data/vts.db-wal data/vts.db-shm
sudo chown 10001:10001 data/vts.db
dc start app
```

Sau đó `/status`, `/reconcile` (lấy bù hoạt động phát sinh sau thời điểm backup), `/ranking` để đối chiếu. `TOKEN_ENCRYPTION_KEY` phải là khoá đã dùng khi tạo bản backup.

## 6. Xoay khoá và bí mật

| Bí mật | Cách đổi | Hậu quả |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/revoke` → token mới vào `.env` → `dc up -d` | Không mất dữ liệu |
| `STRAVA_CLIENT_SECRET` | https://www.strava.com/settings/api → reset secret → `.env` → `dc up -d` | Token runner vẫn dùng được; làm nhanh vì refresh token cần secret mới |
| `STRAVA_WEBHOOK_VERIFY_TOKEN` | Đổi trong `.env` → `dc up -d` → `/webhook delete <id>` → `/webhook subscribe` | Lỡ webhook trong vài phút |
| `TOKEN_ENCRYPTION_KEY` | Chỉ đổi khi nghi lộ. `python -m app.cli gen-key` → `.env` → `dc up -d` | **Mọi token cũ không giải mã được**: `/broadcast` yêu cầu toàn bộ runner `/connect` lại |
| `CF_TUNNEL_TOKEN` | Cloudflare Zero Trust → tunnel → rotate token → `.env` → `dc up -d` | Gián đoạn vài giây |

Luôn `dc exec app python -m app.cli backup` trước khi xoay khoá.

## 7. Gia hạn Strava subscription

- Subscription của tài khoản dev là điều kiện để API hoạt động ở Standard Tier. Hết hạn → OAuth và API trả lỗi, bot ngừng nhận dữ liệu.
- Giá tham khảo khoảng 11,99 USD/tháng hoặc 79,99 USD/năm (giá Mỹ, kiểm tra lại trên Strava).
- Đặt nhắc lịch 14 ngày trước ngày hết hạn. Nếu dùng gói năm, gia hạn trước giải.

## 8. Nộp review Strava để nâng athlete capacity

App mới được tối đa 10 athlete. Trên 10 cần Strava duyệt (cộng đồng báo chờ vài tuần đến vài tháng), nên nộp **ngay khi bot chạy được**, không chờ đến sát giải.

Chuẩn bị:

1. Mô tả use-case ngắn: bot nội bộ cho câu lạc bộ chạy bộ của công ty, runner tự nguyện kết nối, dữ liệu chỉ hiển thị trong nhóm kín, xoá khi rút quyền.
2. Ảnh chụp **nút "Connect with Strava"** đúng mẫu thương hiệu (tin nhắn `/connect` của bot hoặc trang `/connect/{token}`).
3. Ảnh chụp **màn hình hiển thị dữ liệu** (`/me`, `/ranking`, bài sáng).
4. Ảnh có **logo "Powered by Strava"** ở nơi hiển thị dữ liệu. Đây là yêu cầu thương hiệu của Strava (Brand Guidelines); bot phải có logo này ở tin `/start`, `/me` hoặc bài đăng.
5. Nêu rõ scope xin: `read`, `activity:read`; không ghi dữ liệu lên Strava.

Nộp trong trang https://www.strava.com/settings/api (mục nâng capacity / request review) theo hướng dẫn hiện hành của Strava. Theo dõi email trả lời; Strava có thể yêu cầu bổ sung ảnh. Trong khi chờ: giữ dưới 10 athlete, hoặc chạy song song phương án cũ (`legacy/`).

## 9. Khi máy chủ chết hoặc đổi máy

1. Máy mới: cài Docker, clone repo, copy `.env` và toàn bộ `data/` (gồm `vts.db` và `backups/`) từ máy cũ hoặc từ bản backup ngoài.
2. Trỏ DNS sang IP mới (hoặc chạy lại tunnel).
3. `dc up -d --build`, kiểm tra `/health`, `/status`, `/webhook list` (URL không đổi thì không cần subscribe lại).
4. `/reconcile`.
