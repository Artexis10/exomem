use std::ffi::OsStr;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Component, Path, PathBuf};
use std::process::ExitCode;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use clap::{Parser, Subcommand};
use serde::Serialize;

const EXIT_USAGE: u8 = 2;
const EXIT_DATA: u8 = 3;
const EXIT_CONFLICT: u8 = 4;
const EXIT_IO: u8 = 5;
const EXIT_OUTPUT: u8 = 6;

type Result<T> = std::result::Result<T, ToolError>;

#[derive(Debug)]
struct ToolError {
    code: u8,
    message: String,
}

impl ToolError {
    fn exit_code(&self) -> ExitCode {
        ExitCode::from(self.code)
    }
}

fn usage_error(message: impl Into<String>) -> ToolError {
    ToolError {
        code: EXIT_USAGE,
        message: message.into(),
    }
}

fn data_error(message: impl Into<String>) -> ToolError {
    ToolError {
        code: EXIT_DATA,
        message: message.into(),
    }
}

fn conflict_error(message: impl Into<String>) -> ToolError {
    ToolError {
        code: EXIT_CONFLICT,
        message: message.into(),
    }
}

fn io_error(message: impl Into<String>) -> ToolError {
    ToolError {
        code: EXIT_IO,
        message: message.into(),
    }
}

fn output_error(message: impl Into<String>) -> ToolError {
    ToolError {
        code: EXIT_OUTPUT,
        message: message.into(),
    }
}

#[derive(Debug, Parser)]
#[command(name = "rust_write_lite")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    Edit(EditArgs),
    Note(NoteArgs),
}

#[derive(Debug, Parser)]
struct EditArgs {
    #[arg(long)]
    vault: PathBuf,
    #[arg(long)]
    path: String,
    #[arg(long)]
    old: String,
    #[arg(long)]
    new: String,
    #[arg(long)]
    replace_all: bool,
    #[arg(long, default_value = "2026-07-07")]
    date: String,
}

#[derive(Debug, Parser)]
struct NoteArgs {
    #[arg(long)]
    vault: PathBuf,
    #[arg(long)]
    path: String,
    #[arg(long)]
    title: String,
    #[arg(long = "note-type", default_value = "insight")]
    note_type: String,
    #[arg(long)]
    content: String,
    #[arg(long, default_value = "2026-07-07")]
    date: String,
}

#[derive(Serialize)]
struct WriteOutput<'a> {
    op: &'a str,
    path: &'a str,
    duration_ms: f64,
    bytes: usize,
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("{}", err.message);
            err.exit_code()
        }
    }
}

fn run() -> Result<()> {
    let command = Cli::parse().command;
    let started = Instant::now();
    match command {
        Command::Edit(args) => run_edit(args, started),
        Command::Note(args) => run_note(args, started),
    }
}

fn run_edit(args: EditArgs, started: Instant) -> Result<()> {
    let target = resolve_target(&args.vault, &args.path)?;
    if !target.is_file() {
        return Err(data_error(format!(
            "target does not exist: {}",
            target.display()
        )));
    }
    let raw = fs::read_to_string(&target)
        .map_err(|err| io_error(format!("could not read {}: {err}", target.display())))?;
    if args.old.is_empty() {
        return Err(usage_error("--old cannot be empty"));
    }
    let text = normalize_newlines(&raw);
    let (frontmatter, body) =
        split_frontmatter(&text).ok_or_else(|| data_error("target has no frontmatter block"))?;
    let count = body.matches(&args.old).count();
    if count == 0 {
        return Err(data_error("old string not found"));
    }
    if count > 1 && !args.replace_all {
        return Err(conflict_error(format!(
            "old string occurs {count} times; pass --replace-all to replace every occurrence"
        )));
    }

    let updated_fm = set_or_append_frontmatter(frontmatter, "updated", &args.date);
    let replaced = if args.replace_all {
        body.replace(&args.old, &args.new)
    } else {
        body.replacen(&args.old, &args.new, 1)
    };
    let new_body = format!("{}\n", replaced.trim_end());
    let new_text = format!("---\n{updated_fm}\n---\n{new_body}");
    atomic_write(&target, &new_text)?;
    print_json(
        "edit",
        &args.path,
        started.elapsed().as_secs_f64() * 1000.0,
        new_text.len(),
    )?;
    Ok(())
}

