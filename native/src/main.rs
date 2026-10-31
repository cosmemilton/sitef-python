//! Native ABI boundary, implemented from manufacturer documentation, not from
//! another integration project. No CliSiTef binary is linked or distributed.
#[cfg(all(windows, not(target_arch = "x86_64")))]
compile_error!("The production CliSiTef runtime supports Windows x64 only");

use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use encoding_rs::WINDOWS_1252;
use libloading::Library;
use serde::Deserialize;
use serde_json::{json, Value};
use std::ffi::{c_char, c_long, c_void, CString};
use std::io::{self, BufRead, Read, Write};
#[cfg(windows)]
use std::io::{Seek, SeekFrom};
use std::path::Path;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, TcpStream};
use std::time::Duration;

const MAX_FRAME: usize = 2 * 1024 * 1024;
const MIN_BUFFER: usize = 20_000;
const MAX_BUFFER: usize = 1024 * 1024;
const CONTINUE: i32 = 10_000;

// The VRS-258 manual specifies long* for TipoCampo. C long is 32-bit on
// Windows x64. Unix is used only for our explicitly selected synthetic ABI
// fixtures; it is not a production runtime target.
type Configure = unsafe extern "system" fn(*const c_char, *const c_char, *const c_char, i16) -> i32;
type ConfigureEx = unsafe extern "system" fn(*const c_char, *const c_char, *const c_char, i16, *const c_char) -> i32;
type Start = unsafe extern "system" fn(i32, *const c_char, *const c_char, *const c_char, *const c_char, *const c_char, *const c_char) -> i32;
type ContinueFn = unsafe extern "system" fn(*mut i32, *mut c_long, *mut i16, *mut i16, *mut c_char, i32, i32) -> i32;
type Finish = unsafe extern "system" fn(i16, *const c_char, *const c_char, *const c_char, *const c_char);
type NoArgs = unsafe extern "system" fn() -> i32;
type OneString = unsafe extern "system" fn(*const c_char) -> i32;
type TwoStrings = unsafe extern "system" fn(*const c_char, *const c_char) -> i32;
type TwoOutputStrings = unsafe extern "system" fn(*mut c_char, *mut c_char) -> i32;
type Barcode = unsafe extern "system" fn(*const c_char, *mut i16) -> i32;
type RemoveCard = unsafe extern "system" fn(*const c_void, *const c_void) -> i32;
type Banking = unsafe extern "system" fn(*const c_char, *const c_char, *const c_char, *const c_char, *const c_char) -> i32;
type IntArg = unsafe extern "system" fn(i32) -> i32;
type IntString = unsafe extern "system" fn(i32, *const c_char) -> i32;
type PasswordDirect = unsafe extern "system" fn(*const c_char, *mut c_char) -> i32;

#[derive(Debug)]
struct BridgeError { code: &'static str, message: &'static str }
type Result<T> = std::result::Result<T, BridgeError>;
fn fail<T>(code: &'static str, message: &'static str) -> Result<T> { Err(BridgeError { code, message }) }

#[derive(Clone, Copy, Deserialize, Default)]
#[serde(rename_all = "kebab-case")]
enum NativeEncoding {
    #[default]
    #[serde(rename="windows-1252")]
    Windows1252,
    #[serde(rename="utf-8")]
    Utf8,
    Ascii,
}
impl NativeEncoding {
    fn name(self) -> &'static str {
        match self { Self::Windows1252 => "windows-1252", Self::Utf8 => "utf-8", Self::Ascii => "ascii" }
    }
    fn encode(self, text: &str) -> Result<Vec<u8>> {
        if text.contains('\0') { return fail("INVALID_PARAMETER", "Native strings cannot contain NUL bytes"); }
        let bytes = match self {
            Self::Utf8 => text.as_bytes().to_vec(),
            Self::Ascii if text.is_ascii() => text.as_bytes().to_vec(),
            Self::Ascii => return fail("ENCODING_ERROR", "Input cannot be represented as ASCII"),
            Self::Windows1252 => {
                let (bytes, _, errors) = WINDOWS_1252.encode(text);
                if errors { return fail("ENCODING_ERROR", "Input cannot be represented as Windows-1252"); }
                bytes.into_owned()
            }
        };
        Ok(bytes)
    }
    fn string(self, text: &str) -> Result<CString> {
        CString::new(self.encode(text)?).map_err(|_| BridgeError { code: "INVALID_PARAMETER", message: "Invalid native string" })
    }
    fn max_bytes(self,text:&str,max:usize)->Result<()> {
        if self.encode(text)?.len()>max {return fail("INVALID_PARAMETER","Encoded native input exceeds its documented byte limit");}
        Ok(())
    }
    fn exact_bytes(self,text:&str,expected:usize)->Result<()> {
        if self.encode(text)?.len()!=expected {return fail("INVALID_PARAMETER","Encoded native input does not match its documented fixed byte length");}
        Ok(())
    }
    fn decode(self, bytes: &[u8]) -> (String, bool) {
        match self {
            Self::Utf8 => match std::str::from_utf8(bytes) { Ok(s) => (s.to_owned(), false), Err(_) => (String::from_utf8_lossy(bytes).into_owned(), true) },
            Self::Ascii => (bytes.iter().map(|&b| if b.is_ascii() { b as char } else { '\u{fffd}' }).collect(), !bytes.is_ascii()),
            Self::Windows1252 => { let (text, _, errors) = WINDOWS_1252.decode(bytes); (text.into_owned(), errors) }
        }
    }
}

