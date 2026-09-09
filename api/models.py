from django.db import models


class Drain(models.Model):
    """
    One row = one physical drain location.
    Holds its CURRENT status — gets updated every time a new inspection runs.
    """
    area_name = models.CharField(max_length=200)
    latitude = models.FloatField()
    longitude = models.FloatField()

    risk_level = models.CharField(max_length=20, default="LOW")
    blockage_ratio = models.FloatField(default=0.0)

    rainfall_last_hour_mm = models.FloatField(default=0.0)
    humidity_pct = models.IntegerField(default=0)
    temperature_c = models.FloatField(default=0.0)
    dispatch_action = models.TextField(blank=True, default="")

    last_inspection_date = models.DateField(null=True, blank=True)
    last_cleaning_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"{self.area_name} ({self.risk_level})"


class Inspection(models.Model):
    """
    One row = one historical inspection event for a drain.
    A Drain can have many Inspections (that's the 'history' the app shows).
    """
    drain = models.ForeignKey(Drain, on_delete=models.CASCADE, related_name="inspections")
    date = models.DateField(auto_now_add=True)

    blockage_ratio = models.FloatField()
    risk_level = models.CharField(max_length=20)
    status = models.CharField(max_length=50, default="Pending Cleanup")

    image = models.ImageField(upload_to="processed_images/", null=True, blank=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.drain.area_name} — {self.date} — {self.risk_level}"
