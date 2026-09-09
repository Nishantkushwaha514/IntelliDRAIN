import os
import io
import math
from unittest.mock import patch, MagicMock
from requests.exceptions import RequestException

from django.test import TestCase
from django.urls import reverse
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from api.detector import calculate_blockage_and_risk
from api.services import fetch_live_rainfall

# =====================================================================
# HELPERS FOR MOCKING YOLO RESULTS
# =====================================================================

class MockTensor:
    def __init__(self, values):
        self.values = values

    def tolist(self):
        return self.values


class MockBox:
    def __init__(self, cls_id, conf, xyxy_coords):
        self.cls = [cls_id]
        self.conf = [conf]
        self.xyxy = [MockTensor(xyxy_coords)]


class MockResult:
    def __init__(self, boxes):
        self.boxes = boxes


# =====================================================================
# 1. DETECTOR ENGINE TESTS
# =====================================================================

class DetectorTests(TestCase):
    def setUp(self):
        # Create a simple dummy PIL image for tests (100 x 100 pixels)
        self.dummy_image = Image.new("RGB", (100, 100), color="white")

    @patch("api.detector.get_yolo_model")
    def test_calculate_blockage_no_detections(self, mock_get_model):
        """
        Verify that when no objects are detected, blockage is 0% and risk is Low.
        """
        mock_model = MagicMock()
        mock_model.names = {0: "bottle", 1: "cup", 2: "handbag", 3: "bowl"}
        mock_model.return_value = [MockResult([])]
        mock_get_model.return_value = mock_model

        result = calculate_blockage_and_risk(self.dummy_image)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["blockage_percentage"], 0.0)
        self.assertEqual(result["flood_risk"], "Low")
        self.assertEqual(result["total_garbage_area_px"], 0.0)
        self.assertEqual(result["total_drain_area_px"], 10000)
        self.assertEqual(len(result["detections"]), 0)

    @patch("api.detector.get_yolo_model")
    def test_calculate_blockage_non_garbage_detections(self, mock_get_model):
        """
        Verify that detected objects not in the GARBAGE_CLASSES list are ignored.
        """
        mock_model = MagicMock()
        mock_model.names = {0: "bottle", 4: "person", 5: "car"}
        # Person detected (not garbage)
        mock_box = MockBox(cls_id=4, conf=0.9, xyxy_coords=[0, 0, 50, 50])
        mock_model.return_value = [MockResult([mock_box])]
        mock_get_model.return_value = mock_model

        result = calculate_blockage_and_risk(self.dummy_image)
        self.assertEqual(result["blockage_percentage"], 0.0)
        self.assertEqual(result["flood_risk"], "Low")
        self.assertEqual(len(result["detections"]), 0)

    @patch("api.detector.get_yolo_model")
    def test_calculate_blockage_with_garbage(self, mock_get_model):
        """
        Verify correct blockage calculation when target garbage items are detected.
        """
        mock_model = MagicMock()
        mock_model.names = {0: "bottle", 1: "cup"}
        # Bottle: 20x30 = 600 px area
        box1 = MockBox(cls_id=0, conf=0.85, xyxy_coords=[10, 10, 30, 40])
        # Cup: 10x10 = 100 px area
        box2 = MockBox(cls_id=1, conf=0.90, xyxy_coords=[50, 50, 60, 60])
        mock_model.return_value = [MockResult([box1, box2])]
        mock_get_model.return_value = mock_model

        # Image is 100x100 = 10000 px area. Expected blockage = (700 / 10000) * 100 = 7.0%
        result = calculate_blockage_and_risk(self.dummy_image)
        self.assertEqual(result["blockage_percentage"], 7.00)
        self.assertEqual(result["flood_risk"], "Low")
        self.assertEqual(result["total_garbage_area_px"], 700.0)
        self.assertEqual(len(result["detections"]), 2)
        self.assertEqual(result["detections"][0]["class"], "bottle")
        self.assertEqual(result["detections"][0]["confidence"], 0.85)

    @patch("api.detector.get_yolo_model")
    def test_calculate_blockage_capping_at_100(self, mock_get_model):
        """
        Verify that total blockage percentage is capped at 100% if detected areas exceed image area.
        """
        mock_model = MagicMock()
        mock_model.names = {0: "bottle"}
        # Bottle: 120x120 = 14400 px area (greater than 10000 px image area)
        box1 = MockBox(cls_id=0, conf=0.95, xyxy_coords=[0, 0, 120, 120])
        mock_model.return_value = [MockResult([box1])]
        mock_get_model.return_value = mock_model

        result = calculate_blockage_and_risk(self.dummy_image)
        self.assertEqual(result["blockage_percentage"], 100.0)
        self.assertEqual(result["flood_risk"], "Critical")

    @patch("api.detector.get_yolo_model")
    def test_calculate_blockage_risk_thresholds(self, mock_get_model):
        """
        Verify that risk categories map correctly based on blockage threshold ranges.
        """
        mock_model = MagicMock()
        mock_model.names = {0: "bottle"}
        mock_get_model.return_value = mock_model

        thresholds = [
            ([0, 0, 100, 24], "Low"),       # 24% blockage
            ([0, 0, 100, 25], "Moderate"),  # 25% blockage
            ([0, 0, 100, 49], "Moderate"),  # 49% blockage
            ([0, 0, 100, 50], "High"),      # 50% blockage
            ([0, 0, 100, 74], "High"),      # 74% blockage
            ([0, 0, 100, 75], "Critical"),  # 75% blockage
        ]

        for coords, expected_risk in thresholds:
            box = MockBox(cls_id=0, conf=0.90, xyxy_coords=coords)
            mock_model.return_value = [MockResult([box])]
            result = calculate_blockage_and_risk(self.dummy_image)
            self.assertEqual(result["flood_risk"], expected_risk, f"Coords {coords} failed. Expected {expected_risk}.")