#[derive(Default)]
struct LoopState { command: i32, field_type: c_long, min: i16, max: i16, buffer: Vec<u8> }
struct Host {
    // Function pointers never outlive this owner. All calls run on this single thread.
    library: Library,
    encoding: NativeEncoding,
    configured: bool,
    active: bool,
    state: LoopState,
    test_mode: bool,
}

// Windows x64 uses undecorated exports. Only a fixed, documented symbol
// allowlist is passed to this resolver.
unsafe fn symbol<T: Copy>(library: &Library, name: &str, _stack_bytes: u32) -> Option<T> {
    if let Ok(s) = library.get::<T>(name.as_bytes()) { return Some(*s); }
    None
}

impl Host {
    unsafe fn get<T: Copy>(&self, name: &str, stack_bytes: u32) -> Result<T> {
        symbol(&self.library, name, stack_bytes).ok_or(BridgeError { code: "MISSING_SYMBOL", message: "Requested function is not exported by this library version" })
    }
    fn idle(&self) -> Result<()> {
        if self.active { fail("TRANSACTION_ACTIVE", "A native transaction is still active") } else { Ok(()) }
    }
    fn ready(&self) -> Result<()> {
        if self.configured { Ok(()) } else { fail("NOT_CONFIGURED", "Configure the native library first") }
    }
    fn finish_start(&mut self, status: i32) -> Value {
        self.state = LoopState::default();
        self.active = status == CONTINUE;
        json!({"status":status})
    }
    fn capabilities(&self) -> Value {
        let mut functions = serde_json::Map::new();
        for &(method, name, stack) in FUNCTIONS {
            let available = unsafe { symbol::<unsafe extern "system" fn()>(&self.library, name, stack).is_some() };
            let invocation_method=match method {"configureEx"=>"configure","readChipEx"=>"readChip",_=>method};
            let callable=match method {
                "configureEx"|"readChipEx"|"pinpadInfo"=>false,
                "configure"=>available || unsafe {symbol::<ConfigureEx>(&self.library,"ConfiguraIntSiTefInterativoEx",20).is_some()},
                "readChip"=>available || unsafe {symbol::<IntString>(&self.library,"LeTrilhaChipInterativoEx",8).is_some()},
                _=>available,
            };
            functions.insert(method.into(), json!({"symbol":name,"available":available,"callable":callable,"invocationMethod":invocation_method}));
        }
        json!({"protocolVersion":1,"bridgeVersion":env!("CARGO_PKG_VERSION"),"platform":std::env::consts::OS,"architecture":std::env::consts::ARCH,"runtimeTarget":"windows-x64","nativeEncoding":self.encoding.name(),"fieldTypeBytes":std::mem::size_of::<c_long>(),"testMode":self.test_mode,"minimumBufferSize":MIN_BUFFER,"functions":functions})
    }
    fn call(&mut self, method: &str, params: Value) -> Result<Value> {
        match method {
            "capabilities" => Ok(self.capabilities()),
            "configure" => {
                self.idle()?;
                let p: ConfigureParams = parse(params)?;
                if p.reserved != 0 { return fail("INVALID_PARAMETER", "reserved must be zero"); }
                self.encoding.exact_bytes(&p.store_id,8)?;
                self.encoding.exact_bytes(&p.terminal_id,8)?;
                let server = self.encoding.string(&p.server)?;
                let store = self.encoding.string(&p.store_id)?;
                let terminal = self.encoding.string(&p.terminal_id)?;
                let additional = self.encoding.string(&p.additional_parameters)?;
                let status = unsafe {
                    if let Some(f) = symbol::<ConfigureEx>(&self.library, "ConfiguraIntSiTefInterativoEx", 20) {
                        f(server.as_ptr(), store.as_ptr(), terminal.as_ptr(), 0, additional.as_ptr())
                    } else {
                        if !p.additional_parameters.is_empty() { return fail("MISSING_SYMBOL", "Extended configuration is unavailable and additional parameters cannot be discarded"); }
                        let f: Configure = self.get("ConfiguraIntSiTefInterativo", 16)?;
                        f(server.as_ptr(), store.as_ptr(), terminal.as_ptr(), 0)
                    }
                };
                self.configured = status == 0;
                Ok(json!({"status":status}))
            }
            "start" => {
                self.ready()?; self.idle()?;
                let p: StartParams = parse(params)?;
                self.encoding.max_bytes(&p.fiscal_document,20)?;
                self.encoding.max_bytes(&p.operator,20)?;
                let strings = [&p.amount, &p.fiscal_document, &p.fiscal_date, &p.fiscal_time, &p.operator, &p.additional_parameters].iter().map(|s| self.encoding.string(s)).collect::<Result<Vec<_>>>()?;
                let f: Start = unsafe { self.get("IniciaFuncaoSiTefInterativo", 28)? };
                let status = unsafe { f(p.function_id, strings[0].as_ptr(), strings[1].as_ptr(), strings[2].as_ptr(), strings[3].as_ptr(), strings[4].as_ptr(), strings[5].as_ptr()) };
                Ok(self.finish_start(status))
            }
            "continue" => self.continue_call(parse(params)?),
            "finish" => {
                self.ready()?; self.idle()?;
                let p: FinishParams = parse(params)?;
                if p.confirm != 0 && p.confirm != 1 { return fail("INVALID_PARAMETER", "confirm must be zero or one"); }
                self.encoding.max_bytes(&p.fiscal_document,20)?;
                let strings = [&p.fiscal_document, &p.fiscal_date, &p.fiscal_time, &p.additional_parameters].iter().map(|s| self.encoding.string(s)).collect::<Result<Vec<_>>>()?;
                let f: Finish = unsafe { self.get("FinalizaFuncaoSiTefInterativo", 20)? };
                unsafe { f(p.confirm, strings[0].as_ptr(), strings[1].as_ptr(), strings[2].as_ptr(), strings[3].as_ptr()) };
                Ok(json!({"returned":true}))
            }
            "version" => {
                self.idle()?;
                let f: TwoOutputStrings = unsafe { self.get("ObtemVersao", 8)? };
                let mut a = vec![0u8; 65]; let mut b = vec![0u8; 65];
                let status = unsafe { f(a.as_mut_ptr().cast(), b.as_mut_ptr().cast()) };
                Ok(json!({"status":status,"libraryVersion":self.output(&a)?,"interfaceVersion":self.output(&b)?}))
            }
            "pendingCount" => {
                self.ready()?; self.idle()?;
                let p: PendingParams = parse(params)?;
                self.encoding.max_bytes(&p.fiscal_document,20)?;
                let date = self.encoding.string(&p.fiscal_date)?;
                let document = self.encoding.string(&p.fiscal_document)?;
                let f: TwoStrings = unsafe { self.get("ObtemQuantidadeTransacoesPendentes", 8)? };
                let count = unsafe { f(date.as_ptr(), document.as_ptr()) };
                // Positive return values here are counts, not authorizer denials.
                Ok(json!({"count":count,"status":count}))
            }
            "flushMessages" | "pinpadPresent" | "pinpadKeepAlive" | "pinpadKey" => {
                self.ready()?; self.idle()?;
                let name = match method { "flushMessages" => "DescarregaMensagens", "pinpadPresent" => "VerificaPresencaPinPad", "pinpadKeepAlive" => "KeepAlivePinPad", _ => "LeTeclaPinPad" };
                let f: NoArgs = unsafe { self.get(name, 0)? };
                Ok(json!({"status":unsafe {f()}}))
            }
            "pinpadPermanentMessage" | "pinpadMessage" | "pinpadYesNo" => {
                self.ready()?; self.idle()?;
                let p: MessageParams = parse(params)?;
                let message = self.encoding.string(&p.message)?;
                let name = match method { "pinpadPermanentMessage" => "EscreveMensagemPermanentePinPad", "pinpadMessage" => "EscreveMensagemPinPad", _ => "LeSimNaoPinPad" };
                let f: OneString = unsafe { self.get(name, 4)? };
                Ok(json!({"status":unsafe {f(message.as_ptr())}}))
            }
            "pinpadInfo" => {
                // VRS-258 gives a minimum allocation, not a proven maximum; the
                // ABI supplies no output capacity. Do not guess a safe bound.
                fail("UNVERIFIED_OUTPUT_BOUND","Direct pinpad information has no verified output bound; use start functionId 775 and the bounded continuation loop")
            }
            "validateBarcode" => {
                // May be called during command 31/35 collection; no transaction is started.
                self.ready()?;
                let p: BarcodeParams = parse(params)?;
                let data = self.encoding.string(&p.data)?;
                let f: Barcode = unsafe { self.get("ValidaCampoCodigoEmBarras", 8)? };
                let mut kind: i16 = -1;
                let status = unsafe { f(data.as_ptr(), &mut kind) };
                Ok(json!({"status":status,"documentType":kind}))
            }
            "removeCard" => {
                self.ready()?; self.idle()?;
                let f: RemoveCard = unsafe { self.get("RemoveCartaoPinPad", 8)? };
                Ok(json!({"status":unsafe {f(std::ptr::null(),std::ptr::null())}}))
            }
            "startBanking" => {
                self.ready()?; self.idle()?;
                let p: BankingParams = parse(params)?;
                self.encoding.max_bytes(&p.fiscal_document,20)?;
                self.encoding.max_bytes(&p.operator,20)?;
                let strings = [&p.fiscal_document, &p.fiscal_date, &p.fiscal_time, &p.operator, &p.additional_parameters].iter().map(|s| self.encoding.string(s)).collect::<Result<Vec<_>>>()?;
                let f: Banking = unsafe { self.get("CorrespondenteBancarioSiTefInterativo", 20)? };
                let status = unsafe {f(strings[0].as_ptr(),strings[1].as_ptr(),strings[2].as_ptr(),strings[3].as_ptr(),strings[4].as_ptr())};
                Ok(self.finish_start(status))
            }
            "readSecureCard" | "readTrack3" => {
                self.ready()?; self.idle()?;
                let p: MessageParams = parse(params)?;
                let data = self.encoding.string(&p.message)?;
                let name = if method=="readSecureCard" { "LeCartaoSeguro" } else { "LeTrilha3" };
                let f: OneString = unsafe {self.get(name,4)?};
                let status = unsafe {f(data.as_ptr())};
                Ok(self.finish_start(status))
            }
            "readPassword" | "readPasswordDirect" => {
                self.ready()?;
                let p: SecurityKeyParams = parse(params)?;
                let key_bytes = self.encoding.encode(&p.security_key)?;
                if key_bytes.len()!=64 {return fail("INVALID_PARAMETER","securityKey must contain exactly 64 encoded bytes");}
                let key = CString::new(key_bytes).map_err(|_|BridgeError {code:"INVALID_PARAMETER",message:"Invalid securityKey"})?;
                if method=="readPasswordDirect" {
                    // Direct reader is documented for use inside an interactive
                    // collection. It does not initiate a second native loop.
                    let f:PasswordDirect = unsafe {self.get("LeSenhaDireto",8)?};
                    let mut encrypted=vec![0u8;21];
                    let status=unsafe {f(key.as_ptr(),encrypted.as_mut_ptr().cast())};
                    Ok(json!({"status":status,"encryptedPassword":self.output(&encrypted)?}))
                } else {
                    self.idle()?;
                    let f:OneString = unsafe {self.get("LeSenhaInterativo",4)?};
                    let status=unsafe {f(key.as_ptr())};
                    Ok(self.finish_start(status))
                }
            }
            "readChip" => {
                self.ready()?; self.idle()?;
                let p: ChipParams = parse(params)?;
                let status = unsafe {
                    if let Some(f) = symbol::<IntString>(&self.library,"LeTrilhaChipInterativoEx",8) {
                        let additional = self.encoding.string(&p.additional_parameters)?;
                        f(p.modality, additional.as_ptr())
                    } else {
                        if !p.additional_parameters.is_empty() {return fail("MISSING_SYMBOL","Extended chip reading is unavailable; additional parameters cannot be discarded");}
                        let f: IntArg = self.get("LeTrilhaChipInterativo",4)?;
                        f(p.modality)
                    }
                };
                Ok(self.finish_start(status))
            }
            _ => fail("UNSUPPORTED_METHOD", "Unknown bridge method"),
        }
    }
    fn output(&self, bytes: &[u8]) -> Result<Value> {
        let end = bytes.iter().position(|&b| b == 0).ok_or(BridgeError {code:"UNTERMINATED_BUFFER",message:"Native function did not NUL-terminate its output"})?;
        let raw = &bytes[..end];
        let (text, decoding_error) = self.encoding.decode(raw);
        Ok(json!({"text":text,"rawBase64":BASE64.encode(raw),"decodingError":decoding_error}))
    }
    fn continue_call(&mut self, p: ContinueParams) -> Result<Value> {
        self.ready()?;
        if !self.active { return fail("NO_ACTIVE_TRANSACTION", "No iterative transaction is active"); }
        if !(MIN_BUFFER..=MAX_BUFFER).contains(&p.buffer_size) { return fail("INVALID_BUFFER_SIZE", "bufferSize must be between 20000 and 1048576 bytes"); }
        let raw = match p.buffer_base64 {
            Some(s) => BASE64.decode(s).map_err(|_|BridgeError {code:"INVALID_PARAMETER",message:"Invalid base64 native buffer"})?,
            None => self.encoding.encode(&p.buffer)?,
        };
        if raw.contains(&0) {return fail("INVALID_PARAMETER","Native buffer cannot contain NUL bytes");}
        if raw.len() >= p.buffer_size {return fail("BUFFER_TOO_SMALL","Input and terminating NUL must fit in bufferSize");}
        validate_collection(self.state.command,self.state.min,self.state.max,&raw,p.continue_flag)?;
        if self.state.command == 0 && matches!(p.continue_flag,0|10000) && raw != self.state.buffer {
            return fail("DATA_ECHO_REQUIRED","Command zero must echo the exact original native buffer bytes");
        }
        let f: ContinueFn = unsafe {self.get("ContinuaFuncaoSiTefInterativo",28)?};
        let mut buffer = vec![0u8;p.buffer_size];
        buffer[..raw.len()].copy_from_slice(&raw);
        let status = unsafe {f(&mut self.state.command,&mut self.state.field_type,&mut self.state.min,&mut self.state.max,buffer.as_mut_ptr().cast(),p.buffer_size as i32,p.continue_flag)};
        self.active = status == CONTINUE;
        let end = buffer.iter().position(|&b|b == 0).ok_or(BridgeError {code:"UNTERMINATED_BUFFER",message:"Native continuation did not NUL-terminate its output; transaction outcome is uncertain"})?;
        self.state.buffer = buffer[..end].to_vec();
        let (text, decoding_error) = self.encoding.decode(&self.state.buffer);
        Ok(json!({"status":status,"command":self.state.command,"fieldType":self.state.field_type,"minLength":self.state.min,"maxLength":self.state.max,"buffer":text,"bufferBase64":BASE64.encode(&self.state.buffer),"decodingError":decoding_error}))
    }
}

