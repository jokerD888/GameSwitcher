# -*- mode: python ; coding: utf-8 -*-

excludes = [
    'numpy', 'scipy',
    'PIL._avif', 'PIL._webp', 'PIL._imagingcms', 'PIL._imagingft',
    'PIL.PdfImagePlugin', 'PIL.TiffImagePlugin', 'PIL.Jpeg2KImagePlugin',
    'PIL.ImageTk', 'PIL.ImageQt', 'PIL.ImageWin', 'PIL.GifImagePlugin',
    'PIL.SpiderImagePlugin', 'PIL.MpoImagePlugin', 'PIL.EpsImagePlugin',
    'PIL.DdsImagePlugin', 'PIL.IcnsImagePlugin', 'PIL.FpxImagePlugin',
    'PIL.PcdImagePlugin', 'PIL.PcxImagePlugin', 'PIL.BlpImagePlugin',
    'PIL.BufrStubImagePlugin', 'PIL.CurImagePlugin', 'PIL.DcxImagePlugin',
    'PIL.FitsImagePlugin', 'PIL.FliImagePlugin', 'PIL.FtexImagePlugin',
    'PIL.GbrImagePlugin', 'PIL.GribStubImagePlugin', 'PIL.Hdf5StubImagePlugin',
    'PIL.ImImagePlugin', 'PIL.ImtImagePlugin', 'PIL.IptcImagePlugin',
    'PIL.McIdasImagePlugin', 'PIL.MicImagePlugin', 'PIL.MpegImagePlugin',
    'PIL.MspImagePlugin', 'PIL.PalmImagePlugin', 'PIL.PixarImagePlugin',
    'PIL.PpmImagePlugin', 'PIL.PsdImagePlugin', 'PIL.QoiImagePlugin',
    'PIL.SgiImagePlugin', 'PIL.SunImagePlugin', 'PIL.TgaImagePlugin',
    'PIL.WmfImagePlugin', 'PIL.XbmImagePlugin', 'PIL.XpmImagePlugin',
    'PIL.XvThumbImagePlugin',
    'tkinter', '_tkinter', 'tcl', 'tk',
    'yaml', 'unittest', 'test', 'pydoc', 'pydoc_data', 'doctest',
    'email', 'html', 'http', 'urllib', 'xml', 'xmlrpc',
    'ssl', '_ssl', 'multiprocessing', 'sqlite3', '_sqlite3',
    'decimal', '_decimal', 'bz2', 'lzma', 'asyncio',
    'setuptools', 'distutils', 'pkg_resources',
    'PyQt5', 'PyQt6', 'PySide2', 'PySide6'
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["pystray._win32"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)

excluded_bin_keywords = {
    'openblas', 'libcrypto', 'libssl', 'libexpat', 'libmpdec', 'liblzma',
    '_avif', '_webp', '_imagingcms', '_imagingft', '_yaml', 'tcl', 'tk'
}
a.binaries = [
    x for x in a.binaries
    if not any(k in x[0].lower() for k in excluded_bin_keywords)
]

excluded_data_keywords = {'numpy', 'tcl', 'tk'}
a.datas = [
    x for x in a.datas
    if not any(k in x[0].lower() for k in excluded_data_keywords)
]

pyz = PYZ(a.pure, optimize=1)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="GameSwitcher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
)
