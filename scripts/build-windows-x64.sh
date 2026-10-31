#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
python3 - <<'PY'
from pathlib import Path
import os
import shlex
import subprocess

root = Path.cwd()
target = 'x86_64-pc-windows-msvc'
installed = subprocess.check_output(['rustup', 'target', 'list', '--installed'], text=True).splitlines()
if target not in installed:
    raise SystemExit('Missing Rust standard library. Run: rustup target add ' + target)

program_files = Path(os.environ.get('SITEF_PROGRAMFILES_X86', '/mnt/c/Program Files (x86)'))
msvc_candidates = sorted((program_files / 'Microsoft Visual Studio').glob('*/*/VC/Tools/MSVC/*/lib/x64'))
sdk_versions = sorted((program_files / 'Windows Kits/10/Lib').glob('*'))
sdk_candidates = [p for p in sdk_versions if (p / 'ucrt/x64').is_dir() and (p / 'um/x64').is_dir()]

def resolve(variable, default):
    path = Path(os.environ[variable]) if variable in os.environ else default
    if path is None or not path.is_dir():
        raise SystemExit('Set ' + variable + ' to the existing Windows x64 import-library directory')
    return path

msvc = resolve('SITEF_MSVC_LIB_DIR', msvc_candidates[-1] if msvc_candidates else None)
ucrt = resolve('SITEF_UCRT_LIB_DIR', sdk_candidates[-1] / 'ucrt/x64' if sdk_candidates else None)
um = resolve('SITEF_WINDOWS_UM_LIB_DIR', sdk_candidates[-1] / 'um/x64' if sdk_candidates else None)
imports = root / '.build/msvc-imports-x64'
imports.mkdir(parents=True, exist_ok=True)
# The Windows SDK uses mixed-case .Lib names. Normalized symlinks are local,
# ignored build artifacts; compiler/SDK files are never copied into the package.
for directory in (msvc, ucrt, um):
    for library in directory.iterdir():
        if library.suffix.lower() == '.lib':
            normalized = imports / library.name.lower()
            if not normalized.exists():
                normalized.symlink_to(library)
sysroot = Path(subprocess.check_output(['rustc', '--print', 'sysroot'], text=True).strip())
host = next(line.split(': ', 1)[1] for line in subprocess.check_output(['rustc', '-vV'], text=True).splitlines() if line.startswith('host: '))
linker = sysroot / ('lib/rustlib/' + host + '/bin/rust-lld')
if not linker.is_file():
    raise SystemExit('The installed Rust toolchain does not include rust-lld')
environment = os.environ.copy()
environment['CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER'] = str(linker)
flags = environment.get('CARGO_ENCODED_RUSTFLAGS', '').split('\x1f') if environment.get('CARGO_ENCODED_RUSTFLAGS') else shlex.split(environment.get('RUSTFLAGS', ''))
flags += ['-Ctarget-feature=+crt-static', '-Lnative=' + str(imports)]
environment['CARGO_ENCODED_RUSTFLAGS'] = '\x1f'.join(flags)
subprocess.run(['cargo', 'build', '--locked', '--release', '--target', target], cwd=root / 'native', env=environment, check=True)
print(root / 'native/target' / target / 'release/sitef-bridge.exe')
PY
