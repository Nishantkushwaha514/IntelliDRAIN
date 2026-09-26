import os
import io
from unittest.mock import patch, MagicMock
from requests.exceptions import RequestException

from django.test import TestCase
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from api.models import Drain, Inspection
from api.detector import DrainAnalysisSchema, GeminiDrainDetector, get_detector
from api.risk import compute_risk_score, classify_risk
from api.services import fetch_live_rainfall


# =====================================================================
# 1. RISK FORMULA TESTS
# =====================================================================

class RiskFormulaTests(TestCase):
    def test_compute_risk_score_boundaries(self):
        # Zero blockage, zero rain -> 0.0
        self.assertEqual(compute_risk_score(0.0, 0.0), 0.0)

        # Full blockage (1.0) -> 1.0 regardless of rain
        self.assertEqual(compute_risk_score(1.0, 0.0), 1.0)
        self.assertEqual(compute_risk_score(1.0, 25.0), 1.0)

        # Extreme rain (>= 50mm) -> 1.0 regardless of blockage
        self.assertEqual(compute_risk_score(0.0, 50.0), 1.0)
        self.assertEqual(compute_risk_score(0.0, 100.0), 1.0)

        # Intermediate calculation
        score = compute_risk_score(0.3, 10.0)
        self.assertTrue(0.40 <= score <= 0.55)

    def test_classify_risk(self):
        level_crit, action_crit = classify_risk(0.80)
        self.assertEqual(level_crit, "CRITICAL")
        self.assertIn("Immediate dispatch", action_crit)

        level_elev, action_elev = classify_risk(0.50)
        self.assertEqual(level_elev, "ELEVATED")
        self.assertIn("Monitored status", action_elev)

        level_low, action_low = classify_risk(0.20)
        self.assertEqual(level_low, "LOW")
        self.assertIn("Routine maintenance", action_low)


# =====================================================================
# 2. DETECTOR ENGINE TESTS
# =====================================================================