# =====================================================================
# 2. WEATHER SERVICE TESTS
# =====================================================================

class WeatherServiceTests(TestCase):
    @patch("requests.get")
    def test_fetch_live_rainfall_success(self, mock_get):
        """
        Verify weather details parsing when OpenWeatherMap API returns a success.
        """
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
        """
        Verify fallback to 0.0mm when 'rain' key is absent in API response.
        """
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
        """
        Verify that 401 Unauthorized API responses trigger local mock development fallback.
        """
        mock_response = MagicMock()
        mock_response.status_code = 401
        # requests raise_for_status throws HTTPError
        from requests.exceptions import HTTPError
        mock_response.raise_for_status.side_effect = HTTPError("Unauthorized access")
        mock_get.return_value = mock_response

        res = fetch_live_rainfall(19.0, 73.0)
        self.assertTrue(res["success"])
        self.assertEqual(res["rainfall_mm"], 12.5)  # fallback constant
        self.assertEqual(res["humidity"], 85)       # fallback constant
        self.assertEqual(res["temperature"], 26.5)  # fallback constant
        self.assertIn("note", res)

    @patch("requests.get")
    def test_fetch_live_rainfall_network_exception_failure(self, mock_get):
        """
        Verify failure return format when there's an actual network exception (non-401).
        """
        # Configure API key to NOT be default placeholder to bypass the placeholder check in services
        with patch.dict(os.environ, {"OPENWEATHER_API_KEY": "REAL_KEY_FOR_TEST"}):
            mock_get.side_effect = RequestException("Connection timeout")
            res = fetch_live_rainfall(19.0, 73.0)
            self.assertFalse(res["success"])
            self.assertEqual(res["error"], "Connection timeout")


# =====================================================================
# 3. BLOCKAGE DETECTION VIEW TESTS
# =====================================================================

