# Hướng dẫn runner: kết nối Strava với bot VTS Running Club

Chỉ cần làm một lần, mất khoảng 1 phút.

## Kết nối

1. Mở Telegram, tìm bot của club (admin gửi link), bấm **Start**.
2. Gõ `/connect`. Bot gửi link **Connect with Strava** (link sống 30 phút).
3. Bấm link, đăng nhập Strava nếu cần, bấm **Authorize**. Bot chỉ xin quyền **đọc** hoạt động của bạn (`activity:read`), không đăng gì lên Strava của bạn.
4. Quay lại Telegram, gõ `/me`. Thấy tên Strava và số km là xong. Bot tự lấy lại các hoạt động từ ngày bắt đầu giải.

Khi kết nối, bạn đồng ý cho bot hiển thị **tên và số km quy đổi** của bạn trong nhóm kín của club. Không hiển thị bản đồ, link hoạt động hay dữ liệu khác.

## Để hoạt động được tính

| Nên | Không được tính |
|---|---|
| Bật GPS và ghi bằng đồng hồ/điện thoại, hoạt động có bản đồ | Hoạt động **không có bản đồ GPS** |
| Chọn đúng loại: Run, Walk, Hike, Ride, Swim... | **Nhập tay** (Manual entry) |
| Để chế độ hiển thị *Everyone* hoặc *Followers* | **Treadmill / máy tập** (trainer) |
| Đủ quãng đường tối thiểu, pace trong khoảng cho phép (xem `/rules`) | Hoạt động ảo: VirtualRun, VirtualRide, xe điện trợ lực, yoga, tập tạ... |
| Đồng bộ lên Strava trong ngày | Hoạt động để **Only You**: bot không đọc được với quyền `activity:read` |

Km quy đổi: đạp xe tính 0,25; bơi tính 4 (100 m bơi = 400 m chạy) theo luật mặc định. Luật hiện hành: gõ `/rules`.

## Lệnh thường dùng

| Lệnh | Ý nghĩa |
|---|---|
| `/me` | Tổng km, số ngày chạy, hoạt động gần nhất |
| `/today` | Thống kê hôm nay (`/today 12/01` cho ngày khác) |
| `/ranking` | Bảng xếp hạng |
| `/log` | 10 hoạt động gần nhất của bạn kèm trạng thái **và lý do nếu bị loại** |
| `/rules` | Luật đang áp dụng |
| `/unlink` | Ngắt kết nối, xoá token và dữ liệu của bạn khỏi bot |
| `/help` | Danh sách lệnh |

## Khi bị loại

Bot nhắn riêng cho bạn kèm lý do, ví dụ *không có bản đồ GPS*, *nhập tay*, *pace nhanh bất thường (3:41)*. Gõ `/log` để xem lại. Thấy sai, gửi admin **mã hoạt động** hiện trong `/log` để kiểm tra.

## Thắc mắc nhanh

- **Chạy xong mà `/me` chưa có?** Chờ vài phút (Strava gửi tin cho bot sau khi đồng hồ đồng bộ xong). Quá 3 giờ chưa có thì báo admin.
- **Link kết nối hết hạn?** Gõ `/connect` lấy link mới.
- **Muốn dừng?** Gõ `/unlink`, hoặc vào Strava → Settings → My Apps → Revoke Access. Dữ liệu của bạn trong bot bị xoá.