fn run_note(args: NoteArgs, started: Instant) -> Result<()> {
    let target = resolve_target(&args.vault, &args.path)?;
    if target.exists() {
        return Err(conflict_error(format!(
            "target already exists: {}",
            target.display()
        )));
    }
    let body = normalize_newlines(&args.content);
    let note = format!(
        "---\ntype: {}\ntitle: {}\nstatus: active\ncreated: {}\nupdated: {}\n---\n{}\n",
        args.note_type,
        args.title,
        args.date,
        args.date,
        body.trim_end()
    );
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| io_error(format!("could not create {}: {err}", parent.display())))?;
    }
    atomic_write(&target, &note)?;
    print_json(
        "note",
        &args.path,
        started.elapsed().as_secs_f64() * 1000.0,
        note.len(),
    )?;
    Ok(())
}

fn resolve_target(vault: &Path, rel: &str) -> Result<PathBuf> {
    if rel.trim().is_empty() {
        return Err(usage_error("--path cannot be empty"));
    }
    let rel_path = Path::new(rel);
    if rel_path.is_absolute() {
        return Err(usage_error("--path must be vault-relative"));
    }
    if rel_path.components().any(|component| {
        matches!(
            component,
            Component::ParentDir | Component::RootDir | Component::Prefix(_)
        )
    }) {
        return Err(usage_error("--path must stay inside the vault"));
    }
    Ok(vault.join(rel_path))
}

fn normalize_newlines(text: &str) -> String {
    text.replace("\r\n", "\n").replace('\r', "\n")
}

fn split_frontmatter(text: &str) -> Option<(&str, &str)> {
    if !text.starts_with("---\n") {
        return None;
    }
    let rest = &text[4..];
    let idx = rest.find("\n---\n")?;
    Some((&rest[..idx], &rest[idx + 5..]))
}

fn set_or_append_frontmatter(frontmatter: &str, key: &str, value: &str) -> String {
    let prefix = format!("{key}:");
    let mut replaced = false;
    let mut lines = Vec::new();
    for line in frontmatter.lines() {
        if line.trim_start().starts_with(&prefix) {
            lines.push(format!("{key}: {value}"));
            replaced = true;
        } else {
            lines.push(line.to_string());
        }
    }
    if !replaced {
        lines.push(format!("{key}: {value}"));
    }
    lines.join("\n")
}

fn atomic_write(path: &Path, content: &str) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| usage_error(format!("target has no parent: {}", path.display())))?;
    let file_name = path
        .file_name()
        .and_then(OsStr::to_str)
        .ok_or_else(|| usage_error(format!("target has invalid file name: {}", path.display())))?;
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|err| io_error(format!("system time error: {err}")))?
        .as_nanos();
    let mut tmp = parent.join(format!(".{file_name}.{stamp}.tmp"));
    for attempt in 0..100 {
        if attempt > 0 {
            tmp = parent.join(format!(".{file_name}.{stamp}-{attempt}.tmp"));
        }
        match OpenOptions::new().write(true).create_new(true).open(&tmp) {
            Ok(mut f) => {
                if let Err(err) = f.write_all(content.as_bytes()) {
                    let _ = fs::remove_file(&tmp);
                    return Err(io_error(format!(
                        "could not write {}: {err}",
                        tmp.display()
                    )));
                }
                break;
            }
            Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(err) => {
                return Err(io_error(format!(
                    "could not create {}: {err}",
                    tmp.display()
                )));
            }
        }
    }
    fs::rename(&tmp, path).map_err(|err| {
        let _ = fs::remove_file(&tmp);
        io_error(format!("could not replace {}: {err}", path.display()))
    })?;
    Ok(())
}

fn print_json(op: &str, path: &str, duration_ms: f64, bytes: usize) -> Result<()> {
    let output = WriteOutput {
        op,
        path,
        duration_ms: (duration_ms * 1000.0).round() / 1000.0,
        bytes,
    };
    let encoded = serde_json::to_string(&output)
        .map_err(|err| output_error(format!("could not encode JSON: {err}")))?;
    println!("{encoded}");
    Ok(())
}
