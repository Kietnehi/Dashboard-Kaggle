import os
import sys
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure test uses virtual environment and kaggle_runtime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kaggle_service import monitor_service
from main import app
from fastapi.testclient import TestClient

class TestDatasetFeatures(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_check_dataset_slug_available(self):
        # Existing dataset in cache should return available: False
        res = monitor_service.check_dataset_slug_available("lifemate-asr-cache", "kitnehi1211")
        self.assertFalse(res["available"])
        self.assertIn("đã có", res["message"])

        # Completely random new slug should check kaggle status
        with patch.object(monitor_service, "get_credential_by_username", return_value={"username": "kitnehi1211", "key": "fake"}):
            with patch("kaggle_service.KaggleApi") as mock_api_cls:
                mock_api = MagicMock()
                mock_api_cls.return_value = mock_api
                # status raises 404
                mock_api.dataset_status.side_effect = Exception("404 Not Found")
                res = monitor_service.check_dataset_slug_available("new-unique-dataset-xyz-123", "kitnehi1211")
                self.assertTrue(res["available"])
                self.assertIn("hợp lệ", res["message"])

    def test_create_dataset_validation_no_files(self):
        # Empty file tuples and no local folder
        res = monitor_service.create_dataset(
            title="Valid Dataset Title",
            slug="valid-dataset-slug",
            is_private=True,
            file_tuples=[],
            local_folder_path=""
        )
        self.assertFalse(res["success"])
        self.assertIn("Cần ít nhất", res["message"])

    def test_create_dataset_validation_invalid_folder(self):
        res = monitor_service.create_dataset(
            title="Valid Dataset Title",
            slug="valid-dataset-slug",
            is_private=True,
            local_folder_path="/non_existent_folder_path_9999"
        )
        self.assertFalse(res["success"])
        self.assertIn("không tồn tại", res["message"])

    def test_create_dataset_success_with_files(self):
        # Test creating dataset with mock KaggleApi
        sample_file_content = b"col1,col2\nval1,val2\n"
        file_tuples = [("data.csv", sample_file_content)]

        with patch.object(monitor_service, "get_credential_by_username", return_value={"username": "kitnehi1211", "key": "fake"}):
            with patch("kaggle_service.KaggleApi") as mock_api_cls:
                mock_api = MagicMock()
                mock_api_cls.return_value = mock_api
                mock_resp = MagicMock()
                mock_resp.error = None
                mock_resp.status = "ok"
                mock_api.dataset_create_new.return_value = mock_resp

                with patch.object(monitor_service, "save_cache", return_value=True):
                    res = monitor_service.create_dataset(
                        title="Speech Recognition Vietnamese",
                        slug="speech-recognition-vietnamese",
                        is_private=True,
                        file_tuples=file_tuples,
                        target_account="kitnehi1211"
                    )
                    self.assertTrue(res["success"])
                    self.assertEqual(res["ref"], "kitnehi1211/speech-recognition-vietnamese")
                    self.assertTrue(res["is_private"])
                    self.assertIn("kitnehi1211", res["url"])

                    # Verify new entry was prepended to cache
                    first_ds = monitor_service.data["all_datasets"][0]
                    self.assertEqual(first_ds["ref"], "kitnehi1211/speech-recognition-vietnamese")
                    self.assertTrue(first_ds["is_private"])
                    self.assertEqual(first_ds["account"], "kitnehi1211")

    def test_create_dataset_public_mode(self):
        sample_file_content = b"col1,col2\nval1,val2\n"
        file_tuples = [("data.csv", sample_file_content)]

        with patch.object(monitor_service, "get_credential_by_username", return_value={"username": "kitnehi1211", "key": "fake"}):
            with patch("kaggle_service.KaggleApi") as mock_api_cls:
                mock_api = MagicMock()
                mock_api_cls.return_value = mock_api
                mock_resp = MagicMock()
                mock_resp.error = None
                mock_resp.status = "ok"
                mock_api.dataset_create_new.return_value = mock_resp

                with patch.object(monitor_service, "save_cache", return_value=True):
                    res = monitor_service.create_dataset(
                        title="Public Benchmark Data",
                        slug="public-benchmark-data",
                        is_private=False,
                        file_tuples=file_tuples,
                        target_account="kitnehi1211"
                    )
                    self.assertTrue(res["success"])
                    self.assertFalse(res["is_private"])
                    # Ensure public=True was passed to KaggleApi
                    mock_api.dataset_create_new.assert_called_once()
                    _, kwargs = mock_api.dataset_create_new.call_args
                    self.assertTrue(kwargs.get("public"))

    def test_create_dataset_auto_share_all(self):
        sample_file_content = b"col1,col2\nval1,val2\n"
        file_tuples = [("data.csv", sample_file_content)]

        with patch.object(monitor_service, "get_credential_by_username", return_value={"username": "kitnehi1211", "key": "fake"}):
            with patch("kaggle_service.KaggleApi") as mock_api_cls:
                mock_api = MagicMock()
                mock_api_cls.return_value = mock_api
                mock_resp = MagicMock()
                mock_resp.error = None
                mock_resp.status = "ok"
                mock_api.dataset_create_new.return_value = mock_resp

                with patch.object(monitor_service, "get_all_account_usernames", return_value=["kitnehi1211", "baovuong3009", "justb2464"]):
                    with patch.object(monitor_service, "share_dataset_collaborators", return_value={"success": True, "collaborators": ["baovuong3009", "justb2464"]}) as mock_share:
                        with patch.object(monitor_service, "save_cache", return_value=True):
                            res = monitor_service.create_dataset(
                                title="Auto Shared Training Data",
                                slug="auto-shared-training-data",
                                is_private=True,
                                file_tuples=file_tuples,
                                target_account="kitnehi1211",
                                auto_share_all=True
                            )
                            self.assertTrue(res["success"])
                            self.assertEqual(res["collaborators"], ["baovuong3009", "justb2464"])
                            self.assertIn("tài khoản trong pool", res["message"])
                            mock_share.assert_called_once()

    def test_update_dataset_privacy(self):
        with patch.object(monitor_service, "get_credential_by_username", return_value={"username": "kitnehi1211", "key": "fake"}):
            with patch("kaggle_service.KaggleApi") as mock_api_cls:
                mock_api = MagicMock()
                mock_api_cls.return_value = mock_api
                mock_client = MagicMock()
                mock_api.build_kaggle_client.return_value.__enter__.return_value = mock_client

                # Mock get_dataset_metadata
                mock_get_res = MagicMock()
                mock_get_res.info.title = "lifemate-asr-cache"
                mock_get_res.info.licenses = [MagicMock(name="other")]
                mock_client.datasets.dataset_api_client.get_dataset_metadata.return_value = mock_get_res

                # Mock update_dataset_metadata response
                mock_update_res = MagicMock()
                mock_update_res.errors = []
                mock_client.datasets.dataset_api_client.update_dataset_metadata.return_value = mock_update_res

                with patch.object(monitor_service, "save_cache", return_value=True):
                    # Step 1: Change Private dataset to Public (allowed)
                    res = monitor_service.update_dataset_privacy("kitnehi1211/lifemate-asr-cache", is_private=False)
                    self.assertTrue(res["success"])
                    self.assertFalse(res["is_private"])
                    self.assertIn("Public", res["message"])

                    # Step 2: Attempting to rollback Public dataset back to Private must be BLOCKED
                    # according to Kaggle policy (one-way visibility)
                    mock_get_res.info.is_private = False
                    res2 = monitor_service.update_dataset_privacy("kitnehi1211/lifemate-asr-cache", is_private=True)
                    self.assertFalse(res2["success"])
                    self.assertIn("không thể chuyển ngược lại về Private", res2["message"])

    def test_api_endpoints(self):
        # 1. Test check-slug endpoint
        resp = self.client.get("/api/datasets/check-slug?slug=lifemate-asr-cache")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["available"])

        # 2. Test set-privacy endpoint
        with patch.object(monitor_service, "update_dataset_privacy", return_value={"success": True, "is_private": False, "message": "OK"}):
            resp = self.client.post("/api/datasets/set-privacy", json={"ref": "kitnehi1211/test", "is_private": False})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["success"])

        # 3. Test create endpoint via multipart form
        with patch.object(monitor_service, "create_dataset", return_value={"success": True, "ref": "kitnehi1211/api-test", "url": "https://kaggle.com", "is_private": True}):
            resp = self.client.post(
                "/api/datasets/create",
                data={
                    "title": "API Test Dataset",
                    "slug": "api-test-dataset",
                    "is_private": "true",
                    "target_account": "kitnehi1211"
                },
                files=[
                    ("files", ("test.csv", b"id,val\n1,2\n", "text/csv"))
                ]
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["success"])

        # 4. Test create-local endpoint via JSON
        with patch.object(monitor_service, "create_dataset", return_value={"success": True, "ref": "kitnehi1211/local-test", "url": "https://kaggle.com", "is_private": False}):
            resp = self.client.post(
                "/api/datasets/create-local",
                json={
                    "title": "Local Test Dataset",
                    "slug": "local-test-dataset",
                    "is_private": False,
                    "local_folder_path": "/fake/path"
                }
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["success"])

        # 5. Test get collaborators endpoint
        with patch.object(monitor_service, "get_dataset_collaborators", return_value={"success": True, "owner": "kitnehi1211", "slug": "test", "collaborators": [{"username": "baovuong3009", "role": "WRITER"}]}):
            resp = self.client.get("/api/datasets/collaborators?ref=kitnehi1211/test")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["success"])
            self.assertEqual(len(data["collaborators"]), 1)

    def test_share_dataset_merging_collaborators(self):
        with patch.object(monitor_service, "get_credential_by_username", return_value={"username": "kitnehi1211", "key": "fake"}):
            with patch("kaggle_service.KaggleApi") as mock_api_cls:
                mock_api = MagicMock()
                mock_api_cls.return_value = mock_api
                mock_client = MagicMock()
                mock_api.build_kaggle_client.return_value.__enter__.return_value = mock_client

                # Existing collaborator: baovuong3009
                existing_c = MagicMock()
                existing_c.username = "baovuong3009"
                existing_c.role = "WRITER"

                mock_get_res = MagicMock()
                mock_get_res.info.title = "test-dataset"
                mock_get_res.info.collaborators = [existing_c]
                mock_get_res.info.licenses = [MagicMock(name="CC0-1.0")]
                mock_client.datasets.dataset_api_client.get_dataset_metadata.return_value = mock_get_res

                mock_update_res = MagicMock()
                mock_update_res.errors = []
                mock_client.datasets.dataset_api_client.update_dataset_metadata.return_value = mock_update_res

                with patch.object(monitor_service, "save_cache", return_value=True):
                    # Add new collaborator: justb2464
                    res = monitor_service.share_dataset_collaborators(
                        owner_account="kitnehi1211",
                        dataset_slug="test-dataset",
                        collaborator_usernames=["justb2464"],
                        role="WRITER"
                    )
                    self.assertTrue(res["success"])
                    # Ensure both baovuong3009 and justb2464 are preserved!
                    self.assertIn("baovuong3009", res["collaborators"])
                    self.assertIn("justb2464", res["collaborators"])

if __name__ == "__main__":
    unittest.main()
