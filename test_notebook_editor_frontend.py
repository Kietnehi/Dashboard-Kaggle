import unittest
import json

class TestNotebookEditorFrontend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open("static/index.html", "r", encoding="utf-8") as f:
            cls.html = f.read()

    def test_notebook_studio_html_elements_exist(self):
        # 1. Main Studio Section
        self.assertTrue('id="trainerNotebookStudioSection"' in self.html, "Missing id=trainerNotebookStudioSection")
        # 2. Toolbar buttons and badges
        self.assertTrue('id="btnStudioAddCodeCell"' in self.html, "Missing id=btnStudioAddCodeCell")
        self.assertTrue('id="btnStudioAddMdCell"' in self.html, "Missing id=btnStudioAddMdCell")
        self.assertTrue('id="btnStudioToggleViewMode"' in self.html, "Missing id=btnStudioToggleViewMode")
        self.assertTrue('id="studioCellsCountBadge"' in self.html, "Missing id=studioCellsCountBadge")
        self.assertTrue('id="studioVersionTargetBadge"' in self.html, "Missing id=studioVersionTargetBadge")
        # 3. Containers for cells and raw mode
        self.assertTrue('id="trainerStudioCellsList"' in self.html, "Missing id=trainerStudioCellsList")
        self.assertTrue('id="trainerRawSourceTextarea"' in self.html, "Missing id=trainerRawSourceTextarea")

    def test_notebook_studio_javascript_functions_exist(self):
        required_functions = [
            "function syncStudioToSourceCode",
            "function syncSourceCodeToStudio",
            "function renderStudioCells",
            "function addStudioCell",
            "function deleteStudioCell",
            "function moveStudioCell",
            "function toggleStudioCellCollapse",
            "function toggleStudioViewMode"
        ]
        for fn in required_functions:
            self.assertTrue(fn in self.html, f"Expected {fn} to be defined in static/index.html")

    def test_notebook_json_compatibility(self):
        sample_nb = {
            "cells": [
                {
                    "cell_type": "markdown",
                    "metadata": {},
                    "source": "# Title"
                },
                {
                    "cell_type": "code",
                    "execution_count": None,
                    "metadata": {},
                    "outputs": [],
                    "source": "import numpy as np\nprint('v1')"
                }
            ],
            "metadata": {
                "language_info": {"name": "python"}
            },
            "nbformat": 4,
            "nbformat_minor": 4
        }
        serialized = json.dumps(sample_nb)
        deserialized = json.loads(serialized)
        self.assertEqual(len(deserialized["cells"]), 2)
        self.assertEqual(deserialized["cells"][1]["cell_type"], "code")

if __name__ == "__main__":
    unittest.main()
