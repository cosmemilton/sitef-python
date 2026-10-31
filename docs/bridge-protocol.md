# Bridge protocol, version 1

This is the common wire contract for the owned Rust helper and the Node.js,
Python and PHP clients. It is a custom NDJSON protocol, not JSON-RPC 2.0: do not
add a `jsonrpc` property. The canonical helper source is `native/` in the Node
repository; ports may ship that owned source snapshot and build scripts, never
the proprietary CliSiTef DLLs, INI files, manuals, certificates or traces.

The production runtime is **Windows x64**. WSL/Linux builds are permitted only
for explicit `testMode` tests against our synthetic C fixture. Library paths are
absolute, supplied by the integrator, and point to the interface DLL. The helper
rejects Win32/ARM64 PE files and internal CliSiTef libraries.

## Transports

With no command-line arguments, the helper reads requests from stdin and writes
responses to stdout. Node/Python normally use this transport. Spawn an executable
directly, with separated arguments and no shell. On Windows, hide the helper
window. Consume both output streams without copying diagnostics into logs.

PHP on Windows may select an authenticated loopback socket to obtain socket
deadlines without relying on nonblocking `proc_open` pipes:

```text
sitef-bridge.exe --connect 127.0.0.1:PORT --token-env SITEF_BRIDGE_TOKEN
```

The client binds a listener to `127.0.0.1` on an ephemeral port **before** spawning
the helper. Generate a fresh cryptographically random 32-byte nonce, represented
as 64 hexadecimal characters; pass it only in the child environment under the
named variable, never as a command argument. The helper removes that variable
before connecting or loading any vendor library. It connects within a 10-second
connect timeout. Only the exact IPv4 address `127.0.0.1` and a port from 1 to 65535
are accepted. DNS names, IPv6, other loopback IPs and remote addresses are rejected.

The first line on that socket is an authentication handshake, not a reply:

```json
{"protocolVersion":1,"transport":"tcp-loopback","token":"<the nonce supplied by the client>"}
```

Before sending requests, the client must bound/read this line, require protocol
version 1 and transport `tcp-loopback`, and compare the nonce in constant time.
Reject a wrong/truncated handshake; do not log it. There is no server-side bridge
listener, reconnect or session resumption. Subsequent socket frames use exactly
the same request/response contract as stdio. TCP mode emits no protocol or error
diagnostics to stdout/stderr. Startup argument errors exit with code 2; startup
or transport I/O failures exit with code 1; normal EOF exits with code 0. These
exit codes do not describe a financial outcome.

## Framing and envelopes

Each frame is one UTF-8 JSON object followed by LF, without a BOM. Embedded
receipt line breaks must be escaped by the JSON serializer. Do not pretty-print
wire frames. CRLF input is accepted as trailing JSON whitespace; output uses LF.
Clients serialize calls and keep at most one request outstanding per helper.
The helper loads one library for its lifetime and executes native calls in order.

Request schema:

```json
{"id":1,"method":"capabilities","params":{}}
```

`id` is a nonnegative integer; common clients must limit it to JavaScript's safe
integer range, 0 through 9007199254740991. The native parser accepts `u64`, but
larger IDs cannot be represented exactly by every client. `method` is a string.
`params` is an object; omit it only to use `{}`. Outer properties other than
`id`, `method`, and `params` are rejected. Typed parameter objects reject unknown
properties rather than silently dropping them. Parameterless methods use `{}`.

Success envelope:

```json
{"id":1,"result":{"status":0}}
```

Bridge-error envelope:

```json
{"id":1,"error":{"code":"MISSING_SYMBOL","message":"Requested function is not exported by this library version"}}
```

A reply has exactly one of `result` or `error`; its ID must match the outstanding
request. Malformed/oversized requests may receive `id:null`. The request allocation
limit is 2097152 bytes including framing, checked before JSON parsing. An oversized
frame terminates the helper after its error reply. Clients also need bounded
response buffers, strict UTF-8/JSON parsing, expected IDs and deadlines. A native
buffer can expand when encoded as UTF-8 plus base64; do not assume its byte capacity
equals the resulting JSON frame size. The normal continuation capacity is 20000.

## Parameter and result schemas

Names and casing are identical across language clients. All native text inputs
reject embedded NUL; numeric native inputs are integers, never coerced booleans.
`additionalParameters` is the unmodified native string, default `""`, not a closed
list of recognized options. Future vendor options must remain expressible.

