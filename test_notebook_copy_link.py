import unittest

class TestNotebookCopyLink(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open("static/index.html", "r", encoding="utf-8") as f:
            cls.html = f.read()

    def test_copy_notebook_link_helper_exists(self):
        self.assertTrue("function copyNotebookLinkByRef(ref)" in self.html,
                        "Missing copyNotebookLinkByRef(ref) helper in static/index.html")

    def test_all_notebooks_table_has_copy_link_button(self):
        # In renderKernelsTab, we expect copyNotebookLinkByRef to be called for rows in kernels table
        self.assertTrue("copyNotebookLinkByRef('${k.ref}')" in self.html,
                        "All Notebooks table row should include a copyNotebookLinkByRef('${k.ref}') button")
        self.assertTrue("Copy Link" in self.html,
                        "Table should have user-visible 'Copy Link' button text or title")

    def test_inspector_modal_has_copy_link_button(self):
        # Inspector modal should have a copy link button alongside the 'Mở trên Kaggle' link
        self.assertTrue('id="inspectorCopyLinkBtn"' in self.html,
                        "Inspector modal should have id=\"inspectorCopyLinkBtn\"")
        self.assertTrue("copyCurrentInspectorLink" in self.html,
                        "Inspector modal should have copyCurrentInspectorLink function")

if __name__ == '__main__':
    unittest.main()
