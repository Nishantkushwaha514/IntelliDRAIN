from django.contrib import admin
from .models import Drain, Inspection


@admin.register(Drain)
class DrainAdmin(admin.ModelAdmin):
    list_display = ("area_name", "risk_level", "blockage_ratio", "last_inspection_date")
    list_filter = ("risk_level",)


@admin.register(Inspection)
class InspectionAdmin(admin.ModelAdmin):
    list_display = ("drain", "date", "risk_level", "blockage_ratio", "status")
    list_filter = ("risk_level", "status")