| Method | Parameters | Result |
| --- | --- | --- |
| `load` | `libraryPath:string`; optional `workingDirectory:string`, `nativeEncoding`, `testMode:boolean=false` | Capability object below |
| `capabilities` | `{}` | Capability object |
| `configure` | `server:string`, `storeId:string`, `terminalId:string`; `reserved:integer=0`, `additionalParameters:string=""` | `{status:integer}` |
| `start` | `functionId:int32`, `amount:string`, `fiscalDocument:string`, `fiscalDate:string`, `fiscalTime:string`, `operator:string`; `additionalParameters:string=""` | `{status:integer}` |
| `continue` | `continueFlag:integer`, `bufferSize:integer`; `buffer:string=""`, optional `bufferBase64:string` | Continuation result below |
| `finish` | `confirm:0\|1`, `fiscalDocument:string`, `fiscalDate:string`, `fiscalTime:string`; `additionalParameters:string=""` | `{returned:true}` |
| `version` | `{}` | `{status,libraryVersion:NativeText,interfaceVersion:NativeText}` |
| `pendingCount` | `fiscalDocument:string`, `fiscalDate:string` | `{count:integer,status:integer}`; both preserve the same native return |
| `flushMessages` | `{}` | `{status:integer}` |
| `pinpadPresent`, `pinpadKeepAlive`, `pinpadKey` | `{}` | `{status:integer}` |
| `pinpadPermanentMessage`, `pinpadMessage`, `pinpadYesNo` | `message:string` | `{status:integer}` |
| `validateBarcode` | `data:string` | `{status:integer,documentType:integer}` |
| `removeCard` | `{}` | `{status:integer}` |
| `startBanking` | `fiscalDocument:string`, `fiscalDate:string`, `fiscalTime:string`, `operator:string`; `additionalParameters:string=""` | `{status:integer}`; may start continuation |
| `readSecureCard`, `readTrack3` | `message:string` | `{status:integer}`; may start continuation |
| `readPassword` | `securityKey:string` | `{status:integer}`; may start continuation |
| `readPasswordDirect` | `securityKey:string` | `{status:integer,encryptedPassword:NativeText}` |
| `readChip` | `modality:int32`; `additionalParameters:string=""` | `{status:integer}`; may start continuation |
| `pinpadInfo` | `{}` | Always bridge error `UNVERIFIED_OUTPUT_BOUND`; use function ID 775 through continuation instead |

`configureEx` and `readChipEx` are capability entries for export variants, not
separate methods. Their `invocationMethod` values are `configure` and `readChip`.
Those methods prefer the extended export; they may use the basic export only
when the extended one is absent and no additional parameters would be discarded.

Store/terminal identifiers are exactly 8 **encoded bytes**. Fiscal document and
operator are at most 20 encoded bytes. Fiscal dates/times use `AAAAMMDD`/`HHMMSS`;
SDKs validate calendar/time values. Transaction amounts are decimal strings with
comma and two decimals, or empty when the amount will be collected later. Other
collected quantities may have their own precision; retain native text. Security
keys for the proprietary-card password API contain exactly 64 encoded bytes.
These APIs do not authorize collecting ordinary payment-card PINs in an HTML form.

`nativeEncoding` accepts `windows-1252` (current explicit default), `utf-8` or
`ascii`. The integrator must verify the encoding for its vendor distribution.
`NativeText` is `{text:string,rawBase64:string,decodingError:boolean}`. Lossy output
decoding is marked and the original bytes are retained; it does not rewrite status.

Continuation result:

```json
{"status":10000,"command":30,"fieldType":999123,"minLength":1,"maxLength":4,"buffer":"Collect","bufferBase64":"Q29sbGVjdA==","decodingError":false}
```

That field is a synthetic example, not a documented product field. Native
`fieldType` remains an unrestricted numeric field ID. `bufferSize` is from 20000
through 1048576; the input and terminating NUL must fit. If `bufferBase64` is
provided, those bytes take precedence over text encoding. Raw bytes cannot include
NUL. On command 0, echo the exact original native bytes, preferably the returned
base64, until navigation/cancellation changes the flow. The helper preserves the
last command/type/minimum/maximum internally between continuation calls.

Continue while native status is 10000. Continuation flags are `0` next, `1` back,
`2` cancel current bill in the documented bill-payment contexts, `10000` for
previously collected and confirmed input, and `-1` for cooperative cancellation.
Do not invent acceptance of flag 2 or 10000 for every prompt. Plain-field byte
limits are enforced for commands 29/30/34/41 when submitting a value. Cheque and
barcode prefixes/cancellation payloads have separate application-level semantics.

Only interpret returned commands when status is 10000. Unknown fields may be
stored/forwarded while executing their command; unknown interactive commands
require an explicit handler or cooperative cancellation, never implicit success.
Printing receipts and the application/fiscal decision remain client obligations.

## Capabilities and lifecycle

