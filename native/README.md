# Native host

Rust executable using JSON lines on stdin/stdout, with **Windows x64 as its only
production runtime**. The SDK supplies an absolute path to an integrator-owned
**interface** library (`CliSiTef64I.dll` in the supplied simulator). Internal
`CliSiTef.dll` is rejected. PE headers must identify AMD64 PE32+ before loading;
Win32/x86 and ARM64 libraries are rejected. A Windows non-x64 target fails at
compile time. No vendor library or manual is included.

Stdio remains the default. An optional authenticated loopback transport is
available for clients such as PHP on Windows:
`--connect 127.0.0.1:PORT --token-env SITEF_BRIDGE_TOKEN`. The client creates the
listener first and supplies a fresh 64-hex-character nonce in the child
environment. The helper removes it, connects within 10 seconds, and sends
`{protocolVersion:1,transport:"tcp-loopback",token:nonce}` before the same NDJSON
requests/replies. Only the exact IPv4 loopback address and a nonzero port are
accepted. TCP mode leaves stdout/stderr empty. See [the shared protocol](../docs/bridge-protocol.md) for
the full language-neutral contract and error/state semantics.

WSL is the development environment. `testMode: true` on a non-Windows host permits
only our synthetic native ABI tests; it does not provide a Linux production
runtime. Production DLLs execute in the Windows x64 helper process.

The API contract comes exclusively from the newer VRS-258 and Wallets 1.43 PDFs
in the original Win32 ZIP. PDFs in the x64 ZIP were not read or used.
The supplied x64 binary is older than those documents. Capabilities therefore
report the symbols actually present, and optional wrappers fail explicitly when
an entrypoint is missing; newer documented features still require vendor tests.

On WSL with an installed Visual Studio C++ x64 toolchain and Windows SDK, run
`rustup target add x86_64-pc-windows-msvc` once, then
`bash scripts/build-windows-x64.sh` from the project root. The script uses Rust's
bundled `rust-lld` and existing Windows import libraries, with static CRT. Set
`SITEF_MSVC_LIB_DIR`, `SITEF_UCRT_LIB_DIR`, and `SITEF_WINDOWS_UM_LIB_DIR` to override
auto-detected x64 library folders; `SITEF_PROGRAMFILES_X86` overrides the default
WSL mount `/mnt/c/Program Files (x86)`. It downloads no vendor library and copies
no compiler libraries into packages. Missing Microsoft PDB warnings affect debug
information only and do not establish runtime failure.

`load` parameters:

```json
{"id":1,"method":"load","params":{"libraryPath":"C:\\vendor\\CliSiTef64I.dll","nativeEncoding":"windows-1252"}}
```

Optional `workingDirectory` must be absolute, defaults to the library directory,
and is set only inside the helper process. `nativeEncoding` accepts
`windows-1252` (initial explicit default), `utf-8`, or `ascii`. Actual vendor
encoding must be checked in simulator tests before production. Input encoding
loss and embedded NUL are rejected. Returned native data retains original base64
bytes and an explicit `decodingError` marker; malformed native text never silently
replaces the financial status. `continue.bufferBase64` bypasses text encoding;
the response field has the same name.

`load` and `capabilities` return:

```json
{
  "protocolVersion": 1,
  "bridgeVersion": "0.1.0",
  "platform": "windows",
  "architecture": "x86_64",
  "runtimeTarget": "windows-x64",
  "nativeEncoding": "windows-1252",
  "fieldTypeBytes": 4,
  "testMode": false,
  "minimumBufferSize": 20000,
  "functions": {
    "start": {"symbol": "IniciaFuncaoSiTefInterativo", "available": true, "callable": true}
  }
}
```

Errors have `{id,error:{code,message}}`; native returns keep their original
numbers in `result`, with no context-free success/denial mapping. Error messages
never interpolate native buffers, collection results, or native loader paths.
The process loads one library for its lifetime and serializes all calls.

Methods implemented:

| Method | Parameters | Result |
| --- | --- | --- |
| configure | server, storeId, terminalId, reserved=0, additionalParameters | status |
| start | functionId, amount, fiscalDocument, fiscalDate, fiscalTime, operator, additionalParameters | status |
| continue | buffer, bufferBase64?, continueFlag, bufferSize (20000–1048576) | status, command, fieldType, minLength, maxLength, buffer, bufferBase64, decodingError |
| finish | confirm (0 or 1), fiscalDocument, fiscalDate, fiscalTime, additionalParameters | returned:true (native void) |
| version | none | status, libraryVersion, interfaceVersion |
| pendingCount | fiscalDate, fiscalDocument | count, status (same native return) |
| flushMessages | none | status |
| pinpadPresent / pinpadKeepAlive / pinpadKey | none | status |
| pinpadPermanentMessage / pinpadMessage / pinpadYesNo | message | status |
| pinpadInfo | none | UNVERIFIED_OUTPUT_BOUND; use start functionId 775 |
| validateBarcode | data | status, documentType |
| removeCard | none | status |
| startBanking | fiscalDocument, fiscalDate, fiscalTime, operator, additionalParameters | status |
| readSecureCard / readTrack3 | message | status (then continue loop) |
| readPassword | securityKey (exactly 64 native bytes) | status (then continue loop) |
| readPasswordDirect | securityKey (exactly 64 native bytes) | status, encryptedPassword |
| readChip | modality, additionalParameters | status (then continue loop) |

Version output strings are objects `{text,rawBase64,decodingError}`. In
capabilities, `available` reports an exported symbol and `callable` additionally
requires an enabled callable wrapper. `configureEx` and `readChipEx` describe
export variants, not separate methods: their `invocationMethod` values point to
`configure` and `readChip`, which select the extended export when present. These
two metadata variants have `callable:false`. `pinpadInfo` is blocked because its direct
ABI has no capacity argument and the manual states a minimum allocation without
a verified maximum output bound. Function 775 uses bounded continuation instead.
`readPassword` is a proprietary store-card function, not a way to collect payment
card PINs; encrypted material must never enter logs or generic UI inputs.

Continuation retains native command/type/size outputs across calls as VRS-258
requires. The first call starts with zeroed outputs. The caller must echo the
original bytes for command 0 and provide collection responses for other commands.
`AbortSignal`, a timeout, a process exit, and a closed modal do not establish a
financial cancellation. Send `continueFlag:-1` and process the terminal result.
There is no automatic retry, confirmation, reversal, or journal in this low-level
host; the SDK/integrator owns durable decisions and recovery.

Explicit coverage gaps pending verification: direct secure-card/track
entrypoints with native callbacks and ASCII-interface variants are not exposed
as generic arbitrary FFI. They require verified signatures, cancellation and
bounded buffer tests. All standard transaction modalities remain accessible via
`start.functionId`, including secure-reader modalities 430/431, and unrestricted
raw `additionalParameters`. The initial helper must not be described as complete
coverage or homologated until these gaps and vendor simulator tests are closed.

Run own-contract tests using `cargo test` and `python3 tests/smoke.py`. The latter
compiles an original synthetic C shared library and tests actual process IPC;
it never loads vendor binaries.
`python3 tests/smoke_tcp.py` additionally verifies the opt-in socket handshake,
nonce removal before FFI, and the same loop against our own synthetic fixture.
