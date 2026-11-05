import os
import json
from datetime import date, datetime
from django.conf import settings


def _json_serial(obj):
    """JSON serializer for objects not serializable by default json code."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


def export_drain_history_json(drain, base_dir=None) -> str:
    """
    Exports a drain's master data and full chronological inspection history
    into a dedicated JSON file (e.g. drain_history/D-01_history.json).

    Returns the absolute path of the written file.
    """
    if base_dir is None:
        base_dir = getattr(settings, 'DRAIN_HISTORY_DIR', os.path.join(settings.BASE_DIR, 'drain_history'))

    os.makedirs(base_dir, exist_ok=True)

    filename = f"{drain.drain_id or f'drain_{drain.id}'}_history.json"
    file_path = os.path.join(base_dir, filename)

    from .views import serialize_drain, serialize_inspection

    drain_info = serialize_drain(drain, full=True)
    inspections = [serialize_inspection(r) for r in drain.inspections.all().order_by('-timestamp')]

    payload = {
        "drain": drain_info,
        "total_inspections": len(inspections),
        "exported_at": datetime.now().isoformat(),
        "history": inspections,
    }

    # Write atomically via temp file to avoid partial reads by external processes
    temp_path = f"{file_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=_json_serial)

    os.replace(temp_path, file_path)
    return file_path


def export_all_drains_history(base_dir=None) -> list[str]:
    """Exports history JSON files for all drains in the database."""
    from .models import Drain
    files_created = []
    for drain in Drain.objects.all():
        path = export_drain_history_json(drain, base_dir=base_dir)
        files_created.append(path)
    return files_created
