# Bot bài đăng hằng ngày – VTS Running Club (chạy trên GitHub Actions)

Mỗi sáng bot soạn một bài đăng cho Club Strava rồi gửi vào Telegram của bạn. Bạn chỉ cần copy và dán lên Strava. Bot chạy hoàn toàn trên GitHub, không cần VPS hay máy cá nhân bật sẵn.

| Giờ VN (xấp xỉ) | Việc |
|---|---|
| 23:30 | Userbot gửi `/ranking` + `/today` cho @tdht_bot, lưu bảng tổng và thống kê km ngày (đã quy đổi theo loại vận động) vào `data/snapshots/` |
| 07:00 | Lấy top 3 bảng tổng + top 3 km hôm qua từ snapshot đêm trước → soạn bài → gửi Telegram |

> Lịch chạy của GitHub đôi khi trễ 5–30 phút vào giờ cao điểm. Đây là chuyện bình thường.

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

## Xử lý sự cố

- **Nhận tin "⚠️ Bot bài đăng Strava lỗi"** thì mở tab Actions để xem log của lần chạy đỏ.
- **Session hết hạn / bị Telegram đăng xuất:** chạy lại bước 3b trong một Codespace mới rồi cập nhật secret `TG_SESSION_STRING`. Telegram → Settings → Devices sẽ hiện phiên này; đừng bấm Terminate.
- **Bóc tách sai bảng:** phản hồi gốc của @tdht_bot được lưu ở `data/raw/NGÀY.txt` (ranking) và `data/raw/NGÀY-today.txt` (/today).
- **Bài đã soạn** được lưu trong `data/posts/`. Muốn soạn lại thì chạy workflow `post` thủ công.
