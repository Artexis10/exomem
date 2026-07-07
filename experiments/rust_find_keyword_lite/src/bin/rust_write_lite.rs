use std::env;
use std::ffi::OsStr;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Component, Path, PathBuf};
use std::process::ExitCode;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

#[derive(Debug)]
enum Command {
    Edit(EditArgs),
    Note(NoteArgs),
}

#[derive(Debug)]
struct EditArgs {
    vault: PathBuf,
    path: String,
    old: String,
    new: String,
    replace_all: bool,
    date: String,
}

#[derive(Debug)]
struct NoteArgs {
    vault: PathBuf,
    path: String,
    title: String,
    note_type: String,
    content: String,
    date: String,
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("{err}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), String> {
    let started = Instant::now();
    match parse_args()? {
        Command::Edit(args) => run_edit(args, started),
        Command::Note(args) => run_note(args, started),
    }
}

fn run_edit(args: EditArgs, started: Instant) -> Result<(), String> {
    let target = resolve_target(&args.vault, &args.path)?;
    if !target.is_file() {
        return Err(format!("target does not exist: {}", target.display()));
    }
    let raw = fs::read_to_string(&target)
        .map_err(|err| format!("could not read {}: {err}", target.display()))?;
    if args.old.is_empty() {
        return Err("--old cannot be empty".to_string());
    }
    let text = normalize_newlines(&raw);
    let (frontmatter, body) =
        split_frontmatter(&text).ok_or_else(|| "target has no frontmatter block".to_string())?;
    let count = body.matches(&args.old).count();
    if count == 0 {
        return Err("old string not found".to_string());
    }
    if count > 1 && !args.replace_all {
        return Err(format!(
            "old string occurs {count} times; pass --replace-all to replace every occurrence"
        ));
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
    );
    Ok(())
}

fn run_note(args: NoteArgs, started: Instant) -> Result<(), String> {
    let target = resolve_target(&args.vault, &args.path)?;
    if target.exists() {
        return Err(format!("target already exists: {}", target.display()));
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
            .map_err(|err| format!("could not create {}: {err}", parent.display()))?;
    }
    atomic_write(&target, &note)?;
    print_json(
        "note",
        &args.path,
        started.elapsed().as_secs_f64() * 1000.0,
        note.len(),
    );
    Ok(())
}

fn parse_args() -> Result<Command, String> {
    let mut iter = env::args().skip(1);
    let command = iter.next().ok_or_else(usage)?;
    match command.as_str() {
        "edit" => parse_edit(iter.collect()),
        "note" => parse_note(iter.collect()),
        "--help" | "-h" => Err(usage()),
        _ => Err(format!("unknown command: {command}\n{}", usage())),
    }
}

fn parse_edit(args: Vec<String>) -> Result<Command, String> {
    let mut vault = None;
    let mut path = None;
    let mut old = None;
    let mut new = None;
    let mut replace_all = false;
    let mut date = "2026-07-07".to_string();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--vault" => {
                i += 1;
                vault = args.get(i).map(PathBuf::from);
            }
            "--path" => {
                i += 1;
                path = args.get(i).cloned();
            }
            "--old" => {
                i += 1;
                old = args.get(i).cloned();
            }
            "--new" => {
                i += 1;
                new = args.get(i).cloned();
            }
            "--date" => {
                i += 1;
                date = args
                    .get(i)
                    .cloned()
                    .ok_or_else(|| "--date requires a value".to_string())?;
            }
            "--replace-all" => replace_all = true,
            other => return Err(format!("unknown edit argument: {other}")),
        }
        i += 1;
    }
    Ok(Command::Edit(EditArgs {
        vault: vault.ok_or_else(|| "--vault is required".to_string())?,
        path: path.ok_or_else(|| "--path is required".to_string())?,
        old: old.ok_or_else(|| "--old is required".to_string())?,
        new: new.ok_or_else(|| "--new is required".to_string())?,
        replace_all,
        date,
    }))
}

fn parse_note(args: Vec<String>) -> Result<Command, String> {
    let mut vault = None;
    let mut path = None;
    let mut title = None;
    let mut note_type = "insight".to_string();
    let mut content = None;
    let mut date = "2026-07-07".to_string();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--vault" => {
                i += 1;
                vault = args.get(i).map(PathBuf::from);
            }
            "--path" => {
                i += 1;
                path = args.get(i).cloned();
            }
            "--title" => {
                i += 1;
                title = args.get(i).cloned();
            }
            "--note-type" => {
                i += 1;
                note_type = args
                    .get(i)
                    .cloned()
                    .ok_or_else(|| "--note-type requires a value".to_string())?;
            }
            "--content" => {
                i += 1;
                content = args.get(i).cloned();
            }
            "--date" => {
                i += 1;
                date = args
                    .get(i)
                    .cloned()
                    .ok_or_else(|| "--date requires a value".to_string())?;
            }
            other => return Err(format!("unknown note argument: {other}")),
        }
        i += 1;
    }
    Ok(Command::Note(NoteArgs {
        vault: vault.ok_or_else(|| "--vault is required".to_string())?,
        path: path.ok_or_else(|| "--path is required".to_string())?,
        title: title.ok_or_else(|| "--title is required".to_string())?,
        note_type,
        content: content.ok_or_else(|| "--content is required".to_string())?,
        date,
    }))
}

fn resolve_target(vault: &Path, rel: &str) -> Result<PathBuf, String> {
    if rel.trim().is_empty() {
        return Err("--path cannot be empty".to_string());
    }
    let rel_path = Path::new(rel);
    if rel_path.is_absolute() {
        return Err("--path must be vault-relative".to_string());
    }
    if rel_path.components().any(|component| {
        matches!(
            component,
            Component::ParentDir | Component::RootDir | Component::Prefix(_)
        )
    }) {
        return Err("--path must stay inside the vault".to_string());
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

fn atomic_write(path: &Path, content: &str) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("target has no parent: {}", path.display()))?;
    let file_name = path
        .file_name()
        .and_then(OsStr::to_str)
        .ok_or_else(|| format!("target has invalid file name: {}", path.display()))?;
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|err| format!("system time error: {err}"))?
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
                    return Err(format!("could not write {}: {err}", tmp.display()));
                }
                break;
            }
            Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(err) => return Err(format!("could not create {}: {err}", tmp.display())),
        }
    }
    fs::rename(&tmp, path).map_err(|err| {
        let _ = fs::remove_file(&tmp);
        format!("could not replace {}: {err}", path.display())
    })?;
    Ok(())
}

fn print_json(op: &str, path: &str, duration_ms: f64, bytes: usize) {
    println!(
        "{{\"op\":\"{}\",\"path\":\"{}\",\"duration_ms\":{:.3},\"bytes\":{}}}",
        json_escape(op),
        json_escape(path),
        duration_ms,
        bytes
    );
}

fn json_escape(value: &str) -> String {
    let mut out = String::new();
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c.is_control() => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn usage() -> String {
    "usage:\n  rust_write_lite edit --vault <vault> --path <rel.md> --old <text> --new <text> [--replace-all] [--date YYYY-MM-DD]\n  rust_write_lite note --vault <vault> --path <rel.md> --title <title> --content <markdown> [--note-type insight] [--date YYYY-MM-DD]".to_string()
}