The capability object contains `protocolVersion`, `bridgeVersion`, `platform`,
`architecture`, `runtimeTarget`, `nativeEncoding`, `fieldTypeBytes`, `testMode`,
`minimumBufferSize`, and `functions`. Production values include platform `windows`,
architecture `x86_64`, runtime target `windows-x64` and 4-byte field type.

Each `functions` entry contains `{symbol,available,callable,invocationMethod}`.
`available` means the named export exists. `callable` means its wrapper can be
called; state restrictions still apply. Export variants and deliberately blocked
functions have `callable:false`. `configure`/`readChip` can be callable through an
extended export even if their basic export's `available` is false. Export presence
does not prove support for every newer documented operation or field.

After load, configure before starting an operation. Only one native iterative
operation is active at a time. Configuration, new starts, finish and ordinary
auxiliary calls are rejected while one is active; the specifically documented
direct collection helpers do not start a second loop. A terminal loop result and
the final fiscal decision are distinct. `finish` wraps a native void function:
`returned:true` establishes local return, not remote confirmation or delivery.

This low-level helper has no durable financial journal and no automatic retry,
confirmation or reversal policy. The language SDK/integrator must persist identity
and decision before financial calls, preserve selective finalization parameters,
lock each terminal, and reconcile uncertain outcomes. Timeout, EOF, malformed
frames, a killed child and a closed modal do not establish cancellation. Never
repeat a financial call automatically after a lost reply. Normal EOF unloads the
helper; it does not automatically finalize an outstanding transaction.

## Errors and native return domains

Bridge errors are separate from native returns. Known current bridge codes:

| Category | Codes |
| --- | --- |
| Request/dispatch | `INVALID_REQUEST`, `INVALID_PARAMETER`, `FRAME_TOO_LARGE`, `UNSUPPORTED_METHOD` |
| Library/platform | `NOT_LOADED`, `ALREADY_LOADED`, `LIBRARY_NOT_FOUND`, `INVALID_LIBRARY_PATH`, `INVALID_LIBRARY_FORMAT`, `WRONG_LIBRARY_ARCHITECTURE`, `LIBRARY_LOAD_FAILED`, `INVALID_WORKING_DIRECTORY`, `UNSUPPORTED_PLATFORM`, `MISSING_SYMBOL` |
| Session | `NOT_CONFIGURED`, `TRANSACTION_ACTIVE`, `NO_ACTIVE_TRANSACTION` |
| Encoding/buffers | `ENCODING_ERROR`, `BUFFER_TOO_SMALL`, `INVALID_BUFFER_SIZE`, `INVALID_INPUT_LENGTH`, `INVALID_CONTINUE_FLAG`, `INVALID_NATIVE_LIMITS`, `DATA_ECHO_REQUIRED`, `UNTERMINATED_BUFFER` |
| Explicit implementation gate | `UNVERIFIED_OUTPUT_BOUND` |

Messages are for humans; program against codes and retain unknown future codes.
Do not interpret every bridge error as proof that no native side effect occurred:
an output-buffer error can be detected after entering the library. Local client
timeouts/process failures also require an explicit uncertain-state policy.

Native `{status}` values are retained verbatim and interpreted in their own domain:
transaction 0 succeeds, 10000 continues, other positives indicate authorizer denial,
and negatives indicate the documented cancellation/error/reserved conditions.
Configuration, pending counts, pinpad presence/keys/confirmation, barcode validation
and secure-read functions have different meanings for the same integers. Consult
the contextual tables/helpers; do not label a positive pending count as denial.
Never log field contents, receipts, PAN/tracks/PIN material, security keys or nonce.

## Shared catalog and remaining coverage

`protocol/catalog.json` is generated from our owned TypeScript constants by
`node scripts/export-catalog.mjs`. `--check` verifies the snapshot without writing.
It needs Node >=22.18 and no compiled `dist` or vendor library. Its schema version
is 1; numeric maps use JSON string keys, `fieldIds` maps friendly aliases to integers,
and `fieldRanges` is an array of `{from,to,kind}`. The source checksum binds the
snapshot to UTF-8 TypeScript source with CRLF normalized to LF, making it stable
across Windows/Linux checkouts. Python/PHP may package this same data file.

The snapshot includes metadata, context-specific return tables, continuation
flags, commands, field aliases/ranges, events, function names and current INI/
additional-parameter hints. These lists are not allowlists. The field-by-field
VRS-258 audit identified 444 field entries; full semantics and product workflows
remain TODO. Aliases, exported symbols and synthetic tests do not establish full
implementation or homologation. The real supplier DLL has only been checked for
load/configuration/version; no financial transaction or pinpad operation has been
validated against it. See `implementation-status.md` and `validation.md`.
