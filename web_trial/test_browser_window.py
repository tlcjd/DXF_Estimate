# -*- coding: utf-8 -*-
"""Isolated launch tests: never starts a real browser."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from web_trial import browser_window


class BrowserAppWindowTest(unittest.TestCase):
    def test_prefers_chrome_and_creates_isolated_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            chrome=root/'Google'/'Chrome'/'Application'/'chrome.exe'
            chrome.parent.mkdir(parents=True)
            chrome.touch()
            edge=root/'Microsoft'/'Edge'/'Application'/'msedge.exe'
            edge.parent.mkdir(parents=True)
            edge.touch()
            env={'LOCALAPPDATA':str(root), 'PROGRAMFILES':str(root),
                 'PROGRAMFILES(X86)':str(root),
                 'DXF_WEB_PROFILE_DIR':str(root/'profile')}
            with patch.object(browser_window.subprocess, 'Popen') as popen:
                self.assertTrue(browser_window.launch_app_window('http://127.0.0.1:31500/', env=env))
            args=popen.call_args.args[0]
            self.assertEqual(str(chrome), args[0])
            self.assertIn('--app=http://127.0.0.1:31500/', args)
            self.assertIn('--user-data-dir='+str(root/'profile'), args)
            self.assertTrue((root/'profile').is_dir())

    def test_edge_fallback_stays_app_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            edge=root/'Microsoft'/'Edge'/'Application'/'msedge.exe'
            edge.parent.mkdir(parents=True)
            edge.touch()
            env={'PROGRAMFILES':str(root), 'PROGRAMFILES(X86)':str(root), 'LOCALAPPDATA':str(root)}
            with patch.object(browser_window.subprocess, 'Popen') as popen:
                self.assertTrue(browser_window.launch_app_window('http://127.0.0.1:31234/', env=env))
            self.assertEqual(str(edge), popen.call_args.args[0][0])
            self.assertIn('--app=http://127.0.0.1:31234/', popen.call_args.args[0])

    def test_unknown_browser_does_not_open_normal_tab(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            env={'LOCALAPPDATA':str(root),'PROGRAMFILES':str(root),'PROGRAMFILES(X86)':str(root)}
            with patch.object(browser_window.shutil, 'which', return_value=None), patch.object(browser_window.subprocess, 'Popen') as popen:
                self.assertFalse(browser_window.launch_app_window('http://127.0.0.1:31234/', env=env))
            popen.assert_not_called()

if __name__=='__main__':
    unittest.main()
