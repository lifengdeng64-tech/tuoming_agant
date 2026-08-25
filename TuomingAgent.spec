# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_submodules


datas = [("app.py", ".")]
binaries = []
hiddenimports = []
hiddenimports += collect_submodules("tuoming_agent")
for package in (
    "streamlit",
    "plotly",
    "duckdb",
    "pyarrow",
    "openpyxl",
    "langchain_openai",
    "langchain_anthropic",
    "langchain_google_genai",
):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += [
        item
        for item in package_datas
        if "/.agents/" not in item[0].replace("\\", "/")
    ]
    binaries += package_binaries
    hiddenimports += package_hidden
hiddenimports += collect_submodules("pystray")

analysis = Analysis(
    ["desktop_entry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter.test"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="TuomingAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TuomingAgent",
)
