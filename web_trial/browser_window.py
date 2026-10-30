# -*- coding: utf-8 -*-
"""Launch the offline viewer in a *separate* Chrome/Edge app window on Windows.

This does not embed a browser or alter the existing Python backend.  A dedicated
user-data directory keeps the viewer separate from personal browsing windows.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


def _browser_candidates(env: dict[str, str]):
    """Return preferred Windows browser executable locations (Chrome before Edge)."""
    if env.get('DXF_WEB_BROWSER'):
        yield '사용자 지정 브라우저', Path(env['DXF_WEB_BROWSER'])
    local = Path(env.get('LOCALAPPDATA', '')) if env.get('LOCALAPPDATA') else None
    pf = Path(env.get('PROGRAMFILES', '')) if env.get('PROGRAMFILES') else None
    pf86 = Path(env.get('PROGRAMFILES(X86)', '')) if env.get('PROGRAMFILES(X86)') else None
    chrome_dirs = [(local, 'Google/Chrome/Application/chrome.exe'),
                   (pf, 'Google/Chrome/Application/chrome.exe'),
                   (pf86, 'Google/Chrome/Application/chrome.exe')]
    edge_dirs = [(pf86, 'Microsoft/Edge/Application/msedge.exe'),
                 (pf, 'Microsoft/Edge/Application/msedge.exe'),
                 (local, 'Microsoft/Edge/Application/msedge.exe')]
    for base, relative in chrome_dirs:
        if base:
            yield 'Chrome', base / relative
    chrome = shutil.which('chrome.exe') or shutil.which('chrome')
    if chrome:
        yield 'Chrome', Path(chrome)
    for base, relative in edge_dirs:
        if base:
            yield 'Edge', base / relative
    edge = shutil.which('msedge.exe') or shutil.which('msedge')
    if edge:
        yield 'Edge', Path(edge)


def find_browser(env: dict[str, str] | None = None):
    env = env or os.environ
    for name, path in _browser_candidates(env):
        if path.is_file():
            return name, path
    return None


def launch_app_window(address: str, *, env: dict[str, str] | None = None):
    """Open a single tabless, address-bar-less, separate local-browser window.

    Returns False and prints an actionable message if no supported browser exists.
    Browser launch must not stop the already running local web server.
    """
    env = env or os.environ
    found = find_browser(env)
    if not found:
        print('[앱 창] Chrome 또는 Edge 실행파일을 찾지 못했습니다.', flush=True)
        print('[앱 창] 브라우저 설치 경로를 DXF_WEB_BROWSER 환경변수로 지정할 수 있습니다.', flush=True)
        print(f'[앱 창] 수동 접속: {address}', flush=True)
        return False

    name, exe = found
    # Persistent, standalone user profile: never reuse Chrome's everyday tab/profile.
    # Profile can be safely regenerated independently of existing drawing data.
    default_profile = Path(env.get('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local')) / 'DXF_Estimate_Web' / 'ChromeAppProfile'
    profile = Path(env.get('DXF_WEB_PROFILE_DIR') or default_profile).expanduser().resolve()
    try:
        profile.mkdir(parents=True, exist_ok=True)
        args = [str(exe), f'--user-data-dir={profile}', '--no-first-run',
                '--no-default-browser-check', '--disable-sync',
                '--window-size=1500,950', f'--app={address}']
        popen_kwargs = {}
        if sys.platform == 'win32':
            popen_kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        subprocess.Popen(args, env=env, **popen_kwargs)
        print(f'[앱 창] {name} 독립창 실행: {address}', flush=True)
        return True
    except (OSError, ValueError) as exc:
        print(f'[앱 창] 브라우저 실행 실패: {type(exc).__name__}: {exc}', flush=True)
        print(f'[앱 창] 수동 접속: {address}', flush=True)
        return False
