from django.contrib import admin
from .models import Drain, Inspection, FloodRiskLog


@admin.register(Drain)
class DrainAdmin(admin.ModelAdmin):
    list_display = ("drain_id", "name", "approx_area", "latitude", "longitude", "current_status", "risk_level", "last_updated")
    search_fields = ("drain_id", "name", "approx_area", "area_name")
    list_filter = ("current_status", "risk_level")


@admin.register(Inspection)
class InspectionAdmin(admin.ModelAdmin):
    list_display = ("drain", "timestamp", "previous_status", "new_status", "status", "risk_level", "blockage_ratio")
    search_fields = ("drain__drain_id", "drain__name", "remarks", "condition")
    list_filter = ("risk_level", "status", "new_status")


@admin.register(FloodRiskLog)
class FloodRiskLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'risk_level', 'risk_score', 'blockage_ratio', 'rainfall_mm', 'timestamp')
    list_filter = ('risk_level', 'timestamp')
    search_fields = ('risk_level', 'dispatch_action')
    ordering = ('-timestamp',)

