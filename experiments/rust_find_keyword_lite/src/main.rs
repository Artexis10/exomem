use std::cmp::Ordering;
use std::env;
use std::ffi::OsStr;
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::process::ExitCode;
use std::time::Instant;

const EXCLUDED_DIR_NAMES: &[&str] = &["_Schema", "_attachments", "_archive", "_trash"];
const NAVIGATION_BASENAMES: &[&str] = &["index.md", "log.md"];

#[derive(Debug)]
struct Args {
    vault: PathBuf,
    query: String,
    limit: usize,
}

#[derive(Debug)]
struct Hit {
    path: String,
    title: String,
    updated: String,
    sort_updated: String,
}

#[derive(Debug)]
struct Page {
    path: String,
    title: String,
    updated: String,
    body_norm: String,
    title_norm: String,
}

#[derive(Default)]
struct Stats {
    scanned: usize,
    read_errors: usize,
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
    let args = parse_args()?;
    let started = Instant::now();
    let query_norm = args.query.trim().to_lowercase();
    let tokens: Vec<&str> = query_norm.split_whitespace().collect();
    let mut stats = Stats::default();
    let mut hits = Vec::new();

    if !tokens.is_empty() {
        let kb = args.vault.join("Knowledge Base");
        if !kb.is_dir() {
            return Err(format!(
                "missing Knowledge Base directory: {}",
                kb.display()
            ));
        }
        let mut paths = Vec::new();
        walk_md(&kb, &mut paths)?;
        for path in paths {
            if is_navigation_file(&path) {
                continue;
            }
            match parse_page(&path, &args.vault) {
                Some(page) => {
                    stats.scanned += 1;
                    if tokens
                        .iter()
                        .all(|tok| page.title_norm.contains(tok) || page.body_norm.contains(tok))
                    {
                        let sort_updated = if page.updated.is_empty() {
                            "0000-00-00".to_string()
                        } else {
                            page.updated.clone()
                        };
                        hits.push(Hit {
                            path: page.path,
                            title: page.title,
                            updated: page.updated,
                            sort_updated,
                        });
                    }
                }
                None => stats.read_errors += 1,
            }
        }
    }

    hits.sort_by(|a, b| match b.sort_updated.cmp(&a.sort_updated) {
        Ordering::Equal => b.path.cmp(&a.path),
        other => other,
    });
    if hits.len() > args.limit {
        hits.truncate(args.limit);
    }

    print_json(&hits, started.elapsed().as_secs_f64() * 1000.0, &stats);
    Ok(())
}

fn parse_args() -> Result<Args, String> {
    let mut vault = None;
    let mut query = None;
    let mut limit = 15usize;
    let mut iter = env::args().skip(1);

    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--vault" => vault = iter.next().map(PathBuf::from),
            "--query" => query = iter.next(),
            "--limit" => {
                let raw = iter
                    .next()
                    .ok_or_else(|| "--limit requires a value".to_string())?;
                limit = raw
                    .parse::<usize>()
                    .map_err(|_| format!("invalid --limit value: {raw}"))?;
            }
            "--help" | "-h" => {
                return Err(
                    "usage: rust_find_keyword_lite --vault <path> --query <text> [--limit N]"
                        .to_string(),
                );
            }
            _ if arg.starts_with("--vault=") => {
                vault = Some(PathBuf::from(arg.trim_start_matches("--vault=")));
            }
            _ if arg.starts_with("--query=") => {
                query = Some(arg.trim_start_matches("--query=").to_string());
            }
            _ if arg.starts_with("--limit=") => {
                let raw = arg.trim_start_matches("--limit=");
                limit = raw
                    .parse::<usize>()
                    .map_err(|_| format!("invalid --limit value: {raw}"))?;
            }
            _ => return Err(format!("unknown argument: {arg}")),
        }
    }

    Ok(Args {
        vault: vault.ok_or_else(|| "--vault is required".to_string())?,
        query: query.ok_or_else(|| "--query is required".to_string())?,
        limit: limit.max(1),
    })
}

fn walk_md(root: &Path, out: &mut Vec<PathBuf>) -> Result<(), String> {
    let entries = fs::read_dir(root)
        .map_err(|err| format!("could not read directory {}: {err}", root.display()))?;
    let mut entries: Vec<_> = entries.filter_map(Result::ok).collect();
    entries.sort_by_key(|entry| entry.file_name());

    for entry in entries {
        let path = entry.path();
        let file_type = entry
            .file_type()
            .map_err(|err| format!("could not stat {}: {err}", path.display()))?;
        if file_type.is_dir() {
            if EXCLUDED_DIR_NAMES
                .iter()
                .any(|name| path.file_name() == Some(OsStr::new(name)))
            {
                continue;
            }
            walk_md(&path, out)?;
        } else if file_type.is_file() && is_markdown_file(&path) && !is_sync_conflict(&path) {
            out.push(path);
        }
    }
    Ok(())
}

