# Implementation Plan: Django REST API for YOLO Drain Blockage Detection

This plan outlines the steps to build a Django REST API that integrates your existing YOLO-based drain blockage detection code (`detect.py`) to serve an Android client. The API will accept an uploaded image and return the calculated blockage percentage and flood risk level in a clean JSON format.

## User Review Required

> [!IMPORTANT]
> **Host Configuration**: To allow an Android emulator or a physical Android device on the same local network to access the API, we will configure Django's `ALLOWED_HOSTS` to `['*']` and explain how to run the server binding to all network interfaces (`0.0.0.0`).
> **Model Caching**: To avoid reloading the 6.5MB YOLO model (`yolov8n.pt`) on every request, we will load the model once in memory and cache it.

## Open Questions

1. **Grate Area Calculation**: In `detect.py`, `TOTAL_DRAIN_AREA` is hardcoded as `640 * 640`. However, uploaded images can be of any dimensions.
   - **Option A (Recommended)**: Use the actual dimensions of the uploaded image (e.g., `width * height`) as the total drain area, which matches the bounding boxes scale.
   - **Option B**: Maintain the hardcoded `640 * 640` limit from `detect.py`.
   - *We will implement Option A by default as it is mathematically consistent with arbitrary image uploads, but can adjust based on your feedback.*
2. **Flood Risk Categories**: We propose the following default risk levels. Please let us know if they need to be updated:
   - **Low Risk**: Blockage < 25%
   - **Moderate Risk**: 25% <= Blockage < 50%
   - **High Risk**: 50% <= Blockage < 75%
   - **Critical Risk**: Blockage >= 75%

---

## Proposed Changes

We will install `django`, `djangorestframework`, and `pillow` on your system.

### Django Project Structure

We will create a Django project structure inside the current workspace directory:
```
INTELLIDRAIN-AI/
├── manage.py
├── yolov8n.pt
├── detect.py
├── intellidrain/
│   ├── __init__.py
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
└── api/
    ├── __init__.py
    ├── apps.py
    ├── detector.py  <-- [NEW] YOLO integration helper
    ├── urls.py      <-- [NEW] App-level routing
    └── views.py     <-- [NEW] Blockage API View
```

---

### Django Backend Setup

#### [NEW] [detector.py](file:///c:/Users/admin/Desktop/INTELLIDRAIN-AI/api/detector.py)
A module to load the YOLO model once and perform the calculations. It preserves the Windows DLL-loading logic from `detect.py`.

#### [NEW] [views.py](file:///c:/Users/admin/Desktop/INTELLIDRAIN-AI/api/views.py)
A Django REST Framework `APIView` that handles the POST requests at `/api/detect/`. It receives the image, parses it using PIL, calls the detector, and returns the response.

#### [NEW] [urls.py](file:///c:/Users/admin/Desktop/INTELLIDRAIN-AI/api/urls.py)
App-level routing mapping the API endpoint to the view.

#### [MODIFY] [settings.py](file:///c:/Users/admin/Desktop/INTELLIDRAIN-AI/intellidrain/settings.py)
Configure Django settings to:
- Add `rest_framework` and `api` to `INSTALLED_APPS`.
- Set `ALLOWED_HOSTS = ['*']` for local network testing.

#### [MODIFY] [urls.py](file:///c:/Users/admin/Desktop/INTELLIDRAIN-AI/intellidrain/urls.py)
Include the `api` app routes.

---

## Verification Plan

### Automated Tests
We will verify the API using a custom Python script that sends a test image to the local server and asserts the JSON format:
```bash
# Run Django server in one process
python manage.py runserver 127.0.0.1:8000

# Run a test request script in another process
python scratch/test_api.py
```

### Manual Verification
We will run `curl` to ensure it returns the correct response:
```bash
curl -X POST -F "image=@test_image.jpg" http://127.0.0.1:8000/api/detect/
```
The expected JSON response structure:
```json
{
  "status": "success",
  "blockage_percentage": 24.5,
  "flood_risk": "Low",
  "total_garbage_area_px": 100352,
  "total_drain_area_px": 409600,
  "detections": [
    {
      "class": "bottle",
      "confidence": 0.89,
      "box": [100.0, 150.0, 250.0, 400.0],
      "area_px": 37500.0
    }
  ]
}
```
