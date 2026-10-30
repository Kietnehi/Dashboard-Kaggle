import unittest

class TestNotebookInputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open("static/index.html", "r", encoding="utf-8") as f:
            cls.html = f.read()

    def test_model_input_badge_styling_in_render_selected_inputs(self):
        # Verify renderSelectedInputs renders distinct style/badge for 'model'
        self.assertTrue("type === 'model'" in self.html or "i.type === 'model'" in self.html, "Expected model type check in input rendering")
        self.assertTrue("bg-emerald-50" in self.html or "text-emerald-700" in self.html or "text-emerald-800" in self.html, "Expected emerald badge for model inputs")

    def test_kaggle_model_url_parsing_in_frontend(self):
        # Verify frontend parses kaggle.com/models/...
        self.assertTrue("kaggle.com/models" in self.html, "Expected parsing of kaggle.com/models URLs in frontend")

    def test_submit_run_notebook_sends_model_sources(self):
        # Verify submitRunNotebook extracts and posts model_sources
        self.assertTrue("model_sources" in self.html, "Expected model_sources to be included in submit payload")
        self.assertTrue("i.type === 'model'" in self.html or "type === 'model'" in self.html, "Expected filtering of model type inputs")

if __name__ == "__main__":
    unittest.main()