class BlockageDetectionViewTests(TestCase):
    def setUp(self):
        self.url = reverse("blockage-detection")

        # Generate a small valid GIF image in memory
        img = Image.new("RGB", (50, 50), color="blue")
        img_bytes = io.BytesIO()
        img.save(img_bytes, format="JPEG")
        img_bytes.seek(0)
        self.valid_image_file = SimpleUploadedFile("test.jpg", img_bytes.read(), content_type="image/jpeg")

    def test_post_missing_image(self):
        """
        Ensure sending POST without an 'image' file returns 400 Bad Request.
        """
        response = self.client.post(self.url, data={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertEqual(response.json()["error"], "No image file provided. Please send an image file under the key 'image'.")

    def test_post_invalid_image_file(self):
        """
        Ensure sending an invalid non-image file returns 400 Bad Request.
        """
        invalid_file = SimpleUploadedFile("test.txt", b"not-an-image-data", content_type="text/plain")
        response = self.client.post(self.url, {"image": invalid_file})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertTrue(response.json()["error"].startswith("Invalid image file."))

    @patch("api.views.calculate_blockage_and_risk")
    def test_post_valid_image_success(self, mock_calc):
        """
        Ensure post success returns HTTP 200 with standard results structure.
        """
        mock_result = {
            "status": "success",
            "blockage_percentage": 15.5,
            "flood_risk": "Low",
            "total_garbage_area_px": 38750.0,
            "total_drain_area_px": 250000,
            "detections": []
        }
        mock_calc.return_value = mock_result

        response = self.client.post(self.url, {"image": self.valid_image_file})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), mock_result)

    @patch("api.views.calculate_blockage_and_risk")
    def test_post_detector_error_handling(self, mock_calc):
        """
        Ensure exceptions raised inside detector are captured and return HTTP 500.
        """
        mock_calc.side_effect = Exception("GPU out of memory")
        response = self.client.post(self.url, {"image": self.valid_image_file})
        self.assertEqual(response.status_code, 500)
        self.assertIn("error", response.json())
        self.assertEqual(response.json()["error"], "An error occurred during object detection: GPU out of memory")

    def test_post_valid_image_integration_real_model(self):
        """
        Integration test verifying end-to-end execution of YOLOv8 engine on the actual test image.
        """
        image_path = os.path.join(settings.BASE_DIR, "test_image.jpg")
        model_path = os.path.join(settings.BASE_DIR, "yolov8n.pt")

        if not os.path.exists(image_path) or not os.path.exists(model_path):
            self.skipTest("Missing yolov8n.pt weights or test_image.jpg in project directory. Skipping integration test.")

        with open(image_path, "rb") as img_file:
            uploaded_image = SimpleUploadedFile("test_image.jpg", img_file.read(), content_type="image/jpeg")

        response = self.client.post(self.url, {"image": uploaded_image})
        self.assertEqual(response.status_code, 200)
        
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("blockage_percentage", data)
        self.assertIn("flood_risk", data)
        self.assertIn("total_garbage_area_px", data)
        self.assertIn("total_drain_area_px", data)
        self.assertIn("detections", data)


# =====================================================================
# 4. FLOOD RISK ASSESSMENT VIEW TESTS
# =====================================================================

class FloodRiskAssessmentViewTests(TestCase):
    def setUp(self):
        self.url = reverse("flood_risk_assessment")

    def test_post_missing_latitude_or_longitude(self):
        """
        Ensure POST without latitude or longitude returns 400 Bad Request.
        """
        # Missing longitude
        response = self.client.post(self.url, {"latitude": 18.5, "blockage_ratio": 0.5}, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Both 'latitude' and 'longitude' are required.")

        # Missing latitude
        response = self.client.post(self.url, {"longitude": 73.2, "blockage_ratio": 0.5}, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Both 'latitude' and 'longitude' are required.")

    def test_post_invalid_blockage_ratio_value(self):
        """
        Ensure POST with non-numeric blockage_ratio returns 400 Bad Request.
        """
        response = self.client.post(
            self.url, 
            {"latitude": 18.5, "longitude": 73.2, "blockage_ratio": "highly_blocked"}, 
            content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Invalid blockage_ratio value.")

    @patch("api.views.fetch_live_rainfall")
    def test_post_weather_service_gateway_error(self, mock_fetch):
        """
        Ensure 502 Bad Gateway is returned when weather service reports a failure.
        """
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
        """
        Verify that out-of-bound blockage ratios are clipped strictly to [0.0, 1.0].
        """
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
        """
        Assert custom non-linear predictive index formulas under specific joint rain + blockage values.
        """
        # Define scenarios: (blockage, rainfall) -> expected risk level & minimum score range
        # Formula: risk_score = 1.0 - ((1.0 - blockage_ratio)^1.2 * (1.0 - rainfall_intensity)^1.0)
        # where rainfall_intensity = min(rainfall_mm / 50.0, 1.0)
        
        scenarios = [
            # 1. Low blockage (0.0) and No rain (0.0mm) -> score = 0.0 (LOW)
            (0.0, 0.0, "LOW", 0.0, 0.05),
            
            # 2. Moderate blockage (0.3) and Moderate rain (10.0mm -> intensity = 0.2)
            # score = 1.0 - (0.7^1.2 * 0.8) = 1.0 - (0.65179 * 0.8) = 1.0 - 0.5214 = 0.479 (ELEVATED)
            (0.3, 10.0, "ELEVATED", 0.47, 0.49),
            
            # 3. High blockage (0.5) and High rain (25.0mm -> intensity = 0.5)
            # score = 1.0 - (0.5^1.2 * 0.5) = 1.0 - (0.435275 * 0.5) = 1.0 - 0.2176 = 0.782 (CRITICAL)
            (0.5, 25.0, "CRITICAL", 0.77, 0.79),
            
            # 4. Maximum limits: Blockage (1.0) or extreme rain (>=50.0mm) -> score = 1.0 (CRITICAL)
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
