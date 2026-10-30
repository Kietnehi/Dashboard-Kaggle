import unittest

class TestVersioningActions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open("static/index.html", "r", encoding="utf-8") as f:
            cls.html = f.read()

    def test_inspector_modal_edit_version_button_exists(self):
        self.assertTrue('id="btnInspectorEditVersion"' in self.html, "Expected id=btnInspectorEditVersion in inspectorModal header")
        self.assertTrue('onclick="editNotebookFromInspector()"' in self.html, "Expected onclick=editNotebookFromInspector() on edit button")

    def test_edit_functions_defined_in_javascript(self):
        self.assertTrue('function editNotebookFromInspector' in self.html, "Expected function editNotebookFromInspector to be defined")
        self.assertTrue('function quickEditNotebook' in self.html, "Expected function quickEditNotebook to be defined")

    def test_kernels_table_has_edit_button(self):
        self.assertTrue('quickEditNotebook(' in self.html, "Expected quickEditNotebook to be called in renderKernelsTab")

if __name__ == "__main__":
    unittest.main()
