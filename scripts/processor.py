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
    print(f"  Analyzing {os.path.basename(image_path)} for {drain.name or drain.area_name} ({drain.drain_id})...")

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

    previous_status = drain.current_status or "Normal"
    if risk_level == "CRITICAL":
        new_status = "Blocked"
    elif risk_level == "ELEVATED":
        new_status = "Elevated Risk"
    else:
        new_status = "Normal"

    drain.current_status = new_status
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
        previous_status=previous_status,
        new_status=new_status,
        remarks=f"Camera scan analysis: blockage {blockage_ratio*100:.1f}%, rainfall {rainfall_mm}mm, risk {risk_level}",
        condition="Restricted Flow" if risk_level in ("CRITICAL", "ELEVATED") else "Clear Flow",
        blockage_ratio=round(blockage_ratio, 3),
        risk_level=risk_level,
        status="Pending Cleanup" if risk_level in ("CRITICAL", "ELEVATED") else "Cleared"
    )
    with open(image_path, "rb") as f:
        inspection.image.save(os.path.basename(image_path), File(f), save=True)

    print(f"  [SAVED] {drain.drain_id} ({drain.name or drain.area_name}) -> {new_status} (blockage {blockage_ratio:.2f}, risk score {risk_score})")
    return True


def scan_once():
    if not os.path.isdir(IMAGES_ROOT):
        print(f"[WARN] {IMAGES_ROOT} does not exist yet — nothing to scan.")
        return

    for folder_name in os.listdir(IMAGES_ROOT):
        folder_path = os.path.join(IMAGES_ROOT, folder_name)
        if not os.path.isdir(folder_path):
            continue

        # Extract potential drain identifier from folder name (e.g., drain_1, drain_D-01, D-01, D-01_Sangamwadi)
        raw_name = folder_name.replace("drain_", "")
        identifier = raw_name.split("_")[0]

        drain = Drain.objects.filter(drain_id__iexact=identifier).first()
        if not drain and identifier.isdigit():
            drain = Drain.objects.filter(id=int(identifier)).first()
        if not drain:
            drain = Drain.objects.filter(drain_id__iexact=raw_name).first()

        if not drain:
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
            try:
                success = process_image(drain, image_path)
                if success:
                    dest_file = os.path.join(processed_dir, filename)
                    if os.path.exists(dest_file):
                        base, ext = os.path.splitext(filename)
                        dest_file = os.path.join(processed_dir, f"{base}_{int(time.time())}{ext}")
                    shutil.move(image_path, dest_file)
            except Exception as e:
                print(f"  [ERROR] Unexpected error processing {filename}: {e}")


def run_forever():
    print("=" * 60)
    print("IntelliDRAIN Scanner started")
    print(f"Watching: {IMAGES_ROOT}")
    print(f"Scan interval: {SCAN_INTERVAL_SECONDS} seconds")
    print("=" * 60)
    try:
        while True:
            print(f"\n[SCAN] {time.strftime('%Y-%m-%d %H:%M:%S')}")
            scan_once()
            time.sleep(SCAN_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nScanner stopped by user.")


if __name__ == "__main__":
    run_forever()