fn is_markdown_file(path: &Path) -> bool {
    path.extension()
        .and_then(OsStr::to_str)
        .map(|ext| ext.eq_ignore_ascii_case("md"))
        .unwrap_or(false)
}

fn is_sync_conflict(path: &Path) -> bool {
    path.file_name()
        .and_then(OsStr::to_str)
        .map(|name| name.contains(".sync-conflict-"))
        .unwrap_or(false)
}

fn is_navigation_file(path: &Path) -> bool {
    path.file_name()
        .and_then(OsStr::to_str)
        .map(|name| {
            NAVIGATION_BASENAMES
                .iter()
                .any(|nav| name.eq_ignore_ascii_case(nav))
        })
        .unwrap_or(false)
}

fn parse_page(path: &Path, vault: &Path) -> Option<Page> {
    let raw = fs::read_to_string(path).ok()?;
    let text = raw.replace("\r\n", "\n").replace("\r", "\n");
    let (frontmatter, mut body) = split_frontmatter(&text);
    if body.starts_with('\n') {
        body = &body[1..];
    }
    let title = first_h1(body).unwrap_or_else(|| {
        path.file_stem()
            .and_then(OsStr::to_str)
            .unwrap_or("")
            .to_string()
    });
    let updated = frontmatter_value(frontmatter, "updated")
        .or_else(|| frontmatter_value(frontmatter, "captured"))
        .unwrap_or_default();
    Some(Page {
        path: posix_relative_path(path, vault),
        title_norm: title.to_lowercase(),
        body_norm: body.trim().to_lowercase(),
        title,
        updated,
    })
}

fn split_frontmatter(text: &str) -> (&str, &str) {
    if !text.starts_with("---\n") {
        return ("", text);
    }
    let rest = &text[4..];
    match rest.find("\n---\n") {
        Some(idx) => (&rest[..idx], &rest[idx + 5..]),
        None => ("", text),
    }
}

fn first_h1(body: &str) -> Option<String> {
    body.lines()
        .find_map(|line| line.strip_prefix("# ").map(|s| s.trim().to_string()))
}

fn frontmatter_value(frontmatter: &str, key: &str) -> Option<String> {
    let prefix = format!("{key}:");
    for line in frontmatter.lines() {
        let trimmed = line.trim_start();
        if !trimmed.starts_with(&prefix) {
            continue;
        }
        let value = trimmed[prefix.len()..].trim();
        if value.is_empty() {
            return None;
        }
        return Some(unquote_scalar(value));
    }
    None
}

fn unquote_scalar(value: &str) -> String {
    let bytes = value.as_bytes();
    if bytes.len() >= 2 {
        let first = bytes[0];
        let last = bytes[bytes.len() - 1];
        if (first == b'"' && last == b'"') || (first == b'\'' && last == b'\'') {
            return value[1..value.len() - 1].to_string();
        }
    }
    value.to_string()
}

fn posix_relative_path(path: &Path, vault: &Path) -> String {
    let rel = path.strip_prefix(vault).unwrap_or(path);
    rel.components()
        .filter_map(|component| match component {
            Component::Normal(part) => Some(part.to_string_lossy().into_owned()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("/")
}

fn print_json(hits: &[Hit], total_ms: f64, stats: &Stats) {
    print!("{{\"hits\":[");
    for (idx, hit) in hits.iter().enumerate() {
        if idx > 0 {
            print!(",");
        }
        print!(
            "{{\"path\":\"{}\",\"title\":\"{}\",\"updated\":\"{}\"}}",
            json_escape(&hit.path),
            json_escape(&hit.title),
            json_escape(&hit.updated),
        );
    }
    println!(
        "],\"timings\":{{\"total_ms\":{:.3},\"scanned\":{},\"read_errors\":{}}}}}",
        total_ms, stats.scanned, stats.read_errors
    );
}

fn json_escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            ch if ch <= '\u{1f}' => out.push_str(&format!("\\u{:04x}", ch as u32)),
            ch => out.push(ch),
        }
    }
    out
}
