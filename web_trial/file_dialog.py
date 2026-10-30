# -*- coding: utf-8 -*-
"""로컬 PC의 '파일 열기' 창으로 DXF 경로를 받는다.

브라우저는 보안상 실제 파일 경로를 알려주지 않으므로(업로드 복사만 가능),
로컬 서버가 Windows 기본 파일 선택 창을 직접 띄워 원본 경로를 얻는다.
원본 DXF는 경로만 읽고 수정하지 않는다.
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import threading
from pathlib import Path

_LOCK = threading.Lock()

# 창이 Chrome 앱 창 뒤에 숨지 않도록 TopMost 소유자 폼을 둔다.
# 경로·제목의 한글이 깨지지 않게 스크립트는 -EncodedCommand(UTF-16), 출력은 UTF-8.
_PS_SCRIPT = r'''
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
$owner.StartPosition = 'CenterScreen'
$owner.Show()
$owner.Activate()
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = 'DXF 파일 열기'
$dialog.Filter = 'DXF 도면 (*.dxf)|*.dxf'
$dialog.CheckFileExists = $true
$dialog.Multiselect = $false
if ($env:DXF_DIALOG_DIR -and (Test-Path -LiteralPath $env:DXF_DIALOG_DIR)) { $dialog.InitialDirectory = $env:DXF_DIALOG_DIR }
$result = $dialog.ShowDialog($owner)
$owner.Close()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($dialog.FileName) }
'''


def _windows_dialog(initial_dir: str) -> str:
    encoded = base64.b64encode(_PS_SCRIPT.encode('utf-16-le')).decode('ascii')
    env = dict(os.environ, DXF_DIALOG_DIR=initial_dir or '')
    done = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-STA', '-ExecutionPolicy', 'Bypass',
         '-EncodedCommand', encoded],
        capture_output=True, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if done.returncode != 0:
        detail = done.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise RuntimeError('파일 선택 창을 열 수 없습니다: ' + (detail[-1] if detail else f'code {done.returncode}'))
    return done.stdout.decode('utf-8-sig', 'replace').strip()


def _tk_dialog(initial_dir: str) -> str:
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    try:
        return filedialog.askopenfilename(parent=root, title='DXF 파일 열기', initialdir=initial_dir or None,
                                          filetypes=[('DXF 도면', '*.dxf')]) or ''
    finally:
        root.destroy()


def ask_dxf_path(initial_dir: str = '') -> Path | None:
    """파일 선택 창을 띄워 고른 DXF 경로를 돌려준다. 취소하면 None."""
    if not _LOCK.acquire(blocking=False):
        raise ValueError('파일 선택 창이 이미 열려 있습니다. 작업표시줄에서 창을 확인하세요.')
    try:
        picked = _windows_dialog(initial_dir) if sys.platform == 'win32' else _tk_dialog(initial_dir)
    finally:
        _LOCK.release()
    return Path(picked) if picked else None
