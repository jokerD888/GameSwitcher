# -*- mode: python ; coding: utf-8 -*-

# Comprehensive excludes to prevent bundling heavy unneeded modules
excludes = [
    # Heavy scientific stack (never used by GameSwitcher)
    'numpy',
    'scipy',

    # Heavy unused Pillow plugins and decoders
    'PIL._avif',
    'PIL._webp',
    'PIL._imagingcms',
    'PIL._imagingft',

    # Pillow pure-python plugins for unused formats
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

    # GUI toolkits not used (we use pure win32 & pystray)
    'tkinter',
    '_tkinter',
    'tcl',
    'tk',

    # Unused standard library & 3rd party modules
    'yaml',
    'unittest',
    'test',
    'pydoc',
    'pydoc_data',
    'doctest',
    'email',
    'html',
    'http',
    'urllib',
    'xml',
    'xmlrpc',
    'ssl',
    '_ssl',
    'multiprocessing',
    'sqlite3',
    '_sqlite3',
    'decimal',
    '_decimal',
    'bz2',
    'lzma',
    'asyncio',
    'setuptools',
    'distutils',
    'pkg_resources',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)

# Strip out unnecessary heavy binary DLLs/pyds if transitively included
excluded_bin_keywords = {
    'openblas', 'libcrypto', 'libssl', 'libexpat', 'libmpdec', 'liblzma',
    '_avif', '_webp', '_imagingcms', '_imagingft', '_yaml', 'tcl', 'tk'
}
a.binaries = [
    x for x in a.binaries
    if not any(k in x[0].lower() for k in excluded_bin_keywords)
]

# Strip out any lingering test/doc data
excluded_data_keywords = {'numpy', 'tcl', 'tk'}
a.datas = [
    x for x in a.datas
    if not any(k in x[0].lower() for k in excluded_data_keywords)
]

pyz = PYZ(a.pure, optimize=1)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='GameSwitcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
