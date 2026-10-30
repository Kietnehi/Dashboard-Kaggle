import unittest
from unittest.mock import MagicMock, patch
from main import app, KernelRunRequest
from fastapi.testclient import TestClient

client = TestClient(app)

class TestNotebookEditorBackend(unittest.TestCase):
    def test_kernel_run_request_accepts_model_sources(self):
        req_data = {
            "target_account": "kitnehi1211",
            "slug": "test-notebook",
            "title": "Test Notebook",
            "source_code": "print('hello')",
            "model_sources": ["google/gemma-2/transformers/2b-it"]
        }
        obj = KernelRunRequest(**req_data)
        self.assertEqual(obj.model_sources, ["google/gemma-2/transformers/2b-it"])

    def test_model_source_url_cleaner(self):
        from kaggle_service import monitor_service
        # Test helper or clean logic in push_and_run_kernel
        # We test that URLs matching kaggle.com/models/... are properly classified as 'model'
        sample_urls = [
            "https://www.kaggle.com/models/google/gemma-2/transformers/2b-it",
            "https://www.kaggle.com/models/google/gemma-2/pyTorch/2b-it/1",
            "google/gemma-2/transformers/2b-it"
        ]
        # We test clean_source logic if exposed or run through helper
        cleaned = monitor_service.clean_input_source(sample_urls[0])
        self.assertEqual(cleaned, ("model", "google/gemma-2/transformers/2b-it"))

    def test_get_notebook_source_and_meta_with_version(self):
        from kaggle_service import monitor_service
        with patch("kaggle_service.KaggleApi") as mock_api_cls:
            mock_api = MagicMock()
            mock_api_cls.return_value = mock_api
            mock_client = MagicMock()
            mock_api.build_kaggle_client.return_value.__enter__.return_value = mock_client
            
            mock_res = MagicMock()
            mock_res.metadata.to_dict.return_value = {
                "title": "Version 1 Notebook",
                "currentVersionNumber": 2,
                "modelDataSources": ["google/gemma-2/transformers/2b-it"],
                "datasetDataSources": ["owner/test-ds"]
            }
            mock_res.blob.source = '{"cells": []}'
            mock_client.kernels.kernels_api_client.get_kernel.return_value = mock_res
            
            res = monitor_service.get_notebook_source_and_meta("kitnehi1211", "test-notebook", version=1)
            self.assertTrue(res.get("success"))
            self.assertEqual(res.get("loaded_version"), 1)
            self.assertEqual(res.get("current_head_version"), 2)
            self.assertIn("google/gemma-2/transformers/2b-it", res.get("model_sources", []))

    def test_api_runner_notebook_meta_endpoint_supports_version(self):
        with patch("main.monitor_service.get_notebook_source_and_meta") as mock_get_meta:
            mock_get_meta.return_value = {
                "success": True,
                "title": "V1",
                "loaded_version": 1,
                "current_head_version": 3,
                "model_sources": ["google/gemma-2/transformers/2b-it"]
            }
            response = client.get("/api/runner/notebook-meta?account=kitnehi1211&ref=test-notebook&version=1")
            self.assertEqual(response.status_code, 200)
            mock_get_meta.assert_called_with("kitnehi1211", "test-notebook", version=1)
            data = response.json()
            self.assertEqual(data.get("loaded_version"), 1)
            self.assertEqual(data.get("current_head_version"), 3)

if __name__ == "__main__":
    unittest.main()
