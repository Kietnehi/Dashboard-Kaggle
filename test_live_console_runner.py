import os
import sys
import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kaggle_service import monitor_service, _clean_kernel_session_log

class TestLiveConsoleRunner(unittest.TestCase):
    def test_clean_kernel_session_log_json_array(self):
        raw = '[{"stream_name":"stderr","time":1.23,"data":"Epoch 1/5\\n"},{"stream_name":"stdout","time":1.45,"data":"Loss: 0.234\\n"}]'
        cleaned = _clean_kernel_session_log(raw)
        self.assertEqual(cleaned, "Epoch 1/5\nLoss: 0.234\n")

    def test_clean_kernel_session_log_unclosed_array(self):
        # Kaggle stream chunk ending with comma or unclosed bracket
        raw = '[{"stream_name":"stderr","time":1.23,"data":"Step 100 done\\n"},'
        cleaned = _clean_kernel_session_log(raw)
        self.assertIn("Step 100 done", cleaned)

    def test_fetch_kernel_stream_log_json_array_stream(self):
        # Mock http response that yields lines like Kaggle stream
        mock_lines = [
            '[{"stream_name":"stderr","time":7.1,"data":"0.00s - Debugger warning\\n"}\n',
            ',{"stream_name":"stdout","time":8.9,"data":"Training started\\n"}\n',
            ']\n'
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_lines.return_value = mock_lines

        with patch("kaggle_service.KaggleApi") as mock_api_cls:
            mock_api = MagicMock()
            mock_api_cls.return_value = mock_api
            mock_client = MagicMock()
            mock_api.build_kaggle_client.return_value.__enter__.return_value = mock_client
            mock_client._http_client._session.get.return_value = mock_resp

            fake_cred = {"username": "kitnehi1211", "key": "fakekey"}
            result = monitor_service._fetch_kernel_stream_log(fake_cred, "kitnehi1211", "test-kernel")
            self.assertIn("0.00s - Debugger warning", result)
            self.assertIn("Training started", result)

    def test_fetch_kernel_stream_log_sse_format(self):
        mock_lines = [
            'data: {"data":"Processing batch 1\\n"}\n',
            'data: {"data":"Processing batch 2\\n"}\n',
            'data: END_OF_LOG\n'
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_lines.return_value = mock_lines

        with patch("kaggle_service.KaggleApi") as mock_api_cls:
            mock_api = MagicMock()
            mock_api_cls.return_value = mock_api
            mock_client = MagicMock()
            mock_api.build_kaggle_client.return_value.__enter__.return_value = mock_client
            mock_client._http_client._session.get.return_value = mock_resp

            fake_cred = {"username": "kitnehi1211", "key": "fakekey"}
            result = monitor_service._fetch_kernel_stream_log(fake_cred, "kitnehi1211", "test-kernel")
            self.assertIn("Processing batch 1", result)
            self.assertIn("Processing batch 2", result)

    def test_update_training_history_status(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json", encoding="utf-8") as tf:
            initial = [{"id": "run-1", "ref": "phatle54/lifemate-agent-sft", "status": "QUEUED"}]
            json.dump(initial, tf)
            temp_path = tf.name

        try:
            with patch("kaggle_service.HISTORY_FILE", temp_path):
                monitor_service.update_training_history_status("phatle54/lifemate-agent-sft", "RUNNING")
                with open(temp_path, "r", encoding="utf-8") as f:
                    updated = json.load(f)
                self.assertEqual(updated[0]["status"], "RUNNING")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_slug_cleaning_with_owner_prefix(self):
        # Slug containing owner/ should be stripped down to the clean notebook slug
        slug = "phatle54/lifemate-agent-sft-oracle"
        clean_slug = str(slug or "").strip()
        if "/" in clean_slug:
            clean_slug = clean_slug.split("/")[-1].strip()
        import re
        clean_slug = re.sub(r'[^a-zA-Z0-9\-]+', '-', clean_slug.lower()).strip('-')
        self.assertEqual(clean_slug, "lifemate-agent-sft-oracle")

    def test_push_and_run_kernel_save_only_quick_save(self):
        from kagglesdk.kernels.types.kernels_enums import KernelExecutionType
        
        with patch("kaggle_service.KaggleApi") as mock_api_cls:
            mock_api = MagicMock()
            mock_api_cls.return_value = mock_api
            mock_client = MagicMock()
            mock_api.build_kaggle_client.return_value.__enter__.return_value = mock_client
            
            mock_save_resp = MagicMock()
            mock_save_resp.error = None
            mock_save_resp.url = "https://www.kaggle.com/code/kitnehi1211/test-slug"
            mock_save_resp.version_number = 5
            mock_client.kernels.kernels_api_client.save_kernel.return_value = mock_save_resp
            
            with patch.object(monitor_service, "get_credential_by_username", return_value={"username": "kitnehi1211", "key": "abc"}):
                res = monitor_service.push_and_run_kernel(
                    target_account="kitnehi1211",
                    slug="test-slug",
                    title="Test Notebook",
                    source_code="# print('hello')",
                    save_only=True
                )
                
                # Check save_kernel called with QUICK_SAVE
                self.assertTrue(mock_client.kernels.kernels_api_client.save_kernel.called)
                save_req = mock_client.kernels.kernels_api_client.save_kernel.call_args[0][0]
                self.assertEqual(save_req.kernel_execution_type, KernelExecutionType.QUICK_SAVE)
                self.assertFalse(save_req.enable_gpu)
                self.assertFalse(save_req.machine_shape)
                
                # Verify result
                self.assertTrue(res.get("success"))
                self.assertTrue(res.get("saved_only"))
                self.assertEqual(res.get("version_number"), 5)
                
                # Verify NOT enqueued in running_cache
                for r in monitor_service.running_cache:
                    self.assertNotEqual(r.get("ref"), "kitnehi1211/test-slug")

    def test_share_notebook_datasets_delegation(self):
        def fake_cred(u):
            return {"username": "kitnehi1211", "key": "abc"} if u == "kitnehi1211" else None

        with patch.object(monitor_service, "get_credential_by_username", side_effect=fake_cred):
            with patch.object(monitor_service, "share_dataset_collaborators", return_value={"success": True}) as mock_share:
                res = monitor_service.share_notebook_datasets(
                    owner_account="kitnehi1211",
                    dataset_sources=["kitnehi1211/my-dataset", "thanhkhil/other-dataset"],
                    collaborator_usernames=["baovuong3009", "lngchip"],
                    role="WRITER"
                )
                self.assertTrue(res.get("success"))
                # Only datasets owned by credentials available (kitnehi1211) get shared
                self.assertEqual(mock_share.call_count, 1)

if __name__ == "__main__":
    unittest.main()
