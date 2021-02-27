# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(['AC4QGP.py'],
             pathex=['dist'],
             binaries=[ ( './dist/_sounddevice_data/portaudio-binaries/libportaudio64bit.dll', '.' ) ],
             datas=[],
             hiddenimports=['py._path.local','py._vendored_packages.iniconfig'],
             hookspath=[],
             runtime_hooks=[],
             excludes=[],
             win_no_prefer_redirects=False,
             win_private_assemblies=False,
             cipher=block_cipher,
             noarchive=False)
pyz = PYZ(a.pure, a.zipped_data,
             cipher=block_cipher)
exe = EXE(pyz,
          a.scripts,
          a.binaries,
          a.zipfiles,
          a.datas,
          [],
          name='AC4QGP',
          debug=False,
          bootloader_ignore_signals=False,
          strip=False,
          upx=True,
          upx_exclude=[],
          runtime_tmpdir=None,
          console=True )



