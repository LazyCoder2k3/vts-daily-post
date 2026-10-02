# Bot bài đăng hằng ngày – VTS Running Club (chạy trên GitHub Actions)

Mỗi sáng bot soạn một bài đăng cho Club Strava rồi gửi vào Telegram của bạn. Bạn chỉ cần copy và dán lên Strava. Bot chạy hoàn toàn trên GitHub, không cần VPS hay máy cá nhân bật sẵn.

| Giờ VN (xấp xỉ) | Việc |
|---|---|
| 23:20 và 23:50 | Userbot gửi `/ranking` + `/today` cho @tdht_bot, lưu bảng tổng và thống kê km ngày (đã quy đổi theo loại vận động) vào `data/snapshots/`. Hai lượt dự phòng nhau, lượt đúng giờ muộn nhất được giữ |
| 06:47 | Lấy top 3 bảng tổng + top 3 km hôm qua từ snapshot đêm trước → soạn bài → gửi Telegram |

> ⚠️ Lịch `schedule` của GitHub **không đảm bảo giờ chạy**: thực tế đã trễ 3–4 tiếng nhiều ngày liền.
> Vì `/today` chỉ trả về "hôm nay", snapshot chạy sau 0h sẽ mất km của ngày hôm trước. Bot xử lý như sau:
>
> - Snapshot chạy **trước 06:00 giờ VN** được coi là chạy trễ của đêm hôm trước: lưu vào ngày hôm trước, gắn cờ `late`, **không dùng `/today`** và không ghi đè snapshot đúng giờ nếu đã có.
> - Khi đó bài đăng ước tính km ngày bằng chênh lệch bảng tổng giữa hai snapshot, và tin nhắn gửi cho admin có dòng **"⚠️ Dữ liệu bị suy giảm"** nêu rõ lý do (snapshot trễ, `/today` lỗi, thiếu snapshot hôm qua...).
> - Nếu `/today` lỗi nhưng `/ranking` ổn, bot vẫn lưu `/ranking` thay vì bỏ cả lượt.
> - Giờ bắt đầu thực của mỗi lượt được in ở bước "Chọn lệnh" trong log Actions.

---

## Bước 1 – Tạo repo PRIVATE

Vào https://github.com/new, đặt tên ví dụ `vts-daily-post`, chọn **Private** rồi bấm Create.

⚠️ Repo bắt buộc phải là Private, vì dữ liệu bảng xếp hạng có tên đồng nghiệp.

## Bước 2 – Mở Codespace và đưa code lên

Codespace là một máy ảo có terminal chạy ngay trên trình duyệt, nên không cần cài Python trên máy bạn.

1. Trong repo, bấm **Code → Codespaces → Create codespace on main**.
2. Kéo file `vts-daily-post.zip` vào khung Explorer bên trái để tải lên.
3. Trong Terminal, chạy:

```bash
unzip -o vts-daily-post.zip && rm vts-daily-post.zip
pip install -r requirements.txt
```

## Bước 3 – Lấy thông tin Telegram

**a) API của userbot:** vào https://my.telegram.org, chọn **API development tools**, tạo app, rồi ghi lại `api_id` và `api_hash`.

**b) Đăng nhập userbot**, trong Terminal của Codespace:

```bash
export TG_API_ID=123456 TG_API_HASH=abcdef...
python main.py login
```

Nhập số điện thoại (+84...) và mã OTP mà Telegram gửi. Bot sẽ in ra một chuỗi dài giữa hai dòng `===`. Đó là `TG_SESSION_STRING`.
⚠️ Chuỗi này tương đương quyền đăng nhập tài khoản Telegram của bạn. Chỉ dán nó vào GitHub Secret, không gửi cho ai.

**c) Bot nhận bài:** nhắn @BotFather, gõ `/newbot`, lấy token. Mở bot vừa tạo, bấm **Start**, rồi chạy:

