import os
import sys
import time
import shutil
from datetime import date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'intellidrain.settings')

import django
django.setup()

from django.core.files import File
from api.models import Drain, Inspection
from api.detector import get_detector
from api.services import fetch_live_rainfall
from api.risk import compute_risk_score, classify_risk

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_ROOT = os.path.join(BASE_DIR, "drain_images")
SCAN_INTERVAL_SECONDS = 10  # change to a smaller number (e.g. 30) for fast local testing
VALID_EXTENSIONS = (".jpg", ".jpeg", ".png")

    
def process_image(drain: Drain, image_path: str):
    print(f"  Analyzing {os.path.basename(image_path)} for {drain.area_name}...")

    detector = get_detector()
    with open(image_path, "rb") as f:
        analysis = detector.analyze_uploaded_file(f)

    if analysis.get("status") == "error":
        print(f"  [SKIPPED] Gemini analysis failed: {analysis.get('error')}")
        return False

    blockage_ratio = analysis.get("blockage_percentage", 0.0) / 100.0

    weather = fetch_live_rainfall(drain.latitude, drain.longitude)
    rainfall_mm = weather["rainfall_mm"] if weather.get("success") else 0.0
    humidity = weather.get("humidity", 0)
    temperature = weather.get("temperature", 0.0)

    risk_score = compute_risk_score(blockage_ratio, rainfall_mm)
    risk_level, dispatch_action = classify_risk(risk_score)

    drain.blockage_ratio = round(blockage_ratio, 3)
    drain.risk_level = risk_level
    drain.dispatch_action = dispatch_action
    drain.rainfall_last_hour_mm = rainfall_mm
    drain.humidity_pct = humidity
    drain.temperature_c = temperature
    drain.last_inspection_date = date.today()
    drain.save()

    inspection = Inspection(
        drain=drain,
        blockage_ratio=round(blockage_ratio, 3),
        risk_level=risk_level,
        status="Pending Cleanup" if risk_level in ("CRITICAL", "ELEVATED") else "Cleared"
    )
    with open(image_path, "rb") as f:
        inspection.image.save(os.path.basename(image_path), File(f), save=True)

    print(f"  [SAVED] {drain.area_name} -> {risk_level} (blockage {blockage_ratio:.2f}, risk score {risk_score})")
    return True


def scan_once():
    if not os.path.isdir(IMAGES_ROOT):
        print(f"[WARN] {IMAGES_ROOT} does not exist yet — nothing to scan.")
        return

    for folder_name in os.listdir(IMAGES_ROOT):
        folder_path = os.path.join(IMAGES_ROOT, folder_name)
        if not os.path.isdir(folder_path) or not folder_name.startswith("drain_"):
            continue

        try:
            drain_id = int(folder_name.replace("drain_", ""))
        except ValueError:
            print(f"[WARN] Skipping folder with unexpected name: {folder_name}")
            continue

        try:
            drain = Drain.objects.get(id=drain_id)
        except Drain.DoesNotExist:
            print(f"[WARN] No Drain with id={drain_id} exists in the database — skipping {folder_name}. Add it in /admin/ first.")
            continue

        processed_dir = os.path.join(folder_path, "processed")
        os.makedirs(processed_dir, exist_ok=True)

        new_images = [
            f for f in os.listdir(folder_path)
            if f.lower().endswith(VALID_EXTENSIONS) and os.path.isfile(os.path.join(folder_path, f))
        ]

        if not new_images:
            continue

        print(f"[FOUND] {len(new_images)} new image(s) in {folder_name}")
        for filename in new_images:
            image_path = os.path.join(folder_path, filename)
            success = process_image(drain, image_path)
            if success:
                shutil.move(image_path, os.path.join(processed_dir, filename))


def run_forever():
    print("=" * 60)
    print("IntelliDRAIN Scanner started")
    print(f"Watching: {IMAGES_ROOT}")
    print(f"Scan interval: {SCAN_INTERVAL_SECONDS} seconds")
    print("=" * 60)
    while True:
        print(f"\n[SCAN] {time.strftime('%Y-%m-%d %H:%M:%S')}")
        scan_once()
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