class DetectorTests(TestCase):
    def test_drain_analysis_schema_validation(self):
        data = {
            "blockage_percentage": 35.5,
            "detected_objects": ["leaves", "plastic bottle"],
            "water_flow_status": "Restricted",
            "vision_confidence": "High",
            "reasoning": "Debris partially covering grate."
        }
        schema = DrainAnalysisSchema(**data)
        self.assertEqual(schema.blockage_percentage, 35.5)
        self.assertEqual(schema.detected_objects, ["leaves", "plastic bottle"])
        self.assertEqual(schema.water_flow_status, "Restricted")
        self.assertEqual(schema.vision_confidence, "High")

    def test_gemini_detector_missing_api_key(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
            with self.assertRaises(ValueError):
                GeminiDrainDetector()

    @patch("api.detector.genai.Client")
    def test_analyze_uploaded_file_success(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        mock_parsed = DrainAnalysisSchema(
            blockage_percentage=42.0,
            detected_objects=["mud", "wrappers"],
            water_flow_status="Restricted",
            vision_confidence="High",
            reasoning="Grate is 42% covered by mud and wrappers."
        )
        mock_response = MagicMock()
        mock_response.parsed = mock_parsed
        mock_client.models.generate_content.return_value = mock_response

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            detector = GeminiDrainDetector()

            # Create dummy image in memory
            buf = io.BytesIO()
            Image.new("RGB", (100, 100), color="blue").save(buf, format="JPEG")
            buf.seek(0)

            result = detector.analyze_uploaded_file(buf)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["blockage_percentage"], 42.0)
            self.assertEqual(result["detected_objects"], ["mud", "wrappers"])
            self.assertEqual(result["water_flow_status"], "Restricted")
            self.assertEqual(result["vision_confidence"], "High")

    @patch("api.detector.genai.Client")
    def test_analyze_uploaded_file_failure_handling(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.models.generate_content.side_effect = Exception("API rate limit reached")

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            detector = GeminiDrainDetector()

            buf = io.BytesIO()
            Image.new("RGB", (100, 100), color="red").save(buf, format="JPEG")
            buf.seek(0)

            result = detector.analyze_uploaded_file(buf)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["blockage_percentage"], 0.0)
            self.assertEqual(result["water_flow_status"], "Unknown")
            self.assertIn("API rate limit reached", result["error"])


# =====================================================================
# 3. WEATHER SERVICE TESTS
# =====================================================================

class WeatherServiceTests(TestCase):
    @patch("requests.get")
    def test_fetch_live_rainfall_success(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rain": {"1h": 8.5},
            "main": {"humidity": 78, "temp": 24.2}
        }
        mock_get.return_value = mock_response

        res = fetch_live_rainfall(19.0, 73.0)
        self.assertTrue(res["success"])
        self.assertEqual(res["rainfall_mm"], 8.5)
        self.assertEqual(res["humidity"], 78)
        self.assertEqual(res["temperature"], 24.2)

    @patch("requests.get")
    def test_fetch_live_rainfall_no_active_precipitation(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "main": {"humidity": 60, "temp": 30.0}
        }
        mock_get.return_value = mock_response

        res = fetch_live_rainfall(19.0, 73.0)
        self.assertTrue(res["success"])
        self.assertEqual(res["rainfall_mm"], 0.0)
        self.assertEqual(res["humidity"], 60)
        self.assertEqual(res["temperature"], 30.0)

    @patch("requests.get")
    def test_fetch_live_rainfall_unauthorized_fallback(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 401
        from requests.exceptions import HTTPError
        mock_response.raise_for_status.side_effect = HTTPError("Unauthorized access")
        mock_get.return_value = mock_response

        res = fetch_live_rainfall(19.0, 73.0)
        self.assertTrue(res["success"])
        self.assertEqual(res["rainfall_mm"], 12.5)
        self.assertEqual(res["humidity"], 85)
        self.assertEqual(res["temperature"], 26.5)
        self.assertIn("note", res)

    @patch("requests.get")
    def test_fetch_live_rainfall_network_exception_failure(self, mock_get):
        with patch.dict(os.environ, {"OPENWEATHER_API_KEY": "REAL_KEY_FOR_TEST"}):
            mock_get.side_effect = RequestException("Connection timeout")
            res = fetch_live_rainfall(19.0, 73.0)
            self.assertFalse(res["success"])
            self.assertEqual(res["error"], "Connection timeout")


# =====================================================================
# 4. BLOCKAGE DETECTION VIEW TESTS
# =====================================================================

class BlockageDetectionViewTests(TestCase):
    def setUp(self):
        self.url = reverse("blockage-detection")

        img = Image.new("RGB", (50, 50), color="blue")
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="JPEG")
        img_bytes.seek(0)
        self.valid_image_file = SimpleUploadedFile("test.jpg", img_bytes.read(), content_type="image/jpeg")

    def test_post_missing_image(self):
        response = self.client.post(self.url, data={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertEqual(response.json()["error"], "No image file provided. Please send an image file under the key 'image'.")

    def test_post_invalid_image_file(self):
        invalid_file = SimpleUploadedFile("test.txt", b"not-an-image-data", content_type="text/plain")
        response = self.client.post(self.url, {"image": invalid_file})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertTrue(response.json()["error"].startswith("Invalid image file."))

    @patch("api.views.get_detector")
    def test_post_valid_image_success(self, mock_get_detector):
        mock_detector = MagicMock()
        mock_detector.analyze_uploaded_file.return_value = {
            "status": "success",
            "blockage_percentage": 25.0,
            "detected_objects": ["plastic bottle"],
            "water_flow_status": "Restricted",
            "vision_confidence": "High",
            "reasoning": "A bottle partially covers the intake."
        }
        mock_get_detector.return_value = mock_detector

        response = self.client.post(self.url, {"image": self.valid_image_file})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["blockage_ratio"], 0.25)
        self.assertEqual(data["blockage_percentage"], 25.0)
        self.assertIn("computed_risk_score", data)
        self.assertIn("flood_risk_level", data)
        self.assertIn("dispatch_action", data)

    @patch("api.views.get_detector")
    def test_post_detector_error_handling(self, mock_get_detector):
        mock_detector = MagicMock()
        mock_detector.analyze_uploaded_file.return_value = {
            "status": "error",
            "error": "Quota exceeded",
            "blockage_percentage": 0.0,
        }
        mock_get_detector.return_value = mock_detector

        response = self.client.post(self.url, {"image": self.valid_image_file})
        self.assertEqual(response.status_code, 500)
        self.assertIn("error", response.json())
        self.assertIn("Quota exceeded", response.json()["error"])


# =====================================================================
# 5. FLOOD RISK ASSESSMENT VIEW TESTS
# =====================================================================

class FloodRiskAssessmentViewTests(TestCase):
    def setUp(self):
        self.url = reverse("flood_risk_assessment")

    def test_post_missing_latitude_or_longitude(self):
        response = self.client.post(self.url, {"latitude": 18.5, "blockage_ratio": 0.5}, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Both 'latitude' and 'longitude' are required.")

        response = self.client.post(self.url, {"longitude": 73.2, "blockage_ratio": 0.5}, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Both 'latitude' and 'longitude' are required.")

    def test_post_invalid_blockage_ratio_value(self):
        response = self.client.post(
            self.url, 
            {"latitude": 18.5, "longitude": 73.2, "blockage_ratio": "highly_blocked"}, 
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Invalid blockage_ratio value.")

    @patch("api.views.fetch_live_rainfall")
    def test_post_weather_service_gateway_error(self, mock_fetch):
        mock_fetch.return_value = {"success": False, "error": "API timeout connection details"}
        
        response = self.client.post(
            self.url, 
            {"latitude": 18.5, "longitude": 73.2, "blockage_ratio": 0.4}, 
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "Failed to fetch environmental metrics.")
        self.assertEqual(response.json()["details"], "API timeout connection details")

    @patch("api.views.fetch_live_rainfall")
    def test_post_blockage_ratio_clipping_logic(self, mock_fetch):
        mock_fetch.return_value = {
            "success": True,
            "rainfall_mm": 0.0,
            "humidity": 50,
            "temperature": 25.0
        }

        # Case A: blockage_ratio < 0 (should clip to 0.0)
        response = self.client.post(
            self.url, 
            {"latitude": 18.5, "longitude": 73.2, "blockage_ratio": -0.7}, 
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["risk_analysis"]["input_blockage_ratio"], 0.0)

        # Case B: blockage_ratio > 1 (should clip to 1.0)
        response = self.client.post(
            self.url, 
            {"latitude": 18.5, "longitude": 73.2, "blockage_ratio": 1.9}, 
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["risk_analysis"]["input_blockage_ratio"], 1.0)

    @patch("api.views.fetch_live_rainfall")
    def test_joint_risk_score_matrix_math(self, mock_fetch):
        scenarios = [
            (0.0, 0.0, "LOW", 0.0, 0.05),
            (0.3, 10.0, "ELEVATED", 0.47, 0.50),
            (0.5, 25.0, "CRITICAL", 0.77, 0.80),
            (1.0, 10.0, "CRITICAL", 0.99, 1.00),
            (0.2, 60.0, "CRITICAL", 0.99, 1.00)
        ]

        for blockage, rainfall, expected_level, min_score, max_score in scenarios:
            mock_fetch.return_value = {
                "success": True,
                "rainfall_mm": rainfall,
                "humidity": 70,
                "temperature": 27.0
            }
            
            response = self.client.post(
                self.url, 
                {"latitude": 18.5, "longitude": 73.2, "blockage_ratio": blockage}, 
                content_type="application/json"
            )
            self.assertEqual(response.status_code, 200)
            
            risk_analysis = response.json()["risk_analysis"]
            score = risk_analysis["computed_risk_score"]
            level = risk_analysis["flood_risk_level"]
            
            self.assertEqual(level, expected_level)
            self.assertTrue(min_score <= score <= max_score, f"Score {score} not between {min_score} and {max_score} for blockage={blockage}, rain={rainfall}")


# =====================================================================
# 6. DRAIN MODEL AND ENDPOINT TESTS
# =====================================================================

class DrainModelAndEndpointTests(TestCase):
    def setUp(self):
        self.drain = Drain.objects.create(
            drain_id="D-TEST",
            name="Test Drain Location",
            area_name="Test Drain Location",
            approx_area="Test Drain Location",
            latitude=18.5204,
            longitude=73.8567,
            risk_level="LOW",
            blockage_ratio=0.1,
            current_status="Normal"
        )
        self.inspection = Inspection.objects.create(
            drain=self.drain,
            previous_status="Initial",
            new_status="Normal",
            remarks="Initial inspection",
            blockage_ratio=0.1,
            risk_level="LOW",
            status="Cleared"
        )

    def test_drain_str(self):
        self.assertIn("D-TEST", str(self.drain))
        self.assertIn("Test Drain Location", str(self.drain))

    def test_inspection_str(self):
        self.assertIn("D-TEST", str(self.inspection))

    def test_drain_list_view(self):
        response = self.client.get(reverse("drain-list"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(len(data) >= 1)
        found = any(d["drainId"] == "D-TEST" for d in data)
        self.assertTrue(found)

    def test_drain_list_filtering(self):
        # Filter by name
        res_name = self.client.get(reverse("drain-list"), {"name": "Sangamwadi"})
        self.assertEqual(res_name.status_code, 200)
        data = res_name.json()
        self.assertTrue(len(data) >= 1)
        self.assertEqual(data[0]["drainId"], "D-01")

        # Filter by location / area
        res_loc = self.client.get(reverse("drain-list"), {"location": "Koregaon"})
        self.assertEqual(res_loc.status_code, 200)
        self.assertTrue(any(d["drainId"] == "D-03" for d in res_loc.json()))

        # Filter by drainId
        res_id = self.client.get(reverse("drain-list"), {"drainId": "D-02"})
        self.assertEqual(res_id.status_code, 200)
        self.assertEqual(len(res_id.json()), 1)
        self.assertEqual(res_id.json()[0]["name"], "Shivajinagar Central")

    def test_reference_master_drain_d01_sangamwadi(self):
        response = self.client.get(reverse("drain-detail", kwargs={"identifier": "D-01"}))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["drainId"], "D-01")
        self.assertEqual(data["name"], "Sangamwadi Drain")
        self.assertEqual(data["latitude"], 18.5416)
        self.assertEqual(data["longitude"], 73.8734)
        self.assertEqual(data["approxArea"], "Sangamwadi")
        self.assertEqual(data["currentStatus"], "Normal")

    def test_drain_detail_view_found_by_integer_and_drain_id(self):
        # By string drain_id
        res1 = self.client.get(reverse("drain-detail", kwargs={"identifier": "D-TEST"}))
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.json()["drainId"], "D-TEST")

        # By integer database id
        res2 = self.client.get(reverse("drain-detail", kwargs={"identifier": str(self.drain.id)}))
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json()["id"], self.drain.id)

    def test_drain_detail_view_not_found(self):
        response = self.client.get(reverse("drain-detail", kwargs={"identifier": "D-99999"}))
        self.assertEqual(response.status_code, 404)

    def test_drain_current_status_retrieval_and_update(self):
        # 1. Get initial status
        res_get = self.client.get(reverse("drain-status", kwargs={"identifier": "D-01"}))
        self.assertEqual(res_get.status_code, 200)
        self.assertEqual(res_get.json()["currentStatus"], "Normal")

        # 2. Update status with remarks
        update_payload = {
            "currentStatus": "Blocked",
            "remarks": "Water accumulation detected",
            "condition": "Restricted flow",
            "blockage_ratio": 0.75
        }
        res_update = self.client.post(
            reverse("drain-status", kwargs={"identifier": "D-01"}),
            update_payload,
            content_type="application/json"
        )
        self.assertEqual(res_update.status_code, 200)
        update_data = res_update.json()
        self.assertEqual(update_data["previousStatus"], "Normal")
        self.assertEqual(update_data["currentStatus"], "Blocked")
        self.assertEqual(update_data["risk_level"], "CRITICAL")
        self.assertEqual(update_data["historyEntry"]["remarks"], "Water accumulation detected")

        # 3. Verify status changed in database
        drain = Drain.objects.get(drain_id="D-01")
        self.assertEqual(drain.current_status, "Blocked")
        self.assertEqual(drain.risk_level, "CRITICAL")

        # 4. Verify history has recorded the change
        res_hist = self.client.get(reverse("drain-history", kwargs={"identifier": "D-01"}))
        self.assertEqual(res_hist.status_code, 200)
        history = res_hist.json()["history"]
        self.assertTrue(len(history) >= 2)
        latest_entry = history[0]
        self.assertEqual(latest_entry["previousStatus"], "Normal")
        self.assertEqual(latest_entry["newStatus"], "Blocked")
        self.assertEqual(latest_entry["remarks"], "Water accumulation detected")

    def test_drain_history_isolation_between_drains(self):
        # Update D-01
        self.client.post(
            reverse("drain-status", kwargs={"identifier": "D-01"}),
            {"currentStatus": "Blocked", "remarks": "Debris lodged in D-01 grate"},
            content_type="application/json"
        )

        # Update D-02
        self.client.post(
            reverse("drain-status", kwargs={"identifier": "D-02"}),
            {"currentStatus": "Elevated", "remarks": "High silt deposit in D-02"},
            content_type="application/json"
        )

        # Verify D-01 history does NOT contain D-02 remarks
        d1_hist = self.client.get(reverse("drain-history", kwargs={"identifier": "D-01"})).json()["history"]
        d1_remarks = [h["remarks"] for h in d1_hist]
        self.assertIn("Debris lodged in D-01 grate", d1_remarks)
        self.assertNotIn("High silt deposit in D-02", d1_remarks)

        # Verify D-02 history does NOT contain D-01 remarks
        d2_hist = self.client.get(reverse("drain-history", kwargs={"identifier": "D-02"})).json()["history"]
        d2_remarks = [h["remarks"] for h in d2_hist]
        self.assertIn("High silt deposit in D-02", d2_remarks)
        self.assertNotIn("Debris lodged in D-01 grate", d2_remarks)

    def test_add_new_drain_dynamic(self):
        payload = {
            "drainId": "D-11",
            "name": "Yerawada East Drain",
            "latitude": 18.5529,
            "longitude": 73.8903,
            "approxArea": "Yerawada",
            "remarks": "New test drain installation"
        }
        response = self.client.post(
            reverse("drain-list"),
            payload,
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["drainId"], "D-11")
        self.assertEqual(data["name"], "Yerawada East Drain")
        self.assertEqual(data["latitude"], 18.5529)
        self.assertEqual(data["longitude"], 73.8903)
        self.assertEqual(data["approxArea"], "Yerawada")
        self.assertEqual(data["currentStatus"], "Normal")

        # Verify it can be retrieved via detail
        detail_res = self.client.get(reverse("drain-detail", kwargs={"identifier": "D-11"}))
        self.assertEqual(detail_res.status_code, 200)

        # Verify initial history exists
        hist_res = self.client.get(reverse("drain-history", kwargs={"identifier": "D-11"}))
        self.assertEqual(hist_res.status_code, 200)
        self.assertEqual(len(hist_res.json()["history"]), 1)

    def test_add_drain_validation_duplicate_id(self):
        payload = {
            "drainId": "D-01",  # already exists in seeded master data
            "name": "Duplicate Sangamwadi",
            "latitude": 18.5416,
            "longitude": 73.8734,
            "approxArea": "Sangamwadi"
        }
        response = self.client.post(reverse("drain-list"), payload, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("already exists", response.json()["error"])

    def test_add_drain_validation_coordinates_out_of_bounds(self):
        # Latitude > 90
        res1 = self.client.post(
            reverse("drain-list"),
            {"drainId": "D-901", "name": "Invalid Lat", "latitude": 95.0, "longitude": 73.0, "approxArea": "Test"},
            content_type="application/json"
        )
        self.assertEqual(res1.status_code, 400)
        self.assertIn("Latitude must be between -90 and 90", res1.json()["error"])

        # Latitude < -90
        res2 = self.client.post(
            reverse("drain-list"),
            {"drainId": "D-902", "name": "Invalid Lat", "latitude": -95.0, "longitude": 73.0, "approxArea": "Test"},
            content_type="application/json"
        )
        self.assertEqual(res2.status_code, 400)
        self.assertIn("Latitude must be between -90 and 90", res2.json()["error"])

        # Longitude > 180
        res3 = self.client.post(
            reverse("drain-list"),
            {"drainId": "D-903", "name": "Invalid Lon", "latitude": 18.0, "longitude": 185.0, "approxArea": "Test"},
            content_type="application/json"
        )
        self.assertEqual(res3.status_code, 400)
        self.assertIn("Longitude must be between -180 and 180", res3.json()["error"])

        # Non-numeric coordinate
        res4 = self.client.post(
            reverse("drain-list"),
            {"drainId": "D-904", "name": "Bad Coord", "latitude": "invalid", "longitude": 73.0, "approxArea": "Test"},
            content_type="application/json"
        )
        self.assertEqual(res4.status_code, 400)
        self.assertIn("numeric value", res4.json()["error"])

    def test_add_drain_validation_missing_required_fields(self):
        # Missing approxArea
        res = self.client.post(
            reverse("drain-list"),
            {"drainId": "D-905", "name": "Missing Area", "latitude": 18.0, "longitude": 73.0},
            content_type="application/json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Missing required fields", res.json()["error"])

    def test_update_status_on_non_existent_drain(self):
        response = self.client.post(
            reverse("drain-status", kwargs={"identifier": "D-DOESNOTEXIST"}),
            {"currentStatus": "Blocked"},
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 404)

    def test_add_history_entry_endpoint(self):
        payload = {
            "newStatus": "Warning",
            "remarks": "Quarterly manual inspection completed",
            "condition": "Moderate debris accumulation"
        }
        response = self.client.post(
            reverse("drain-history", kwargs={"identifier": "D-03"}),
            payload,
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["newStatus"], "Warning")
        self.assertEqual(data["remarks"], "Quarterly manual inspection completed")

        # Verify drain status updated
        drain = Drain.objects.get(drain_id="D-03")
        self.assertEqual(drain.current_status, "Warning")

    def test_drain_history_legacy_list_format(self):
        response = self.client.get(f"{reverse('drain-history', kwargs={'identifier': 'D-TEST'})}?style=list")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertTrue(len(data) >= 1)
        self.assertEqual(data[0]["status"], "Cleared")

