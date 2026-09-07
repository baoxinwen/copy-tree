# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 构建配置。"""

import os
import re

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

# 从源码读取版本号，生成 exe 的 VS_VERSIONINFO 资源块，
# 供资源管理器文件属性与将来的版本比较使用
with open(os.path.join(SPECPATH, 'src', 'copytree', '__init__.py'), encoding='utf-8') as _f:
    _match = re.search(r'__version__\s*=\s*[\'"]([^\'"]+)[\'"]', _f.read())
APP_VERSION = _match.group(1) if _match else '0.0.0'
_parts = [int(x) for x in APP_VERSION.split('.')]
_filevers = tuple((_parts + [0, 0, 0, 0])[:4])


def _version_info(description, original_filename):
    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=_filevers,
            prodvers=_filevers,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo([
                StringTable('080404b0', [
                    StringStruct('CompanyName', 'copy-tree'),
                    StringStruct('FileDescription', description),
                    StringStruct('FileVersion', APP_VERSION),
                    StringStruct('InternalName', original_filename),
                    StringStruct('OriginalFilename', original_filename),
                    StringStruct('ProductName', 'copy-tree'),
                    StringStruct('ProductVersion', APP_VERSION),
                ])
            ]),
            VarFileInfo([VarStruct('Translation', [2052, 1200])]),
        ],
    )


a = Analysis(
    ['src/main.py'],
    pathex=['src'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'unittest', 'email', 'html', 'http', 'xmlrpc',
        'pydoc', 'doctest', 'difflib',
        'xml.etree', 'xml.dom.minidom',
        'xml.sax', 'csv', 'sqlite3', 'pdb',
        'lib2to3', 'distutils', 'setuptools', 'pip',
        'encodings.mac_roman', 'encodings.cp437',
    ],
    noarchive=True,
)

pyz = PYZ(a.pure)

gui_exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='copy-tree',
    version=_version_info('copy-tree 右键目录树复制工具', 'copy-tree.exe'),
    debug=False,
    bootloader_ignore_signals=True,
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
    icon='src/copytree/icon.ico',
)

cli_exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='copy-tree-cli',
    version=_version_info('copy-tree 命令行版（脚本与重定向）', 'copy-tree-cli.exe'),
    debug=False,
    bootloader_ignore_signals=True,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='src/copytree/icon.ico',
)