```bash
export NOTIFY_BOT_TOKEN=123:ABC...
python main.py chat-id
```

## Bước 4 – Đẩy code lên repo

```bash
git add . && git commit -m "VTS daily post bot" && git push
```

`.gitignore` đã chặn sẵn `.env` và file session. Làm xong bước này thì xoá Codespace đi (**Code → Codespaces → … → Delete**).

## Bước 5 – Khai báo Secrets

Vào repo, chọn **Settings → Secrets and variables → Actions → New repository secret**, rồi tạo:

| Secret | Giá trị |
|---|---|
| `TG_API_ID` | api_id |
| `TG_API_HASH` | api_hash |
| `TG_SESSION_STRING` | chuỗi in ra ở bước 3b |
| `NOTIFY_BOT_TOKEN` | token BotFather |
| `NOTIFY_CHAT_ID` | số in ra ở bước 3c |
| `ANTHROPIC_API_KEY` hoặc `GEMINI_API_KEY` | *(tùy chọn)* nếu muốn AI viết lại bài |

Tab **Variables** là tùy chọn: `LLM_PROVIDER` (`anthropic`/`gemini`), `HASHTAGS`, `SHOW_DAILY_KM` (`0` để ẩn km ở top ngày).

## Bước 6 – Chạy thử

Vào tab **Actions → VTS daily Strava post → Run workflow**:

1. Chọn `snapshot` và chờ dấu ✅. Log sẽ in số runner và top 3. Đây là mốc đầu tiên để tính km theo ngày.
2. Chọn `post`. Bạn sẽ nhận 2 tin Telegram. Lần đầu chưa có mục top km trong ngày vì chưa có snapshot hôm trước.

Sau đó bot tự chạy mỗi ngày đến 15/10, rồi tự dừng.

---

## Chạy đúng giờ 23:50 bằng trigger ngoài (khuyến nghị)

Cách duy nhất để chốt số liệu đúng giờ là gọi `workflow_dispatch` từ bên ngoài, thường chạy trong vài giây.

1. GitHub → Settings → Developer settings → Fine-grained tokens: tạo token chỉ cho repo này, quyền **Actions: Read and write**. Token lưu ở phía bạn, đừng gửi cho ai.
2. Trên https://cron-job.org (hoặc crontab của một máy luôn bật) tạo job lúc **23:50, múi giờ Asia/Ho_Chi_Minh**:
   - URL: `https://api.github.com/repos/<owner>/vts-daily-post/actions/workflows/daily.yml/dispatches`
   - Method `POST`, header `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`
   - Body: `{"ref":"main","inputs":{"command":"snapshot"}}`

Các lượt `schedule` trong workflow vẫn giữ làm dự phòng.

## Xử lý sự cố

- **Bài/tin cảnh báo có dòng "⚠️ Dữ liệu bị suy giảm"** nghĩa là snapshot trễ hoặc thiếu; km trong ngày chỉ là ước tính.
- **Lỗi "@tdht_bot không trả bảng ... trong 90s"**: log kèm các tin bot đã trả lời, xem đó để biết bot im lặng hay đổi định dạng. Lượt chạy bị lỗi này sẽ tốn ~6 phút vì retry.

- **Nhận tin "⚠️ Bot bài đăng Strava lỗi"** thì mở tab Actions để xem log của lần chạy đỏ.
- **Session hết hạn / bị Telegram đăng xuất:** chạy lại bước 3b trong một Codespace mới rồi cập nhật secret `TG_SESSION_STRING`. Telegram → Settings → Devices sẽ hiện phiên này; đừng bấm Terminate.
- **Bóc tách sai bảng:** phản hồi gốc của @tdht_bot được lưu ở `data/raw/NGÀY.txt` (ranking) và `data/raw/NGÀY-today.txt` (/today).
- **Bài đã soạn** được lưu trong `data/posts/`. Muốn soạn lại thì chạy workflow `post` thủ công.