fn validate_collection(command:i32,min:i16,max:i16,raw:&[u8],flag:i32)->Result<()> {
    if !matches!(flag,-1|0|1|2|10000) {return fail("INVALID_CONTINUE_FLAG","continueFlag is not a documented navigation value");}
    // Navigation and cancellation do not submit a collected value. Cheque and
    // barcode commands have prefixes/cancellation payloads and require their
    // own validation, so do not apply plain-field byte limits to them.
    if matches!(flag,0|10000) && matches!(command,29|30|34|41) {
        if min<0 || max<min {return fail("INVALID_NATIVE_LIMITS","Native collection length limits are invalid");}
        if raw.len()<min as usize || raw.len()>max as usize {return fail("INVALID_INPUT_LENGTH","Encoded field length is outside the native byte limits");}
    }
    Ok(())
}

const FUNCTIONS: &[(&str,&str,u32)] = &[
    ("configure","ConfiguraIntSiTefInterativo",16),("configureEx","ConfiguraIntSiTefInterativoEx",20),
    ("start","IniciaFuncaoSiTefInterativo",28),("continue","ContinuaFuncaoSiTefInterativo",28),("finish","FinalizaFuncaoSiTefInterativo",20),
    ("version","ObtemVersao",8),("pendingCount","ObtemQuantidadeTransacoesPendentes",8),("flushMessages","DescarregaMensagens",0),
    ("pinpadPresent","VerificaPresencaPinPad",0),("pinpadKeepAlive","KeepAlivePinPad",0),("pinpadKey","LeTeclaPinPad",0),
    ("pinpadPermanentMessage","EscreveMensagemPermanentePinPad",4),("pinpadMessage","EscreveMensagemPinPad",4),("pinpadYesNo","LeSimNaoPinPad",4),
    ("pinpadInfo","ObtemInformacoesPinPad",4),("validateBarcode","ValidaCampoCodigoEmBarras",8),("removeCard","RemoveCartaoPinPad",8),
    ("startBanking","CorrespondenteBancarioSiTefInterativo",20),("readSecureCard","LeCartaoSeguro",4),("readTrack3","LeTrilha3",4),
    ("readPassword","LeSenhaInterativo",4),("readPasswordDirect","LeSenhaDireto",8),("readChip","LeTrilhaChipInterativo",4),("readChipEx","LeTrilhaChipInterativoEx",8),
];

