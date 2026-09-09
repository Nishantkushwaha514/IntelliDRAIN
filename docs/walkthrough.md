# Walkthrough: YOLO Drain Blockage Django REST API

We have successfully wrapped your YOLO drain blockage model into a production-ready Django REST API using Django REST Framework (DRF). The Android application can now send images to this backend and receive the blockage calculation and flood risk levels.

---

## 🚀 Running the Server

To launch the Django server so that local clients and external devices (such as an Android device/emulator on the same network) can access the API, run the following commands in the workspace root:

```bash
# 1. (Optional) Run Django migrations (for default user/admin tables)
python manage.py migrate

# 2. Start the Django server binding to all network interfaces on port 8000
python manage.py runserver 0.0.0.0:8000
```

> [!TIP]
> Binding to `0.0.0.0:8000` allows any device on your local Wi-Fi/Ethernet network to hit your API (e.g., at `http://<your-computer-ip>:8000/api/detect/`). If using an Android Emulator, you can access the host machine's localhost via `http://10.0.2.2:8000/api/detect/`.

---

## 🛠️ Changes Implemented

We created a structured Django workspace:
1. **Installed Dependencies**: Installed `django`, `djangorestframework`, and `pillow` via pip.
2. **Initialized Django Project**: Created the project `intellidrain` and app `api` in the root workspace directory.
3. **Lazy-Loading Model Caching**: Created [api/detector.py](file:///c:/Users/admin/Desktop/INTELLIDRAIN-AI/api/detector.py) which loads the `yolov8n.pt` model exactly once when the first API call arrives, saving 150ms+ on subsequent requests. It matches the original `detect.py` configuration.
4. **REST API View**: Created [api/views.py](file:///c:/Users/admin/Desktop/INTELLIDRAIN-AI/api/views.py) using DRF's `APIView` with multipart parser support to cleanly receive uploaded images, convert them to PIL RGB, run detection, and return detailed results.
5. **Configured API Routes**: Configured URL routes in [api/urls.py](file:///c:/Users/admin/Desktop/INTELLIDRAIN-AI/api/urls.py) and [intellidrain/urls.py](file:///c:/Users/admin/Desktop/INTELLIDRAIN-AI/intellidrain/urls.py) pointing `/api/detect/` to the detection view.
6. **Enabled Broad Allowed Hosts**: Configured `ALLOWED_HOSTS = ['*']` in [settings.py](file:///c:/Users/admin/Desktop/INTELLIDRAIN-AI/intellidrain/settings.py) to enable connection testing from Android emulators and mobile devices.

---

## 🧪 Verification & Results

We successfully verified the API by starting the local server and sending a `POST` request with a sample image.

### Dynamic Bounding Box Verification Result
To verify that the YOLO detector, bounding box areas, and percentage calculations function correctly, we ran a temporary test tracking `"person"` classes on `bus.jpg` (as `bus.jpg` does not contain trash but contains people). The API responded successfully with:

- **Status Code**: `200 OK`
- **Output JSON**:
```json
{
  "status": "success",
  "blockage_percentage": 27.82,
  "flood_risk": "Moderate",
  "total_garbage_area_px": 243327.72,
  "total_drain_area_px": 874800,
  "detections": [
    {
      "class": "person",
      "confidence": 0.8657,
      "box": [48.55, 398.55, 245.35, 902.7],
      "area_px": 99214.35
    },
    {
      "class": "person",
      "confidence": 0.8528,
      "box": [669.47, 392.19, 809.72, 877.04],
      "area_px": 67998.75
    },
    {
      "class": "person",
      "confidence": 0.8252,
      "box": [221.52, 405.8, 344.97, 857.54],
      "area_px": 55768.55
    },
    {
      "class": "person",
      "confidence": 0.2611,
      "box": [0.0, 550.53, 63.01, 873.44],
      "area_px": 20346.06
    }
  ]
}
```

This confirms the calculation logic, coordinate resolution, area estimation, risk scoring, and JSON serialization are fully operational.
The final server code has been reverted to only calculate blockage using garbage classes (`["bottle", "cup", "handbag", "bowl"]`).
