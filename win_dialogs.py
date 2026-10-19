#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 原生文件/目录对话框（纯 ctypes 调系统 API，零第三方依赖）。

- pick_file / pick_save：comdlg32.GetOpenFileNameW / GetSaveFileNameW
- pick_dir：shell32.SHBrowseForFolderW（经典目录选择框）
对话框自带模态消息循环，可在任意工作线程直接调用；无需 tkinter、无主窗口。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_IS_WINDOWS = sys.platform == "win32"

OFN_FILEMUSTEXIST = 0x1000
OFN_OVERWRITEPROMPT = 0x2
OFN_HIDEREADONLY = 0x4
OFN_NOCHANGEDIR = 0x8

BIF_RETURNONLYFSDIRS = 0x1
BIF_NEWDIALOGSTYLE = 0x40


class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR),
        ("lCustData", wintypes.LPARAM),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", wintypes.LPCWSTR),
        ("pvReserved", ctypes.c_void_p),
        ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD),
    ]


class BROWSEINFOW(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", wintypes.LPWSTR),
        ("lpszTitle", wintypes.LPCWSTR),
        ("ulFlags", wintypes.UINT),
        ("lpfn", ctypes.c_void_p),
        ("lParam", wintypes.LPARAM),
        ("iImage", ctypes.c_int),
    ]


def _owner() -> int:
    """不挂 owner：跨进程/跨线程 owner 属未定义行为（可能导致崩溃），置空最稳。"""
    return 0


_FILTER = "JSON 文件 (*.json)\0*.json\0所有文件 (*.*)\0*.*\0"


def pick_file(title: str = "选择文件") -> str:
    if not _IS_WINDOWS:
        return ""
    buf = ctypes.create_unicode_buffer(4096)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = _owner()
    ofn.lpstrFilter = _FILTER
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = 4096
    ofn.lpstrTitle = title
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_HIDEREADONLY | OFN_NOCHANGEDIR
    ok = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
    return buf.value if ok else ""


def pick_save(default_name: str = "tag_table.json") -> str:
    if not _IS_WINDOWS:
        return ""
    buf = ctypes.create_unicode_buffer(4096)
    try:
        buf.value = default_name
    except Exception:  # noqa: BLE001
        pass
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = _owner()
    ofn.lpstrFilter = _FILTER
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = 4096
    ofn.lpstrTitle = "保存到"
    ofn.lpstrDefExt = "json"
    ofn.Flags = OFN_OVERWRITEPROMPT | OFN_HIDEREADONLY | OFN_NOCHANGEDIR
    ok = ctypes.windll.comdlg32.GetSaveFileNameW(ctypes.byref(ofn))
    return buf.value if ok else ""


def pick_dir(title: str = "选择目录") -> str:
    if not _IS_WINDOWS:
        return ""
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    try:
        display = ctypes.create_unicode_buffer(260)
        bi = BROWSEINFOW()
        bi.hwndOwner = _owner()
        bi.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
        bi.lpszTitle = title
        bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
        pidl = ctypes.windll.shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return ""
        try:
            path_buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetPathFromIDListW(pidl, path_buf):
                return path_buf.value
            return ""
        finally:
            ctypes.windll.ole32.CoTaskMemFree(pidl)
    finally:
        ole32.CoUninitialize()