#[derive(Deserialize)]
#[serde(rename_all="camelCase",deny_unknown_fields)]
struct LoadParams { library_path:String, working_directory:Option<String>, #[serde(default)] native_encoding:NativeEncoding, #[serde(default)] test_mode:bool }
#[derive(Deserialize)]
#[serde(rename_all="camelCase",deny_unknown_fields)]
struct ConfigureParams { server:String, store_id:String, terminal_id:String, #[serde(default)] reserved:i16, #[serde(default)] additional_parameters:String }
#[derive(Deserialize)]
#[serde(rename_all="camelCase",deny_unknown_fields)]
struct StartParams { function_id:i32, amount:String, fiscal_document:String, fiscal_date:String, fiscal_time:String, operator:String, #[serde(default)] additional_parameters:String }
#[derive(Deserialize)]
#[serde(rename_all="camelCase",deny_unknown_fields)]
struct ContinueParams { #[serde(default)] buffer:String, buffer_base64:Option<String>, continue_flag:i32, buffer_size:usize }
#[derive(Deserialize)]
#[serde(rename_all="camelCase",deny_unknown_fields)]
struct FinishParams { confirm:i16, fiscal_document:String, fiscal_date:String, fiscal_time:String, #[serde(default)] additional_parameters:String }
#[derive(Deserialize)]
#[serde(rename_all="camelCase",deny_unknown_fields)]
struct PendingParams { fiscal_document:String, fiscal_date:String }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MessageParams { message:String }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BarcodeParams { data:String }
#[derive(Deserialize)]
#[serde(rename_all="camelCase",deny_unknown_fields)]
struct BankingParams { fiscal_document:String, fiscal_date:String, fiscal_time:String, operator:String, #[serde(default)] additional_parameters:String }
#[derive(Deserialize)]
#[serde(rename_all="camelCase",deny_unknown_fields)]
struct ChipParams { modality:i32, #[serde(default)] additional_parameters:String }
#[derive(Deserialize)]
#[serde(rename_all="camelCase",deny_unknown_fields)]
struct SecurityKeyParams { security_key:String }
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request { id:u64, method:String, #[serde(default="empty_params")] params:Value }
fn empty_params()->Value {json!({})}
fn parse<T:serde::de::DeserializeOwned>(v:Value)->Result<T> {serde_json::from_value(v).map_err(|_|BridgeError {code:"INVALID_PARAMETER",message:"Request parameters do not match the method schema"})}

fn load(params:Value)->Result<Host> {
    let p:LoadParams = parse(params)?;
    #[cfg(not(windows))]
    if !p.test_mode {return fail("UNSUPPORTED_PLATFORM","Production runtime is Windows x64 only; non-Windows hosts permit explicit testMode synthetic fixtures only");}
    let path = Path::new(&p.library_path);
    if !path.is_absolute() {return fail("INVALID_LIBRARY_PATH","libraryPath must be absolute");}
    let path = path.canonicalize().map_err(|_|BridgeError {code:"LIBRARY_NOT_FOUND",message:"Native library does not exist or is not accessible"})?;
    if !path.is_file() {return fail("INVALID_LIBRARY_PATH","libraryPath must point to a file");}
    #[cfg(windows)]
    verify_x64_library(&path)?;
    let filename = path.file_name().and_then(|s|s.to_str()).unwrap_or("").to_ascii_lowercase();
    if matches!(filename.as_str(),"clisitef.dll"|"clisitef32.dll"|"clisitef64.dll"|"libclisitef.so") {return fail("INVALID_LIBRARY_PATH","Load the CliSiTefI interface library, not the internal CliSiTef library");}
    let directory = match p.working_directory {
        Some(ref s) => {let d=Path::new(s); if !d.is_absolute() {return fail("INVALID_WORKING_DIRECTORY","workingDirectory must be absolute");} d.canonicalize().map_err(|_|BridgeError {code:"INVALID_WORKING_DIRECTORY",message:"workingDirectory is not accessible"})?},
        None => path.parent().ok_or(BridgeError {code:"INVALID_LIBRARY_PATH",message:"Native library must have a parent directory"})?.to_path_buf(),
    };
    if !directory.is_dir() {return fail("INVALID_WORKING_DIRECTORY","workingDirectory must be a directory");}
    let library = unsafe {
        #[cfg(windows)]
        { Library::from(libloading::os::windows::Library::load_with_flags(&path,0x00000100|0x00001000).map_err(|_|BridgeError {code:"LIBRARY_LOAD_FAILED",message:"Cannot load interface library; verify architecture and companion dependencies"})?) }
        #[cfg(not(windows))]
        { Library::new(&path).map_err(|_|BridgeError {code:"LIBRARY_LOAD_FAILED",message:"Cannot load interface library; verify architecture and companion dependencies"})? }
    };
    for (name,stack) in [("IniciaFuncaoSiTefInterativo",28),("ContinuaFuncaoSiTefInterativo",28),("FinalizaFuncaoSiTefInterativo",20)] {
        if unsafe {symbol::<unsafe extern "system" fn()>(&library,name,stack)}.is_none() {return fail("MISSING_SYMBOL","Interface library does not export all required transaction functions");}
    }
    if unsafe {symbol::<Configure>(&library,"ConfiguraIntSiTefInterativo",16)}.is_none() && unsafe {symbol::<ConfigureEx>(&library,"ConfiguraIntSiTefInterativoEx",20)}.is_none() {return fail("MISSING_SYMBOL","Interface library does not export a configuration function");}
    std::env::set_current_dir(&directory).map_err(|_|BridgeError {code:"INVALID_WORKING_DIRECTORY",message:"Cannot enter workingDirectory"})?;
    Ok(Host {library,encoding:p.native_encoding,configured:false,active:false,state:LoopState::default(),test_mode:p.test_mode})
}

#[cfg(any(windows,test))]
fn validate_pe_headers(dos:&[u8],pe:&[u8])->Result<()> {
    if dos.len()<64 || &dos[..2]!=b"MZ" || pe.len()<26 || &pe[..4]!=b"PE\0\0" {return fail("INVALID_LIBRARY_FORMAT","Interface library must be a valid PE dynamic library");}
    let machine=u16::from_le_bytes([pe[4],pe[5]]);
    let magic=u16::from_le_bytes([pe[24],pe[25]]);
    if machine!=0x8664 || magic!=0x20b {return fail("WRONG_LIBRARY_ARCHITECTURE","Interface library must target Windows x64 (AMD64 PE32+)");}
    Ok(())
}

#[cfg(windows)]
fn verify_x64_library(path:&Path)->Result<()> {
    let mut file=std::fs::File::open(path).map_err(|_|BridgeError {code:"INVALID_LIBRARY_FORMAT",message:"Cannot read native PE header"})?;
    let mut dos=[0u8;64];
    file.read_exact(&mut dos).map_err(|_|BridgeError {code:"INVALID_LIBRARY_FORMAT",message:"Native library has an incomplete DOS header"})?;
    if &dos[..2]!=b"MZ" {return fail("INVALID_LIBRARY_FORMAT","Interface library must be a Windows PE file");}
    let offset=u32::from_le_bytes([dos[60],dos[61],dos[62],dos[63]]) as u64;
    if offset<64 {return fail("INVALID_LIBRARY_FORMAT","Native library has an invalid PE header offset");}
    file.seek(SeekFrom::Start(offset)).map_err(|_|BridgeError {code:"INVALID_LIBRARY_FORMAT",message:"Cannot locate native PE header"})?;
    let mut pe=[0u8;26];
    file.read_exact(&mut pe).map_err(|_|BridgeError {code:"INVALID_LIBRARY_FORMAT",message:"Native library has an incomplete PE header"})?;
    validate_pe_headers(&dos,&pe)
}

fn respond(writer:&mut impl Write,id:Value,result:Result<Value>)->io::Result<()> {
    let response = match result {Ok(v)=>json!({"id":id,"result":v}),Err(e)=>json!({"id":id,"error":{"code":e.code,"message":e.message}})};
    serde_json::to_writer(&mut *writer,&response)?;
    writer.write_all(b"\n")?; writer.flush()
}

fn serve<R:BufRead,W:Write>(mut input:R,mut output:W)->io::Result<()> {
    let mut host:Option<Host>=None;
    loop {
        // Bound allocation before parsing. A malformed/oversized frame is fatal
        // because stream synchronization and financial outcome cannot be assumed.
        let mut frame=Vec::new();
        let read=(&mut input).take((MAX_FRAME+1) as u64).read_until(b'\n',&mut frame)?;
        if read==0 {break;}
        if frame.len()>MAX_FRAME {respond(&mut output,Value::Null,fail("FRAME_TOO_LARGE","IPC frame exceeds size limit"))?;break;}
        let request:Request=match serde_json::from_slice(&frame) {Ok(r)=>r,Err(_)=>{respond(&mut output,Value::Null,fail("INVALID_REQUEST","Malformed IPC request"))?;continue;}};
        let result = if request.method=="load" {
            if host.is_some() {fail("ALREADY_LOADED","A bridge process loads only one library for its lifetime")} else {
                match load(request.params) {Ok(h)=>{let cap=h.capabilities();host=Some(h);Ok(cap)},Err(e)=>Err(e)}
            }
        } else if let Some(ref mut h)=host {h.call(&request.method,request.params)} else {fail("NOT_LOADED","Load an interface library first")};
        respond(&mut output,json!(request.id),result)?;
    }
    Ok(())
}

#[derive(Debug,PartialEq)]
enum TransportMode { Stdio, Tcp {address:SocketAddrV4,token_env:String} }

fn transport_mode(args:&[String])->Result<TransportMode> {
    if args.is_empty() {return Ok(TransportMode::Stdio);}
    if args.len()!=4 {return fail("INVALID_ARGUMENTS","TCP transport requires --connect and --token-env");}
    let mut address=None;
    let mut token_env=None;
    for pair in args.chunks_exact(2) {
        match pair[0].as_str() {
            "--connect" if address.is_none()=>{
                let port=pair[1].strip_prefix("127.0.0.1:").ok_or(BridgeError {code:"INVALID_ARGUMENTS",message:"TCP transport permits only the exact IPv4 loopback address"})?;
                if port.is_empty() || !port.bytes().all(|b|b.is_ascii_digit()) {return fail("INVALID_ARGUMENTS","TCP transport requires a numeric loopback port");}
                let port:u16=port.parse().map_err(|_|BridgeError {code:"INVALID_ARGUMENTS",message:"TCP port is out of range"})?;
                if port==0 {return fail("INVALID_ARGUMENTS","TCP port must be greater than zero");}
                address=Some(SocketAddrV4::new(Ipv4Addr::LOCALHOST,port));
            }
            "--token-env" if token_env.is_none()=>{
                let key=&pair[1];
                let mut bytes=key.bytes();
                let first=bytes.next();
                if !first.is_some_and(|b|b.is_ascii_alphabetic()||b==b'_') || !bytes.all(|b|b.is_ascii_alphanumeric()||b==b'_') {return fail("INVALID_ARGUMENTS","Token environment name is invalid");}
                token_env=Some(key.clone());
            }
            _=>return fail("INVALID_ARGUMENTS","Unknown or duplicate transport argument"),
        }
    }
    match (address,token_env) {
        (Some(address),Some(token_env))=>Ok(TransportMode::Tcp {address,token_env}),
        _=>fail("INVALID_ARGUMENTS","TCP transport requires --connect and --token-env"),
    }
}

fn valid_transport_token(token:&str)->bool {token.len()==64 && token.bytes().all(|b|b.is_ascii_hexdigit())}
fn transport_handshake(token:&str)->Value {json!({"protocolVersion":1,"transport":"tcp-loopback","token":token})}

fn run_transport(mode:TransportMode)->io::Result<()> {
    match mode {
        TransportMode::Stdio=>serve(io::stdin().lock(),io::stdout().lock()),
        TransportMode::Tcp {address,token_env}=>{
            // Read/remove before connecting or loading a vendor library. The
            // nonce is never included in arguments, errors, stdout or stderr.
            let value=std::env::var(&token_env);
            std::env::remove_var(&token_env);
            let token=value.map_err(|_|io::Error::new(io::ErrorKind::InvalidInput,"Missing transport credential"))?;
            if !valid_transport_token(&token) {return Err(io::Error::new(io::ErrorKind::InvalidInput,"Invalid transport credential"));}
            let mut stream=TcpStream::connect_timeout(&SocketAddr::V4(address),Duration::from_secs(10))?;
            stream.set_nodelay(true)?;
            serde_json::to_writer(&mut stream,&transport_handshake(&token))?;
            stream.write_all(b"\n")?;
            stream.flush()?;
            drop(token);
            let reader=stream.try_clone()?;
            serve(io::BufReader::new(reader),stream)
        }
    }
}

fn main()->std::process::ExitCode {
    let args=std::env::args_os().skip(1).map(|value|value.into_string()).collect::<std::result::Result<Vec<_>,_>>();
    let mode=match args.ok().and_then(|args|transport_mode(&args).ok()) {Some(mode)=>mode,None=>return std::process::ExitCode::from(2)};
    // Startup/transport failures are intentionally silent. In TCP mode neither
    // standard stream carries protocol data or native/logging diagnostics.
    match run_transport(mode) {Ok(())=>std::process::ExitCode::SUCCESS,Err(_)=>std::process::ExitCode::from(1)}
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn input_rejects_nul_and_unrepresentable_text() {
        assert!(NativeEncoding::Windows1252.encode("x\0y").is_err());
        assert!(NativeEncoding::Windows1252.encode("💳").is_err());
        assert!(NativeEncoding::Ascii.encode("ação").is_err());
    }
    #[test] fn cp1252_round_trip_preserves_accents_and_euro() {
        let bytes=NativeEncoding::Windows1252.encode("Ação €").unwrap();
        assert_eq!(bytes,vec![65,231,227,111,32,128]);
        assert_eq!(NativeEncoding::Windows1252.decode(&bytes),("Ação €".into(),false));
    }
    #[test] fn utf8_corruption_is_explicit() {assert!(NativeEncoding::Utf8.decode(&[0xff]).1);}
    #[test] fn encoding_names_match_public_ipc_contract() {
        assert!(serde_json::from_value::<NativeEncoding>(json!("windows-1252")).is_ok());
        assert!(serde_json::from_value::<NativeEncoding>(json!("utf-8")).is_ok());
        assert!(serde_json::from_value::<NativeEncoding>(json!("ascii")).is_ok());
    }
    #[test] fn documented_input_limits_are_native_bytes() {
        let text="é".repeat(20);
        assert!(NativeEncoding::Windows1252.max_bytes(&text,20).is_ok());
        assert!(NativeEncoding::Utf8.max_bytes(&text,20).is_err());
        assert!(NativeEncoding::Utf8.exact_bytes("éééé",8).is_ok());
        assert!(NativeEncoding::Windows1252.exact_bytes("éééé",8).is_err());
    }
    #[test] fn production_pe_architecture_is_x64_only() {
        let mut dos=[0u8;64];dos[..2].copy_from_slice(b"MZ");
        let mut pe=[0u8;26];pe[..4].copy_from_slice(b"PE\0\0");
        pe[4..6].copy_from_slice(&0x8664u16.to_le_bytes());
        pe[24..26].copy_from_slice(&0x20bu16.to_le_bytes());
        assert!(validate_pe_headers(&dos,&pe).is_ok());
        pe[4..6].copy_from_slice(&0x14cu16.to_le_bytes());
        assert!(matches!(validate_pe_headers(&dos,&pe),Err(BridgeError{code:"WRONG_LIBRARY_ARCHITECTURE",..})));
        pe[4..6].copy_from_slice(&0xaa64u16.to_le_bytes());
        assert!(validate_pe_headers(&dos,&pe).is_err());
        assert!(validate_pe_headers(&[],&[]).is_err());
    }
    #[test] fn tcp_arguments_restrict_address_and_require_both_options() {
        let args=|values:&[&str]|values.iter().map(|s|s.to_string()).collect::<Vec<_>>();
        assert_eq!(transport_mode(&[]).unwrap(),TransportMode::Stdio);
        assert!(transport_mode(&args(&["--connect","127.0.0.1:12345","--token-env","SITEF_BRIDGE_TOKEN"])).is_ok());
        for endpoint in ["localhost:12345","127.0.0.2:12345","0.0.0.0:12345","192.0.2.1:12345","[::1]:12345","127.0.0.1:0","127.0.0.1:65536","127.0.0.1:+1"] {
            assert!(transport_mode(&args(&["--connect",endpoint,"--token-env","SITEF_BRIDGE_TOKEN"])).is_err());
        }
        assert!(transport_mode(&args(&["--connect","127.0.0.1:12345"])).is_err());
        assert!(transport_mode(&args(&["--connect","127.0.0.1:1","--connect","127.0.0.1:2"])).is_err());
        assert!(transport_mode(&args(&["--connect","127.0.0.1:1","--token-env","BAD=NAME"])).is_err());
    }
    #[test] fn tcp_handshake_and_nonce_format_are_explicit() {
        let token="a".repeat(64);
        assert!(valid_transport_token(&token));
        assert!(!valid_transport_token("short"));
        assert!(!valid_transport_token(&"z".repeat(64)));
        let handshake=transport_handshake(&token);
        assert_eq!(handshake,json!({"protocolVersion":1,"transport":"tcp-loopback","token":token}));
    }
    #[test] fn wrong_parameter_types_are_not_coerced() {
        assert!(parse::<FinishParams>(json!({"confirm":true,"fiscalDocument":"1","fiscalDate":"20261030","fiscalTime":"120000"})).is_err());
        assert!(parse::<ContinueParams>(json!({"buffer":"","continueFlag":0,"bufferSize":-1})).is_err());
    }
    #[test] fn collection_limits_count_encoded_bytes_and_allow_prefilled_flag() {
        assert!(validate_collection(30,2,2,"é".as_bytes(),10000).is_ok());
        assert!(validate_collection(30,2,2,&[0xe9],0).is_err());
        assert!(validate_collection(30,2,2,&[], -1).is_ok());
        assert!(validate_collection(0,0,0,b"receipt",0).is_ok());
        assert!(validate_collection(35,30,60,b"2:",0).is_ok());
    }
}
