# pyrefly: ignore [missing-import]
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone


class Drain(models.Model):
    """
    One row = one physical drain location.
    Holds its unique identity, location coordinates, and CURRENT status.
    """
    drain_id = models.CharField(max_length=50, unique=True, db_index=True, null=True, blank=True)
    name = models.CharField(max_length=200, blank=True, default="")
    approx_area = models.CharField(max_length=200, blank=True, default="")
    area_name = models.CharField(max_length=200, blank=True, default="")  # Retained for backward compatibility

    latitude = models.FloatField(
        validators=[MinValueValidator(-90.0), MaxValueValidator(90.0)]
    )
    longitude = models.FloatField(
        validators=[MinValueValidator(-180.0), MaxValueValidator(180.0)]
    )

    current_status = models.CharField(max_length=50, default="Normal")
    last_updated = models.DateTimeField(auto_now=True)

    risk_level = models.CharField(max_length=20, default="LOW")
    blockage_ratio = models.FloatField(default=0.0)

    rainfall_last_hour_mm = models.FloatField(default=0.0)
    humidity_pct = models.IntegerField(default=0)
    temperature_c = models.FloatField(default=0.0)
    dispatch_action = models.TextField(blank=True, default="")

    last_inspection_date = models.DateField(null=True, blank=True)
    last_cleaning_date = models.DateField(null=True, blank=True)

    def save(self, *args, **kwargs):
        # Keep name, approx_area, and area_name in sync
        if not self.name and self.area_name:
            self.name = f"{self.area_name} Drain"
        if not self.approx_area and self.area_name:
            self.approx_area = self.area_name
        if not self.area_name:
            self.area_name = self.approx_area or self.name
        super().save(*args, **kwargs)

    def __str__(self):
        display_name = self.name or self.area_name or self.drain_id
        return f"{self.drain_id} - {display_name} ({self.current_status})"


class Inspection(models.Model):
    """
    One row = one historical inspection or status change event for a drain.
    A Drain has isolated Inspections linked via ForeignKey.
    """
    drain = models.ForeignKey(Drain, on_delete=models.CASCADE, related_name="inspections")
    date = models.DateField(auto_now_add=True)
    timestamp = models.DateTimeField(default=timezone.now)

    previous_status = models.CharField(max_length=50, blank=True, default="")
    new_status = models.CharField(max_length=50, blank=True, default="")
    remarks = models.TextField(blank=True, default="")
    condition = models.CharField(max_length=100, blank=True, default="")
    inspection_info = models.TextField(blank=True, default="")

    blockage_ratio = models.FloatField(default=0.0)
    risk_level = models.CharField(max_length=20, default="LOW")
    status = models.CharField(max_length=50, default="Pending Cleanup")

    image = models.ImageField(upload_to="processed_images/", null=True, blank=True)

    class Meta:
        ordering = ["-timestamp", "-date"]

    def __str__(self):
        drain_id = getattr(self.drain, "drain_id", "") or str(self.drain.id)
        drain_name = getattr(self.drain, "name", "") or self.drain.area_name
        return f"{drain_id} ({drain_name}) — {self.date} — {self.new_status or self.status}"

