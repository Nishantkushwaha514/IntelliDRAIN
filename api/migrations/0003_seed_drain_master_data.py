from django.db import migrations

MASTER_DRAINS = [
    {
        "drain_id": "D-01",
        "name": "Sangamwadi Drain",
        "latitude": 18.5416,
        "longitude": 73.8734,
        "approx_area": "Sangamwadi",
    },
    {
        "drain_id": "D-02",
        "name": "Shivajinagar Central",
        "latitude": 18.5307,
        "longitude": 73.8478,
        "approx_area": "Shivajinagar",
    },
    {
        "drain_id": "D-03",
        "name": "Koregaon Stream",
        "latitude": 18.5369,
        "longitude": 73.8864,
        "approx_area": "Koregaon Park",
    },
    {
        "drain_id": "D-04",
        "name": "Kalyani Nagar East",
        "latitude": 18.5481,
        "longitude": 73.9027,
        "approx_area": "Kalyani Nagar",
    },
    {
        "drain_id": "D-05",
        "name": "Viman Drain",
        "latitude": 18.5662,
        "longitude": 73.9149,
        "approx_area": "Viman Nagar",
    },
    {
        "drain_id": "D-06",
        "name": "Wadgaon Channel",
        "latitude": 18.5537,
        "longitude": 73.9279,
        "approx_area": "Wadgaon Sheri",
    },
    {
        "drain_id": "D-07",
        "name": "Kharadi North",
        "latitude": 18.5668,
        "longitude": 73.9461,
        "approx_area": "Kharadi",
    },
    {
        "drain_id": "D-08",
        "name": "Mundhwa Creek",
        "latitude": 18.5264,
        "longitude": 73.9235,
        "approx_area": "Mundhwa",
    },
    {
        "drain_id": "D-09",
        "name": "Hadapsar West",
        "latitude": 18.5087,
        "longitude": 73.9258,
        "approx_area": "Hadapsar",
    },
    {
        "drain_id": "D-10",
        "name": "Magarpatta South",
        "latitude": 18.5073,
        "longitude": 73.9168,
        "approx_area": "Magarpatta",
    },
]


def seed_master_data(apps, schema_editor):
    Drain = apps.get_model('api', 'Drain')
    Inspection = apps.get_model('api', 'Inspection')

    # 1. Backfill any existing drains that have empty drain_id
    for drain in Drain.objects.filter(drain_id__isnull=True):
        drain.drain_id = f"D-P{drain.id:02d}"
        if not drain.name:
            drain.name = f"{drain.area_name} Drain"
        if not drain.approx_area:
            drain.approx_area = drain.area_name
        drain.save()

    for drain in Drain.objects.filter(drain_id=""):
        drain.drain_id = f"D-P{drain.id:02d}"
        if not drain.name:
            drain.name = f"{drain.area_name} Drain"
        if not drain.approx_area:
            drain.approx_area = drain.area_name
        drain.save()

    # 2. Seed reference master drains D-01 through D-10
    for item in MASTER_DRAINS:
        drain, created = Drain.objects.get_or_create(
            drain_id=item["drain_id"],
            defaults={
                "name": item["name"],
                "latitude": item["latitude"],
                "longitude": item["longitude"],
                "approx_area": item["approx_area"],
                "area_name": item["approx_area"],
                "current_status": "Normal",
                "risk_level": "LOW",
                "blockage_ratio": 0.0,
            }
        )
        if created:
            Inspection.objects.create(
                drain=drain,
                previous_status="Initial",
                new_status="Normal",
                remarks="Drain registered into monitoring system",
                status="Cleared",
                blockage_ratio=0.0,
                risk_level="LOW",
            )


def reverse_seed_master_data(apps, schema_editor):
    Drain = apps.get_model('api', 'Drain')
    drain_ids = [d["drain_id"] for d in MASTER_DRAINS]
    Drain.objects.filter(drain_id__in=drain_ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0002_alter_inspection_options_drain_approx_area_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_master_data, reverse_seed_master_data),
    ]
