from PIL import Image
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework import status

from .detector import get_detector
from .services import fetch_live_rainfall
from .risk import compute_risk_score, classify_risk
from .models import Drain, Inspection


class BlockageDetectionView(APIView):
    """
    Accepts an image upload, runs Gemini analysis, and returns the
    calculated blockage percentage and flood risk level.
    Does NOT save anything — this is a pure calculator.
    """
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request, *args, **kwargs):
        if 'image' not in request.FILES:
            return Response(
                {"error": "No image file provided. Please send an image file under the key 'image'."},
                status=status.HTTP_400_BAD_REQUEST
            )

        uploaded_image = request.FILES['image']

        try:
            pil_image = Image.open(uploaded_image)
            pil_image.verify()
            uploaded_image.seek(0)
        except Exception as e:
            return Response(
                {"error": f"Invalid image file. Details: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            detector = get_detector()
            analysis = detector.analyze_uploaded_file(uploaded_image)
            if analysis.get("status") == "error":
                return Response(
                    {"error": f"An error occurred during object detection: {analysis.get('error')}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
        except Exception as e:
            return Response(
                {"error": f"An error occurred during object detection: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        blockage_pct = analysis.get("blockage_percentage", 0.0)
        blockage_ratio = blockage_pct / 100.0

        rain_mm = request.data.get("rain_mm")
        lat = request.data.get("latitude")
        lon = request.data.get("longitude")

        if rain_mm is not None:
            try:
                rainfall_mm = float(rain_mm)
            except (ValueError, TypeError):
                rainfall_mm = 0.0
        elif lat is not None and lon is not None:
            weather_metrics = fetch_live_rainfall(float(lat), float(lon))
            rainfall_mm = weather_metrics["rainfall_mm"] if weather_metrics.get("success") else 0.0
        else:
            rainfall_mm = 0.0

        risk_score = compute_risk_score(blockage_ratio, rainfall_mm)
        risk_level, dispatch_action = classify_risk(risk_score)

        return Response({
            "status": "success",
            "blockage_ratio": round(blockage_ratio, 3),
            "blockage_percentage": blockage_pct,
            "computed_risk_score": risk_score,
            "flood_risk_level": risk_level,
            "dispatch_action": dispatch_action,
            "detected_objects": analysis.get("detected_objects", []),
            "water_flow_status": analysis.get("water_flow_status", "Unknown"),
            "vision_confidence": analysis.get("vision_confidence", "Low"),
            "reasoning": analysis.get("reasoning", "")
        }, status=status.HTTP_200_OK)


class FloodRiskAssessmentView(APIView):
    """
    Takes lat/lon + a blockage ratio, fetches live weather, and returns
    a flood risk assessment.
    """
    def post(self, request, *args, **kwargs):
        lat = request.data.get("latitude")
        lon = request.data.get("longitude")

        try:
            blockage_ratio = max(0.0, min(float(request.data.get("blockage_ratio", 0.0)), 1.0))
        except (ValueError, TypeError):
            return Response({"error": "Invalid blockage_ratio value."}, status=status.HTTP_400_BAD_REQUEST)

        if lat is None or lon is None:
            return Response({"error": "Both 'latitude' and 'longitude' are required."}, status=status.HTTP_400_BAD_REQUEST)

        weather_metrics = fetch_live_rainfall(float(lat), float(lon))
        if not weather_metrics["success"]:
            return Response(
                {"error": "Failed to fetch environmental metrics.", "details": weather_metrics.get("error")},
                status=status.HTTP_502_BAD_GATEWAY
            )

        rainfall_mm = weather_metrics["rainfall_mm"]

        risk_score = compute_risk_score(blockage_ratio, rainfall_mm)
        risk_level, dispatch_action = classify_risk(risk_score)

        return Response({
            "status": "success",
            "coordinates": {"lat": lat, "lon": lon},
            "environmental_metrics": {
                "rainfall_last_hour_mm": rainfall_mm,
                "normalized_intensity": round(min(rainfall_mm / 50.0, 1.0), 2),
                "humidity_pct": weather_metrics.get("humidity"),
                "temperature_c": weather_metrics.get("temperature")
            },
            "risk_analysis": {
                "input_blockage_ratio": blockage_ratio,
                "computed_risk_score": risk_score,
                "flood_risk_level": risk_level,
                "dispatch_action": dispatch_action
            }
        }, status=status.HTTP_200_OK)


# ------------------------------------------------------------------
# Read-only endpoints for the Android app
# ------------------------------------------------------------------

class DrainListView(APIView):
    """GET /api/drains/ — all drains, for the Map and Home screens."""
    def get(self, request):
        drains = Drain.objects.all()
        data = [{
            "id": d.id,
            "latitude": d.latitude,
            "longitude": d.longitude,
            "area_name": d.area_name,
            "risk_level": d.risk_level,
            "blockage_ratio": d.blockage_ratio,
            "last_inspection_date": d.last_inspection_date,
        } for d in drains]
        return Response(data, status=status.HTTP_200_OK)


class DrainDetailView(APIView):
    """GET /api/drains/{id}/ — full detail for one drain."""
    def get(self, request, id):
        try:
            d = Drain.objects.get(id=id)
        except Drain.DoesNotExist:
            return Response({"error": "Drain not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "id": d.id,
            "latitude": d.latitude,
            "longitude": d.longitude,
            "area_name": d.area_name,
            "risk_level": d.risk_level,
            "blockage_ratio": d.blockage_ratio,
            "last_inspection_date": d.last_inspection_date,
            "rainfall_last_hour_mm": d.rainfall_last_hour_mm,
            "humidity_pct": d.humidity_pct,
            "temperature_c": d.temperature_c,
            "dispatch_action": d.dispatch_action,
            "last_cleaning_date": d.last_cleaning_date,
        }, status=status.HTTP_200_OK)


class DrainHistoryView(APIView):
    """GET /api/drains/{id}/history/ — past inspections for one drain, newest first."""
    def get(self, request, id):
        if not Drain.objects.filter(id=id).exists():
            return Response({"error": "Drain not found."}, status=status.HTTP_404_NOT_FOUND)

        records = Inspection.objects.filter(drain_id=id)
        data = [{
            "date": r.date,
            "blockage_ratio": r.blockage_ratio,
            "risk_level": r.risk_level,
            "status": r.status,
        } for r in records]
        return Response(data, status=status.HTTP_200_OK)
